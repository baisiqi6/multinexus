"""Strict v1 parser for Coordinate's optional ExecutorBinding snapshot.

This module is MultiNexus-side consumption only.  It does not import Coordinate
Python code or read Coordinate SQLite.  Typed mismatches fail before any adapter
invocation.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any


EXECUTOR_CONTRACT_VERSION = 1
_MAX_LABEL_LEN = 64
_MAX_ID_LEN = 256
_MAX_CAPABILITY_LEN = 64
_MAX_CAPABILITIES = 32

# Bounded identity labels: no path separators, shell metacharacters, whitespace,
# or control characters.
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

_BINDING_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CATALOG_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class ExecutorBindingError(ValueError):
    """Raised when a typed executor binding is missing, malformed, or conflicts with local identity."""


def _field_mismatch_message(
    label: str, actual_keys: set[Any], expected_keys: set[str]
) -> str:
    missing = sorted(expected_keys - actual_keys)
    unexpected_count = len(actual_keys - expected_keys)
    return (
        f"{label} has incorrect fields: missing={missing}, "
        f"unexpected_count={unexpected_count}, total_count={len(actual_keys)}"
    )


class ExecutorBinding:
    """Validated v1 executor binding snapshot.

    Provider is retained as audit metadata but is never executed or compared to
    local runtime config.
    """

    def __init__(
        self,
        *,
        binding_id: str,
        source_id: str,
        source_version: int,
        catalog_hash: str,
        executor_definition_id: str,
        executor_instance_id: str,
        runner_profile_id: str,
        provider: str,
        adapter: str,
        capabilities: tuple[str, ...],
    ):
        self.binding_id = binding_id
        self.source_id = source_id
        self.source_version = source_version
        self.catalog_hash = catalog_hash
        self.executor_definition_id = executor_definition_id
        self.executor_instance_id = executor_instance_id
        self.runner_profile_id = runner_profile_id
        self.provider = provider
        self.adapter = adapter
        self.capabilities = capabilities

    def result_evidence(self) -> dict[str, Any]:
        """Redacted evidence for a typed job result."""
        return {
            "executor_binding_id": self.binding_id,
            "executor_definition_id": self.executor_definition_id,
            "runner_profile_id": self.runner_profile_id,
        }


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_bounded_label(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ExecutorBindingError(f"{label} must be a string")
    if value != value.strip():
        raise ExecutorBindingError(f"{label} must not have surrounding whitespace")
    if not value:
        raise ExecutorBindingError(f"{label} is required")
    if len(value) > _MAX_LABEL_LEN:
        raise ExecutorBindingError(f"{label} exceeds {_MAX_LABEL_LEN} characters")
    if not _SAFE_LABEL_RE.match(value):
        raise ExecutorBindingError(f"{label} contains unsafe characters: {value!r}")
    return value


def _validate_capabilities(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ExecutorBindingError(f"{label} must be a list")
    if len(value) > _MAX_CAPABILITIES:
        raise ExecutorBindingError(f"{label} exceeds {_MAX_CAPABILITIES} items")
    seen: set[str] = set()
    normalized: list[str] = []
    for item in value:
        cap = _validate_bounded_label(item, f"{label} item")
        if len(cap) > _MAX_CAPABILITY_LEN:
            raise ExecutorBindingError(f"{label} item exceeds {_MAX_CAPABILITY_LEN} characters")
        if cap in seen:
            raise ExecutorBindingError(f"{label} contains duplicate capability: {cap!r}")
        seen.add(cap)
        normalized.append(cap)
    if normalized != sorted(normalized):
        raise ExecutorBindingError(f"{label} must be sorted")
    return tuple(normalized)


def _validate_binding_id(value: Any) -> str:
    if not isinstance(value, str) or not _BINDING_ID_RE.match(value):
        raise ExecutorBindingError("binding_id must be sha256:<64-hex>")
    return value


def _validate_catalog_hash(value: Any) -> str:
    if not isinstance(value, str) or not _CATALOG_HASH_RE.match(value):
        raise ExecutorBindingError("catalog_hash must be 64 lowercase hex characters")
    return value


def _validate_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutorBindingError(f"{label} must be an integer")
    if value < 0:
        raise ExecutorBindingError(f"{label} must be non-negative")
    return value


def _canonical_snapshot_dict(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Canonical form used for the binding digest (binding_id excluded)."""
    keys = {
        "contract_version",
        "source_id",
        "source_version",
        "catalog_hash",
        "executor_definition_id",
        "executor_instance_id",
        "runner_profile_id",
        "provider",
        "adapter",
        "capabilities",
    }
    if set(snapshot.keys()) - {"binding_id"} != keys:
        raise ExecutorBindingError(_field_mismatch_message(
            "binding snapshot", set(snapshot.keys()) - {"binding_id"}, keys
        ))
    return {k: snapshot[k] for k in sorted(keys)}


def parse_executor_binding(data: Any) -> ExecutorBinding:
    """Strictly parse a v1 executor binding snapshot dict.

    Raises ExecutorBindingError on any structural or cryptographic violation.
    """
    if not isinstance(data, dict):
        raise ExecutorBindingError("executor_binding must be an object")

    expected_keys = {
        "contract_version",
        "binding_id",
        "source_id",
        "source_version",
        "catalog_hash",
        "executor_definition_id",
        "executor_instance_id",
        "runner_profile_id",
        "provider",
        "adapter",
        "capabilities",
    }
    if set(data.keys()) != expected_keys:
        raise ExecutorBindingError(
            _field_mismatch_message("executor_binding", set(data.keys()), expected_keys)
        )

    contract_version = _validate_positive_int(data.get("contract_version"), "contract_version")
    if contract_version != EXECUTOR_CONTRACT_VERSION:
        raise ExecutorBindingError("executor_binding contract_version must be 1")

    binding_id = _validate_binding_id(data.get("binding_id"))
    source_id = _validate_bounded_label(data.get("source_id"), "source_id")
    source_version = _validate_positive_int(data.get("source_version"), "source_version")
    catalog_hash = _validate_catalog_hash(data.get("catalog_hash"))
    executor_definition_id = _validate_bounded_label(
        data.get("executor_definition_id"), "executor_definition_id"
    )
    executor_instance_id = _validate_bounded_label(
        data.get("executor_instance_id"), "executor_instance_id"
    )
    runner_profile_id = _validate_bounded_label(
        data.get("runner_profile_id"), "runner_profile_id"
    )
    provider = _validate_bounded_label(data.get("provider"), "provider")
    adapter = _validate_bounded_label(data.get("adapter"), "adapter")
    capabilities = _validate_capabilities(data.get("capabilities"), "capabilities")

    expected_id = f"sha256:{hashlib.sha256(_canonical_json(_canonical_snapshot_dict(data)).encode('utf-8')).hexdigest()}"
    if binding_id != expected_id:
        raise ExecutorBindingError(
            f"executor_binding digest mismatch: expected {expected_id}, got {binding_id}"
        )

    return ExecutorBinding(
        binding_id=binding_id,
        source_id=source_id,
        source_version=source_version,
        catalog_hash=catalog_hash,
        executor_definition_id=executor_definition_id,
        executor_instance_id=executor_instance_id,
        runner_profile_id=runner_profile_id,
        provider=provider,
        adapter=adapter,
        capabilities=capabilities,
    )


def validate_executor_binding(
    data: Any,
    *,
    agent_id: str,
    adapter: str,
) -> ExecutorBinding | None:
    """Validate an optional executor binding against this agent's local identity.

    - ``None`` means a legacy untyped exact-instance job and is accepted.
    - A typed binding must match ``agent_id`` as both instance id and runner
      profile id, and its ``adapter`` label must match the local ``adapter``.
    - ``provider`` is validated as a bounded label but not compared to local
      config; it is non-executable audit metadata.

    Raises ExecutorBindingError with a ``executor_binding_mismatch:`` prefix on
    any mismatch so the caller can distinguish it from an empty queue.
    """
    if data is None:
        return None

    try:
        binding = parse_executor_binding(data)
    except ExecutorBindingError as exc:
        raise ExecutorBindingError(f"executor_binding_mismatch: {exc}") from exc

    if binding.executor_instance_id != agent_id:
        raise ExecutorBindingError(
            f"executor_binding_mismatch: instance_id {binding.executor_instance_id!r} != {agent_id!r}"
        )
    if binding.runner_profile_id != agent_id:
        raise ExecutorBindingError(
            f"executor_binding_mismatch: runner_profile_id {binding.runner_profile_id!r} != {agent_id!r}"
        )
    if binding.adapter != adapter:
        raise ExecutorBindingError(
            f"executor_binding_mismatch: adapter {binding.adapter!r} != local {adapter!r}"
        )

    return binding
