import asyncio
import json
import logging
from typing import Any

from ..models import AgentConfig
from .base import AdapterResult, AgentAdapter
from .utils import async_subprocess_kwargs, filtered_env, terminate_owned_process_group

log = logging.getLogger(__name__)


def _timeout_message(kind: str, elapsed_seconds: int, total_seconds: int) -> str:
    return (
        f"Claude timeout:{kind} after {elapsed_seconds}s "
        f"(total budget {total_seconds}s). Aborted, no handoff."
    )


def _emit_progress(on_progress, update: dict) -> None:
    if not on_progress:
        return
    on_progress(update)


def _progress_summary_from_event(event: dict) -> str:
    content = event.get("content")
    if content is None and isinstance(event.get("message"), dict):
        content = event["message"].get("content")
    if isinstance(content, str):
        return content.strip()[:1000]
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") not in {"text", "text_delta"}:
                continue
            text = item.get("text") or item.get("delta")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n".join(parts).strip()[:1000]
    return ""


class _BoundedProviderEvidence:
    """Collect only bounded model, session, CLI, and tool identifiers."""

    SOURCE = "claude_cli_stream_json"
    MAX_TOOL_NAMES = 16

    def __init__(self):
        self.session_id: str | None = None
        self.requested_model: str | None = None
        self.observed_model: str | None = None
        self.cli_version: str | None = None
        self._tool_names: set[str] = set()
        self._tool_ids: set[str] = set()
        self._unidentified_tool_count = 0

    def ingest_event(self, event: dict) -> dict | None:
        """Ingest only bounded fields from supported Claude event shapes."""
        if not isinstance(event, dict):
            return None

        event_type = event.get("type")
        if event_type == "system" and event.get("subtype") == "init":
            self._ingest_init(event)

        safe_progress: dict | None = None
        if event_type == "message_start":
            self._ingest_message(event.get("message"))
        elif event_type == "content_block_start":
            safe_progress = self._ingest_tool_block(event.get("content_block"))
        elif event_type == "stream_event":
            stream_event = event.get("event")
            if isinstance(stream_event, dict):
                stream_type = stream_event.get("type")
                if stream_type == "message_start":
                    self._ingest_message(stream_event.get("message"))
                elif stream_type == "content_block_start":
                    safe_progress = self._ingest_tool_block(
                        stream_event.get("content_block")
                    )
        elif event_type == "assistant":
            message = event.get("message")
            self._ingest_message(message)
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        progress = self._ingest_tool_block(block)
                        if safe_progress is None:
                            safe_progress = progress
        return safe_progress

    def _ingest_init(self, event: dict) -> None:
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id:
            self.session_id = session_id
        model = event.get("model")
        if isinstance(model, str) and model:
            self.requested_model = model
        cli_version = event.get("claude_code_version")
        if isinstance(cli_version, str) and cli_version:
            self.cli_version = cli_version

    def _ingest_message(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        model = message.get("model")
        if isinstance(model, str) and model and self.observed_model is None:
            self.observed_model = model

    def _ingest_tool_block(self, block: Any) -> dict | None:
        """Extract only a tool id and name, never its input or result."""
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            return None

        name = block.get("name")
        tool_id = block.get("id")
        is_new = False
        if isinstance(tool_id, str) and tool_id:
            if tool_id not in self._tool_ids:
                self._tool_ids.add(tool_id)
                is_new = True
        elif isinstance(name, str) and name:
            self._unidentified_tool_count += 1
            is_new = True

        if isinstance(name, str) and name:
            self._tool_names.add(name)
            if is_new:
                return {
                    "stage": "tool",
                    "summary": f"tool_use: {name}",
                    "session_id": self.session_id or "",
                }
        return None

    def to_dict(self) -> dict:
        return {
            "source": self.SOURCE,
            "session_id": self.session_id or "",
            "requested_model": self.requested_model or "",
            "observed_model": self.observed_model or "",
            "cli_version": self.cli_version or "",
            "tool_names": sorted(self._tool_names)[: self.MAX_TOOL_NAMES],
            "tool_use_count": len(self._tool_ids) + self._unidentified_tool_count,
        }

    def has_evidence(self) -> bool:
        return bool(
            self.session_id
            or self.requested_model
            or self.observed_model
            or self.cli_version
            or self._tool_names
            or self._tool_ids
            or self._unidentified_tool_count
        )


def _metadata_with_evidence(
    evidence: _BoundedProviderEvidence,
    *,
    timeout: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return metadata dict containing optional timeout and bounded evidence.

    The timeout dict stays at the top level for backward compatibility with
    worker code that inspects ``result.metadata["timeout"]``.  Evidence is
    added as a sibling key when any bounded fields have been observed.
    """
    metadata: dict[str, Any] = {}
    if timeout is not None:
        metadata["timeout"] = timeout
    if evidence.has_evidence():
        metadata["provider_evidence"] = evidence.to_dict()
    return metadata


class ClaudeAdapter(AgentAdapter):
    def __init__(self, config: AgentConfig):
        super().__init__(name="claude", timeout=config.timeout)
        self.config = config

    def _with_system_prompt(self, prompt: str) -> str:
        if not self.config.system_prompt.strip():
            return prompt
        return f"{self.config.system_prompt.strip()}\n\nUSER: {prompt}"

    def _build_cmd(self, *, resume_session_id: str | None = None) -> list[str]:
        cmd = [
            self.config.claude_bin,
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
        ]
        if self.config.claude_dangerously_skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if self.config.model:
            cmd += ["--model", self.config.model]
        if resume_session_id:
            cmd += ["--resume", resume_session_id]
        return cmd

    async def call(
        self,
        prompt: str,
        *,
        timeout: int | None = None,
        work_dir: str | None = None,
        on_progress=None,
    ) -> AdapterResult:
        return await self._run(prompt, timeout=timeout, work_dir=work_dir, on_progress=on_progress)

    async def resume(
        self,
        session_id: str,
        prompt: str,
        *,
        timeout: int | None = None,
        work_dir: str | None = None,
        on_progress=None,
    ) -> AdapterResult:
        result = await self._run(
            prompt,
            timeout=timeout,
            work_dir=work_dir,
            on_progress=on_progress,
            resume_session_id=session_id,
        )
        result.resumed = True
        return result

    async def _run(
        self,
        prompt: str,
        *,
        timeout: int | None = None,
        work_dir: str | None = None,
        on_progress=None,
        resume_session_id: str | None = None,
    ) -> AdapterResult:
        timeout = timeout or self.config.timeout
        cmd = self._build_cmd(resume_session_id=resume_session_id)
        cwd = work_dir or self.config.work_dir

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=filtered_env(cwd=cwd),
                limit=10 * 1024 * 1024,
                **async_subprocess_kwargs(),
            )
        except FileNotFoundError:
            return AdapterResult(text=f"Claude CLI not found: {self.config.claude_bin}")

        response_text = ""
        session_id: str | None = resume_session_id
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        saw_output = False
        cleanup_attempted = False
        pid = proc.pid
        evidence = _BoundedProviderEvidence()

        async def cleanup() -> None:
            nonlocal cleanup_attempted
            if cleanup_attempted:
                return
            cleanup_attempted = True
            await terminate_owned_process_group(proc)

        try:
            full_prompt = self._with_system_prompt(prompt)
            assert proc.stdin is not None
            last_activity = loop.time()
            log.debug("claude_child_boundary monotonic_ns=%s pid=%s", int(last_activity * 1_000_000_000), pid)
            proc.stdin.write(full_prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

            while True:
                now = loop.time()
                if now >= deadline:
                    await cleanup()
                    return AdapterResult(
                        text=_timeout_message("total", timeout, timeout),
                        session_id=session_id,
                        metadata=_metadata_with_evidence(
                            evidence,
                            timeout={
                                "kind": "total",
                                "configured_budget_seconds": timeout,
                                "session_id": session_id or "",
                                "resume_allowed": bool(session_id),
                            },
                        ),
                    )

                assert proc.stdout is not None
                idle_timeout = (
                    self.config.activity_timeout if saw_output else self.config.first_byte_timeout
                )
                read_timeout = min(idle_timeout, deadline - now)
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=read_timeout)
                except asyncio.TimeoutError:
                    elapsed = loop.time() - last_activity
                    if not saw_output and elapsed >= self.config.first_byte_timeout:
                        await cleanup()
                        return AdapterResult(
                            text=_timeout_message("first_byte", self.config.first_byte_timeout, timeout),
                            session_id=session_id,
                            metadata=_metadata_with_evidence(
                                evidence,
                                timeout={
                                    "kind": "first_byte",
                                    "configured_budget_seconds": timeout,
                                    "session_id": session_id or "",
                                    "resume_allowed": bool(session_id),
                                },
                            ),
                        )
                    if saw_output and elapsed >= self.config.activity_timeout:
                        await cleanup()
                        return AdapterResult(
                            text=_timeout_message("activity", self.config.activity_timeout, timeout),
                            session_id=session_id,
                            metadata=_metadata_with_evidence(
                                evidence,
                                timeout={
                                    "kind": "activity",
                                    "configured_budget_seconds": timeout,
                                    "session_id": session_id or "",
                                    "resume_allowed": bool(session_id),
                                },
                            ),
                        )
                    continue
                if not raw:
                    break
                last_activity = loop.time()
                saw_output = True
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Feed parsed event into bounded evidence accumulator.
                tool_progress = evidence.ingest_event(event)
                if tool_progress:
                    _emit_progress(on_progress, tool_progress)

                if (
                    event.get("type") == "system"
                    and event.get("subtype") == "init"
                    and event.get("session_id")
                ):
                    session_id = event["session_id"]
                    _emit_progress(
                        on_progress,
                        {
                            "stage": "session",
                            "summary": "Claude session initialized",
                            "session_id": session_id,
                        },
                    )
                summary = _progress_summary_from_event(event)
                if summary:
                    _emit_progress(
                        on_progress,
                        {"stage": "stream", "summary": summary, "session_id": session_id or ""},
                    )
                if event.get("type") == "result":
                    if event.get("subtype") == "error" or event.get("is_error"):
                        return AdapterResult(
                            text=f"Claude error: {event.get('result', '')[:500]}",
                            session_id=session_id,
                            metadata=_metadata_with_evidence(evidence),
                        )
                    response_text = event.get("result", "") or response_text

            await proc.wait()
            if not response_text and proc.returncode != 0:
                assert proc.stderr is not None
                stderr = (await proc.stderr.read()).decode("utf-8", errors="replace")
                return AdapterResult(
                    text=f"Claude CLI failed ({proc.returncode}): {stderr[:500]}",
                    session_id=session_id,
                    metadata=_metadata_with_evidence(evidence),
                )
            return AdapterResult(
                text=response_text.strip() or "(no response)",
                session_id=session_id,
                metadata=_metadata_with_evidence(evidence),
            )
        except asyncio.CancelledError:
            await cleanup()
            raise
        except Exception as exc:
            if not cleanup_attempted:
                await cleanup()
            return AdapterResult(
                text=f"Claude error: {exc}",
                session_id=session_id,
                metadata=_metadata_with_evidence(evidence),
            )

    async def health_check(self) -> dict:
        import shutil

        found = shutil.which(self.config.claude_bin)
        return {
            "adapter": "claude",
            "bin": self.config.claude_bin,
            "available": found is not None,
            "path": found,
        }
