"""OpenClaw CLI agent — runs `openclaw agent` as a subprocess (like Claude/Codex).

Uses the OpenClaw CLI's `agent --json` command to send a message and parse
the JSON response. This is the correct way to call OpenClaw from multinexus
since the gateway does not expose an OpenAI-compatible HTTP API.
"""

import asyncio
import hashlib
import json as _json
import logging
import os
import subprocess
import sys

from .base import AgentOfflineError, AgentTimeoutError, BaseAgent

_IS_WIN = sys.platform == "win32"
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if _IS_WIN else {}

log = logging.getLogger(__name__)

# Keys that must not propagate to the openclaw subprocess
_STRIP_ENV_KEYS: frozenset[str] = frozenset({
    "DISCORD_TOKEN",
    "PRIVATE_DB_PATH",
})

_MODEL_OVERRIDE_UNAUTHORIZED = "provider/model overrides are not authorized"


def _filtered_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _STRIP_ENV_KEYS}


def _safe_session_id(value: str) -> str:
    """Return a compact OpenClaw session id safe for CLI arguments."""
    clean = "".join(ch if ch.isalnum() or ch in "._:-" else "-" for ch in value)
    if len(clean) <= 120:
        return clean
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]
    return f"{clean[:100]}-{digest}"


class OpenClawCLIAgent(BaseAgent):
    """OpenClaw agent via `openclaw agent --json` subprocess.

    Parameters:
        agent_id:    Workspace/agent ID in OpenClaw config (default: "main")
        timeout:     Request timeout in seconds
        model:       Model override (e.g. "astron-code-latest")
        thinking:    Thinking level (off|minimal|low|medium|high|xhigh|adaptive|max)
    """

    _ACTIVITY_TIMEOUT = 120

    def __init__(
        self,
        agent_id: str = "main",
        timeout: int = 600,
        model: str | None = None,
        thinking: str | None = None,
    ):
        super().__init__(name="LocalAgent", timeout=timeout)
        self.agent_id = agent_id
        self.model = model
        self.thinking = thinking
        self._current_proc: asyncio.subprocess.Process | None = None

    async def kill(self) -> None:
        if self._current_proc is not None:
            proc = self._current_proc
            self._current_proc = None
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass

    async def call(
        self,
        messages: list[dict],
        system_prompt: str,
        mission: str = "",
        workspace: str = "",
        session_id: str | None = None,
    ) -> tuple[str, dict]:
        """Call OpenClaw agent via CLI subprocess.

        system_prompt is ignored — OpenClaw builds its own from SOUL.md etc.
        Conversation history is replayed in the user message.
        """
        prompt = self._build_prompt(messages, mission=mission, workspace=workspace)
        use_model_override = bool(self.model)
        try:
            try:
                stdout_data, stderr_data = await self._run_agent_command(
                    prompt, use_model_override=use_model_override, session_id=session_id
                )
            except AgentOfflineError as exc:
                if use_model_override and _MODEL_OVERRIDE_UNAUTHORIZED in str(exc).lower():
                    log.warning(
                        "OpenClaw rejected model override %r; retrying without --model",
                        self.model,
                    )
                    stdout_data, stderr_data = await self._run_agent_command(
                        prompt, use_model_override=False, session_id=session_id
                    )
                else:
                    raise

            # Parse JSON output
            try:
                result = _json.loads(stdout_data)
            except _json.JSONDecodeError:
                raise AgentOfflineError(
                    f"OpenClaw returned non-JSON: {stdout_data[:300]}"
                )

            status = result.get("status", "")
            if status != "ok":
                summary = result.get("summary", "unknown")
                raise AgentOfflineError(
                    f"OpenClaw agent returned status '{status}' ({summary})"
                )

            # Extract text from payloads
            payloads = result.get("result", {}).get("payloads", [])
            text_parts = []
            for p in payloads:
                t = p.get("text", "")
                if t:
                    text_parts.append(t)
            response_text = "\n".join(text_parts)

            # Extract metadata
            meta = result.get("result", {}).get("meta", {})
            agent_meta = meta.get("agentMeta", {})
            usage = agent_meta.get("usage", {})

            metadata = {
                "tokens_input": usage.get("input"),
                "tokens_output": usage.get("output"),
                "session_id": agent_meta.get("sessionId"),
                "provider": agent_meta.get("provider"),
                "model": agent_meta.get("model"),
                "duration_ms": meta.get("durationMs"),
            }

            log.info(
                "OpenClaw CLI — chars: %d, input: %d, output: %d, model: %s, session: %s",
                len(response_text),
                usage.get("input", 0),
                usage.get("output", 0),
                agent_meta.get("model", "?"),
                agent_meta.get("sessionId", "none"),
            )

            return response_text, metadata

        except FileNotFoundError:
            raise AgentOfflineError("OpenClaw CLI not found. Is it installed?")
        finally:
            _proc = self._current_proc
            self._current_proc = None
            if _proc is not None and _proc.returncode is None:
                try:
                    _proc.kill()
                except Exception:
                    pass

    async def _run_agent_command(
        self,
        prompt: str,
        *,
        use_model_override: bool,
        session_id: str | None = None,
    ) -> tuple[str, str]:
        cmd = ["openclaw", "agent", "--agent", self.agent_id, "--json"]
        if session_id:
            cmd += ["--session-id", _safe_session_id(session_id)]
        if use_model_override and self.model:
            cmd += ["--model", self.model]
        if self.thinking:
            cmd += ["--thinking", self.thinking]
        cmd += ["--message", prompt, "--timeout", str(self.timeout)]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_filtered_env(),
            **_NO_WINDOW,
        )
        self._current_proc = proc

        loop = asyncio.get_event_loop()
        deadline = loop.time() + self.timeout

        stdout_chunks: list[bytes] = []
        while True:
            now = loop.time()
            if now >= deadline:
                raise AgentTimeoutError(
                    f"{self.name} exceeded total timeout of {self.timeout}s"
                )
            read_timeout = min(self._ACTIVITY_TIMEOUT, deadline - now)
            try:
                chunk = await asyncio.wait_for(
                    proc.stdout.read(65536), timeout=read_timeout
                )
            except asyncio.TimeoutError:
                if loop.time() - deadline + self.timeout >= self._ACTIVITY_TIMEOUT:
                    raise AgentTimeoutError(
                        f"{self.name} stopped responding (no activity for {self._ACTIVITY_TIMEOUT}s)"
                    )
                continue
            if not chunk:
                break
            stdout_chunks.append(chunk)

        await proc.wait()
        stdout_data = b"".join(stdout_chunks).decode("utf-8", errors="replace")
        stderr_data = (await proc.stderr.read()).decode("utf-8", errors="replace")

        if proc.returncode != 0:
            err = stderr_data.strip() or stdout_data.strip()
            raise AgentOfflineError(
                f"OpenClaw CLI failed (code {proc.returncode}): {err[:300]}"
            )

        return stdout_data, stderr_data

    async def health_check(self) -> dict:
        try:
            proc = await asyncio.create_subprocess_exec(
                "openclaw", "health",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_filtered_env(),
                **_NO_WINDOW,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            output = stdout.decode().strip()
            if proc.returncode == 0:
                return {"status": "ok", "backend": "openclaw", "agent_id": self.agent_id}
            return {"status": "offline", "error": output[:200]}
        except FileNotFoundError:
            return {"status": "offline", "error": "OpenClaw CLI not found"}
        except Exception as e:
            return {"status": "offline", "error": str(e)}

    def _build_prompt(self, messages: list[dict], mission: str = "", workspace: str = "") -> str:
        """Build prompt from conversation history.

        OpenClaw manages its own system prompt (SOUL.md), so we only send
        mission, workspace, and conversation history.
        """
        parts = []

        if mission:
            parts.append(f"## MISSION\n{mission}")

        if workspace:
            parts.append(f"\n## [Working notes]\n{workspace}")

        if parts:
            parts.append("")

        for msg in messages:
            role = msg["role"].upper()
            parts.append(f"{role}: {msg['content']}")

        return "\n".join(parts)
