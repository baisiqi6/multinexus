"""CLI-based agents — Claude Code and Codex via subprocess."""

import asyncio
import json as _json
import logging
import os
import re
import subprocess
import sys

from .base import AgentOfflineError, AgentRateLimitError, AgentTimeoutError, BaseAgent

# On Windows, npm-installed CLIs need .cmd extension for subprocess_exec
_IS_WIN = sys.platform == "win32"
_CODEX_CMD = "codex.cmd" if _IS_WIN else "codex"
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if _IS_WIN else {}

log = logging.getLogger(__name__)

# Patterns that indicate a usage/rate limit rather than a generic offline error
_RATE_LIMIT_PATTERNS = [
    "rate limit",
    "rate_limit",
    "usage limit",
    "quota exceeded",
    "quota_exceeded",
    "insufficient_quota",
    "too many requests",
    "overloaded",
    "plan limit",
    "monthly limit",
    "weekly limit",
    "session limit",
]

# Environment keys that must never propagate to subprocess agents
_STRIP_ENV_KEYS: frozenset[str] = frozenset({
    "DISCORD_TOKEN",
    "PRIVATE_DB_PATH",
    "OPENAI_API_KEY",
})

# Codex --json event types that carry no assistant text (suppress unmatched-event debug log)
_CODEX_NON_TEXT_EVENTS: frozenset[str] = frozenset({
    "thread.started", "turn.started", "turn.completed",
    "item.started", "item.completed",
})


def _filtered_env() -> dict[str, str]:
    """Return os.environ minus keys that must not reach subprocess agents."""
    return {k: v for k, v in os.environ.items() if k not in _STRIP_ENV_KEYS}


def _is_rate_limit(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in _RATE_LIMIT_PATTERNS)


def _extract_codex_text(event: dict) -> str:
    """Extract assistant text from a Codex --json JSONL event.

    Confirmed shapes from codex-cli 0.120.0 (probed live):
      item.completed: {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
      turn.completed: {"type":"turn.completed","usage":{...}}  ← no text, skip

    Legacy shapes retained for forward/backward compat:
      {"type":"agent_message","message":{"role":"assistant","content":[{"type":"text","text":"..."}]}}
      {"type":"message","content":"..."}
      {"type":"response.output_item.added","item":{"type":"message","content":[{"type":"output_text","text":"..."}]}}
    """
    # Shape 1 (primary — confirmed): item.completed with agent_message text
    item = event.get("item", {})
    if isinstance(item, dict) and item.get("type") == "agent_message":
        t = item.get("text", "")
        if t:
            return t

    # Shape 2 (legacy): agent_message/message with content list
    msg = event.get("message") or item
    content = msg.get("content", []) if isinstance(msg, dict) else []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("text", "output_text"):
                t = block.get("text", "")
                if t:
                    return t

    # Shape 3 (legacy): flat "content" string
    flat = event.get("content")
    if isinstance(flat, str) and flat:
        return flat

    # Shape 4 (legacy): nested response.output with text
    for out_item in event.get("response", {}).get("output", []):
        for block in out_item.get("content", []):
            if isinstance(block, dict):
                t = block.get("text", "")
                if t:
                    return t

    return ""


class ClaudeAgent(BaseAgent):
    """Claude Code CLI agent via `claude -p` with session persistence and full tool access."""

    _ACTIVITY_TIMEOUT = 90

    def __init__(self, timeout: int = 120, work_dir: str | None = None, model: str | None = None):
        super().__init__(name="Claude", timeout=timeout)
        self.work_dir = work_dir
        self.model = model  # e.g. "claude-sonnet-4-6" or "claude-opus-4-6"
        self._current_proc: asyncio.subprocess.Process | None = None

    async def kill(self) -> None:
        """Kill the currently running subprocess if any. No-op if idle."""
        if self._current_proc is not None:
            proc = self._current_proc
            self._current_proc = None
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass

    async def call(self, messages: list[dict], system_prompt: str,
                   mission: str = "", workspace: str = "",
                   work_dir: str | None = None,
                   timeout: int | None = None,
                   on_chunk=None) -> tuple[str, dict]:
        """Fresh Claude CLI call. Returns (response_text, metadata) with session_id in metadata."""
        prompt = self._build_prompt(messages, system_prompt, mission=mission, workspace=workspace)
        effective_dir = work_dir or self.work_dir

        cmd = [
            "claude", "-p", "--verbose", "--output-format", "stream-json",
            "--include-partial-messages", "--dangerously-skip-permissions",
        ]
        if self.model:
            cmd += ["--model", self.model]

        return await self._run_claude_command(
            cmd, prompt, effective_dir=effective_dir,
            effective_timeout=timeout or self.timeout, on_chunk=on_chunk,
        )

    async def resume(self, session_id: str, prompt: str,
                     work_dir: str | None = None,
                     timeout: int | None = None,
                     on_chunk=None) -> tuple[str, dict]:
        """Resume an existing Claude CLI session."""
        effective_dir = work_dir or self.work_dir

        cmd = [
            "claude", "-p", "--verbose", "--output-format", "stream-json",
            "--include-partial-messages", "--dangerously-skip-permissions",
            "--resume", session_id,
        ]
        if self.model:
            cmd += ["--model", self.model]

        return await self._run_claude_command(
            cmd, prompt, effective_dir=effective_dir,
            effective_timeout=timeout or self.timeout, on_chunk=on_chunk,
        )

    async def _run_claude_command(self, cmd: list[str], prompt: str, *,
                                  effective_dir: str | None,
                                  effective_timeout: int,
                                  on_chunk=None) -> tuple[str, dict]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=effective_dir,
                env=_filtered_env(),
                **_NO_WINDOW,
            )
            self._current_proc = proc

            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()

            loop = asyncio.get_event_loop()
            deadline = loop.time() + effective_timeout
            last_activity = loop.time()

            response_text = ""
            metadata: dict = {}
            session_id: str | None = None

            while True:
                now = loop.time()
                if now >= deadline:
                    raise AgentTimeoutError(
                        f"Claude exceeded total timeout of {effective_timeout}s"
                    )
                read_timeout = min(self._ACTIVITY_TIMEOUT, deadline - now)
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=read_timeout)
                except asyncio.TimeoutError:
                    if loop.time() - last_activity >= self._ACTIVITY_TIMEOUT:
                        raise AgentTimeoutError(
                            f"Claude stopped responding (no activity for {self._ACTIVITY_TIMEOUT}s)"
                        )
                    continue

                if not raw:
                    break

                last_activity = loop.time()
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    event = _json.loads(line)
                except _json.JSONDecodeError:
                    continue

                event_type = event.get("type")

                # Capture session_id from any event that carries it
                evt_session = event.get("session_id")
                if isinstance(evt_session, str) and evt_session:
                    session_id = evt_session

                if event_type == "assistant":
                    if on_chunk is not None:
                        content_blocks = event.get("message", {}).get("content", [])
                        for block in content_blocks:
                            if block.get("type") == "text":
                                partial_text = block.get("text", "")
                                if partial_text:
                                    await on_chunk(partial_text)

                elif event_type == "result":
                    subtype = event.get("subtype", "")
                    result_text = event.get("result", "")
                    if subtype == "error" or event.get("is_error"):
                        if _is_rate_limit(result_text):
                            raise AgentRateLimitError(f"Claude usage/rate limit: {result_text[:200]}")
                        raise AgentOfflineError(f"Claude error: {result_text[:200]}")
                    response_text = result_text
                    usage = event.get("usage", {})
                    metadata = {
                        "tokens_input": usage.get("input_tokens"),
                        "tokens_output": usage.get("output_tokens"),
                        "tokens_cache_read": usage.get("cache_read_input_tokens"),
                        "cost_usd": event.get("total_cost_usd"),
                        "session_id": session_id,
                    }

            await proc.wait()

            if not response_text and proc.returncode != 0:
                stderr_data = await proc.stderr.read()
                err = stderr_data.decode().strip()
                if _is_rate_limit(err):
                    raise AgentRateLimitError(f"Claude usage/rate limit: {err[:200]}")
                raise AgentOfflineError(f"Claude CLI failed (code {proc.returncode}): {err[:200]}")

            log.info(
                "Claude — chars: %d, cost: $%.4f, session: %s, exit: %d",
                len(response_text),
                metadata.get("cost_usd") or 0,
                session_id or "none",
                proc.returncode,
            )
            return response_text, metadata

        except FileNotFoundError:
            raise AgentOfflineError("Claude CLI not found. Is it installed?")
        finally:
            _proc = self._current_proc
            self._current_proc = None
            if _proc is not None and _proc.returncode is None:
                try:
                    _proc.kill()
                except Exception:
                    pass

    async def health_check(self) -> dict:
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_NO_WINDOW,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            version = stdout.decode().strip()
            return {"status": "ok", "model": f"Claude Code ({version})"}
        except Exception as e:
            return {"status": "offline", "error": str(e)}

    def _build_prompt(self, messages: list[dict], system_prompt: str,
                      mission: str = "", workspace: str = "") -> str:
        """Build 4-layer prompt: IDENTITY → MISSION → SCRATCH → HISTORY."""
        parts = []

        # Layer 1: IDENTITY (system prompt)
        parts.append(system_prompt)

        # Layer 2: MISSION (per-channel north star, if any)
        if mission:
            parts.append(f"\n## MISSION\n{mission}")

        # Layer 3: SCRATCH (agent's own working notes from previous turns)
        if workspace:
            parts.append(f"\n## [{self.name} working notes]\n{workspace}")

        parts.append("")  # blank line separator before history

        # Layer 4: HISTORY
        for msg in messages:
            role = msg["role"].upper()
            parts.append(f"{role}: {msg['content']}")

        return "\n".join(parts)


class CodexAgent(BaseAgent):
    """Codex CLI agent via stdin pipe to `codex exec`."""

    def __init__(self, timeout: int = 120, work_dir: str | None = None, activity_timeout: int = 300):
        super().__init__(name="Codex", timeout=timeout)
        self.work_dir = work_dir
        self.activity_timeout = activity_timeout
        self._current_proc: asyncio.subprocess.Process | None = None

    async def call(self, messages: list[dict], system_prompt: str,
                   mission: str = "", workspace: str = "",
                   work_dir: str | None = None,
                   timeout: int | None = None,
                   activity_timeout: int | None = None,
                   on_chunk=None) -> tuple[str, dict]:
        prompt = self._build_prompt(messages, system_prompt, mission=mission, workspace=workspace)
        effective_dir = work_dir or self.work_dir
        effective_timeout = timeout or self.timeout

        try:
            cmd_args = [
                _CODEX_CMD, "exec",
                "--skip-git-repo-check",
                "--sandbox", "danger-full-access",
                "--json",  # JSONL event stream — enables streaming + token metadata
                "-",
            ]
            if effective_dir:
                cmd_args.insert(2, "-C")
                cmd_args.insert(3, effective_dir)

            return await self._run_codex_command(
                cmd_args,
                prompt,
                effective_dir=effective_dir,
                effective_timeout=effective_timeout,
                activity_timeout=activity_timeout,
                on_chunk=on_chunk,
            )

        except FileNotFoundError:
            raise AgentOfflineError("Codex CLI not found. Is it installed?")
        finally:
            _proc = self._current_proc
            self._current_proc = None
            if _proc is not None and _proc.returncode is None:
                try:
                    _proc.kill()
                except Exception:
                    pass

    async def resume(self, session_id: str, prompt: str,
                     work_dir: str | None = None,
                     timeout: int | None = None,
                     activity_timeout: int | None = None,
                     on_chunk=None) -> tuple[str, dict]:
        effective_dir = work_dir or self.work_dir
        effective_timeout = timeout or self.timeout

        try:
            cmd_args = [_CODEX_CMD, "resume", session_id, "--json", "-"]
            if effective_dir:
                cmd_args.insert(2, "-C")
                cmd_args.insert(3, effective_dir)

            return await self._run_codex_command(
                cmd_args,
                prompt,
                effective_dir=effective_dir,
                effective_timeout=effective_timeout,
                activity_timeout=activity_timeout,
                on_chunk=on_chunk,
            )
        except FileNotFoundError:
            raise AgentOfflineError("Codex CLI not found. Is it installed?")
        finally:
            _proc = self._current_proc
            self._current_proc = None
            if _proc is not None and _proc.returncode is None:
                try:
                    _proc.kill()
                except Exception:
                    pass

    async def kill(self) -> None:
        """Kill the currently running subprocess if any. No-op if idle."""
        if self._current_proc is not None:
            proc = self._current_proc
            self._current_proc = None
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass

    async def _run_codex_command(self, cmd_args: list[str], prompt: str, *,
                                 effective_dir: str | None,
                                 effective_timeout: int,
                                 activity_timeout: int | None = None,
                                 on_chunk=None) -> tuple[str, dict]:
        proc = await asyncio.create_subprocess_exec(
            *cmd_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=effective_dir,
            env=_filtered_env(),
            limit=10 * 1024 * 1024,  # 10 MB — prevents ValueError on long Codex lines
            **_NO_WINDOW,
        )
        self._current_proc = proc

        # Write prompt to stdin then close.
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()

        # With --json, Codex emits events during tool calls, but can go silent during response
        # synthesis (or during long-running shell commands like gradle). Configurable via
        # activity_timeout on the agent instance, or per-invocation override (e.g. -t flag).
        _ACTIVITY_TIMEOUT = activity_timeout or self.activity_timeout
        loop = asyncio.get_event_loop()
        deadline = loop.time() + effective_timeout
        last_activity = loop.time()

        response_text = ""
        last_chunk_sent = ""  # track last text sent to on_chunk to avoid re-sending
        tokens_used = 0
        tokens_input = 0
        tokens_cache_read = 0
        codex_session_id: str | None = None

        while True:
            now = loop.time()
            if now >= deadline:
                raise AgentTimeoutError(f"Codex exceeded total timeout of {effective_timeout}s")
            read_timeout = min(_ACTIVITY_TIMEOUT, deadline - now)
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=read_timeout)
            except asyncio.TimeoutError:
                if loop.time() - last_activity >= _ACTIVITY_TIMEOUT:
                    raise AgentTimeoutError(
                        f"Codex stopped responding (no activity for {_ACTIVITY_TIMEOUT}s)"
                    )
                continue
            if not raw:
                break
            last_activity = loop.time()

            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                event = _json.loads(line)
            except _json.JSONDecodeError:
                # Non-JSON line — log and skip (shouldn't happen in --json mode)
                log.debug("Codex non-JSON line: %s", line[:120])
                continue

            event_type = event.get("type", "")
            log.debug("Codex event: %s", event_type)

            if event_type == "session_meta":
                codex_session_id = event.get("payload", {}).get("id")

            # Extract text from known event shapes.
            partial_text = _extract_codex_text(event)
            if not partial_text and event_type and event_type not in _CODEX_NON_TEXT_EVENTS:
                # Truly unknown shape — log full event at DEBUG so we can add support for it.
                log.debug("Codex unmatched event (%s): %s", event_type, str(event)[:300])
            if partial_text and partial_text != last_chunk_sent:
                response_text = partial_text  # Codex events carry accumulated text
                last_chunk_sent = partial_text
                if on_chunk is not None:
                    await on_chunk(partial_text)

            # Token counts from turn.completed: input_tokens, cached_input_tokens, output_tokens
            usage = event.get("usage") or event.get("response", {}).get("usage", {})
            if usage:
                tokens_used = usage.get("output_tokens") or usage.get("completion_tokens") or tokens_used
                tokens_input = usage.get("input_tokens") or tokens_input
                tokens_cache_read = usage.get("cached_input_tokens") or tokens_cache_read

        await proc.wait()
        err_data = await proc.stderr.read()
        err_output = err_data.decode("utf-8", errors="replace").replace("\r\n", "\n")

        if proc.returncode != 0:
            log.error("Codex CLI non-zero exit (code %d): %s", proc.returncode, err_output[:200])
            if not response_text:
                if _is_rate_limit(err_output):
                    raise AgentRateLimitError(f"Codex usage/rate limit: {err_output[:200]}")
                raise AgentOfflineError(f"Codex CLI failed: {err_output[:200]}")
            # Partial output present — return it but the error is already logged above.

        metadata = {
            "tokens_output": tokens_used or None,
            "tokens_input": tokens_input or None,
            "tokens_cache_read": tokens_cache_read or None,
            "codex_session_id": codex_session_id,
        }
        log.info("Codex — chars: %d, tokens: %d, exit: %d", len(response_text), tokens_used, proc.returncode)
        return response_text, metadata

    async def health_check(self) -> dict:
        try:
            proc = await asyncio.create_subprocess_exec(
                _CODEX_CMD, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_NO_WINDOW,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            version = stdout.decode().strip()
            return {"status": "ok", "model": f"Codex ({version})"}
        except Exception as e:
            return {"status": "offline", "error": str(e)}

    def _build_prompt(self, messages: list[dict], system_prompt: str,
                      mission: str = "", workspace: str = "") -> str:
        """Build 4-layer prompt: IDENTITY → MISSION → SCRATCH → HISTORY."""
        parts = []

        # Layer 1: IDENTITY (system prompt)
        parts.append(system_prompt)

        # Layer 2: MISSION (per-channel north star, if any)
        if mission:
            parts.append(f"\n## MISSION\n{mission}")

        # Layer 3: SCRATCH (agent's own working notes from previous turns)
        if workspace:
            parts.append(f"\n## [{self.name} working notes]\n{workspace}")

        parts.append("")  # blank line separator before history

        # Layer 4: HISTORY
        for msg in messages:
            role = msg["role"].upper()
            parts.append(f"{role}: {msg['content']}")

        return "\n".join(parts)

    def _extract_tokens(self, stderr_output: str) -> int:
        """Extract the token count from Codex stderr output."""
        tok_match = re.search(r"\ntokens used\n([\d,]+)", stderr_output)
        if tok_match:
            return int(tok_match.group(1).replace(",", ""))
        return 0

    def _extract_response(self, output: str) -> tuple[str, int]:
        """Extract the actual response and token count from Codex CLI output.

        Returns (response_text, tokens_used).
        Codex prints a header block (version, model, settings) followed by
        the actual response and optionally a 'tokens used\\nN,NNN' footer.
        """
        lines = output.split("\n")

        # Find the last 'codex' marker line — response follows
        codex_idx = None
        for i, line in enumerate(lines):
            if line.strip() == "codex":
                codex_idx = i

        if codex_idx is not None and codex_idx + 1 < len(lines):
            response = "\n".join(lines[codex_idx + 1:]).strip()
        else:
            response = output

        # Extract and strip token count: pattern is "\ntokens used\nN,NNN" at end
        tokens_int = 0
        tok_match = re.search(r"\ntokens used\n([\d,]+)", response)
        if tok_match:
            tokens_int = int(tok_match.group(1).replace(",", ""))
            response = response[:tok_match.start()].strip()
        elif response.endswith("tokens used"):
            response = response.rsplit("\ntokens used", 1)[0].strip()

        return response, tokens_int
