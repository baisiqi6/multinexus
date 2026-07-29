"""Capacity authority projection and canonical digests.

This module owns the separately versioned capacity projection from
``config/agent-registry.toml``. It does not import Coordinate.
"""
from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CAPACITY_CONTRACT_VERSION = 1
MAX_CONCURRENT_JOBS = 32

_SAFE_LABEL_RE = __import__("re").compile(r"^[A-Za-z0-9_.\-]+$")

# Shared agent-registry.toml may contain roster/executor roots; the capacity
# parser ignores them but must reject any unknown or secret-bearing root.
_ALLOWED_SHARED_ROOT_KEYS = {
    "registry",
    "executor_definitions",
    "agents",
    "external_agents",
    "capacity_registry",
    "executor_capacities",
}


class CapacityAuthorityError(Exception):
    """The capacity projection violates the strict allow-list schema."""


@dataclass(frozen=True)
class CapacityPolicy:
    agent_id: str
    max_concurrent_jobs: int

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "max_concurrent_jobs": self.max_concurrent_jobs,
        }


@dataclass(frozen=True)
class CapacityCatalog:
    source_id: str
    source_version: int
    catalog_hash: str
    policies: tuple[CapacityPolicy, ...]


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_bounded_label(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise CapacityAuthorityError(f"{label} must be a string")
    if value != value.strip():
        raise CapacityAuthorityError(f"{label} must not have surrounding whitespace")
    if not value:
        raise CapacityAuthorityError(f"{label} is required")
    if len(value) > 64:
        raise CapacityAuthorityError(f"{label} exceeds 64 characters")
    if not _SAFE_LABEL_RE.match(value):
        raise CapacityAuthorityError(f"{label} contains unsafe characters: {value!r}")
    return value


def _validate_max_concurrent_jobs(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CapacityAuthorityError(f"{label} must be an integer")
    if value < 1 or value > MAX_CONCURRENT_JOBS:
        raise CapacityAuthorityError(
            f"{label} must be between 1 and {MAX_CONCURRENT_JOBS}: got {value}"
        )
    return value


def canonical_capacity_catalog_dict(catalog: CapacityCatalog) -> dict[str, Any]:
    """Return the exact canonical object whose UTF-8 JSON is hashed."""
    policies = sorted(
        [p.canonical_dict() for p in catalog.policies],
        key=lambda p: p["agent_id"],
    )
    return {
        "contract_version": CAPACITY_CONTRACT_VERSION,
        "source_id": catalog.source_id,
        "source_version": catalog.source_version,
        "policies": policies,
    }


def compute_capacity_catalog_hash(catalog: CapacityCatalog) -> str:
    """SHA-256 of the canonical UTF-8 JSON capacity catalog bytes."""
    canonical = _canonical_json(canonical_capacity_catalog_dict(catalog))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_capacity_policy_id(
    *,
    agent_id: str,
    catalog_hash: str,
    max_concurrent_jobs: int,
    source_id: str,
    source_version: int,
) -> str:
    """Return ``sha256:<digest>`` for a capacity policy snapshot."""
    canonical = _canonical_json({
        "agent_id": agent_id,
        "catalog_hash": catalog_hash,
        "contract_version": CAPACITY_CONTRACT_VERSION,
        "max_concurrent_jobs": max_concurrent_jobs,
        "source_id": source_id,
        "source_version": source_version,
    })
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _load_toml(path: str | Path) -> dict[str, Any]:
    return tomllib.loads(Path(path).expanduser().read_text(encoding="utf-8"))


def parse_capacity_catalog(source: str | Path) -> CapacityCatalog:
    """Parse the capacity projection from a TOML authority file.

    Unknown capacity keys, duplicate policies, non-integer capacities, and
    values outside ``1..MAX_CONCURRENT_JOBS`` raise ``CapacityAuthorityError``.
    """
    path = Path(source).expanduser()
    data = _load_toml(path)

    unknown_root = set(data.keys()) - _ALLOWED_SHARED_ROOT_KEYS
    if unknown_root:
        raise CapacityAuthorityError(f"unknown root keys: {sorted(unknown_root)}")

    capacity_registry = data.get("capacity_registry")
    if not isinstance(capacity_registry, dict):
        raise CapacityAuthorityError("missing [capacity_registry] metadata")
    unknown_registry = set(capacity_registry.keys()) - {"id", "version"}
    if unknown_registry:
        raise CapacityAuthorityError(
            f"unknown [capacity_registry] keys: {sorted(unknown_registry)}"
        )

    source_id = capacity_registry.get("id")
    _validate_bounded_label(source_id, "[capacity_registry].id")

    version = capacity_registry.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise CapacityAuthorityError(
            "[capacity_registry].version must be a non-negative integer"
        )
    source_version = version

    policies: list[CapacityPolicy] = []
    seen_agent_ids: set[str] = set()
    for raw in data.get("executor_capacities", []):
        if not isinstance(raw, dict):
            raise CapacityAuthorityError("executor_capacities entry must be a table")
        unknown = set(raw.keys()) - {"agent_id", "max_concurrent_jobs"}
        if unknown:
            raise CapacityAuthorityError(
                f"unknown keys in executor_capacities entry: {sorted(unknown)}"
            )
        agent_id = _validate_bounded_label(raw.get("agent_id"), "executor_capacity.agent_id")
        if agent_id in seen_agent_ids:
            raise CapacityAuthorityError(
                f"duplicate executor_capacity agent_id: {agent_id!r}"
            )
        seen_agent_ids.add(agent_id)
        max_jobs = _validate_max_concurrent_jobs(
            raw.get("max_concurrent_jobs"), f"executor_capacity '{agent_id}'.max_concurrent_jobs"
        )
        policies.append(CapacityPolicy(agent_id=agent_id, max_concurrent_jobs=max_jobs))

    policies_tuple = tuple(policies)
    catalog = CapacityCatalog(
        source_id=source_id,
        source_version=source_version,
        catalog_hash="",
        policies=policies_tuple,
    )
    catalog_hash = compute_capacity_catalog_hash(catalog)
    return CapacityCatalog(
        source_id=source_id,
        source_version=source_version,
        catalog_hash=catalog_hash,
        policies=policies_tuple,
    )


# Alias matching the registry_authority loader naming convention.
load_capacity_authority = parse_capacity_catalog
