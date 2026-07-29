"""Strict v1 parser for Coordinate's ExecutionLease envelope.

This module is MultiNexus-side consumption only. It does NOT import Coordinate
Python code. Lease identity must be validated before context, binding, or payload
are trusted; if the lease envelope is missing or invalid, no provider is invoked.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEASE_CONTRACT_VERSION = 1
MIN_TTL_SECONDS = 30
MAX_TTL_SECONDS = 600
MAX_CONCURRENT_JOBS = 32
MAX_LABEL_LEN = 256
MAX_PATH_LEN = 4096
MAX_HOST_ID_LEN = 64

_LEASE_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

_V1_ENVELOPE_KEYS = frozenset(
    {
        "contract_version",
        "lease_id",
        "job_id",
        "attempt_token",
        "agent_id",
        "runner_profile_id",
        "host_id",
        "resource_kind",
        "resource_key",
        "normalized_path",
        "capacity_policy_id",
        "max_concurrent_jobs",
        "acquired_at",
        "expires_at",
        "server_now",
        "ttl_seconds",
        "renew_interval_seconds",
    }
)


class ExecutionLeaseError(ValueError):
    """Raised when a lease envelope is missing, malformed, or conflicts with authority."""


class ResourceIdentityError(ValueError):
    """Raised when a copied Coordinate resource identity computation is invalid."""


@dataclass(frozen=True)
class WorktreeResource:
    """Coordinate-compatible host-scoped worktree resource identity."""

    host_id: str
    normalized_path: str

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "contract_version": LEASE_CONTRACT_VERSION,
            "resource_kind": "worktree",
            "host_id": self.host_id,
            "normalized_path": self.normalized_path,
        }


@dataclass(frozen=True)
class ExecutionLeaseV1:
    contract_version: int
    lease_id: str
    job_id: str
    attempt_token: int
    agent_id: str
    runner_profile_id: str
    host_id: str
    resource_kind: str
    resource_key: str
    normalized_path: str
    capacity_policy_id: str
    max_concurrent_jobs: int
    acquired_at: str
    expires_at: str
    server_now: str
    ttl_seconds: int
    renew_interval_seconds: int


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Lexical host/path normalization and resource_key algorithm copied from
# Coordinate's execution_resources.py. We deliberately do not import Coordinate
# so that MultiNexus-side lease consumption stays self-contained.
# ---------------------------------------------------------------------------
def _is_windows_drive(path: str) -> bool:
    if len(path) < 2:
        return False
    return path[0].isalpha() and path[1] == ":"


def _is_windows_unc(path: str) -> bool:
    return len(path) >= 2 and path[:2] in ("\\\\", "//")


def normalize_worktree_path(path: str) -> str:
    """Normalize a host-resolved worktree path to a host-scoped lexical identity.

    POSIX: require absolute ``/``, apply ``posixpath.normpath``, Unicode NFC,
    preserve case, preserve root semantics.

    Windows drive/UNC: accept either separator, apply ``ntpath.normpath``,
    canonicalize separators to ``\\``, Unicode NFC plus ``casefold``, and
    preserve drive/UNC root semantics.

    Rejects relative, empty, control/NUL-bearing, and over-4096-character paths.
    """
    if not isinstance(path, str):
        raise ResourceIdentityError("path must be a string")
    path = path.strip()
    if not path:
        raise ResourceIdentityError("path is required")
    if len(path) > MAX_PATH_LEN:
        raise ResourceIdentityError(f"path exceeds {MAX_PATH_LEN} characters")
    if _CONTROL_RE.search(path):
        raise ResourceIdentityError("path contains control characters")

    if _is_windows_unc(path):
        import ntpath

        normalized = ntpath.normpath(path)
        if normalized in (".", ""):
            raise ResourceIdentityError("UNC path collapsed to relative")
        normalized = normalized.replace("/", "\\")
        normalized = unicodedata.normalize("NFC", normalized).casefold()
        return normalized

    if _is_windows_drive(path):
        import ntpath

        if len(path) == 2 or (len(path) > 2 and path[2] not in ("\\", "/")):
            raise ResourceIdentityError("Windows drive path must be absolute")
        normalized = ntpath.normpath(path)
        if normalized in (".", ""):
            raise ResourceIdentityError("drive path collapsed to relative")
        normalized = normalized.replace("/", "\\")
        drive = normalized[0].lower()
        rest = normalized[2:]
        normalized = drive + ":" + rest
        normalized = unicodedata.normalize("NFC", normalized).casefold()
        return normalized

    if not path.startswith("/"):
        raise ResourceIdentityError("POSIX path must be absolute")

    import posixpath

    normalized = posixpath.normpath(path)
    if normalized == ".":
        raise ResourceIdentityError("path collapsed to relative")
    if normalized == "":
        normalized = "/"
    normalized = unicodedata.normalize("NFC", normalized)
    return normalized


def _validate_host_id(host_id: str) -> str:
    if not isinstance(host_id, str):
        raise ResourceIdentityError("host_id must be a string")
    if not host_id:
        raise ResourceIdentityError("host_id is required")
    if host_id != host_id.strip():
        raise ResourceIdentityError("host_id must not have surrounding whitespace")
    if len(host_id) > MAX_HOST_ID_LEN:
        raise ResourceIdentityError(f"host_id exceeds {MAX_HOST_ID_LEN} characters")
    if _CONTROL_RE.search(host_id):
        raise ResourceIdentityError("host_id contains control characters")
    return host_id


def build_worktree_resource(host_id: str, worktree_path: str) -> WorktreeResource:
    """Build a normalized worktree resource identity."""
    host_id = _validate_host_id(host_id)
    normalized_path = normalize_worktree_path(worktree_path)
    return WorktreeResource(host_id=host_id, normalized_path=normalized_path)


def compute_resource_key(resource: WorktreeResource) -> str:
    """Return ``sha256:<digest>`` for the canonical resource object."""
    canonical = _canonical_json(resource.canonical_dict())
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _resource_key_canonical_dict(resource: dict[str, Any]) -> dict[str, Any]:
    keys = {"contract_version", "resource_kind", "host_id", "normalized_path"}
    if set(resource.keys()) != keys:
        raise ResourceIdentityError(
            f"resource object has incorrect fields: {sorted(resource.keys())}"
        )
    if resource["contract_version"] != LEASE_CONTRACT_VERSION:
        raise ResourceIdentityError("resource contract_version must be 1")
    if resource["resource_kind"] != "worktree":
        raise ResourceIdentityError("resource_kind must be 'worktree'")
    return {k: resource[k] for k in sorted(keys)}


def validate_resource_key_matches(
    resource: dict[str, Any], resource_key: str
) -> dict[str, Any]:
    """Validate a stored resource object against its digest.

    Rejects non-canonical paths, malformed host_id, and malformed digests.
    Raises ``ResourceIdentityError`` on malformed stored state.
    """
    if not isinstance(resource_key, str):
        raise ResourceIdentityError("resource_key must be a string")
    if not _DIGEST_RE.match(resource_key):
        raise ResourceIdentityError("resource_key must be sha256:<64 lowercase hex>")
    canonical_dict = _resource_key_canonical_dict(resource)
    _validate_host_id(canonical_dict["host_id"])
    normalized = normalize_worktree_path(canonical_dict["normalized_path"])
    if normalized != canonical_dict["normalized_path"]:
        raise ResourceIdentityError(
            f"normalized_path is not canonical: {canonical_dict['normalized_path']!r} != {normalized!r}"
        )
    canonical = _canonical_json(canonical_dict)
    expected = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
    if resource_key != expected:
        raise ResourceIdentityError(
            f"resource digest mismatch: expected {expected}, got {resource_key}"
        )
    return canonical_dict


# ---------------------------------------------------------------------------
# Lease envelope validation helpers.
# ---------------------------------------------------------------------------
def _validate_required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ExecutionLeaseError(f"{label} is required")
    if not value.strip():
        raise ExecutionLeaseError(f"{label} must be a non-empty string")
    return value


def _validate_bounded_label(value: Any, label: str) -> str:
    s = _validate_required_string(value, label)
    if len(s) > MAX_LABEL_LEN:
        raise ExecutionLeaseError(f"{label} exceeds {MAX_LABEL_LEN} characters")
    if not _SAFE_LABEL_RE.match(s):
        raise ExecutionLeaseError(f"{label} contains unsafe characters: {s!r}")
    return s


def _validate_lease_id(value: Any) -> str:
    s = _validate_required_string(value, "lease_id")
    if not _LEASE_ID_RE.match(s):
        raise ExecutionLeaseError(f"lease_id must be a lowercase UUID: {s!r}")
    return s


def _validate_digest(value: Any, label: str) -> str:
    s = _validate_required_string(value, label)
    if not _DIGEST_RE.match(s):
        raise ExecutionLeaseError(f"{label} must be sha256:<64-hex>: {s!r}")
    return s


def _validate_timestamp(value: Any, label: str) -> str:
    s = _validate_required_string(value, label)
    if not _TS_RE.match(s):
        raise ExecutionLeaseError(
            f"{label} must be ISO-UTC timestamp ending in Z: {s!r}"
        )
    try:
        datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ExecutionLeaseError(f"{label} is not a valid timestamp: {s!r}") from exc
    return s


def _is_absolute_worktree_path(path: str) -> bool:
    """Return True if *path* is an absolute POSIX, Windows drive, or UNC path."""
    if _is_windows_unc(path):
        return True
    if _is_windows_drive(path):
        return len(path) > 2 and path[2] in ("\\", "/")
    return path.startswith("/")


def _validate_path(value: Any, label: str) -> str:
    """Validate *value* as a legal absolute worktree path but preserve it as-is.

    Canonical normalization is intentionally deferred to ``validate_execution_lease``
    so that non-canonical paths are not silently rewritten during parse.
    """
    s = _validate_required_string(value, label)
    if len(s) > MAX_PATH_LEN:
        raise ExecutionLeaseError(f"{label} exceeds {MAX_PATH_LEN} characters")
    if _CONTROL_RE.search(s):
        raise ExecutionLeaseError(f"{label} contains control characters")
    if not _is_absolute_worktree_path(s):
        raise ExecutionLeaseError(f"{label} must be an absolute path: {s!r}")
    return s


def _validate_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutionLeaseError(f"{label} must be an integer")
    if value <= 0:
        raise ExecutionLeaseError(f"{label} must be positive")
    return value


def _validate_ttl(value: Any) -> int:
    ttl = _validate_positive_int(value, "ttl_seconds")
    if ttl < MIN_TTL_SECONDS or ttl > MAX_TTL_SECONDS:
        raise ExecutionLeaseError(
            f"ttl_seconds must be between {MIN_TTL_SECONDS} and {MAX_TTL_SECONDS}: {ttl}"
        )
    return ttl


def _validate_max_concurrent_jobs(value: Any) -> int:
    n = _validate_positive_int(value, "max_concurrent_jobs")
    if n > MAX_CONCURRENT_JOBS:
        raise ExecutionLeaseError(
            f"max_concurrent_jobs must be <= {MAX_CONCURRENT_JOBS}: {n}"
        )
    return n


def _parse_iso(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def parse_execution_lease(data: Any) -> ExecutionLeaseV1:
    """Strictly parse a v1 execution_lease envelope dict.

    Raises ExecutionLeaseError on any structural, type, or cryptographic violation.
    """
    if not isinstance(data, dict):
        raise ExecutionLeaseError("execution_lease must be an object")
    if set(data.keys()) != _V1_ENVELOPE_KEYS:
        missing = sorted(_V1_ENVELOPE_KEYS - set(data.keys()))
        unexpected = sorted(set(data.keys()) - _V1_ENVELOPE_KEYS)
        raise ExecutionLeaseError(
            f"execution_lease has incorrect keys: missing={missing}, unexpected={unexpected}"
        )

    contract_version = data["contract_version"]
    if isinstance(contract_version, bool) or not isinstance(contract_version, int):
        raise ExecutionLeaseError("contract_version must be an integer")
    if contract_version != LEASE_CONTRACT_VERSION:
        raise ExecutionLeaseError(
            f"execution_lease contract_version {contract_version} is not supported"
        )

    lease_id = _validate_lease_id(data["lease_id"])
    job_id = _validate_required_string(data["job_id"], "job_id")
    attempt_token = _validate_positive_int(data["attempt_token"], "attempt_token")
    agent_id = _validate_bounded_label(data["agent_id"], "agent_id")
    runner_profile_id = _validate_bounded_label(
        data["runner_profile_id"], "runner_profile_id"
    )
    host_id = _validate_bounded_label(data["host_id"], "host_id")
    resource_kind = _validate_bounded_label(data["resource_kind"], "resource_kind")
    if resource_kind != "worktree":
        raise ExecutionLeaseError("resource_kind must be 'worktree'")
    resource_key = _validate_digest(data["resource_key"], "resource_key")
    normalized_path = _validate_path(data["normalized_path"], "normalized_path")
    capacity_policy_id = _validate_digest(
        data["capacity_policy_id"], "capacity_policy_id"
    )
    max_concurrent_jobs = _validate_max_concurrent_jobs(data["max_concurrent_jobs"])
    acquired_at = _validate_timestamp(data["acquired_at"], "acquired_at")
    expires_at = _validate_timestamp(data["expires_at"], "expires_at")
    server_now = _validate_timestamp(data["server_now"], "server_now")
    ttl_seconds = _validate_ttl(data["ttl_seconds"])
    renew_interval_seconds = _validate_positive_int(
        data["renew_interval_seconds"], "renew_interval_seconds"
    )
    if renew_interval_seconds >= ttl_seconds:
        raise ExecutionLeaseError(
            f"renew_interval_seconds ({renew_interval_seconds}) must be < ttl_seconds ({ttl_seconds})"
        )

    acquired_dt = _parse_iso(acquired_at)
    expires_dt = _parse_iso(expires_at)
    server_dt = _parse_iso(server_now)
    if not (acquired_dt <= server_dt < expires_dt):
        raise ExecutionLeaseError(
            "server_now must satisfy acquired_at <= server_now < expires_at"
        )

    expected_ttl = int((expires_dt - acquired_dt).total_seconds())
    if expected_ttl != ttl_seconds:
        raise ExecutionLeaseError(
            f"ttl_seconds ({ttl_seconds}) does not match acquired_at/expires_at interval ({expected_ttl})"
        )

    return ExecutionLeaseV1(
        contract_version=LEASE_CONTRACT_VERSION,
        lease_id=lease_id,
        job_id=job_id,
        attempt_token=attempt_token,
        agent_id=agent_id,
        runner_profile_id=runner_profile_id,
        host_id=host_id,
        resource_kind=resource_kind,
        resource_key=resource_key,
        normalized_path=normalized_path,
        capacity_policy_id=capacity_policy_id,
        max_concurrent_jobs=max_concurrent_jobs,
        acquired_at=acquired_at,
        expires_at=expires_at,
        server_now=server_now,
        ttl_seconds=ttl_seconds,
        renew_interval_seconds=renew_interval_seconds,
    )


def validate_execution_lease(
    data: Any,
    *,
    expected_agent_id: str,
    expected_job_id: str | None = None,
    expected_attempt_token: int | None = None,
    execution_context: Any = None,
    executor_binding: Any = None,
) -> ExecutionLeaseV1:
    """Validate a lease envelope and its cross-links to context/binding.

    Raises ExecutionLeaseError on any mismatch. ``execution_context`` and
    ``executor_binding`` are optional but, when supplied, are checked for
    identity consistency.
    """
    lease = parse_execution_lease(data)

    if lease.agent_id != expected_agent_id:
        raise ExecutionLeaseError(
            f"lease agent_id mismatch: {lease.agent_id!r} != {expected_agent_id!r}"
        )
    if expected_job_id is not None and lease.job_id != expected_job_id:
        raise ExecutionLeaseError(
            f"lease job_id mismatch: {lease.job_id!r} != {expected_job_id!r}"
        )
    if (
        expected_attempt_token is not None
        and lease.attempt_token != expected_attempt_token
    ):
        raise ExecutionLeaseError(
            f"lease attempt_token mismatch: {lease.attempt_token} != {expected_attempt_token}"
        )

    canonical_path = normalize_worktree_path(lease.normalized_path)
    if canonical_path != lease.normalized_path:
        raise ExecutionLeaseError(
            f"normalized_path is not canonical: {lease.normalized_path!r} != {canonical_path!r}"
        )

    expected_resource_key = compute_resource_key(
        build_worktree_resource(lease.host_id, lease.normalized_path)
    )
    if expected_resource_key != lease.resource_key:
        raise ExecutionLeaseError(
            f"resource_key mismatch: expected {expected_resource_key}, got {lease.resource_key}"
        )

    if execution_context is not None:
        if not isinstance(execution_context, dict):
            raise ExecutionLeaseError(
                "execution_context must be a dict for lease cross-check"
            )
        if execution_context.get("job_id") != lease.job_id:
            raise ExecutionLeaseError(
                "lease job_id does not match execution_context.job_id"
            )
        if execution_context.get("assigned_agent") != lease.agent_id:
            raise ExecutionLeaseError(
                "lease agent_id does not match execution_context.assigned_agent"
            )
        if execution_context.get("host_id") != lease.host_id:
            raise ExecutionLeaseError(
                "lease host_id does not match execution_context.host_id"
            )
        ctx_worktree = execution_context.get("worktree_path")
        if ctx_worktree != lease.normalized_path:
            raise ExecutionLeaseError(
                f"lease normalized_path does not match execution_context.worktree_path: "
                f"{lease.normalized_path!r} != {ctx_worktree!r}"
            )

    if executor_binding is not None:
        if not isinstance(executor_binding, dict):
            raise ExecutionLeaseError(
                "executor_binding must be a dict for lease cross-check"
            )
        if executor_binding.get("executor_instance_id") != lease.agent_id:
            raise ExecutionLeaseError(
                "lease agent_id does not match executor_binding.executor_instance_id"
            )
        if executor_binding.get("runner_profile_id") != lease.runner_profile_id:
            raise ExecutionLeaseError(
                "lease runner_profile_id does not match executor_binding.runner_profile_id"
            )

    return lease


def lease_to_dict(lease: ExecutionLeaseV1) -> dict[str, Any]:
    """Serialize a validated lease back to the canonical envelope dict."""
    return {
        "contract_version": lease.contract_version,
        "lease_id": lease.lease_id,
        "job_id": lease.job_id,
        "attempt_token": lease.attempt_token,
        "agent_id": lease.agent_id,
        "runner_profile_id": lease.runner_profile_id,
        "host_id": lease.host_id,
        "resource_kind": lease.resource_kind,
        "resource_key": lease.resource_key,
        "normalized_path": lease.normalized_path,
        "capacity_policy_id": lease.capacity_policy_id,
        "max_concurrent_jobs": lease.max_concurrent_jobs,
        "acquired_at": lease.acquired_at,
        "expires_at": lease.expires_at,
        "server_now": lease.server_now,
        "ttl_seconds": lease.ttl_seconds,
        "renew_interval_seconds": lease.renew_interval_seconds,
    }


def load_fixture(name: str) -> dict[str, Any]:
    """Load a canonical fixture from ``tests/fixtures`` by filename."""
    fixtures_dir = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
    path = fixtures_dir / name
    return json.loads(path.read_text(encoding="utf-8"))


def fixture_sha256(name: str) -> str:
    """Return the SHA-256 hex digest of a fixture's raw bytes."""
    fixtures_dir = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
    return hashlib.sha256((fixtures_dir / name).read_bytes()).hexdigest()
