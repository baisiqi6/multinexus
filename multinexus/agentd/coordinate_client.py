"""Client for submitting bridge requests via coordinate runtime.

Uses the coordinate CLI to submit requests, which creates pending jobs
that standalone agentd processes can claim. This is the bridge -> coordinate
part of the N+M runtime boundary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from typing import Any

log = logging.getLogger(__name__)


class CoordinateRuntimeError(RuntimeError):
    """Raised when the Coordinate CLI returns a non-zero exit or non-JSON output."""


def normalize_recovery_reason(value: Any) -> str:
    """Validate and normalize a recovery reason per the Coordinate contract.

    Returns the stripped reason. Raises ``CoordinateRuntimeError`` on any
    contract violation so callers fail closed before invoking subprocesses.
    """
    if not isinstance(value, str):
        raise CoordinateRuntimeError("recovery_reason must be a string")
    reason = value.strip()
    if not reason:
        raise CoordinateRuntimeError("recovery_reason must be non-empty after strip")
    if any(ord(ch) < 32 for ch in reason):
        raise CoordinateRuntimeError("recovery_reason must not contain control characters")
    if len(reason) > 512:
        raise CoordinateRuntimeError("recovery_reason must be at most 512 characters")
    return reason


def normalize_claim_reap_policy(
    reap_mode: Any = "global",
    reap_reason: Any = None,
) -> tuple[str, str | None]:
    """Validate and normalize a claim reap policy per the Coordinate P1 contract.

    Exact modes: ``global`` accepts only ``None`` reason and returns
    ``("global", None)``. ``none`` requires a non-blank, stripped-stable,
    C0/DEL-free string reason of at most 512 code points and 2048 UTF-8 bytes.
    Any other combination fails closed before subprocess invocation.
    """
    if reap_mode not in ("global", "none"):
        raise CoordinateRuntimeError(
            f"reap_mode must be 'global' or 'none', got {reap_mode!r}"
        )
    if reap_mode == "global":
        if reap_reason is not None:
            raise CoordinateRuntimeError(
                "global reap_mode must not carry reap_reason"
            )
        return ("global", None)
    if not isinstance(reap_reason, str):
        raise CoordinateRuntimeError(
            f"none reap_mode requires a string reap_reason, got {type(reap_reason).__name__}"
        )
    reason = reap_reason.strip()
    if not reason:
        raise CoordinateRuntimeError(
            "none reap_mode requires a non-blank reap_reason"
        )
    if reap_reason != reason:
        raise CoordinateRuntimeError(
            "reap_reason must be stripped-stable (no leading/trailing whitespace)"
        )
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in reason):
        raise CoordinateRuntimeError(
            "reap_reason must not contain C0 or DEL control characters"
        )
    if len(reason) > 512:
        raise CoordinateRuntimeError(
            "reap_reason must be at most 512 code points"
        )
    if len(reason.encode("utf-8")) > 2048:
        raise CoordinateRuntimeError(
            "reap_reason must be at most 2048 UTF-8 bytes"
        )
    return "none", reason


def _require_non_empty_workspace(workspace_id: str) -> str:
    """Fail closed before any subprocess invocation when workspace is missing."""
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise CoordinateRuntimeError("workspace_id is required")
    return workspace_id.strip()


class CoordinateRuntimeClient:
    """Submit bridge requests to coordinate runtime.

    Wraps the coordinate CLI:
      runtime request submit <workspace> --target-agent <id> --prompt <text>
        --origin-json <json> --reply-json <json>
    """

    def __init__(
        self,
        *,
        cli_path: str,
        db_path: str,
    ):
        self.cli_path = cli_path
        self.db_path = db_path

        if sys.platform == "win32" and cli_path.endswith(".py"):
            self._base_cmd = [sys.executable, cli_path]
        else:
            self._base_cmd = [cli_path]

    def _base_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["MAC_DB"] = self.db_path
        return env

    async def resolve_channel_workspace(
        self,
        *,
        platform: str,
        channel_id: str,
    ) -> str | None:
        """Resolve (platform, channel_id) -> workspace_id via Coordinate.

        Returns the workspace id when bound, None when unbound. Any Coordinate
        error or malformed envelope raises CoordinateRuntimeError; callers must
        not downgrade it to unbound.
        """
        if not isinstance(platform, str) or not platform.strip():
            raise CoordinateRuntimeError("platform is required")
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise CoordinateRuntimeError("channel_id is required")
        canonical_platform = platform.strip().lower()
        canonical_channel_id = channel_id.strip()
        cmd = [
            *self._base_cmd,
            "workspace", "channel", "resolve",
            canonical_platform,
            canonical_channel_id,
        ]
        result = await asyncio.to_thread(self._run_cli, cmd)
        if not isinstance(result, dict):
            raise CoordinateRuntimeError(
                f"channel resolve returned non-dict result: {result!r}"
            )
        if result.get("error"):
            raise CoordinateRuntimeError(
                f"channel resolve runtime error: {result['error']}"
            )
        status = result.get("status")
        binding = result.get("binding")
        if status == "unbound" and binding is None:
            return None
        if status == "bound" and isinstance(binding, dict):
            workspace_id = binding.get("workspace_id")
            if not isinstance(workspace_id, str) or not workspace_id.strip():
                raise CoordinateRuntimeError(
                    "channel resolve returned bound envelope without a non-empty workspace_id"
                )
            binding_platform = str(binding.get("platform", "")).strip().lower()
            binding_channel_id = str(binding.get("channel_id", "")).strip()
            if binding_platform != canonical_platform:
                raise CoordinateRuntimeError(
                    f"channel resolve platform mismatch: "
                    f"expected {canonical_platform!r}, got {binding_platform!r}"
                )
            if binding_channel_id != canonical_channel_id:
                raise CoordinateRuntimeError(
                    f"channel resolve channel_id mismatch: "
                    f"expected {canonical_channel_id!r}, got {binding_channel_id!r}"
                )
            return workspace_id.strip()
        raise CoordinateRuntimeError(
            f"channel resolve returned unexpected envelope: {result!r}"
        )

    async def submit_request(
        self,
        *,
        target_agent: str,
        prompt: str,
        origin_json: dict,
        reply_json: dict,
        workspace_id: str,
        task_id: str = "",
        message_id: str = "",
        idempotency_key: str = "",
    ) -> dict:
        """Submit a bridge request to coordinate. Returns the coordinate response dict."""
        workspace = _require_non_empty_workspace(workspace_id)
        cmd = [
            *self._base_cmd,
            "runtime", "request", "submit",
            workspace,
            "--target-agent", target_agent,
            "--prompt", prompt,
            "--origin-json", json.dumps(origin_json, ensure_ascii=False),
            "--reply-json", json.dumps(reply_json, ensure_ascii=False),
        ]
        if task_id:
            cmd.extend(["--task-id", task_id])
        idempotency = idempotency_key or message_id
        if idempotency:
            cmd.extend(["--idempotency-key", idempotency])

        log.info("coordinate submit: agent=%s msg=%s workspace=%s", target_agent, message_id, workspace)

        return await asyncio.to_thread(self._run_cli, cmd)

    async def claim_job(
        self,
        *,
        agent_id: str,
        recoverable: bool = False,
        recovery_reason: str = "",
        prior_process_stopped: bool = False,
        reap_mode: str = "global",
        reap_reason: Any = None,
    ) -> dict[str, Any]:
        """Claim the next pending job for this agent. Returns the full result envelope.

        recoverable=True (operator recovery mode only) also claims timed_out+
        recoverable jobs. It requires both a non-empty ``recovery_reason`` and
        ``prior_process_stopped=True`` as evidence; missing evidence fails closed
        before the subprocess is invoked. recoverable=False rejects any supplied
        evidence. Default False = only pending jobs, so a normal worker never
        auto-reclaims a stuck timed_out job.

        reap_mode="global" (default) preserves legacy argv exactly and omits both
        Coordinate reap flags. reap_mode="none" appends ``--reap-mode none
        --reap-reason <reason>``. Policy is validated inside this method before
        any subprocess invocation; callers must never bypass it.

        Even when ``claimed`` is False, the full inner result dict is returned so
        the caller can preserve ``reason`` and blocker diagnostics.

        The caller must validate ``result["execution_context"]`` before invoking
        an adapter; this client preserves the raw Coordinate response.
        """

        # Validate and normalize reap policy before any subprocess invocation.
        normalized_mode, normalized_reap_reason = normalize_claim_reap_policy(
            reap_mode=reap_mode,
            reap_reason=reap_reason,
        )

        if recoverable:
            normalized_reason = normalize_recovery_reason(recovery_reason)
            if prior_process_stopped is not True:
                raise CoordinateRuntimeError(
                    "recoverable claim requires prior_process_stopped=True"
                )
        else:
            if recovery_reason != "":
                raise CoordinateRuntimeError(
                    "non-recoverable claim must not carry recovery_reason"
                )
            if prior_process_stopped is not False:
                raise CoordinateRuntimeError(
                    "non-recoverable claim must not carry prior_process_stopped"
                )
            normalized_reason = ""

        cmd = [
            *self._base_cmd,
            "runtime", "job", "claim",
            "--agent-id", agent_id,
        ]
        if recoverable:
            cmd.append("--recoverable")
            cmd.extend(["--recovery-reason", normalized_reason])
            cmd.append("--prior-process-stopped")
        if normalized_mode == "none":
            assert normalized_reap_reason is not None
            cmd.extend(
                ["--reap-mode", "none", "--reap-reason", normalized_reap_reason]
            )
        result = await asyncio.to_thread(self._run_cli, cmd)
        if not isinstance(result, dict):
            raise CoordinateRuntimeError(
                f"coordinate claim for {agent_id} returned non-dict result"
            )
        inner = result.get("result")
        if not isinstance(inner, dict):
            raise CoordinateRuntimeError(
                f"coordinate claim for {agent_id} returned missing/invalid result envelope"
            )
        return inner

    async def report_job(
        self,
        *,
        job_id: str,
        agent_id: str,
        status: str,
        result_json: dict,
        attempt_token: int | None = None,
        lease_id: str | None = None,
    ) -> dict:
        """Report job result back to coordinate."""
        cmd = [
            *self._base_cmd,
            "runtime", "job", "report",
            job_id,
            "--agent-id", agent_id,
            "--status", status,
            "--result-json", json.dumps(result_json, ensure_ascii=False),
        ]
        if attempt_token is not None:
            cmd.extend(["--attempt-token", str(attempt_token)])
        if lease_id is not None:
            cmd.extend(["--lease-id", lease_id])
        return await asyncio.to_thread(self._run_cli, cmd)

    async def record_progress(
        self,
        *,
        job_id: str,
        agent_id: str,
        stage: str = "",
        summary: str = "",
        session_id: str = "",
        attempt_token: int | None = None,
        lease_id: str | None = None,
    ) -> dict:
        """Record a bounded progress checkpoint for a running job."""
        cmd = [
            *self._base_cmd,
            "runtime", "job", "progress",
            job_id,
            "--agent-id", agent_id,
        ]
        if stage:
            cmd.extend(["--stage", stage])
        if summary:
            cmd.extend(["--summary", summary])
        if session_id:
            cmd.extend(["--session-id", session_id])
        if attempt_token is not None:
            cmd.extend(["--attempt-token", str(attempt_token)])
        if lease_id is not None:
            cmd.extend(["--lease-id", lease_id])
        return await asyncio.to_thread(self._run_cli, cmd)

    async def renew_lease(
        self,
        *,
        job_id: str,
        agent_id: str,
        attempt_token: int,
        lease_id: str,
    ) -> dict:
        """Renew one managed lease."""
        cmd = [
            *self._base_cmd,
            "runtime", "job", "lease", "renew",
            job_id,
            "--agent-id", agent_id,
            "--attempt-token", str(attempt_token),
            "--lease-id", lease_id,
        ]
        return await asyncio.to_thread(self._run_cli, cmd)

    async def reap_leases(
        self,
        *,
        actor: str = "agentd",
        batch_size: int = 100,
    ) -> dict:
        """Expire due active leases and make their jobs recoverable."""
        cmd = [
            *self._base_cmd,
            "runtime", "job", "lease", "reap",
            "--actor", actor,
            "--batch-size", str(batch_size),
        ]
        return await asyncio.to_thread(self._run_cli, cmd)

    async def wait_for_job_result(
        self,
        *,
        job_id: str,
        workspace_id: str,
        poll_interval: float = 2.0,
        timeout: float = 1800.0,
    ) -> dict | None:
        """Poll coordinate until a job reaches a terminal state, then return the result.

        Returns the job dict with result_json populated, or None on timeout.
        """
        import time as _time
        start = _time.monotonic()
        workspace = _require_non_empty_workspace(workspace_id)
        while _time.monotonic() - start < timeout:
            job = await self._get_job(job_id, workspace_id=workspace)
            if job is None:
                await asyncio.sleep(poll_interval)
                continue
            status = job.get("status", "")
            if status in ("done", "failed", "timed_out"):
                return job
            await asyncio.sleep(poll_interval)
        return None

    async def _get_job(self, job_id: str, *, workspace_id: str) -> dict | None:
        """Fetch a single job's current state from coordinate."""
        workspace = _require_non_empty_workspace(workspace_id)
        cmd = [
            *self._base_cmd,
            "job", "list",
            "--workspace-id", workspace,
        ]
        result = await asyncio.to_thread(self._run_cli, cmd)
        if not isinstance(result, dict):
            raise CoordinateRuntimeError(
                f"coordinate job list for {job_id} returned non-dict result"
            )
        jobs = result.get("jobs", [])
        if not isinstance(jobs, list):
            raise CoordinateRuntimeError(
                f"coordinate job list for {job_id} returned non-list jobs"
            )
        for job in jobs:
            if job.get("id") == job_id:
                return job
        return None

    def _run_cli(self, cmd: list[str]) -> dict:
        """Run the Coordinate CLI and return its JSON output as a dict.

        All failure modes (non-zero exit, timeout, non-JSON output, OS errors,
        and malformed envelopes) are normalized into a bounded
        CoordinateRuntimeError so the agentd loop can back off instead of
        spinning or crashing.
        """
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                env=self._base_env(),
            )
        except subprocess.TimeoutExpired:
            raise CoordinateRuntimeError("coordinate CLI timed out")
        except OSError as exc:
            raise CoordinateRuntimeError(f"coordinate CLI execution failed: {exc}")
        except Exception as exc:
            raise CoordinateRuntimeError(f"coordinate CLI unexpected error: {exc}")

        if proc.returncode != 0:
            stderr = proc.stderr[:300] if proc.stderr else ""
            raise CoordinateRuntimeError(
                f"coordinate CLI exit {proc.returncode}: {stderr}"
            )

        stdout = proc.stdout.strip()
        if not stdout:
            raise CoordinateRuntimeError("coordinate CLI returned empty stdout")

        try:
            result = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise CoordinateRuntimeError(f"coordinate CLI non-JSON: {exc}")

        if not isinstance(result, dict):
            raise CoordinateRuntimeError("coordinate CLI returned non-object JSON")

        # If Coordinate itself reported a runtime error, surface it as a bounded
        # exception rather than making every caller inspect an error dict.
        if result.get("error"):
            raise CoordinateRuntimeError(
                f"coordinate runtime error: {result['error']}"
            )

        return result
