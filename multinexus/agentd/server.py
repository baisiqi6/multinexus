"""Agent daemon HTTP server and request processor.

Receives AgentRequest via HTTP POST, processes through existing adapters,
returns AgentResponse. Manages session lifecycle for a single agent identity.
"""

from __future__ import annotations

import asyncio
import logging
import time
from aiohttp import web

from ..adapters.base import AdapterResult
from ..adapters.factory import make_adapter
from ..handoff import split_handoff_lines
from ..handoff_handler import (
    CoordinatorHandoff,
    build_handoff_prompt,
    split_agent_report_lines,
)
from ..models import AgentConfig
from ..protocol import AgentRequest, AgentResponse, PlatformDestination
from ..sessions.store import SessionStore

log = logging.getLogger(__name__)


class AgentDaemon:
    """Local HTTP daemon for one agent identity.

    Endpoints:
        POST /request  — submit an AgentRequest, get an AgentResponse
        GET  /health   — adapter health check
    """

    def __init__(self, config: AgentConfig, *, host: str = "127.0.0.1", port: int = 0):
        self.config = config
        self.adapter = make_adapter(config)
        self.session_store = SessionStore(config.context_db_path)
        self.host = host
        self.port = port
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> int:
        """Start the HTTP server. Returns the actual port bound."""
        self._app = web.Application()
        self._app.router.add_post("/request", self._handle_request)
        self._app.router.add_get("/health", self._handle_health)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        actual_port = self._site._server.sockets[0].getsockname()[1]
        self.port = actual_port
        log.info("AgentDaemon started: agent=%s host=%s port=%s", self.config.id, self.host, actual_port)
        return actual_port

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        log.info("AgentDaemon stopped: agent=%s", self.config.id)

    async def _handle_request(self, request: web.Request) -> web.Response:
        body = await request.text()
        try:
            agent_req = AgentRequest.from_json(body)
        except Exception as exc:
            return web.json_response(
                {"error": f"invalid request: {exc}"},
                status=400,
            )

        if agent_req.agent_id != self.config.id:
            return web.json_response(
                {"error": f"agent_id mismatch: expected {self.config.id}, got {agent_req.agent_id}"},
                status=403,
            )

        response = await self._process_request(agent_req)
        return web.Response(
            text=response.to_json(),
            content_type="application/json",
        )

    async def _handle_health(self, request: web.Request) -> web.Response:
        try:
            health = await self.adapter.health_check()
        except Exception as exc:
            health = {
                "adapter": self.config.adapter,
                "bin": "?",
                "available": False,
                "error": str(exc),
            }
        return web.json_response({"agent_id": self.config.id, **health})

    async def _process_request(self, req: AgentRequest) -> AgentResponse:
        start = time.time()
        progress_state: dict = {"partial": ""}

        async with self._lock:
            try:
                result = await self._run_adapter(req, progress_state=progress_state)
            except Exception as exc:
                log.exception("Adapter failed for request %s", req.request_id)
                result = AdapterResult(text=f"Agent error: {exc}")

        duration_ms = int((time.time() - start) * 1000)

        # Split handoff and report lines from response
        report_lines, response_without_reports = split_agent_report_lines(result.text)
        handoff_lines, display_text = split_handoff_lines(response_without_reports)

        return AgentResponse(
            request_id=req.request_id,
            agent_id=self.config.id,
            text=display_text,
            session_id=result.session_id or "",
            resumed=result.resumed,
            success=not self._is_error(result.text),
            error="" if not self._is_error(result.text) else result.text,
            handoff_lines=handoff_lines,
            report_lines=report_lines,
            duration_ms=duration_ms,
            destination=req.destination,
            metadata=result.metadata,
        )

    async def _run_adapter(
        self,
        req: AgentRequest,
        *,
        progress_state: dict,
    ) -> AdapterResult:
        prompt = req.prompt
        if req.system_prompt and not prompt.startswith(req.system_prompt):
            prompt = f"{req.system_prompt}\n\n{prompt}"

        work_dir = req.work_dir or self.config.work_dir
        timeout = req.timeout or self.config.timeout
        progress_cb = self._make_progress_callback(progress_state)

        # Determine session scope
        if req.session_scope:
            session_scope_id = req.session_scope
        elif req.origin:
            session_scope_id = f"channel:{req.origin.channel_id}"
        else:
            session_scope_id = f"request:{req.request_id}"

        legacy_scope_ids = req.legacy_scope_ids

        # Check for existing session
        existing = self.session_store.get_first_active(
            scope_ids=(session_scope_id, *legacy_scope_ids),
            agent_id=self.config.id,
        )

        # Check work_dir mismatch
        if existing:
            current_work_dir = work_dir
            if (
                current_work_dir
                and existing.get("work_dir")
                and current_work_dir != existing["work_dir"]
            ):
                log.info(
                    "Session work_dir mismatch (had=%s now=%s), marking stale",
                    existing["work_dir"], current_work_dir,
                )
                self.session_store.mark_stale(
                    scope_id=existing["scope_id"],
                    agent_id=self.config.id,
                )
                existing = None

        try:
            if existing:
                log.info(
                    "Resuming session %s for scope=%s",
                    existing["session_id"], existing["scope_id"],
                )
                result: AdapterResult = await asyncio.wait_for(
                    self.adapter.resume(
                        existing["session_id"],
                        prompt,
                        work_dir=work_dir,
                        on_progress=progress_cb,
                    ),
                    timeout=timeout,
                )
                if self._is_error(result.text):
                    log.warning("Resume failed for %s, falling back to fresh call", existing["session_id"])
                    self.session_store.mark_stale(
                        scope_id=existing["scope_id"],
                        agent_id=self.config.id,
                    )
                    existing = None
                    progress_state["partial"] = ""
                    result = await asyncio.wait_for(
                        self.adapter.call(
                            prompt,
                            work_dir=work_dir,
                            on_progress=progress_cb,
                        ),
                        timeout=timeout,
                    )
            else:
                result = await asyncio.wait_for(
                    self.adapter.call(
                        prompt,
                        work_dir=work_dir,
                        on_progress=progress_cb,
                    ),
                    timeout=timeout,
                )
        except asyncio.TimeoutError:
            result = AdapterResult(text=f"Agent error: timed out after {timeout}s")

        # Persist session
        if result.session_id:
            self.session_store.upsert(
                scope_id=session_scope_id,
                agent_id=self.config.id,
                adapter=self.config.adapter,
                session_id=result.session_id,
                work_dir=work_dir,
            )
            if existing and existing["scope_id"] != session_scope_id:
                self.session_store.mark_stale(
                    scope_id=existing["scope_id"],
                    agent_id=self.config.id,
                )

        return result

    @staticmethod
    def _make_progress_callback(progress_state: dict):
        def _on_progress(partial_text):
            if isinstance(partial_text, dict):
                progress_state["partial"] = partial_text.get("summary", "")
                if partial_text.get("session_id"):
                    progress_state["session_id"] = partial_text["session_id"]
                return
            progress_state["partial"] = partial_text
        return _on_progress

    _ERROR_PREFIXES = (
        "Agent error:",
        "OpenCode CLI failed", "OpenCode timed out",
        "Codex CLI failed", "Codex timed out", "Codex stopped responding", "Codex resume failed",
        "Hermes CLI failed", "Hermes timed out",
        "Claude CLI failed", "Claude error:", "Claude timeout",
        "omp CLI failed", "omp timed out",
    )

    @classmethod
    def _is_error(cls, text: str) -> bool:
        return any(text.startswith(p) for p in cls._ERROR_PREFIXES)
