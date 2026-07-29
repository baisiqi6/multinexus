"""Strict parser for Coordinate's v1 ExecutionContext.

This module is MultiNexus-side authority consumption only. It does NOT import
Coordinate code. Any parsed path/branch fields from a handoff message are
advisory metadata for locating bootstrap files; adapter cwd, session scope, and
filesystem execution authority come solely from the Coordinate ``runtime job claim``
response validated here.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

CONTRACT_VERSION = 1
MAX_SCOPE_LEN = 256
MAX_LEGACY_SCOPES = 10

# Scope ids are opaque; we only bound length and characters.
_SAFE_SCOPE_RE = re.compile(r"^[A-Za-z0-9_.:/-]+$")

# Context id is a stable SHA-256 digest with a lowercase 64-hex body.
_CONTEXT_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# Host-absolute path forms: POSIX ``/...`` or Windows ``C:\...`` / UNC ``\\...``.
_WINDOWS_ABS_RE = re.compile(r"^([A-Za-z]:[\\/]|\\{2})")

_V1_SNAPSHOT_KEYS = frozenset({
    "assigned_agent",
    "branch",
    "contract_version",
    "context_id",
    "harness_root",
    "host_id",
    "job_id",
    "legacy_scope_ids",
    "log_handle",
    "session_scope_id",
    "task_id",
    "worktree_path",
    "workspace_id",
    "workspace_path",
})

_LOG_HANDLE_KEYS = frozenset({"kind", "job_id", "logs_path"})


class ExecutionContextError(ValueError):
    """Raised when a claim context is missing, malformed, or fails validation."""


@dataclass(frozen=True)
class ExecutionContextV1:
    context_id: str
    job_id: str
    workspace_id: str
    task_id: str | None
    assigned_agent: str
    host_id: str
    workspace_path: str
    worktree_path: str
    harness_root: str
    branch: str | None
    session_scope_id: str
    legacy_scope_ids: tuple[str, ...]
    log_handle: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.log_handle, MappingProxyType):
            object.__setattr__(self, "log_handle", MappingProxyType(dict(self.log_handle)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": CONTRACT_VERSION,
            "context_id": self.context_id,
            "job_id": self.job_id,
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "assigned_agent": self.assigned_agent,
            "host_id": self.host_id,
            "workspace_path": self.workspace_path,
            "worktree_path": self.worktree_path,
            "harness_root": self.harness_root,
            "branch": self.branch,
            "session_scope_id": self.session_scope_id,
            "legacy_scope_ids": list(self.legacy_scope_ids),
            "log_handle": dict(self.log_handle),
        }

    @property
    def cwd(self) -> str:
        """Adapter cwd is the host-native worktree path."""
        return self.worktree_path


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _compute_context_id(canonical: dict[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(canonical).encode('utf-8')).hexdigest()}"


def _isabs(path: str) -> bool:
    return path.startswith("/") or _WINDOWS_ABS_RE.match(path) is not None


def _has_traversal(segments: list[str]) -> bool:
    return any(seg in {"..", "."} for seg in segments)


def _validate_path(value: Any, label: str) -> str:
    """Validate a host-absolute authority path.

    Rejects non-strings, empty strings, NUL/newline, relative paths, and
    traversal segments. The path is authority data, not a local filesystem path,
    so this is purely lexical.
    """
    if not isinstance(value, str) or not value:
        raise ExecutionContextError(f"{label} is required")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ExecutionContextError(f"{label} contains NUL or newline")
    if not _isabs(value):
        raise ExecutionContextError(f"{label} must be absolute: {value!r}")
    segments = value.replace("\\", "/").split("/")
    if _has_traversal(segments):
        raise ExecutionContextError(f"{label} contains traversal: {value!r}")
    return value


def _is_nonempty_string(value: Any, nullable: bool = False) -> str | None:
    if value is None:
        if nullable:
            return None
        raise ExecutionContextError("required string field is missing")
    if not isinstance(value, str) or not value.strip():
        raise ExecutionContextError("required string field is empty or not a string")
    return value


def _validate_required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ExecutionContextError(f"{label} is required")
    if not value.strip():
        raise ExecutionContextError(f"{label} must be a non-empty string")
    return value


def _validate_scope(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ExecutionContextError(f"{label} is required")
    if len(value) > MAX_SCOPE_LEN or not _SAFE_SCOPE_RE.match(value):
        raise ExecutionContextError(f"{label} contains unsafe characters: {value!r}")
    return value


def _canonical_snapshot_dict(ctx: ExecutionContextV1) -> dict[str, Any]:
    return {
        "assigned_agent": ctx.assigned_agent,
        "branch": ctx.branch,
        "contract_version": CONTRACT_VERSION,
        "harness_root": ctx.harness_root,
        "host_id": ctx.host_id,
        "job_id": ctx.job_id,
        "legacy_scope_ids": list(ctx.legacy_scope_ids),
        "log_handle": dict(ctx.log_handle),
        "session_scope_id": ctx.session_scope_id,
        "task_id": ctx.task_id,
        "worktree_path": ctx.worktree_path,
        "workspace_id": ctx.workspace_id,
        "workspace_path": ctx.workspace_path,
    }


def _validate_log_handle(value: Any, *, expected_job_id: str) -> MappingProxyType[str, Any]:
    if not isinstance(value, dict):
        raise ExecutionContextError("log_handle must be an object")
    if set(value.keys()) != _LOG_HANDLE_KEYS:
        raise ExecutionContextError(
            f"log_handle has incorrect keys: expected {_LOG_HANDLE_KEYS}, got {set(value.keys())}"
        )
    if value.get("kind") != "coordinate_job":
        raise ExecutionContextError(f"log_handle.kind must be 'coordinate_job': {value.get('kind')!r}")
    job_id = value.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise ExecutionContextError("log_handle.job_id is required")
    if job_id != expected_job_id:
        raise ExecutionContextError(
            f"log_handle.job_id mismatch: {job_id!r} != {expected_job_id!r}"
        )
    logs_path = value.get("logs_path")
    if logs_path is not None and (not isinstance(logs_path, str) or not logs_path):
        raise ExecutionContextError("log_handle.logs_path must be a non-empty string or null")
    if logs_path is not None:
        _validate_path(logs_path, "log_handle.logs_path")
    return MappingProxyType({
        "kind": "coordinate_job",
        "job_id": job_id,
        "logs_path": logs_path,
    })


def parse_execution_context(
    data: dict[str, Any],
    *,
    expected_job_id: str | None = None,
    expected_workspace_id: str | None = None,
    expected_assigned_agent: str | None = None,
) -> ExecutionContextV1:
    """Strictly parse and validate a v1 execution_context dict from Coordinate.

    Raises ExecutionContextError on any missing field, type mismatch, unsupported
    version, digest mismatch, or identity conflict.
    """
    if not isinstance(data, dict):
        raise ExecutionContextError("execution_context must be an object")
    if set(data.keys()) != _V1_SNAPSHOT_KEYS:
        raise ExecutionContextError(
            f"execution_context has incorrect keys: expected {_V1_SNAPSHOT_KEYS}, got {set(data.keys())}"
        )
    if data.get("contract_version") != CONTRACT_VERSION:
        raise ExecutionContextError("execution_context contract_version must be 1")

    supplied_id = data.get("context_id")
    if not isinstance(supplied_id, str) or not _CONTEXT_ID_RE.match(supplied_id):
        raise ExecutionContextError("execution_context.context_id must be sha256:<64-hex>")

    # Strict field validation: no silent coercion.
    job_id = _validate_required_string(data.get("job_id"), "execution_context.job_id")
    workspace_id = _validate_required_string(data.get("workspace_id"), "execution_context.workspace_id")
    assigned_agent = _validate_required_string(data.get("assigned_agent"), "execution_context.assigned_agent")
    host_id = _validate_required_string(data.get("host_id"), "execution_context.host_id")
    workspace_path = _validate_path(data.get("workspace_path"), "execution_context.workspace_path")
    worktree_path = _validate_path(data.get("worktree_path"), "execution_context.worktree_path")
    harness_root = _validate_path(data.get("harness_root"), "execution_context.harness_root")
    session_scope_id = _validate_scope(data.get("session_scope_id"), "execution_context.session_scope_id")
    branch = _is_nonempty_string(data.get("branch"), nullable=True)
    task_id = _is_nonempty_string(data.get("task_id"), nullable=True)

    raw_legacy = data.get("legacy_scope_ids")
    # The JSON v1 contract requires a unique list; reject wrong containers
    # and any duplicate (including duplicates of the primary scope).
    if not isinstance(raw_legacy, list):
        raise ExecutionContextError("legacy_scope_ids must be a list")
    if len(raw_legacy) > MAX_LEGACY_SCOPES:
        raise ExecutionContextError(f"legacy_scope_ids exceeds {MAX_LEGACY_SCOPES}")
    seen: set[str] = set()
    legacy_scope_ids: list[str] = []
    for item in raw_legacy:
        scoped = _validate_scope(item, "legacy_scope_ids item")
        if scoped in seen or scoped == session_scope_id:
            raise ExecutionContextError(f"legacy_scope_ids contains duplicate: {scoped!r}")
        seen.add(scoped)
        legacy_scope_ids.append(scoped)

    log_handle = _validate_log_handle(data.get("log_handle"), expected_job_id=job_id)

    ctx = ExecutionContextV1(
        context_id="",
        job_id=job_id,
        workspace_id=workspace_id,
        task_id=task_id,
        assigned_agent=assigned_agent,
        host_id=host_id,
        workspace_path=workspace_path,
        worktree_path=worktree_path,
        harness_root=harness_root,
        branch=branch,
        session_scope_id=session_scope_id,
        legacy_scope_ids=tuple(legacy_scope_ids),
        log_handle=log_handle,
    )
    expected_id = _compute_context_id(_canonical_snapshot_dict(ctx))
    if expected_id != supplied_id:
        raise ExecutionContextError(
            f"execution_context digest mismatch: expected {expected_id}, got {supplied_id}"
        )

    ctx = ExecutionContextV1(
        context_id=supplied_id,
        job_id=ctx.job_id,
        workspace_id=ctx.workspace_id,
        task_id=ctx.task_id,
        assigned_agent=ctx.assigned_agent,
        host_id=ctx.host_id,
        workspace_path=ctx.workspace_path,
        worktree_path=ctx.worktree_path,
        harness_root=ctx.harness_root,
        branch=ctx.branch,
        session_scope_id=ctx.session_scope_id,
        legacy_scope_ids=ctx.legacy_scope_ids,
        log_handle=ctx.log_handle,
    )

    if expected_job_id is not None and ctx.job_id != expected_job_id:
        raise ExecutionContextError(
            f"execution_context job_id mismatch: {ctx.job_id} != {expected_job_id}"
        )
    if expected_workspace_id is not None and ctx.workspace_id != expected_workspace_id:
        raise ExecutionContextError(
            f"execution_context workspace_id mismatch: {ctx.workspace_id} != {expected_workspace_id}"
        )
    if expected_assigned_agent is not None and ctx.assigned_agent != expected_assigned_agent:
        raise ExecutionContextError(
            f"execution_context assigned_agent mismatch: {ctx.assigned_agent} != {expected_assigned_agent}"
        )

    return ctx


def validate_claim_response(
    response: dict[str, Any] | None,
    *,
    agent_id: str,
    workspace_id: str | None = None,
) -> tuple[dict[str, Any], ExecutionContextV1, int]:
    """Validate a Coordinate ``runtime job claim`` response and return (job, context, token).

    Raises ExecutionContextError when the response is not a successful claim or
    its execution context is missing/invalid.
    """
    if response is None:
        raise ExecutionContextError("coordinate claim returned no response")
    result = response.get("result")
    if not isinstance(result, dict):
        raise ExecutionContextError("coordinate claim response missing result")
    if not result.get("claimed"):
        raise ExecutionContextError("coordinate claim did not return a claimed job")

    job = result.get("job")
    if not isinstance(job, dict):
        raise ExecutionContextError("coordinate claim missing job")
    attempt_token = result.get("attempt_token")
    if not isinstance(attempt_token, int):
        raise ExecutionContextError("coordinate claim missing attempt_token")

    raw_context = result.get("execution_context")
    if not isinstance(raw_context, dict):
        raise ExecutionContextError("coordinate claim missing execution_context")

    ctx = parse_execution_context(
        raw_context,
        expected_job_id=job.get("id"),
        expected_workspace_id=workspace_id,
        expected_assigned_agent=agent_id,
    )

    # Bind the full job envelope: identity fields and attempt token must match.
    job_task_id = job.get("task_id")
    if ctx.task_id != job_task_id:
        raise ExecutionContextError(
            f"execution_context task_id mismatch: {ctx.task_id} != {job_task_id}"
        )
    job_workspace_id = job.get("workspace_id")
    if job_workspace_id is not None and ctx.workspace_id != job_workspace_id:
        raise ExecutionContextError(
            f"execution_context workspace_id mismatch: {ctx.workspace_id} != {job_workspace_id}"
        )
    job_assigned_agent = job.get("assigned_agent")
    if job_assigned_agent is not None and ctx.assigned_agent != job_assigned_agent:
        raise ExecutionContextError(
            f"execution_context assigned_agent mismatch: {ctx.assigned_agent} != {job_assigned_agent}"
        )
    job_attempt_count = job.get("attempt_count")
    if not isinstance(job_attempt_count, int):
        raise ExecutionContextError("job attempt_count must be an integer")
    if attempt_token != job_attempt_count:
        raise ExecutionContextError(
            f"attempt_token mismatch: {attempt_token} != {job_attempt_count}"
        )
    if attempt_token < 1:
        raise ExecutionContextError("attempt_token must be positive")

    return job, ctx, attempt_token
