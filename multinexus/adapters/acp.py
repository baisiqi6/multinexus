"""Generic ACP v1 adapter.

Connects to any stdio ACP v1 agent server (for example ``kimi acp``) via the
official ``agent-client-protocol`` Python SDK. This adapter is deliberately
brand-neutral: the concrete agent command is supplied entirely through
``AgentConfig.acp_command`` / ``acp_args``.

Safety invariants (do not relax):

* The wire protocol is pinned to ACP v1 and validated against ``initialize``.
* The client declares no filesystem / terminal / auth capability and refuses
  every permission request (never auto-approves).
* ``on_progress`` only carries bounded, structured, or accumulated text
  evidence. Agent thought chunks, raw ``_meta``, credentials, and raw
  JSON-RPC are never forwarded.
* User-visible ``AdapterResult.text`` carries only stable error categories;
  raw stderr and raw exception detail are never spliced into it.
* The agent subprocess is always a MultiNexus-owned process group and is
  terminated with ``terminate_owned_process_group`` exactly once on every
  exit path.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import acp
from acp import schema

from ..models import AgentConfig
from .base import AdapterResult, AgentAdapter, failed_result, timed_out_result
from .utils import async_subprocess_kwargs, filtered_env, terminate_owned_process_group

log = logging.getLogger(__name__)

# This adapter only speaks ACP wire protocol v1. We fail closed if the SDK we
# are pinned to, or the agent we connect to, negotiates anything else.
EXPECTED_PROTOCOL_VERSION = 1


def _build_client_capabilities() -> schema.ClientCapabilities:
    """Construct a minimal, fail-closed client capability set.

    * no filesystem capability (``fs=None``);
    * ``terminal=False``;
    * auth terminal explicitly ``False`` (no auth-terminal flow);
    * every other capability left unset.

    ``auth`` is constructed explicitly so the SDK does not fall back to its
    default ``{'terminal': False}`` dict, which trips a Pydantic serializer
    warning when the request is serialized.
    """
    return schema.ClientCapabilities(
        fs=None,
        terminal=False,
        auth=schema.AuthCapabilities(terminal=False),
    )


class _AcpClient:
    """Minimal ACP ``Client`` implementation.

    Only ``session_update`` and ``request_permission`` are implemented. Every
    other client handler is intentionally left absent: the SDK router then
    registers it as method-not-found (or, for optional terminal methods, a
    default no-op result), which is fail-closed for a client that declares no
    filesystem/terminal/auth capability. No dynamic fallback is provided, so
    ``getattr(client, "on_connect", None)`` stays ``None`` and the SDK never
    invokes an unexpected handler.
    """

    def __init__(self, sink: "_SessionSink") -> None:
        self._sink = sink

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self._sink.ingest_update(session_id, update)

    async def request_permission(
        self, session_id: str, tool_call: Any, options: Any, **kwargs: Any
    ) -> schema.RequestPermissionResponse:
        # Never auto-approve. Report a cancelled outcome so the agent treats
        # the operation as denied rather than hanging.
        self._sink.note_permission_denied(session_id)
        return schema.RequestPermissionResponse(
            outcome=schema.DeniedOutcome(outcome="cancelled")
        )


class _SessionSink:
    """Accumulates safe text and bounded progress evidence for one prompt.

    Only ``AgentMessageChunk`` text is appended to the final response.
    ``AgentThoughtChunk`` content is counted but its content is never stored
    or forwarded. Progress events are capped dicts.
    """

    MAX_PROGRESS_EVENTS = 64

    def __init__(self, on_progress) -> None:
        self._on_progress = on_progress
        self._text_parts: list[str] = []
        self._emitted = 0
        self._session_id: str | None = None
        self.thought_chunk_count = 0
        self.permission_denied_count = 0

    def ingest_update(self, session_id: str, update: Any) -> None:
        if isinstance(update, schema.AgentMessageChunk):
            text = self._block_text(update.content)
            if text:
                self._text_parts.append(text)
                self._emit(
                    {
                        "stage": "stream",
                        "summary": text[-1000:],
                        "session_id": session_id,
                    }
                )
        elif isinstance(update, schema.AgentThoughtChunk):
            # Count only; never capture or forward thought content.
            self.thought_chunk_count += 1
        elif isinstance(update, (schema.ToolCallStart, schema.ToolCallProgress)):
            title = getattr(update, "title", None)
            if isinstance(title, str) and title.strip():
                self._emit(
                    {
                        "stage": "tool",
                        "summary": f"tool_call: {title.strip()[:200]}",
                        "session_id": session_id,
                    }
                )
        # All other update kinds are ignored deliberately.

    def note_permission_denied(self, session_id: str) -> None:
        self.permission_denied_count += 1
        self._emit(
            {
                "stage": "permission",
                "summary": "permission request denied (default)",
                "session_id": session_id,
            }
        )

    def set_session_id(self, session_id: str) -> None:
        self._session_id = session_id

    @staticmethod
    def _block_text(content: Any) -> str:
        text = getattr(content, "text", None)
        return text if isinstance(text, str) else ""

    def _emit(self, event: dict[str, Any]) -> None:
        if self._on_progress is None:
            return
        if self._emitted >= self.MAX_PROGRESS_EVENTS:
            return
        self._emitted += 1
        self._on_progress(event)

    def final_text(self) -> str:
        return "".join(self._text_parts).strip()


class ACPAdapter(AgentAdapter):
    def __init__(self, config: AgentConfig):
        super().__init__(name="acp", timeout=config.timeout)
        self.config = config

    def _with_system_prompt(self, prompt: str) -> str:
        # Same minimal concatenation rule as the other direct adapters.
        if not self.config.system_prompt.strip():
            return prompt
        return f"{self.config.system_prompt.strip()}\n\nUSER: {prompt}"

    def _cmd(self) -> list[str]:
        return [self.config.acp_command, *self.config.acp_args]

    async def call(
        self,
        prompt: str,
        *,
        timeout: int | None = None,
        work_dir: str | None = None,
        on_progress=None,
    ) -> AdapterResult:
        return await self._run(
            prompt, timeout=timeout, work_dir=work_dir, on_progress=on_progress
        )

    async def resume(
        self,
        session_id: str,
        prompt: str,
        *,
        timeout: int | None = None,
        work_dir: str | None = None,
        on_progress=None,
    ) -> AdapterResult:
        return await self._run(
            prompt,
            timeout=timeout,
            work_dir=work_dir,
            on_progress=on_progress,
            resume_session_id=session_id,
        )

    async def _run(
        self,
        prompt: str,
        *,
        timeout: int | None,
        work_dir: str | None,
        on_progress,
        resume_session_id: str | None = None,
    ) -> AdapterResult:
        if not self.config.acp_command:
            return failed_result(
                text="ACP error: acp_command is not configured",
                category="unavailable",
            )

        timeout = timeout or self.config.timeout
        cwd = work_dir or self.config.work_dir or os.getcwd()
        cmd = self._cmd()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                # Raw agent stderr is neither user-visible evidence nor safe
                # metadata. Discard it at the process boundary so a noisy
                # child cannot fill an unread pipe or leak credentials.
                stderr=asyncio.subprocess.DEVNULL,
                cwd=cwd,
                env=filtered_env(cwd=cwd),
                limit=50 * 1024 * 1024,
                **async_subprocess_kwargs(),
            )
        except FileNotFoundError:
            return failed_result(
                text=f"ACP command not found: {self.config.acp_command}",
                category="unavailable",
            )
        except Exception:
            log.exception("acp spawn failed for command %r", self.config.acp_command)
            return failed_result(
                text="ACP error: spawn failed",
                category="process_error",
            )
        if proc.stdin is None or proc.stdout is None:
            await self._cleanup_process(proc)
            return failed_result(
                text="ACP error: subprocess stdio unavailable",
                category="process_error",
            )

        sink = _SessionSink(on_progress)
        conn = None
        try:
            # create_subprocess_exec already hands us a StreamWriter (stdin)
            # and a StreamReader (stdout); pass them straight to the SDK with
            # no second connect_read_pipe/StreamWriter wrapping.
            try:
                conn = acp.connect_to_agent(_AcpClient(sink), proc.stdin, proc.stdout)
            except Exception as exc:
                log.warning("acp connect failed: %s", self._safe_error_summary(exc))
                return failed_result(
                    text="ACP error: agent prompt failed",
                    category="process_error",
                    diagnostic=self._safe_error_summary(exc),
                )
            return await asyncio.wait_for(
                self._converse(conn, sink, prompt, cwd, resume_session_id),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            if conn is not None:
                await self._best_effort_cancel(conn, sink, resume_session_id)
            return timed_out_result(
                text=f"ACP timeout after {timeout}s. Aborted, no handoff.",
                session_id=self._session_id(sink, resume_session_id),
                metadata={"timeout": {"kind": "total", "configured_budget_seconds": timeout}},
            )
        except asyncio.CancelledError:
            if conn is not None:
                await self._best_effort_cancel(conn, sink, resume_session_id)
            raise
        except Exception as exc:
            # Never splice raw exception or stderr into user-visible text;
            # only the safe exception type enters the bounded diagnostic.
            log.warning("acp prompt failed: %s", self._safe_error_summary(exc))
            return failed_result(
                text="ACP error: agent prompt failed",
                category="provider_error",
                session_id=self._session_id(sink, resume_session_id),
                diagnostic=self._safe_error_summary(exc),
            )
        finally:
            if conn is not None:
                await self._bounded(conn.close(), budget=5.0, label="conn.close")
            # Exactly one bounded process-group cleanup on every exit path.
            await self._cleanup_process(proc)

    async def _converse(
        self,
        conn,
        sink: _SessionSink,
        prompt: str,
        cwd: str,
        resume_session_id: str | None,
    ) -> AdapterResult:
        if acp.PROTOCOL_VERSION != EXPECTED_PROTOCOL_VERSION:
            return failed_result(
                text=(
                    "ACP protocol mismatch: SDK speaks "
                    f"v{acp.PROTOCOL_VERSION}, adapter requires v{EXPECTED_PROTOCOL_VERSION}"
                ),
                category="protocol_error",
            )

        init = await conn.initialize(
            protocol_version=EXPECTED_PROTOCOL_VERSION,
            client_capabilities=_build_client_capabilities(),
        )
        if init.protocol_version != EXPECTED_PROTOCOL_VERSION:
            return failed_result(
                text=(
                    "ACP protocol mismatch: agent negotiated "
                    f"v{init.protocol_version}, adapter requires v{EXPECTED_PROTOCOL_VERSION}"
                ),
                category="protocol_error",
            )

        resumed = False
        if resume_session_id:
            session_id, resumed = await self._resume_session(
                conn, init.agent_capabilities, resume_session_id, cwd
            )
            if not resumed:
                return failed_result(
                    text=(
                        "ACP resume failed closed: agent declares no "
                        "session/resume or legacy session/load capability"
                    ),
                    category="protocol_error",
                    session_id=resume_session_id,
                    metadata=self._metadata(init, None),
                )
        else:
            session = await conn.new_session(cwd=cwd)
            session_id = session.session_id

        sink.set_session_id(session_id)  # used for bounded cancel/metadata

        full_prompt = self._with_system_prompt(prompt)
        response = await conn.prompt(
            session_id=session_id,
            prompt=[schema.TextContentBlock(type="text", text=full_prompt)],
        )
        stop_reason = str(getattr(response, "stop_reason", "") or "")
        metadata = self._metadata(init, stop_reason)
        response_text = sink.final_text()
        if not response_text:
            return failed_result(
                text="(no response)",
                category="no_response",
                session_id=session_id,
                resumed=resumed,
                metadata=metadata,
            )
        return AdapterResult(
            text=response_text,
            session_id=session_id,
            resumed=resumed,
            metadata=metadata,
        )

    async def _resume_session(
        self, conn, capabilities, session_id: str, cwd: str
    ) -> tuple[str, bool]:
        """Pick a resume method strictly from declared capabilities.

        Prefers the formal ``session/resume``; falls back to legacy
        ``session/load``. Returns ``(session_id, True)`` only on a real
        resume; otherwise ``(session_id, False)`` and the caller fails closed.
        """
        caps = capabilities
        resume_cap = getattr(getattr(caps, "session_capabilities", None), "resume", None)
        if resume_cap is not None:
            await conn.resume_session(session_id=session_id, cwd=cwd)
            return session_id, True
        if getattr(caps, "load_session", False):
            await conn.load_session(cwd=cwd, session_id=session_id)
            return session_id, True
        return session_id, False

    def _metadata(self, init, stop_reason: str | None) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "adapter": "acp",
            "protocol_version": int(init.protocol_version),
        }
        info = getattr(init, "agent_info", None)
        if info is not None:
            name = getattr(info, "name", None)
            version = getattr(info, "version", None)
            if isinstance(name, str) and name:
                metadata["agent_name"] = name
            if isinstance(version, str) and version:
                metadata["agent_version"] = version
        if stop_reason:
            metadata["stop_reason"] = stop_reason
        metadata["capabilities"] = self._capability_summary(
            getattr(init, "agent_capabilities", None)
        )
        return metadata

    @staticmethod
    def _capability_summary(caps) -> dict[str, Any]:
        """Bounded, credential-free capability flags for diagnostics."""
        if caps is None:
            return {}
        session_caps = getattr(caps, "session_capabilities", None)
        summary: dict[str, Any] = {
            "load_session": bool(getattr(caps, "load_session", False)),
            "resume": getattr(session_caps, "resume", None) is not None,
        }
        sessions = getattr(session_caps, "list", None)
        if sessions is not None:
            summary["list"] = True
        # auth_methods are never copied into metadata.
        return summary

    async def _best_effort_cancel(self, conn, sink: _SessionSink, fallback: str | None) -> None:
        session_id = self._session_id(sink, fallback)
        if not session_id:
            return
        await self._bounded(conn.cancel(session_id=session_id), budget=2.0, label="cancel")

    @staticmethod
    def _session_id(sink: _SessionSink, fallback: str | None) -> str | None:
        return sink._session_id or fallback

    @staticmethod
    def _safe_error_summary(exc: BaseException) -> str:
        """A bounded, credential-free one-line summary for internal logs."""
        return type(exc).__name__

    async def _cleanup_process(self, proc) -> None:
        await self._bounded(
            terminate_owned_process_group(proc), budget=5.0, label="cleanup"
        )

    @staticmethod
    async def _bounded(coro, *, budget: float, label: str) -> None:
        """Run a cleanup/cancel step with a hard budget, swallowing errors.

        Cleanup must never hang the caller nor mask the original outcome, so
        failures are logged and dropped deliberately (fail loud to logs, not
        to the result path).
        """
        try:
            await asyncio.wait_for(coro, timeout=budget)
        except Exception as exc:  # noqa: BLE001 - bounded cleanup
            log.warning("acp %s failed: %s", label, exc)

    async def health_check(self) -> dict:
        import shutil

        command = self.config.acp_command
        found = shutil.which(command) if command else None
        return {
            "adapter": "acp",
            "command": command,
            "arg_count": len(self.config.acp_args),
            "available": bool(command) and found is not None,
            "path": found,
            "protocol_version": EXPECTED_PROTOCOL_VERSION,
        }
