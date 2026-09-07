"""ZCode CLI direct adapter.

The contract is pinned to the real ZCode 0.16.3 headless JSON shape. Unknown
JSON fails loudly so a vendor change cannot silently lose provider session
identity or turn a failed invocation into a successful delivery.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

from ..models import AgentConfig
from ..usage import unknown_usage_evidence
from .base import AdapterResult, AgentAdapter, failed_result, timed_out_result
from .utils import async_subprocess_kwargs, filtered_env, terminate_owned_process_group

log = logging.getLogger(__name__)

_SESSION_ID_RE = re.compile(r"^sess_[A-Za-z0-9._-]+$")


class ZCodeAdapter(AgentAdapter):
    allow_fresh_fallback_after_resume_error = False

    def __init__(self, config: AgentConfig):
        super().__init__(name="zcode", timeout=config.timeout)
        self.config = config

    def _with_system_prompt(self, prompt: str) -> str:
        if not self.config.system_prompt.strip():
            return prompt
        return f"{self.config.system_prompt.strip()}\n\nUSER: {prompt}"

    def _build_cmd(
        self,
        prompt: str,
        *,
        cwd: str,
        resume_session_id: str | None = None,
    ) -> list[str]:
        cmd = []
        if self.config.zcode_node_bin:
            cmd.append(self.config.zcode_node_bin)
        cmd.append(self.config.zcode_bin)
        cmd += [
            "--prompt",
            prompt,
            "--json",
            "--no-color",
            "--mode",
            self.config.zcode_permission_mode,
            "--cwd",
            cwd,
        ]
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
        if not _SESSION_ID_RE.fullmatch(session_id):
            return failed_result(
                "ZCode resume failed: invalid session id",
                category="protocol_error",
                session_id=session_id,
            )
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
        if self.config.zcode_transport == "app-server":
            from .zcode_protocol import run_native
            return await run_native(
                self.config, self._with_system_prompt(prompt), cwd=cwd,
                timeout=timeout, on_progress=on_progress,
                resume_session_id=resume_session_id,
            )
        if self.config.zcode_transport != "headless":
            return failed_result("ZCode error: unsupported transport", category="unavailable")
        if self.config.zcode_node_bin and not shutil.which(
            self.config.zcode_node_bin
        ):
            return failed_result(
                f"ZCode Node runtime not found: {self.config.zcode_node_bin}",
                category="unavailable",
            )
        if self.config.zcode_node_bin and not self.config.zcode_home_dir:
            return failed_result(
                "ZCode worker home required with Node runtime",
                category="unavailable",
            )
        if self.config.zcode_home_dir:
            home_dir = Path(self.config.zcode_home_dir)
            if not home_dir.is_absolute() or not home_dir.is_dir():
                return failed_result(
                    "ZCode worker home unavailable", category="unavailable"
                )
        cmd = self._build_cmd(
            self._with_system_prompt(prompt),
            cwd=cwd,
            resume_session_id=resume_session_id,
        )

        child_env = filtered_env(cwd=cwd)
        for key in list(child_env):
            if key.upper().startswith("COORDINATE_REMOTE_MCP_"):
                child_env.pop(key)
        if self.config.zcode_home_dir:
            home_dir = str(Path(self.config.zcode_home_dir))
            child_env.update(
                {
                    "HOME": home_dir,
                    "USERPROFILE": home_dir,
                    "APPDATA": str(Path(home_dir) / "AppData" / "Roaming"),
                    "LOCALAPPDATA": str(Path(home_dir) / "AppData" / "Local"),
                    "ZCODE_STORAGE_DIR": str(Path(home_dir) / ".zcode"),
                    "ZCODE_DATA_BASE_DIR": home_dir,
                }
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=cwd,
                env=child_env,
                limit=10 * 1024 * 1024,
                **async_subprocess_kwargs(),
            )
        except FileNotFoundError:
            return failed_result(
                f"ZCode CLI not found: {self.config.zcode_bin}",
                category="unavailable",
            )
        except Exception as exc:
            log.warning("zcode spawn failed: %s", type(exc).__name__)
            return failed_result("ZCode error: spawn failed", category="process_error")

        cleaned = False

        async def cleanup() -> None:
            nonlocal cleaned
            if cleaned:
                return
            cleaned = True
            await terminate_owned_process_group(proc)

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await cleanup()
            return timed_out_result(
                f"ZCode timeout after {timeout}s. Aborted, no handoff.",
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
            log.warning("zcode execution failed: %s", type(exc).__name__)
            return failed_result(
                "ZCode error: execution failed",
                category="process_error",
                session_id=resume_session_id,
            )

        if proc.returncode != 0:
            return failed_result(
                f"ZCode CLI failed ({proc.returncode})",
                category="process_error",
                session_id=resume_session_id,
            )

        try:
            payload = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return failed_result(
                "ZCode error: invalid JSON response",
                category="protocol_error",
                session_id=resume_session_id,
            )
        parsed = self._parse_contract(payload, resume_session_id=resume_session_id)
        if isinstance(parsed, str):
            return failed_result(
                parsed, category="protocol_error", session_id=resume_session_id
            )
        response_text, session_id, metadata = parsed

        if on_progress:
            on_progress(
                {
                    "stage": "complete",
                    "summary": "ZCode turn completed",
                    "session_id": session_id,
                }
            )
        if not response_text:
            return failed_result(
                "(no response)",
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

    def _parse_contract(
        self, payload: Any, *, resume_session_id: str | None
    ) -> tuple[str, str, dict[str, Any]] | str:
        if not isinstance(payload, dict):
            return "ZCode error: unexpected JSON contract"
        session_id = payload.get("sessionId")
        response = payload.get("response")
        projection = payload.get("projection")
        if (
            not isinstance(session_id, str)
            or not _SESSION_ID_RE.fullmatch(session_id)
            or not isinstance(response, str)
            or not isinstance(projection, dict)
            or not isinstance(projection.get("status"), str)
        ):
            return "ZCode error: unexpected JSON contract"
        if resume_session_id and session_id != resume_session_id:
            return "ZCode resume failed: session mismatch"

        turn_count = projection.get("turnCount")
        safe_turn_count = turn_count if isinstance(turn_count, int) else None
        metadata = {
            "adapter": "zcode",
            "provider_evidence": {
                "source": "zcode_cli_json_0_16_3",
                "session_status": projection["status"][:100],
                "turn_count": safe_turn_count,
            },
            # ZCode 当前无 usage contract：显式 unknown 单条，不估算。
            "usage_evidence": unknown_usage_evidence(provider="zcode"),
        }
        return response.strip(), session_id, metadata

    async def health_check(self) -> dict:
        if self.config.zcode_transport == "app-server":
            from .zcode_protocol import CONTRACT, _binaries
            try:
                _binaries(self.config)
                root = self.config.zcode_context_root
                available = isinstance(root, str) and Path(root).is_absolute()
            except (OSError, ValueError):
                available = False
            return {"adapter": "zcode", "transport": "app-server", "contract": CONTRACT,
                    "available": available, "context_validation": "performed before each invocation"}
        if self.config.zcode_transport != "headless":
            return {"adapter": "zcode", "available": False, "error": "unsupported transport"}
        node_path = (
            shutil.which(self.config.zcode_node_bin)
            if self.config.zcode_node_bin
            else None
        )
        path = (
            str(Path(self.config.zcode_bin))
            if self.config.zcode_node_bin and Path(self.config.zcode_bin).is_file()
            else shutil.which(self.config.zcode_bin)
        )
        home_available = (
            (not self.config.zcode_node_bin and not self.config.zcode_home_dir)
            or (
                self.config.zcode_home_dir is not None
                and Path(self.config.zcode_home_dir).is_absolute()
                and Path(self.config.zcode_home_dir).is_dir()
            )
        )
        return {
            "adapter": "zcode",
            "bin": self.config.zcode_bin,
            "node_bin": self.config.zcode_node_bin,
            "available": path is not None
            and (not self.config.zcode_node_bin or node_path is not None)
            and home_available,
            "path": path,
            "node_path": node_path,
            "home_available": home_available,
            "contract": "zcode-cli-json-0.16.3",
        }
