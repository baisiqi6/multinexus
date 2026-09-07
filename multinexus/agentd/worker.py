"""Standalone agentd worker: claims jobs from coordinate, executes adapter, reports.

This is the `agentd` side of the N+M runtime boundary:
```text
bridge -> coordinate runtime request submit -> coordinate job
agentd worker -> coordinate runtime job claim -> execute adapter -> report
```

Run as: ``python -m multinexus.agentd --config agents.toml --agent <id>``
"""

from __future__ import annotations

import asyncio
import json
import inspect
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..adapters.base import (
    OUTCOME_FAILED,
    OUTCOME_SUCCESS,
    OUTCOME_TIMED_OUT,
    NO_RESPONSE_SENTINEL,
    AdapterResult,
    is_error_text,
    safe_exception_diagnostic,
)
from ..adapters.factory import make_adapter
from ..context.envelope import parse_context_envelope
from ..context.prompt import delta_prompt, render_envelope
from ..models import AgentConfig
from ..sessions.store import SessionStore
from .coordinate_client import (
    CoordinateClaimAuthorityUncertainError,
    CoordinateContractError,
    CoordinateClaimReplayConflictError,
    CoordinateClaimTerminalError,
    CoordinateRuntimeError,
    make_coordinate_runtime_client,
    normalize_claim_reap_policy,
    normalize_recovery_reason,
)
from .execution_context import ExecutionContextError, validate_claim_response
from .executor_binding import ExecutorBindingError, validate_executor_binding
from .execution_lease import (
    ExecutionLeaseError,
    ExecutionLeaseV1,
    validate_execution_lease,
)
from .health import AgentdHealthProjection

log = logging.getLogger(__name__)


class LeaseLostError(RuntimeError):
    """Raised when the managed lease is lost or renewal fails authoritatively."""


RENEWAL_SAFETY_MARGIN_SECONDS = 5


@dataclass
class _SessionAttempt:
    # One attempt's observed row. Only our own successful CAS may replace it.
    snapshot: dict | None
    writable: bool = True


class AgentdWorker:
    """Standalone agentd that claims jobs from coordinate and processes them."""

    def __init__(self, config: AgentConfig):
        self.config = config
        self.adapter = make_adapter(config)
        self.session_store = SessionStore(config.context_db_path)
        # Single transport factory: cli (default) or http, never both.
        self.coordinate = make_coordinate_runtime_client(config)
        self.health = AgentdHealthProjection.from_environment(config.id)
        self._running = False
        self._wake = asyncio.Event()
        # Optimization eligibility only; the durable cursor remains solely in
        # SessionStore. A new process must first confirm a full-history turn.
        self._context_sessions: dict[str, tuple[str, str]] = {}

    async def verify_coordinate_contract(self, *, recoverable: bool = False) -> dict[str, Any]:
        """Verify the server/client pairing before the worker can claim work."""
        self.health.write("starting", "contract_probe")
        get_contract = getattr(self.coordinate, "get_runtime_contract", None)
        if not callable(get_contract):
            self.health.write("latched", "contract_mismatch")
            raise CoordinateContractError(
                "coordinate runtime client has no contract probe"
            )
        try:
            contract = await get_contract(recoverable=recoverable)
        except CoordinateContractError:
            self.health.write("latched", "contract_mismatch")
            raise
        except CoordinateRuntimeError:
            self.health.write("degraded", "contract_probe_unavailable")
            raise
        self.health.write("ready")
        return contract

    async def run(
        self,
        *,
        poll_interval: float = 2.0,
        recoverable: bool = False,
        recovery_reason: str = "",
        prior_process_stopped: bool = False,
        reap_mode: str = "global",
        reap_reason: Any = None,
    ) -> None:
        """Main worker loop: claim jobs, execute, report.

        recoverable=True (operator recovery mode only) claims timed_out+recoverable
        jobs to resume them. It requires both a non-empty ``recovery_reason`` and
        ``prior_process_stopped=True``; missing evidence fails closed immediately.
        Default False = normal polling, only pending jobs; stuck timed_out jobs
        are never reclaimed automatically.

        reap_mode="global" (default) preserves legacy claim argv. reap_mode="none"
        forwards the sealed reap policy on every poll. Policy is validated once
        before ``_running=True``.
        """
        normalized_mode, normalized_reap_reason = normalize_claim_reap_policy(
            reap_mode=reap_mode,
            reap_reason=reap_reason,
        )
        if recoverable:
            if prior_process_stopped is not True:
                raise CoordinateRuntimeError(
                    "recoverable worker run requires prior_process_stopped=True"
                )
            normalized_rec_reason = normalize_recovery_reason(recovery_reason)
        else:
            if recovery_reason != "":
                raise CoordinateRuntimeError(
                    "non-recoverable worker run must not carry recovery_reason"
                )
            if prior_process_stopped is not False:
                raise CoordinateRuntimeError(
                    "non-recoverable worker run must not carry prior_process_stopped"
                )
            normalized_rec_reason = ""
        self._running = True
        if self.health.state == "stopped":
            self.health.write("ready")
        if recoverable:
            log.warning(
                "Agentd worker started in RECOVERY mode: agent=%s reason=%s (will claim timed_out+recoverable jobs)",
                self.config.id,
                normalized_rec_reason,
            )
        else:
            log.info("Agentd worker started: agent=%s", self.config.id)
        # One stable operation key per logical claim.  It is retained across
        # an authority-uncertain outcome so a retry can replay the exact
        # server-side claim instead of creating a second attempt.
        claim_request_id = uuid.uuid4().hex
        try:
            claim_signature = inspect.signature(self.coordinate.claim_job)
            claim_supports_request_id = (
                "claim_request_id" in claim_signature.parameters
                or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in claim_signature.parameters.values()
                )
            )
        except (TypeError, ValueError):
            claim_supports_request_id = False
        while self._running:
            try:
                claim_kwargs = {
                    "agent_id": self.config.id,
                    "recoverable": recoverable,
                    "recovery_reason": normalized_rec_reason,
                    "prior_process_stopped": prior_process_stopped,
                }
                if claim_supports_request_id:
                    claim_kwargs["claim_request_id"] = claim_request_id
                if normalized_mode == "none":
                    claim_kwargs.update(
                        reap_mode=normalized_mode,
                        reap_reason=normalized_reap_reason,
                    )
                claim_result = await self.coordinate.claim_job(**claim_kwargs)
            except CoordinateClaimAuthorityUncertainError as exc:
                # The claim response is uncertain. Do not run the adapter until
                # the same fenced operation key gets an authoritative outcome.
                log.critical(
                    "Claim authority uncertain for agent %s: %s. "
                    "Pausing claims while authority is reconciled; process stays alive.",
                    self.config.id,
                    exc,
                )
                if not claim_supports_request_id:
                    self.health.write("latched", "claim_fencing_unavailable")
                    log.critical(
                        "Claim request fencing unavailable for agent %s; staying latched",
                        self.config.id,
                    )
                    while self._running:
                        await self._wake.wait()
                        self._wake.clear()
                    break
                # A sent claim may already own a lease.  Reconcile through the
                # agent-scoped read path before allowing another claim; never
                # treat transport recovery alone as proof that the claim did
                # not commit.
                if await self._reconcile_claim_authority(poll_interval):
                    log.warning(
                        "Claim authority probe complete for agent %s; retrying same claim key",
                        self.config.id,
                    )
                    continue
                log.critical(
                    "Claim authority reconcile unavailable for agent %s; staying latched",
                    self.config.id,
                )
                self.health.write("latched", "claim_authority_uncertain")
                while self._running:
                    await self._wake.wait()
                    self._wake.clear()
                break
            except CoordinateClaimReplayConflictError as exc:
                self.health.write("latched", "claim_replay_conflict")
                log.critical(
                    "Claim replay conflict for agent %s: %s; staying fail-closed",
                    self.config.id,
                    exc,
                )
                while self._running:
                    await self._wake.wait()
                    self._wake.clear()
                break
            except CoordinateClaimTerminalError as exc:
                self.health.write("latched", "claim_terminal_rejection")
                log.critical(
                    "Claim request rejected for agent %s: %s; staying fail-closed",
                    self.config.id,
                    exc,
                )
                while self._running:
                    await self._wake.wait()
                    self._wake.clear()
                break
            except CoordinateRuntimeError as exc:
                log.error(
                    "Coordinate claim error for agent %s: %s", self.config.id, exc
                )
                # Bounded backoff: wait the normal poll interval before retrying.
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=poll_interval)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                continue

            if not isinstance(claim_result, dict) or not claim_result.get("claimed"):
                self._log_claim_blocker(claim_result)
                claim_request_id = uuid.uuid4().hex
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=poll_interval)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                continue

            claim_request_id = uuid.uuid4().hex
            self.health.write("processing")
            await self._process_job(claim_result)
            if self._running:
                self.health.write("ready")
        log.info("Agentd worker stopped: agent=%s", self.config.id)

    async def _reconcile_claim_authority(self, poll_interval: float) -> bool:
        """Wait for a successful, agent-scoped no-active-lease proof.

        The probe is diagnostic once claim-side fencing is available. A
        successful, structurally valid snapshot leaves the operation key
        unchanged so the caller can replay that same key; transport or
        malformed responses keep the worker latched. The bounded backoff
        avoids a hot loop while preserving an immediate stop() wake-up.
        Older clients without fencing stay latched safely.
        """
        reconcile = getattr(self.coordinate, "reconcile_agent", None)
        if not callable(reconcile):
            log.critical(
                "No claim-authority reconcile capability for agent %s; staying latched",
                self.config.id,
            )
            while self._running:
                await self._wake.wait()
                self._wake.clear()
            return False

        delay = max(float(poll_interval), 0.1)
        try:
            snapshot = await reconcile(agent_id=self.config.id)
        except CoordinateRuntimeError as exc:
            log.error(
                "Claim-authority reconcile failed for agent %s: %s",
                self.config.id,
                exc,
            )
            return False
        except Exception as exc:
            log.error(
                "Claim-authority reconcile unexpected failure for agent %s: %s",
                self.config.id,
                type(exc).__name__,
            )
            return False
        else:
            active_leases = snapshot.get("active_leases") if isinstance(snapshot, dict) else None
            if isinstance(active_leases, list):
                log.info(
                    "Claim-authority reconcile observed %d active lease(s) for agent %s; retrying same key",
                    len(active_leases),
                    self.config.id,
                )
            else:
                log.error(
                    "Claim-authority reconcile returned malformed snapshot for agent %s",
                    self.config.id,
                )
                return False

        try:
            await asyncio.wait_for(self._wake.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()
        return self._running

    @staticmethod
    def _sanitize_blocked_field(value: str) -> str:
        """Replace control characters and truncate for safe logging."""
        cleaned = "".join("?" if ord(c) < 32 or ord(c) == 127 else c for c in value)
        return cleaned[:64]

    @staticmethod
    def _log_claim_blocker(claim_result: dict[str, Any] | None) -> None:
        """Log bounded, safe diagnostics when no job was claimed.

        Only allowlisted reasons are logged; oldest identifiers are sanitized.
        Prompt/payload are never emitted.
        """
        allowlist = {
            "queue_empty",
            "capacity_exhausted",
            "resource_blocked",
            "scan_limit_reached",
        }
        reason = ""
        if isinstance(claim_result, dict):
            raw_reason = claim_result.get("reason")
            if isinstance(raw_reason, str):
                reason = raw_reason
        if reason not in allowlist:
            return
        oldest_job_id = ""
        oldest_resource_key = ""
        if isinstance(claim_result, dict):
            raw_job_id = claim_result.get("oldest_blocked_job_id")
            if isinstance(raw_job_id, str):
                oldest_job_id = AgentdWorker._sanitize_blocked_field(raw_job_id)
            raw_resource = claim_result.get("oldest_blocked_resource_key")
            if isinstance(raw_resource, str):
                oldest_resource_key = AgentdWorker._sanitize_blocked_field(raw_resource)
        extras = []
        if oldest_job_id:
            extras.append(f"oldest_job_id={oldest_job_id}")
        if oldest_resource_key:
            extras.append(f"oldest_resource_key={oldest_resource_key}")
        extra = " ".join(extras)
        if reason == "queue_empty":
            log.debug("No job available for agent (queue_empty) %s", extra)
        else:
            log.warning("Job claim blocked: reason=%s %s", reason, extra)

    async def _process_job(self, claim_result: dict[str, Any]) -> None:
        raw_job = claim_result.get("job") or {}
        raw_attempt_token = claim_result.get("attempt_token")
        raw_attempt_int = (
            raw_attempt_token if isinstance(raw_attempt_token, int) else None
        )

        # Lease identity is the only Coordinate authority trusted here.
        # If the field is present, it must parse and identify successfully.
        lease_field_present = "execution_lease" in claim_result
        lease: ExecutionLeaseV1 | None = None
        raw_lease: dict[str, Any] | None = None
        if lease_field_present:
            raw_lease_value = claim_result["execution_lease"]
            if not isinstance(raw_lease_value, dict):
                log.error(
                    "Malformed execution lease for agent %s: not an object",
                    self.config.id,
                )
                # No provider, no report; lease/expiry cleanup will recover.
                return
            raw_lease = raw_lease_value
            try:
                lease = validate_execution_lease(
                    raw_lease,
                    expected_agent_id=self.config.id,
                )
            except ExecutionLeaseError as exc:
                log.error(
                    "Invalid execution lease for agent %s: %s", self.config.id, exc
                )
                # Never use unverified job_id/attempt_token to mutate Coordinate.
                return

        if lease is not None:
            trusted_job_id = lease.job_id
            trusted_attempt_token = lease.attempt_token
            trusted_lease_id = lease.lease_id
        else:
            # Legacy untyped path: fall back to raw claim fields, but only after
            # proving below that this is a genuinely legacy payload.
            trusted_job_id = raw_job.get("id", "?")
            trusted_attempt_token = raw_attempt_int
            trusted_lease_id = None

        # Extract payload. With a valid lease we can report parse failures using
        # trusted lease authority; without one we cannot prove anything.
        try:
            payload = self._extract_payload(raw_job)
        except ValueError as exc:
            log.error("Invalid payload for job %s", trusted_job_id)
            if lease is not None:
                await self.coordinate.report_job(
                    job_id=trusted_job_id,
                    agent_id=self.config.id,
                    status="failed",
                    result_json={"error": str(exc)},
                    attempt_token=trusted_attempt_token,
                    lease_id=trusted_lease_id,
                )
            return

        binding_field_present = "executor_binding" in payload
        binding_snapshot = payload.get("executor_binding")
        context_envelope = None
        origin = payload.get("origin")
        if isinstance(origin, dict):
            context_envelope = parse_context_envelope(origin.get("context"))

        if lease is not None:
            # Managed claim: a valid lease requires a valid typed binding.
            if not binding_field_present:
                log.error(
                    "Managed claim missing executor_binding for agent %s job %s",
                    self.config.id,
                    trusted_job_id,
                )
                await self.coordinate.report_job(
                    job_id=trusted_job_id,
                    agent_id=self.config.id,
                    status="failed",
                    result_json={"error": "managed claim missing executor_binding"},
                    attempt_token=trusted_attempt_token,
                    lease_id=trusted_lease_id,
                )
                return

            try:
                binding = validate_executor_binding(
                    binding_snapshot,
                    agent_id=self.config.id,
                    adapter=self.config.adapter,
                )
            except ExecutorBindingError as exc:
                log.error(
                    "Executor binding mismatch for agent %s: %s", self.config.id, exc
                )
                await self.coordinate.report_job(
                    job_id=trusted_job_id,
                    agent_id=self.config.id,
                    status="failed",
                    result_json={"error": str(exc)},
                    attempt_token=trusted_attempt_token,
                    lease_id=trusted_lease_id,
                )
                return

            if binding is None:
                log.error(
                    "Managed claim has null executor_binding for agent %s job %s",
                    self.config.id,
                    trusted_job_id,
                )
                await self.coordinate.report_job(
                    job_id=trusted_job_id,
                    agent_id=self.config.id,
                    status="failed",
                    result_json={"error": "executor_binding is null"},
                    attempt_token=trusted_attempt_token,
                    lease_id=trusted_lease_id,
                )
                return
        else:
            # No lease: any presence of executor_binding means this is a managed
            # claim that lacks authority. Fail closed silently.
            if binding_field_present:
                log.error(
                    "Managed claim missing execution lease for agent %s job %s; "
                    "dropping without provider or report",
                    self.config.id,
                    trusted_job_id,
                )
                return
            binding = None

        try:
            job, ctx, attempt_token = validate_claim_response(
                {"result": claim_result},
                agent_id=self.config.id,
            )
        except ExecutionContextError as exc:
            log.error("Invalid execution context for agent %s: %s", self.config.id, exc)
            # Use trusted lease authority when available; otherwise raw attempt token.
            if isinstance(trusted_attempt_token, int):
                await self.coordinate.report_job(
                    job_id=trusted_job_id,
                    agent_id=self.config.id,
                    status="failed",
                    result_json={"error": f"execution context rejected: {exc}"},
                    attempt_token=trusted_attempt_token,
                    lease_id=trusted_lease_id,
                )
            return

        # Context is now trusted; cross-check the lease against it.
        if lease is not None:
            try:
                lease = validate_execution_lease(
                    raw_lease,
                    expected_agent_id=self.config.id,
                    expected_job_id=ctx.job_id,
                    expected_attempt_token=attempt_token,
                    execution_context=ctx.to_dict(),
                )
                trusted_lease_id = lease.lease_id
            except ExecutionLeaseError as exc:
                log.error(
                    "Lease cross-check failed for agent %s: %s", self.config.id, exc
                )
                await self.coordinate.report_job(
                    job_id=trusted_job_id,
                    agent_id=self.config.id,
                    status="failed",
                    result_json={"error": f"execution lease cross-check failed: {exc}"},
                    attempt_token=trusted_attempt_token,
                    lease_id=trusted_lease_id,
                )
                return

        # Final cross-check of lease/context/binding before provider invocation.
        if lease is not None:
            try:
                lease = validate_execution_lease(
                    raw_lease,
                    expected_agent_id=self.config.id,
                    expected_job_id=ctx.job_id,
                    expected_attempt_token=attempt_token,
                    execution_context=ctx.to_dict(),
                    executor_binding=binding_snapshot,
                )
                trusted_lease_id = lease.lease_id
            except ExecutionLeaseError as exc:
                log.error(
                    "Lease binding cross-check failed for agent %s: %s",
                    self.config.id,
                    exc,
                )
                await self.coordinate.report_job(
                    job_id=trusted_job_id,
                    agent_id=self.config.id,
                    status="failed",
                    result_json={
                        "error": f"execution lease binding cross-check failed: {exc}"
                    },
                    attempt_token=trusted_attempt_token,
                    lease_id=trusted_lease_id,
                )
                return

        prompt = payload.get("prompt", "")
        log.info(
            "Processing job %s: agent=%s prompt_len=%d cwd=%s lease=%s",
            trusted_job_id,
            self.config.id,
            len(prompt),
            ctx.cwd,
            trusted_lease_id,
        )

        session_scope_id = ctx.session_scope_id
        legacy_scope_ids = ctx.legacy_scope_ids
        work_dir = ctx.cwd
        if context_envelope is not None and (
            context_envelope.session_scope_id != session_scope_id
            or not context_envelope.scope_id.startswith(f"workspace:{ctx.workspace_id}:")
            or context_envelope.current_message_id != str(origin.get("message_id", ""))
        ):
            context_envelope = None
        if context_envelope is not None and render_envelope(
            context_envelope.to_dict(), self.config
        ) != prompt:
            context_envelope = None
        try:
            cursor_before = self.session_store.get(
                scope_id=session_scope_id, agent_id=self.config.id, include_inactive=True
            )
            session_attempt = _SessionAttempt(cursor_before)
        except sqlite3.Error:
            # A cursor store outage must never make execution unsafe or
            # incomplete: discard the optimization envelope and retain the
            # already-rendered bounded full prompt.
            log.warning(
                "SessionStore unavailable for agent=%s scope=%s; using full context",
                self.config.id,
                session_scope_id,
            )
            session_attempt = _SessionAttempt(None, writable=False)
            context_envelope = None
        recovery_session_id = self._recovery_session_id(job)
        progress_callback, progress_tasks, progress_state = (
            self._make_coordinate_progress_callback(
                job_id=trusted_job_id,
                session_scope_id=session_scope_id,
                work_dir=work_dir,
                attempt_token=trusted_attempt_token,
                lease_id=trusted_lease_id,
            )
        )

        # Start the dedicated renewal supervisor before provider invocation.
        lease_lost = asyncio.Event()
        renewal_task: asyncio.Task | None = None
        if (
            trusted_lease_id is not None
            and isinstance(trusted_attempt_token, int)
            and raw_lease is not None
        ):
            renewal_task = asyncio.create_task(
                self._renewal_supervisor(
                    lease=raw_lease,
                    lease_lost=lease_lost,
                )
            )

        start = time.time()
        provider_task: asyncio.Task | None = None
        try:
            provider_task = asyncio.create_task(
                self._call_or_resume(
                    prompt,
                    session_scope_id,
                    legacy_scope_ids,
                    work_dir,
                    on_progress=progress_callback,
                    recovery_session_id=recovery_session_id,
                    context_envelope=context_envelope,
                    session_attempt=session_attempt,
                )
            )
            coros = [provider_task]
            if renewal_task is not None:
                coros.append(renewal_task)
            done, pending = await asyncio.wait(
                coros,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if renewal_task in done:
                # Authoritative lease loss: cancel provider and do not report normally.
                if provider_task and not provider_task.done():
                    provider_task.cancel()
                    try:
                        await provider_task
                    except asyncio.CancelledError:
                        pass
                log.error("Lease lost for job %s; provider cancelled", trusted_job_id)
                if progress_tasks:
                    await asyncio.gather(*progress_tasks, return_exceptions=True)
                return
            # Provider finished first.
            result = provider_task.result()
            if renewal_task and not renewal_task.done():
                renewal_task.cancel()
                try:
                    await renewal_task
                except asyncio.CancelledError:
                    pass
        except asyncio.CancelledError:
            if provider_task and not provider_task.done():
                provider_task.cancel()
                try:
                    await provider_task
                except asyncio.CancelledError:
                    pass
            if renewal_task and not renewal_task.done():
                renewal_task.cancel()
                try:
                    await renewal_task
                except asyncio.CancelledError:
                    pass
            raise
        except Exception as exc:
            if provider_task and not provider_task.done():
                provider_task.cancel()
                try:
                    await provider_task
                except asyncio.CancelledError:
                    pass
            if renewal_task and not renewal_task.done():
                renewal_task.cancel()
                try:
                    await renewal_task
                except asyncio.CancelledError:
                    pass
            # Orchestration boundary: an unexpected exception settles the
            # durable job as an explicit internal_error with a bounded
            # diagnostic. The safe text never leaks the raw exception.
            log.exception("Provider invocation failed for job %s", trusted_job_id)
            result = AdapterResult(
                text="Agent error: internal adapter failure",
                outcome=OUTCOME_FAILED,
                error_category="internal_error",
                diagnostic=safe_exception_diagnostic(exc),
            )

        if progress_tasks:
            await asyncio.gather(*progress_tasks, return_exceptions=True)

        duration_ms = int((time.time() - start) * 1000)

        outcome = result.effective_outcome()
        status = self._status_for_result(result)
        # Delivery decision reads the machine outcome; the legacy sentinel is
        # honored only for results that still carry no structured category.
        no_response = (
            outcome == OUTCOME_FAILED
            and result.error_category == "no_response"
        ) or (
            result.error_category is None
            and result.text.strip() == NO_RESPONSE_SENTINEL
        )
        result_json = {
            # Coordinate creates a response delivery for any non-empty
            # response_text, including failed jobs.  Preserve the diagnostic
            # sentinel separately, but do not publish it as a user reply.
            "response_text": "" if no_response else result.text,
            "session_id": result.session_id or "",
            "duration_ms": duration_ms,
            "execution_context_id": ctx.context_id,
            "worktree_path": ctx.worktree_path,
            "session_scope_id": ctx.session_scope_id,
            "outcome": outcome,
            "attempt_token": trusted_attempt_token,
            "lease_id": trusted_lease_id,
        }
        if no_response:
            result_json["error"] = NO_RESPONSE_SENTINEL
        if result.error_category is not None:
            result_json["error_category"] = result.error_category
        if result.diagnostic:
            result_json["diagnostic"] = result.diagnostic
        if binding is not None:
            result_json.update(binding.result_evidence())
        if progress_state:
            result_json["progress"] = dict(progress_state)
        if "timeout" in result.metadata:
            result_json["timeout"] = {
                **result.metadata["timeout"],
                "progress": dict(progress_state),
            }
        if result.metadata.get("provider_evidence"):
            result_json["provider_evidence"] = result.metadata["provider_evidence"]
        if result.metadata.get("usage_evidence"):
            result_json["usage_evidence"] = result.metadata["usage_evidence"]

        receipt = await self.coordinate.report_job(
            job_id=trusted_job_id,
            agent_id=self.config.id,
            status=status,
            result_json=result_json,
            attempt_token=trusted_attempt_token,
            lease_id=trusted_lease_id,
        )
        if outcome == OUTCOME_SUCCESS and status == "done" and result.session_id:
            if self._reported_success_matches(
                receipt, job_id=trusted_job_id, attempt_token=trusted_attempt_token,
                result_json=result_json,
            ):
                self._persist_session_result(
                    session_attempt, session_scope_id=session_scope_id,
                    work_dir=work_dir, session_id=result.session_id,
                    context_envelope=context_envelope,
                )
            else:
                log.warning("Unconfirmed terminal receipt for job %s; session unchanged", trusted_job_id)
        log.info(
            "Job %s complete: status=%s duration=%dms",
            trusted_job_id,
            status,
            duration_ms,
        )

    def _reported_success_matches(
        self, receipt: Any, *, job_id: str, attempt_token: int | None, result_json: dict
    ) -> bool:
        """Accept applied results and exact terminal replays, never arbitrary 200s."""
        data = receipt.get("result") if isinstance(receipt, dict) else None
        job = data.get("job") if isinstance(data, dict) else None
        if not isinstance(job, dict) or not (
            job.get("id") == job_id
            and job.get("assigned_agent") == self.config.id
            and job.get("status") == "done"
            and type(job.get("attempt_count")) is int
            and type(attempt_token) is int
            and job["attempt_count"] == attempt_token
        ):
            return False
        stored = job.get("result")
        if not isinstance(stored, dict):
            return False
        # Coordinate normalizes usage into a bounded summary. The remaining
        # submitted result, including exact attempt/lease evidence, is stable.
        return all(
            key in stored and stored[key] == value
            for key, value in result_json.items() if key != "usage_evidence"
        )

    def _persist_session_result(
        self, attempt: _SessionAttempt, *, session_scope_id: str, work_dir: str,
        session_id: str, context_envelope,
    ) -> None:
        if not attempt.writable or not session_scope_id:
            return
        try:
            persisted = self.session_store.upsert(
                scope_id=session_scope_id, agent_id=self.config.id,
                adapter=self.config.adapter, session_id=session_id, work_dir=work_dir,
                context_generation=context_envelope.generation if context_envelope else None,
                expected_session=attempt.snapshot,
            )
            if persisted is None:
                log.warning("Session CAS lost for agent=%s scope=%s", self.config.id, session_scope_id)
                return
            if context_envelope is not None:
                advanced = self.session_store.advance_context_cursor(
                    scope_id=session_scope_id, agent_id=self.config.id, session_id=session_id,
                    context_generation=context_envelope.generation,
                    expected_cursor_order_token=persisted["context_cursor_order_token"],
                    expected_cursor_message_id=persisted["context_cursor_message_id"],
                    cursor_order_token=context_envelope.current_order_token,
                    cursor_message_id=context_envelope.current_message_id,
                    expected_session=persisted,
                )
                if advanced:
                    self._context_sessions[session_scope_id] = (session_id, context_envelope.generation)
        except (sqlite3.Error, ValueError):
            self._context_sessions.pop(session_scope_id, None)
            log.warning("Session checkpoint unavailable for agent=%s scope=%s", self.config.id, session_scope_id)

    def _retire_session(self, existing: dict, attempt: _SessionAttempt, scope: str) -> bool:
        """Only this attempt's own retirement authorizes its fresh fallback."""
        self._context_sessions.pop(scope, None)
        try:
            retired = self.session_store.mark_stale(
                scope_id=existing["scope_id"], agent_id=self.config.id,
                expected_session=existing,
            )
        except sqlite3.Error:
            retired = None
        if retired is None:
            attempt.writable = False
            return False
        if existing["scope_id"] == scope:
            attempt.snapshot = retired
        return True

    async def _renewal_supervisor(
        self,
        *,
        lease: dict[str, Any],
        lease_lost: asyncio.Event,
    ) -> None:
        """Dedicated heartbeat: renew the lease before its monotonic deadline.

        Converts Coordinate's server_now + expires_at into a local monotonic
        deadline with a fixed safety margin. Ordinary renewals update the lease
        row without event spam; authoritative rejection or transport failure
        marks the lease lost and exits so the worker can cancel the provider.
        """
        from datetime import datetime, timezone

        lease_id = lease["lease_id"]
        job_id = lease["job_id"]
        attempt_token = lease["attempt_token"]
        agent_id = lease["agent_id"]
        renew_interval_seconds = lease["renew_interval_seconds"]

        def _to_monotonic_deadline(server_now: str, expires_at: str) -> float:
            server_dt = datetime.strptime(server_now, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            expires_dt = datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            remaining = (expires_dt - server_dt).total_seconds()
            return time.monotonic() + remaining - RENEWAL_SAFETY_MARGIN_SECONDS

        deadline = _to_monotonic_deadline(lease["server_now"], lease["expires_at"])

        while True:
            now = time.monotonic()
            if now >= deadline:
                log.warning(
                    "Lease %s deadline reached without successful renewal", lease_id
                )
                lease_lost.set()
                return

            sleep_until = min(deadline, now + max(1, renew_interval_seconds))
            try:
                await asyncio.wait_for(lease_lost.wait(), timeout=sleep_until - now)
                return
            except asyncio.TimeoutError:
                pass

            try:
                response = await self.coordinate.renew_lease(
                    job_id=job_id,
                    agent_id=agent_id,
                    attempt_token=attempt_token,
                    lease_id=lease_id,
                )
            except CoordinateRuntimeError as exc:
                log.error("Lease renewal transport error for %s: %s", lease_id, exc)
                # Fail closed: treat transport failure as lease lost.
                lease_lost.set()
                return

            if not isinstance(response, dict):
                log.error("Lease renewal returned non-dict for %s", lease_id)
                lease_lost.set()
                return

            result = response.get("result")
            if not isinstance(result, dict):
                log.error("Lease renewal missing result for %s", lease_id)
                lease_lost.set()
                return

            new_expires_at = result.get("expires_at")
            new_server_now = result.get("server_now")
            if not isinstance(new_expires_at, str) or not isinstance(
                new_server_now, str
            ):
                log.error("Lease renewal missing timestamps for %s", lease_id)
                lease_lost.set()
                return

            try:
                deadline = _to_monotonic_deadline(new_server_now, new_expires_at)
            except ValueError as exc:
                log.error(
                    "Lease renewal returned invalid timestamps for %s: %s",
                    lease_id,
                    exc,
                )
                lease_lost.set()
                return

            log.debug("Lease %s renewed; new deadline %s", lease_id, deadline)

    def _make_coordinate_progress_callback(
        self,
        *,
        job_id: str,
        session_scope_id: str,
        work_dir: str,
        attempt_token: int | None = None,
        lease_id: str | None = None,
    ):
        tasks: list[asyncio.Task] = []
        state: dict[str, str] = {}
        sent: dict[str, float | str] = {"at": 0.0, "session_id": ""}

        def _record(update: str | dict[str, Any]) -> None:
            progress = self._normalize_progress(update)
            if not progress:
                return
            state.update({k: v for k, v in progress.items() if v})
            session_id = progress.get("session_id", "")
            # Coordinate owns durable in-flight session recovery. SessionStore
            # is updated only after the matching terminal receipt is accepted.
            now = time.monotonic()
            should_send = bool(session_id and session_id != sent.get("session_id"))
            should_send = should_send or (now - float(sent.get("at", 0.0)) >= 10.0)
            if not should_send:
                return
            sent["at"] = now
            if session_id:
                sent["session_id"] = session_id
            tasks.append(
                asyncio.create_task(
                    self.coordinate.record_progress(
                        job_id=job_id,
                        agent_id=self.config.id,
                        stage=progress.get("stage", ""),
                        summary=progress.get("summary", ""),
                        session_id=session_id,
                        attempt_token=attempt_token,
                        lease_id=lease_id,
                    )
                )
            )

        return _record, tasks, state

    @staticmethod
    def _normalize_progress(update: str | dict[str, Any]) -> dict[str, str]:
        if isinstance(update, dict):
            progress: dict[str, str] = {}
            for key in ("stage", "summary", "session_id"):
                value = update.get(key)
                if isinstance(value, str) and value.strip():
                    limit = 200 if key == "session_id" else 1000
                    progress[key] = value.strip()[:limit]
            return progress
        if isinstance(update, str) and update.strip():
            return {"summary": update.strip()[:1000]}
        return {}

    @staticmethod
    def _recovery_session_id(job: dict) -> str:
        for candidate in (
            job.get("terminal_session_id"),
            (job.get("progress") or {}).get("session_id")
            if isinstance(job.get("progress"), dict)
            else "",
            ((job.get("result") or {}).get("timeout") or {}).get("session_id")
            if isinstance(job.get("result"), dict)
            and isinstance((job.get("result") or {}).get("timeout"), dict)
            else "",
            (job.get("result") or {}).get("session_id")
            if isinstance(job.get("result"), dict)
            else "",
        ):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return ""

    @staticmethod
    def _extract_payload(job: dict) -> dict:
        """Accept both current coordinate CLI payload shape and legacy raw JSON."""
        payload = job.get("payload")
        if isinstance(payload, dict):
            return payload
        if payload is not None:
            raise ValueError("payload must be an object")

        payload_json = job.get("payload_json")
        if payload_json in (None, ""):
            return {}
        if isinstance(payload_json, dict):
            return payload_json
        if not isinstance(payload_json, str):
            raise ValueError("payload_json must be a JSON string")
        try:
            decoded = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid payload_json") from exc
        if not isinstance(decoded, dict):
            raise ValueError("payload_json must decode to an object")
        return decoded

    async def _call_or_resume(
        self,
        prompt: str,
        session_scope_id: str,
        legacy_scope_ids: tuple[str, ...],
        work_dir: str | None,
        *,
        on_progress=None,
        recovery_session_id: str = "",
        context_envelope=None,
        session_attempt: _SessionAttempt | None = None,
    ) -> AdapterResult:
        """Check session store for existing session; resume if found, else fresh call."""
        if not session_scope_id:
            if recovery_session_id:
                return await self._resume_recoverable_session(
                    recovery_session_id,
                    prompt,
                    work_dir=work_dir,
                    on_progress=on_progress,
                )
            return await self.adapter.call(
                prompt,
                timeout=self.config.timeout,
                work_dir=work_dir,
                on_progress=on_progress,
            )

        try:
            if session_attempt is None:
                session_attempt = _SessionAttempt(self.session_store.get(
                    scope_id=session_scope_id, agent_id=self.config.id, include_inactive=True
                ))
            existing = session_attempt.snapshot
            if existing is not None and existing["status"] != "active":
                existing = None
            if existing is None and session_attempt.writable:
                existing = self.session_store.get_first_active(
                    scope_ids=legacy_scope_ids, agent_id=self.config.id,
                )
        except sqlite3.Error:
            if session_attempt is None:
                session_attempt = _SessionAttempt(None, writable=False)
            else:
                session_attempt.writable = False
            existing = None
            log.warning("SessionStore unavailable for agent=%s; using full prompt", self.config.id)

        if existing:
            current_work_dir = work_dir
            if (
                current_work_dir
                and existing.get("work_dir")
                and current_work_dir != existing["work_dir"]
            ):
                log.info(
                    "Session work_dir mismatch (had=%s now=%s), marking stale",
                    existing["work_dir"],
                    current_work_dir,
                )
                self._retire_session(existing, session_attempt, session_scope_id)
                existing = None

        # recovery_session_id present means always fail-closed recoverable
        # resume, EVEN IF an existing session is also present. The legacy existing-
        # session resume below falls back to a fresh duplicate on error, which must
        # never happen for a recoverable job (invariant 4: no fresh duplicate).
        if recovery_session_id:
            return await self._resume_recoverable_session(
                recovery_session_id,
                prompt,
                work_dir=work_dir,
                on_progress=on_progress,
            )

        if existing:
            effective_prompt = prompt
            if context_envelope is not None:
                if (
                    self._context_sessions.get(session_scope_id)
                    == (existing["session_id"], context_envelope.generation)
                    and existing["scope_id"] == session_scope_id
                    and existing.get("context_generation") == context_envelope.generation
                    and existing.get("context_cursor_order_token")
                    and existing.get("context_cursor_message_id")
                ):
                    effective_prompt = delta_prompt(
                        context_envelope.to_dict(),
                        existing["context_cursor_order_token"],
                        existing["context_cursor_message_id"],
                        self.config,
                    ) or prompt
            log.info(
                "Resuming session %s for scope=%s",
                existing["session_id"],
                existing["scope_id"],
            )
            result: AdapterResult = await self.adapter.resume(
                existing["session_id"],
                effective_prompt,
                timeout=self.config.timeout,
                work_dir=work_dir,
                on_progress=on_progress,
            )
            if result.effective_outcome() != OUTCOME_SUCCESS:
                allow_fresh_fallback = getattr(
                    self.adapter,
                    "allow_fresh_fallback_after_resume_error",
                    True,
                )
                is_internal_error = (
                    getattr(result, "error_category", None) == "internal_error"
                )
                log.warning(
                    "Resume failed for %s (fresh fallback allowed=%s)",
                    existing["session_id"],
                    allow_fresh_fallback,
                )
                retired = self._retire_session(existing, session_attempt, session_scope_id)
                if retired and allow_fresh_fallback and not is_internal_error:
                    result = await self.adapter.call(
                        prompt,
                        timeout=self.config.timeout,
                        work_dir=work_dir,
                        on_progress=on_progress,
                    )
                else:
                    # The provider session was just made stale.  Do not let the
                    # generic result persistence below reactivate the dead row.
                    result.session_id = None
            return result

        return await self.adapter.call(
            prompt,
            timeout=self.config.timeout,
            work_dir=work_dir,
            on_progress=on_progress,
        )

    async def _resume_recoverable_session(
        self,
        session_id: str,
        prompt: str,
        *,
        work_dir: str | None,
        on_progress=None,
    ) -> AdapterResult:
        log.info("Resuming recoverable session %s", session_id)
        result: AdapterResult = await self.adapter.resume(
            session_id,
            prompt,
            timeout=self.config.timeout,
            work_dir=work_dir,
            on_progress=on_progress,
        )
        if result.effective_outcome() != OUTCOME_SUCCESS:
            # Fail closed without a fresh duplicate; preserve the failure
            # category so the durable job record stays honest.
            return AdapterResult(
                text=(
                    "Agent error: recoverable session resume failed; "
                    "not starting duplicate fresh execution"
                ),
                session_id=session_id,
                resumed=True,
                outcome=OUTCOME_FAILED,
                error_category=(
                    result.error_category
                    if result.error_category is not None
                    else "provider_error"
                ),
                diagnostic=result.diagnostic,
            )
        return result

    def stop(self) -> None:
        self._running = False
        self._wake.set()
        self.health.write("stopped", self.health.reason_code or "shutdown")

    @classmethod
    def _is_error(cls, text: str) -> bool:
        # Legacy/external compatibility seam for plain-text inputs only.
        # AdapterResult consumers must read effective_outcome() instead.
        return is_error_text(text)

    @classmethod
    def _status_for_result(cls, result: AdapterResult) -> str:
        effective = getattr(result, "effective_outcome", None)
        if not callable(effective):
            # Legacy duck-typed result (external/third-party/test double):
            # normalize through the single core seam, never a second classifier.
            effective = AdapterResult(
                text=result.text,
                metadata=getattr(result, "metadata", None) or {},
            ).effective_outcome()
        else:
            effective = effective()
        if effective == OUTCOME_TIMED_OUT:
            return "timed_out"
        if effective == OUTCOME_FAILED:
            return "failed"
        return "done"
