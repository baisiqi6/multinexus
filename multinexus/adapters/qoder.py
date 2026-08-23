"""Qoder CLI direct adapter.

Qoder 1.1.x does not expose a stdio ACP server, so this adapter uses its
headless JSON result contract.  Only bounded provider evidence is retained;
raw events, permission details, and model reasoning are never forwarded.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from typing import Any

from ..models import AgentConfig
from .base import AdapterResult, AgentAdapter, failed_result, timed_out_result
from .utils import async_subprocess_kwargs, filtered_env, terminate_owned_process_group

log = logging.getLogger(__name__)


class QoderAdapter(AgentAdapter):
    def __init__(self, config: AgentConfig):
        super().__init__(name="qoder", timeout=config.timeout)
        self.config = config

    def _with_system_prompt(self, prompt: str) -> str:
        if not self.config.system_prompt.strip():
            return prompt
        return f"{self.config.system_prompt.strip()}\n\nUSER: {prompt}"

    def _build_cmd(self, *, resume_session_id: str | None = None) -> list[str]:
        cmd = [
            self.config.qoder_bin,
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            self.config.qoder_permission_mode,
        ]
        if self.config.model:
            cmd += ["--model", self.config.model]
        if self.config.qoder_reasoning_effort:
            cmd += ["--reasoning-effort", self.config.qoder_reasoning_effort]
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
        timeout = timeout or self.config.timeout
        cwd = work_dir or self.config.work_dir or os.getcwd()
        cmd = self._build_cmd(resume_session_id=resume_session_id)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=cwd,
                env=filtered_env(cwd=cwd),
                limit=10 * 1024 * 1024,
                **async_subprocess_kwargs(),
            )
        except FileNotFoundError:
            return failed_result(
                text=f"Qoder CLI not found: {self.config.qoder_bin}",
                category="unavailable",
            )
        except Exception as exc:
            log.warning("qoder spawn failed: %s", type(exc).__name__)
            return failed_result(
                text="Qoder error: spawn failed",
                category="process_error",
            )

        cleaned = False

        async def cleanup() -> None:
            nonlocal cleaned
            if cleaned:
                return
            cleaned = True
            await terminate_owned_process_group(proc)

        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(self._with_system_prompt(prompt).encode("utf-8")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            await cleanup()
            return timed_out_result(
                text=f"Qoder timeout after {timeout}s. Aborted, no handoff.",
                session_id=resume_session_id,
                metadata={
                    "timeout": {
                        "kind": "total",
                        "configured_budget_seconds": timeout,
                    }
                },
            )
        except asyncio.CancelledError:
            await cleanup()
            raise
        except Exception as exc:
            await cleanup()
            log.warning("qoder execution failed: %s", type(exc).__name__)
            return failed_result(
                text="Qoder error: execution failed",
                category="process_error",
                session_id=resume_session_id,
            )

        if proc.returncode != 0:
            return failed_result(
                text=f"Qoder CLI failed ({proc.returncode})",
                category="process_error",
                session_id=resume_session_id,
            )

        try:
            payload = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return failed_result(
                text="Qoder error: invalid JSON response",
                category="protocol_error",
                session_id=resume_session_id,
            )
        if not isinstance(payload, dict) or payload.get("is_error"):
            return failed_result(
                text="Qoder error: provider returned an error",
                category="provider_error",
                session_id=resume_session_id,
            )

        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            session_id = resume_session_id
        if resume_session_id and session_id != resume_session_id:
            return failed_result(
                text="Qoder resume failed: session mismatch",
                category="protocol_error",
                session_id=resume_session_id,
            )

        text = payload.get("result")
        response_text = text.strip() if isinstance(text, str) else ""
        metadata = self._metadata(payload)
        if on_progress:
            on_progress(
                {
                    "stage": "complete",
                    "summary": "Qoder turn completed",
                    "session_id": session_id or "",
                }
            )
        if not response_text:
            return failed_result(
                text="(no response)",
                category="no_response",
                session_id=session_id,
                resumed=bool(resume_session_id),
                metadata=metadata,
            )

        return AdapterResult(
            text=response_text,
            session_id=session_id,
            resumed=bool(resume_session_id),
            metadata=metadata,
        )

    def _metadata(self, payload: dict[str, Any]) -> dict[str, Any]:
        model_usage = payload.get("modelUsage")
        observed_models = (
            sorted(str(key) for key in model_usage)[:8]
            if isinstance(model_usage, dict)
            else []
        )
        return {
            "adapter": "qoder",
            "provider_evidence": {
                "source": "qoder_cli_json",
                "requested_model": self.config.model or "",
                "observed_models": observed_models,
                "stop_reason": str(payload.get("stop_reason") or "")[:100],
            },
        }

    async def health_check(self) -> dict:
        path = shutil.which(self.config.qoder_bin)
        return {
            "adapter": "qoder",
            "bin": self.config.qoder_bin,
            "available": path is not None,
            "path": path,
        }
