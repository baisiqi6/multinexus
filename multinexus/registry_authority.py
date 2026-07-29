"""Registry authority projection and parity verifier.

This module owns the seam between the source-controlled secret-free authority
(`config/agent-registry.toml`) and the private host runtime config
(`agents.toml`). It is intentionally small and does not import Coordinate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_ROOT_KEYS = {"registry", "agents", "external_agents", "executor_definitions", "capacity_registry", "executor_capacities"}
_REGISTRY_KEYS = {"id", "version"}
_ENTRY_KEYS = {"id", "display_name", "discord_user_id", "executor_definition_id", "runner_profile_id", "enabled"}
_DEFINITION_KEYS = {"id", "provider", "adapter", "capabilities"}
_AGENT_KINDS = {"managed", "external"}

# Bounded identity labels: no path separators, shell metacharacters, whitespace,
# or control characters.  This matches the Coordinate-side validator.
_SAFE_LABEL_RE = __import__("re").compile(r"^[A-Za-z0-9_.\-]+$")


class AuthorityError(Exception):
    """The authority file violates the strict allow-list schema."""


@dataclass(frozen=True)
class AgentEntry:
    id: str
    display_name: str
    discord_user_id: str
    agent_type: str
    executor_definition_id: str | None = None
    runner_profile_id: str | None = None

    def to_canonical_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "discord_user_id": self.discord_user_id,
            "display_name": self.display_name,
            "agent_type": self.agent_type,
        }


@dataclass(frozen=True)
class ExecutorDefinition:
    id: str
    provider: str
    adapter: str
    capabilities: tuple[str, ...]

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "adapter": self.adapter,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True)
class ExecutorInstanceBinding:
    agent_id: str
    executor_definition_id: str
    runner_profile_id: str
    enabled: bool = True

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "executor_definition_id": self.executor_definition_id,
            "runner_profile_id": self.runner_profile_id,
            "enabled": self.enabled,
        }


@dataclass
class Authority:
    source_id: str
    source_version: int
    entries: list[AgentEntry]
    source_hash: str
    executor_definitions: list[ExecutorDefinition]
    executor_bindings: list[ExecutorInstanceBinding]
    executor_catalog_hash: str


@dataclass
class ParityResult:
    ok: bool
    authority: Authority | None = None
    authority_count: int = 0
    runtime_count: int = 0
    source_hash: str | None = None
    authority_hash: str | None = None
    executor_catalog_hash: str | None = None
    diff: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "source_id": self.authority.source_id if self.authority else None,
            "source_version": self.authority.source_version if self.authority else None,
            "source_hash": self.source_hash,
            "authority_hash": self.authority_hash,
            "executor_catalog_hash": self.executor_catalog_hash,
            "authority_count": self.authority_count,
            "runtime_count": self.runtime_count,
            "diff": self.diff,
            "errors": self.errors,
        }


def _is_ascii_decimal_positive(value: str) -> bool:
    return (
        bool(value)
        and value.isascii()
        and value.isdigit()
        and int(value) > 0
    )


def _validate_discord_user_id(value: Any, *, require_string: bool = False) -> str:
    """Normalize a Discord user id or raise ValueError."""
    if value is None:
        raise ValueError("missing discord_user_id")
    if require_string and not isinstance(value, str):
        raise ValueError("invalid discord_user_id: must be a quoted string")
    raw = str(value)
    if raw != raw.strip():
        raise ValueError("invalid discord_user_id: surrounding whitespace is not allowed")
    did = raw
    if not _is_ascii_decimal_positive(did):
        raise ValueError("invalid discord_user_id")
    return did


def _normalize_id(value: Any) -> str:
    if value is None:
        raise ValueError("missing 'id'")
    agent_id = str(value).strip()
    if not agent_id:
        raise ValueError("missing 'id'")
    return agent_id


def _normalize_display_name(value: Any, agent_id: str) -> str:
    display_name = str(value).strip() if value is not None else ""
    return display_name or agent_id


def _validate_bounded_label(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if value != value.strip():
        raise ValueError(f"{label} must not have surrounding whitespace")
    if not value:
        raise ValueError(f"{label} is required")
    if len(value) > 64:
        raise ValueError(f"{label} exceeds 64 characters")
    if not _SAFE_LABEL_RE.match(value):
        raise ValueError(f"{label} contains unsafe characters: {value!r}")
    return value


def _validate_capabilities(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    if len(value) > 32:
        raise ValueError(f"{label} exceeds 32 items")
    seen: set[str] = set()
    normalized: list[str] = []
    for item in value:
        cap = _validate_bounded_label(item, f"{label} item")
        if cap in seen:
            raise ValueError(f"{label} contains duplicate capability: {cap!r}")
        seen.add(cap)
        normalized.append(cap)
    if normalized != sorted(normalized):
        raise ValueError(f"{label} must be sorted")
    return tuple(normalized)


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_hash(entries: list[AgentEntry]) -> str:
    """Deterministic SHA-256 of the normalized roster.

    Matches Coordinate v10's canonical contract exactly:
    id-sorted JSON list with keys id, discord_user_id, display_name, agent_type;
    sort_keys=True, separators=(',', ':'), ensure_ascii=False; UTF-8; SHA-256.
    Executor binding fields are intentionally excluded.
    """
    payload = [entry.to_canonical_dict() for entry in sorted(entries, key=lambda e: e.id)]
    canonical = _canonical_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_executor_catalog_hash(
    source_id: str,
    source_version: int,
    definitions: list[ExecutorDefinition],
    bindings: list[ExecutorInstanceBinding],
) -> str:
    """SHA-256 of the canonical executor catalog projection.

    Matches Coordinate's v1 catalog contract byte-for-byte.
    """
    payload = {
        "contract_version": 1,
        "source_id": source_id,
        "source_version": source_version,
        "executor_definitions": sorted(
            [d.to_canonical_dict() for d in definitions],
            key=lambda d: d["id"],
        ),
        "executor_instance_bindings": sorted(
            [b.to_canonical_dict() for b in bindings],
            key=lambda b: b["agent_id"],
        ),
    }
    canonical = _canonical_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_toml(path: str | Path) -> dict[str, Any]:
    return tomllib.loads(Path(path).expanduser().read_text(encoding="utf-8"))


def _authority_section_label(agent_type: str) -> str:
    return "agents" if agent_type == "managed" else "external_agents"


def load_authority(path: str | Path) -> Authority:
    """Load the canonical authority with a strict allow-list schema.

    Rejects unknown root, [registry] or entry keys (including every
    secret-bearing field) before any deploy or copy.
    """
    try:
        data = _load_toml(path)
    except Exception as exc:
        raise AuthorityError(f"cannot parse authority TOML: {exc}") from exc

    unknown_root = set(data.keys()) - _ROOT_KEYS
    if unknown_root:
        raise AuthorityError(f"unknown root keys: {sorted(unknown_root)}")

    registry = data.get("registry")
    if not isinstance(registry, dict):
        raise AuthorityError("missing [registry] metadata")
    unknown_registry = set(registry.keys()) - _REGISTRY_KEYS
    if unknown_registry:
        raise AuthorityError(f"unknown [registry] keys: {sorted(unknown_registry)}")

    source_id = str(registry.get("id", "")).strip()
    if not source_id:
        raise AuthorityError("[registry].id is required")
    try:
        _validate_bounded_label(source_id, "[registry].id")
    except ValueError as exc:
        raise AuthorityError(str(exc)) from exc

    version = registry.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise AuthorityError("[registry].version must be a non-negative integer")
    source_version = version

    # Executor definitions first so agent bindings can reference them.
    definitions: list[ExecutorDefinition] = []
    seen_def_ids: set[str] = set()
    for raw in data.get("executor_definitions", []):
        if not isinstance(raw, dict):
            raise AuthorityError("executor_definitions entry must be a table")
        unknown = set(raw.keys()) - _DEFINITION_KEYS
        if unknown:
            raise AuthorityError(f"unknown keys in executor_definitions entry: {sorted(unknown)}")

        try:
            def_id = _validate_bounded_label(raw.get("id"), "executor_definition.id")
        except ValueError as exc:
            raise AuthorityError(str(exc)) from exc
        if def_id in seen_def_ids:
            raise AuthorityError(f"duplicate executor_definition id '{def_id}'")
        seen_def_ids.add(def_id)

        try:
            provider = _validate_bounded_label(raw.get("provider"), "executor_definition.provider")
            adapter = _validate_bounded_label(raw.get("adapter"), "executor_definition.adapter")
            capabilities = _validate_capabilities(raw.get("capabilities", []), "executor_definition.capabilities")
        except ValueError as exc:
            raise AuthorityError(str(exc)) from exc

        definitions.append(ExecutorDefinition(
            id=def_id,
            provider=provider,
            adapter=adapter,
            capabilities=capabilities,
        ))

    definition_ids = {d.id for d in definitions}

    entries: list[AgentEntry] = []
    bindings: list[ExecutorInstanceBinding] = []
    seen_ids: dict[str, str] = {}
    seen_discord_ids: dict[str, str] = {}

    for agent_type in ("managed", "external"):
        section = _authority_section_label(agent_type)
        section_data = data.get(section)
        if section_data is None:
            continue
        if not isinstance(section_data, list):
            raise AuthorityError(f"{section} must be a list of tables")

        for raw in section_data:
            if not isinstance(raw, dict):
                raise AuthorityError(f"{section} entry must be a table")
            unknown = set(raw.keys()) - _ENTRY_KEYS
            if unknown:
                raise AuthorityError(f"unknown keys in {section} entry: {sorted(unknown)}")

            try:
                agent_id = _normalize_id(raw.get("id"))
            except ValueError as exc:
                raise AuthorityError(f"{section} entry: {exc}") from exc
            if agent_id in seen_ids:
                raise AuthorityError(
                    f"duplicate agent id '{agent_id}' in {seen_ids[agent_id]} and {section}"
                )
            seen_ids[agent_id] = section

            display_name = _normalize_display_name(raw.get("display_name"), agent_id)
            try:
                discord_user_id = _validate_discord_user_id(
                    raw.get("discord_user_id"), require_string=True
                )
            except ValueError as exc:
                raise AuthorityError(
                    f"{section} entry '{agent_id}': {exc}"
                ) from exc

            if discord_user_id in seen_discord_ids:
                raise AuthorityError(
                    f"duplicate discord_user_id '{discord_user_id}' in "
                    f"{seen_discord_ids[discord_user_id]} and {agent_id}"
                )
            seen_discord_ids[discord_user_id] = agent_id

            executor_definition_id = raw.get("executor_definition_id")
            runner_profile_id = raw.get("runner_profile_id")
            enabled_value = raw.get("enabled", True)
            if isinstance(enabled_value, bool):
                enabled = enabled_value
            else:
                raise AuthorityError(f"{section} entry '{agent_id}': enabled must be a boolean")

            if agent_type == "external":
                if (
                    executor_definition_id is not None
                    or runner_profile_id is not None
                    or "enabled" in raw
                ):
                    raise AuthorityError(
                        f"external agent '{agent_id}' must not carry executor bindings"
                    )
                entries.append(AgentEntry(
                    id=agent_id,
                    display_name=display_name,
                    discord_user_id=discord_user_id,
                    agent_type=agent_type,
                ))
                continue

            if executor_definition_id is None and runner_profile_id is None:
                entries.append(AgentEntry(
                    id=agent_id,
                    display_name=display_name,
                    discord_user_id=discord_user_id,
                    agent_type=agent_type,
                ))
                continue

            if executor_definition_id is None or runner_profile_id is None:
                raise AuthorityError(
                    f"agent '{agent_id}' must set both executor_definition_id and runner_profile_id"
                )

            try:
                executor_definition_id = _validate_bounded_label(
                    executor_definition_id, f"agent '{agent_id}'.executor_definition_id"
                )
                runner_profile_id = _validate_bounded_label(
                    runner_profile_id, f"agent '{agent_id}'.runner_profile_id"
                )
            except ValueError as exc:
                raise AuthorityError(str(exc)) from exc

            if executor_definition_id not in definition_ids:
                raise AuthorityError(
                    f"agent '{agent_id}' references unknown executor_definition_id: {executor_definition_id!r}"
                )
            if runner_profile_id != agent_id:
                raise AuthorityError(
                    f"agent '{agent_id}' runner_profile_id must equal agent_id: got {runner_profile_id!r}"
                )

            entries.append(AgentEntry(
                id=agent_id,
                display_name=display_name,
                discord_user_id=discord_user_id,
                agent_type=agent_type,
                executor_definition_id=executor_definition_id,
                runner_profile_id=runner_profile_id,
            ))
            bindings.append(ExecutorInstanceBinding(
                agent_id=agent_id,
                executor_definition_id=executor_definition_id,
                runner_profile_id=runner_profile_id,
                enabled=enabled,
            ))

    source_hash = canonical_hash(entries)
    executor_catalog_hash = canonical_executor_catalog_hash(
        source_id, source_version, definitions, bindings
    )
    return Authority(
        source_id=source_id,
        source_version=source_version,
        entries=entries,
        source_hash=source_hash,
        executor_definitions=definitions,
        executor_bindings=bindings,
        executor_catalog_hash=executor_catalog_hash,
    )


def project_runtime_roster(path: str | Path) -> tuple[list[AgentEntry], list[str]]:
    """Project Coordinate's canonical fields from a private runtime TOML.

    Extra fields are ignored. Missing/invalid entries are reported as redacted
    errors rather than silently skipped.
    """
    try:
        data = _load_toml(path)
    except Exception as exc:
        return [], [f"cannot parse runtime TOML: {exc}"]

    entries: list[AgentEntry] = []
    errors: list[str] = []
    seen_ids: dict[str, str] = {}
    seen_discord_ids: dict[str, str] = {}

    for agent_type in ("managed", "external"):
        section = _authority_section_label(agent_type)
        section_data = data.get(section)
        if section_data is None:
            continue
        if not isinstance(section_data, list):
            errors.append(f"{section} must be a list of tables")
            continue

        for idx, raw in enumerate(section_data):
            if not isinstance(raw, dict):
                errors.append(f"{section} entry {idx} must be a table")
                continue
            try:
                agent_id = _normalize_id(raw.get("id"))
            except ValueError:
                errors.append(f"{section} entry {idx} missing 'id'")
                continue

            if agent_id in seen_ids:
                errors.append(
                    f"duplicate agent id '{agent_id}' in {seen_ids[agent_id]} and {section}"
                )
                continue
            seen_ids[agent_id] = section

            display_name = _normalize_display_name(raw.get("display_name"), agent_id)
            discord_user_id_value = raw.get("discord_user_id")
            if discord_user_id_value is None:
                errors.append(f"{section} entry '{agent_id}' missing discord_user_id")
                continue
            try:
                discord_user_id = _validate_discord_user_id(discord_user_id_value)
            except ValueError as exc:
                errors.append(f"{section} entry '{agent_id}': {exc}")
                continue

            if discord_user_id in seen_discord_ids:
                errors.append(
                    f"duplicate discord_user_id in {seen_discord_ids[discord_user_id]} and {agent_id}"
                )
                continue
            seen_discord_ids[discord_user_id] = agent_id

            entries.append(AgentEntry(
                id=agent_id,
                display_name=display_name,
                discord_user_id=discord_user_id,
                agent_type=agent_type,
            ))

    return entries, errors


def _diff_rosters(
    authority_entries: list[AgentEntry], runtime_entries: list[AgentEntry]
) -> list[dict[str, Any]]:
    authority_by_id = {e.id: e for e in authority_entries}
    runtime_by_id = {e.id: e for e in runtime_entries}
    diff: list[dict[str, Any]] = []

    for agent_id in sorted(set(authority_by_id) | set(runtime_by_id)):
        a = authority_by_id.get(agent_id)
        r = runtime_by_id.get(agent_id)
        if a is None:
            diff.append({"id": agent_id, "status": "removed"})
        elif r is None:
            diff.append({"id": agent_id, "status": "added"})
        else:
            changes: dict[str, tuple[str, str]] = {}
            for field_name in ("display_name", "discord_user_id", "agent_type"):
                av = getattr(a, field_name)
                rv = getattr(r, field_name)
                if av != rv:
                    changes[field_name] = (rv, av)
            if changes:
                diff.append({"id": agent_id, "status": "changed", "fields": list(changes.keys())})

    return diff


def _runtime_adapter_by_id(path: str | Path) -> dict[str, str]:
    """Return {agent_id: adapter} for managed agents in a runtime TOML."""
    try:
        data = _load_toml(path)
    except Exception:
        return {}
    adapters: dict[str, str] = {}
    for raw in data.get("agents", []):
        if not isinstance(raw, dict):
            continue
        agent_id = str(raw.get("id", "")).strip()
        adapter = raw.get("adapter")
        if agent_id and isinstance(adapter, str):
            adapters[agent_id] = adapter.strip()
    return adapters


def verify_parity(authority_path: str | Path, runtime_path: str | Path) -> ParityResult:
    """Compare the authority to the private runtime projection.

    Verifies both roster parity and executor-binding
    adapter parity.  The executor catalog canonical hash cannot be reproduced
    from the runtime config because the runtime lacks the authority's provider
    labels, so this function checks that every managed authority binding has a
    matching runtime agent whose adapter label agrees.
    """
    try:
        authority = load_authority(authority_path)
    except AuthorityError as exc:
        return ParityResult(ok=False, errors=[f"authority: {exc}"])

    runtime_entries, runtime_errors = project_runtime_roster(runtime_path)
    runtime_adapters = _runtime_adapter_by_id(runtime_path)
    result = ParityResult(
        ok=False,
        authority=authority,
        authority_count=len(authority.entries),
        runtime_count=len(runtime_entries),
        source_hash=authority.source_hash,
        authority_hash=authority.source_hash,
        executor_catalog_hash=authority.executor_catalog_hash,
        errors=[f"runtime: {e}" for e in runtime_errors],
    )
    if runtime_errors:
        return result

    diff = _diff_rosters(authority.entries, runtime_entries)
    if diff:
        result.diff = diff
        return result

    runtime_hash = canonical_hash(runtime_entries)
    if runtime_hash != authority.source_hash:
        result.errors.append("canonical roster hash mismatch")
        return result

    # Executor binding adapter parity.
    authority_bindings_by_agent = {b.agent_id: b for b in authority.executor_bindings}
    for agent_id, binding in authority_bindings_by_agent.items():
        runtime_adapter = runtime_adapters.get(agent_id)
        if runtime_adapter is None:
            result.errors.append(
                f"executor binding parity: missing runtime agent '{agent_id}'"
            )
            continue
        definition = next(
            (d for d in authority.executor_definitions if d.id == binding.executor_definition_id),
            None,
        )
        if definition is None:
            result.errors.append(
                f"executor binding parity: unknown definition '{binding.executor_definition_id}'"
            )
            continue
        if runtime_adapter != definition.adapter:
            result.errors.append(
                f"executor binding parity: agent '{agent_id}' runtime adapter "
                f"{runtime_adapter!r} != authority {definition.adapter!r}"
            )

    if not result.errors:
        result.ok = True
    return result


def _cmd_verify(args: argparse.Namespace) -> int:
    result = verify_parity(args.authority, args.runtime)
    print(_canonical_json(result.to_dict()))
    return 0 if result.ok else 1


def _cmd_show(args: argparse.Namespace) -> int:
    try:
        authority = load_authority(args.authority)
    except AuthorityError as exc:
        print(_canonical_json({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(_canonical_json({
        "ok": True,
        "source_id": authority.source_id,
        "source_version": authority.source_version,
        "source_hash": authority.source_hash,
        "executor_catalog_hash": authority.executor_catalog_hash,
        "count": len(authority.entries),
        "entries": [e.to_canonical_dict() for e in sorted(authority.entries, key=lambda e: e.id)],
        "executor_definitions": [d.to_canonical_dict() for d in sorted(
            authority.executor_definitions, key=lambda d: d.id
        )],
        "executor_bindings": [b.to_canonical_dict() for b in sorted(
            authority.executor_bindings, key=lambda b: b.agent_id
        )],
    }))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify parity between the canonical agent registry authority and a runtime config."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify = subparsers.add_parser("verify", help="Compare authority and runtime TOML")
    verify.add_argument("--authority", required=True)
    verify.add_argument("--runtime", required=True)
    verify.set_defaults(func=_cmd_verify)

    show = subparsers.add_parser("show", help="Emit safe authority evidence as JSON")
    show.add_argument("--authority", required=True)
    show.set_defaults(func=_cmd_show)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
