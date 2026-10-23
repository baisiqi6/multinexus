"""Immutable desktop profiles and guarded private-file loading."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Any, Mapping
import uuid

from ..channel_mcp_transport import ChannelTransportError, _validate_endpoint

MAX_PRIVATE_BYTES = 1024 * 1024
_PROFILE_FIELDS = {"version", "workspace_id", "channel_id", "mcp_url", "channel_profiles", "lifecycle_profile", "bindings", "local_alias", "sender_expectations"}
_BINDING_FIELDS = {"kind", "agent_id", "session_id", "cwd", "revision"}
_RUNTIME_TOOLS = {"coordinate.runtime_request_submit", "coordinate.runtime_job_get"}


class NativeIdentityError(ValueError):
    """A definite fixed native identity/configuration mismatch before enqueue."""


def _invalid() -> ValueError:
    return ValueError("Invalid private desktop configuration.")


def required_string(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise _invalid()
    return value


def absolute_path(value: Any) -> Path:
    path = Path(required_string(str(value) if isinstance(value, Path) else value))
    if not path.is_absolute():
        raise _invalid()
    return path


def read_private_bytes(path: str | Path) -> bytes:
    """Open each component without following symlinks; check the opened file."""
    path = absolute_path(path)
    descriptors = []
    try:
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(directory)
        for part in path.parts[1:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            descriptors.append(directory)
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        descriptors.append(descriptor)
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077 or metadata.st_size > MAX_PRIVATE_BYTES):
            raise _invalid()
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            data = stream.read(MAX_PRIVATE_BYTES + 1)
        if len(data) > MAX_PRIVATE_BYTES:
            raise _invalid()
        return data
    except (OSError, TypeError, UnicodeError):
        raise _invalid() from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    data = {}
    for key, value in pairs:
        if key in data:
            raise _invalid()
        data[key] = value
    return data


def read_private_json(path: str | Path) -> dict:
    try:
        data = json.loads(read_private_bytes(path).decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(_invalid()))
    except (UnicodeError, ValueError, RecursionError):
        raise _invalid() from None
    if not isinstance(data, dict):
        raise _invalid()
    return data


def binding_digest(binding: dict) -> str:
    try:
        canonical = json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, UnicodeError):
        raise _invalid() from None


@dataclass(frozen=True)
class DesktopBinding:
    alias: str
    kind: str
    agent_id: str
    session_id: str
    cwd: str
    revision: str

    def as_dict(self) -> dict[str, str]:
        return {field: getattr(self, field) for field in ("alias", "kind", "agent_id", "session_id", "cwd", "revision")}

    @property
    def digest(self) -> str:
        return binding_digest(self.as_dict())


@dataclass(frozen=True)
class DesktopProfile:
    local_alias: str
    sender_expectations: Mapping[str, Mapping[str, str]]
    workspace_id: str
    channel_id: str
    mcp_url: str
    channel_profiles: Mapping[str, Path]
    lifecycle_profile: Path
    bindings: Mapping[str, DesktopBinding]


def _binding(alias: str, data: Any) -> DesktopBinding:
    if not isinstance(data, dict) or set(data) != _BINDING_FIELDS or not isinstance(data["kind"], str) or data["kind"] not in {"codex", "dsh"}:
        raise _invalid()
    for value in data.values():
        required_string(value)
    absolute_path(data["cwd"])
    native_id = data["session_id"]
    if data["kind"] == "dsh":
        if not native_id.startswith("session-"):
            raise _invalid()
        native_id = native_id[len("session-"):]
    try:
        if str(uuid.UUID(native_id)) != native_id:
            raise _invalid()
    except ValueError:
        raise _invalid() from None
    return DesktopBinding(alias=alias, **data)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
        raise _invalid()
    return value


def credential_config(profile: DesktopProfile, alias: str | None = None) -> dict:
    if alias is not None and alias != profile.local_alias:
        raise ValueError("Only the local sender capability is available.")
    data = read_private_json(profile.lifecycle_profile if alias is None else profile.channel_profiles[alias])
    try:
        _validate_endpoint(data.get("url"))
    except ChannelTransportError:
        raise _invalid() from None
    if data["url"] != profile.mcp_url:
        raise _invalid()
    required_string(data.get("client_id"))
    absolute_path(data.get("token_file"))
    tools = set(_string_list(data.get("tools")))
    if alias is None:
        if (profile.workspace_id not in _string_list(data.get("workspace_ids"))
                or "none" not in _string_list(data.get("platforms")) or not _RUNTIME_TOOLS <= tools):
            raise _invalid()
    elif (data["client_id"] != profile.sender_expectations[alias]["client_id"]
          or data.get("workspace_id") != profile.workspace_id or data.get("channel_id") != profile.channel_id
          or data["url"] != profile.mcp_url or "coordinate.channel_send" not in tools
          or ("client" in data and data["client"] != profile.bindings[alias].kind)):
        raise _invalid()
    if "bypass_system_proxy_for_tailnet" in data and type(data["bypass_system_proxy_for_tailnet"]) is not bool:
        raise _invalid()
    return data


def load_profile(path: str | Path) -> DesktopProfile:
    data = read_private_json(path)
    if type(data.get("version")) is int and data["version"] == 1:
        raise ValueError("Desktop profile version 1 requires an identity upgrade before native operations.")
    if set(data) != _PROFILE_FIELDS or type(data["version"]) is not int or data["version"] != 2:
        raise _invalid()
    for name in ("local_alias", "workspace_id", "channel_id", "mcp_url"):
        required_string(data[name])
    profiles, bindings = data["channel_profiles"], data["bindings"]
    if not isinstance(profiles, dict) or not isinstance(bindings, dict) or not bindings or set(profiles) != {data["local_alias"]} or data["local_alias"] not in bindings:
        raise _invalid()
    expectations = data["sender_expectations"]
    if not isinstance(expectations, dict) or set(expectations) != set(bindings):
        raise _invalid()
    for value in expectations.values():
        validate_sender_tuple(value)
    parsed = {}
    for alias, value in bindings.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", alias):
            raise _invalid()
        parsed[alias] = _binding(alias, value)
    for field in ("agent_id", "session_id"):
        if len({getattr(x, field) for x in parsed.values()}) != len(parsed):
            raise _invalid()
    profile = DesktopProfile(data["local_alias"], MappingProxyType({k: MappingProxyType(dict(v)) for k, v in expectations.items()}), data["workspace_id"], data["channel_id"], data["mcp_url"],
        MappingProxyType({alias: absolute_path(value) for alias, value in profiles.items()}),
        absolute_path(data["lifecycle_profile"]), MappingProxyType(parsed))
    credential_config(profile, profile.local_alias)
    credential_config(profile)
    return profile


def validate_sender_tuple(value):
    if not isinstance(value, (dict, MappingProxyType)) or set(value) != {"client_id", "agent_id", "discord_user_id"}:
        raise _invalid()
    for key in ("client_id", "agent_id"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value[key]):
            raise _invalid()
    if not isinstance(value["discord_user_id"], str) or not re.fullmatch(r"[1-9][0-9]{0,19}", value["discord_user_id"]):
        raise _invalid()


def validate_sender_identity(identity, actual_author, expectation):
    fields = {"version", "client_id", "agent_id", "discord_user_id", "registry_source_id", "registry_source_version", "registry_source_hash"}
    if (not isinstance(identity, dict) or set(identity) != fields
            or type(identity["version"]) is not int or identity["version"] != 1
            or type(identity["registry_source_version"]) is not int or identity["registry_source_version"] < 0
            or not isinstance(identity["registry_source_hash"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", identity["registry_source_hash"])):
        raise _invalid()
    required_string(identity["registry_source_id"])
    actual = {key: identity[key] for key in ("client_id", "agent_id", "discord_user_id")}
    validate_sender_tuple(actual)
    if actual != dict(expectation) or actual_author != identity["discord_user_id"]:
        raise ValueError("Sender identity mismatch.")
    return dict(identity)


def verified_receipt(profile, alias, receipt):
    if (alias != profile.local_alias or not isinstance(receipt, dict) or receipt.get("status") != "sent"
            or receipt.get("identity_status") != "verified" or receipt.get("binding_current") is not True):
        raise ValueError("A verified current local sender receipt is required.")
    return validate_sender_identity(receipt.get("sender_identity"), receipt.get("actual_author_bot_id"),
                                    profile.sender_expectations[alias])
