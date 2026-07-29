"""Session scope key helpers for channel, thread, and coordinator task sessions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

try:
    import discord
except ImportError:  # pragma: no cover - exercised in minimal worker runtimes
    discord = None


@dataclass(frozen=True)
class ScopeDescription:
    scope_id: str
    kind: str
    label: str
    detail: str


# Opaque scope ids are bounded by Coordinate's execution_context contract.
MAX_SCOPE_LEN = 256
_SAFE_SCOPE_RE = re.compile(r"^[A-Za-z0-9_.:/-]+$")


def _validate_scope_component(value: str, label: str) -> str:
    """Fail closed on empty, over-long, or unsafe scope components."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is required")
    value = value.strip()
    if not value:
        raise ValueError(f"{label} must be a non-empty string")
    if len(value) > MAX_SCOPE_LEN:
        raise ValueError(f"{label} exceeds {MAX_SCOPE_LEN} characters")
    if not _SAFE_SCOPE_RE.match(value):
        raise ValueError(f"{label} contains unsafe characters: {value!r}")
    return value


def _validate_final_scope(scope_id: str) -> str:
    """Fail closed when the assembled scope exceeds Coordinate's hard limit."""
    if not isinstance(scope_id, str) or not scope_id:
        raise ValueError("scope_id is required")
    if len(scope_id) > MAX_SCOPE_LEN:
        raise ValueError(f"scope_id exceeds {MAX_SCOPE_LEN} characters")
    if not _SAFE_SCOPE_RE.match(scope_id):
        raise ValueError(f"scope_id contains unsafe characters: {scope_id!r}")
    return scope_id


def channel_scope(channel_id: int | str) -> str:
    return f"channel:{channel_id}"


def thread_scope(thread_id: int | str) -> str:
    return f"thread:{thread_id}"


def task_scope(workspace_id: str, task_id: str) -> str:
    return f"task:{workspace_id}:{task_id}"


def workspace_channel_context_scope(
    workspace_id: str, platform: str, channel_id: int | str
) -> str:
    """Workspace-qualified context scope: parent channel for threads."""
    validated_platform = _validate_scope_component(platform, 'platform').lower()
    scope_id = (
        f"workspace:{_validate_scope_component(workspace_id, 'workspace_id')}:"
        f"{validated_platform}:"
        f"channel:{_validate_scope_component(str(channel_id), 'channel_id')}"
    )
    return _validate_final_scope(scope_id)


def workspace_channel_session_scope(
    workspace_id: str, platform: str, channel_id: int | str
) -> str:
    """Workspace-qualified provider session scope: thread id for threads."""
    validated_platform = _validate_scope_component(platform, 'platform').lower()
    scope_id = (
        f"workspace:{_validate_scope_component(workspace_id, 'workspace_id')}:"
        f"{validated_platform}:"
        f"channel:{_validate_scope_component(str(channel_id), 'channel_id')}"
    )
    return _validate_final_scope(scope_id)


def workspace_discord_thread_scope(workspace_id: str, thread_id: int | str) -> str:
    """Workspace-qualified Discord thread scope."""
    scope_id = (
        f"workspace:{_validate_scope_component(workspace_id, 'workspace_id')}:"
        f"discord:thread:{_validate_scope_component(str(thread_id), 'thread_id')}"
    )
    return _validate_final_scope(scope_id)


def scope_for_channel(channel: Any) -> str:
    if is_thread_channel(channel):
        return thread_scope(channel.id)
    return channel_scope(channel.id)


def scope_for_channel_id(channel_id: int | str, *, is_thread: bool = False) -> str:
    if is_thread:
        return thread_scope(channel_id)
    return channel_scope(channel_id)


def legacy_scope_for_channel_id(channel_id: int | str) -> str:
    return str(channel_id)


def is_thread_channel(channel: Any) -> bool:
    if discord is None:
        return False
    return isinstance(channel, discord.Thread)


def describe_scope(scope_id: str) -> ScopeDescription:
    if scope_id.startswith("workspace:"):
        return ScopeDescription(
            scope_id=scope_id,
            kind="workspace",
            label="workspace-qualified scope",
            detail=scope_id.removeprefix("workspace:"),
        )
    if scope_id.startswith("channel:"):
        return ScopeDescription(
            scope_id=scope_id,
            kind="channel",
            label="channel scope",
            detail=scope_id.removeprefix("channel:"),
        )
    if scope_id.startswith("thread:"):
        return ScopeDescription(
            scope_id=scope_id,
            kind="thread",
            label="thread scope",
            detail=scope_id.removeprefix("thread:"),
        )
    if scope_id.startswith("task:"):
        parts = scope_id.split(":", 2)
        detail = parts[2] if len(parts) == 3 else scope_id.removeprefix("task:")
        return ScopeDescription(
            scope_id=scope_id,
            kind="task",
            label="task scope",
            detail=detail,
        )
    return ScopeDescription(
        scope_id=scope_id,
        kind="legacy",
        label="legacy channel scope",
        detail=scope_id,
    )
