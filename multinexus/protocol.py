"""Platform-agnostic request/response envelope for bridge <-> agentd communication.

Bridges (Discord, KOOK) produce AgentRequests and submit them to the local agentd.
Agentd processes them via existing adapters and returns AgentResponses.

This module is the contract between the platform layer and the agent runtime layer.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Platform(str, Enum):
    DISCORD = "discord"
    KOOK = "kook"


@dataclass(frozen=True)
class PlatformOrigin:
    """Where the request came from (platform-specific)."""

    platform: Platform
    channel_id: str
    thread_id: str | None = None
    message_id: str = ""
    guild_id: str | None = None

    # KOOK-specific: role_id that triggered the mention
    role_id: str | None = None


@dataclass(frozen=True)
class PlatformDestination:
    """Where the response should go (platform-specific)."""

    platform: Platform
    channel_id: str
    thread_id: str | None = None
    reply_to_message_id: str | None = None

    # KOOK-specific: quote reply
    quote_message_id: str | None = None


@dataclass
class AgentRequest:
    """A platform-agnostic agent request submitted by a bridge to agentd."""

    # Identity
    request_id: str
    agent_id: str

    # Content
    prompt: str
    system_prompt: str = ""

    # Platform routing
    origin: PlatformOrigin | None = None
    destination: PlatformDestination | None = None

    # Author info
    author_id: str = ""
    author_name: str = ""
    author_is_bot: bool = False

    # Session management
    session_scope: str = ""
    legacy_scope_ids: tuple[str, ...] = ()

    # Context
    context_channel_id: str = ""
    context_messages_json: str = ""  # JSON-serialized list of context messages

    # Coordinator handoff fields (set when this is a coordinator handoff)
    handoff_workspace_id: str = ""
    handoff_task_id: str = ""
    handoff_bootstrap_content: str = ""

    # Timing
    created_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    # Config overrides for this request
    timeout: int | None = None
    work_dir: str | None = None

    def to_json(self) -> str:
        d = asdict(self)
        if self.origin:
            d["origin"] = asdict(self.origin)
            d["origin"]["platform"] = self.origin.platform.value
        if self.destination:
            d["destination"] = asdict(self.destination)
            d["destination"]["platform"] = self.destination.platform.value
        d["legacy_scope_ids"] = list(self.legacy_scope_ids)
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> AgentRequest:
        d = json.loads(data)
        if d.get("origin"):
            d["origin"] = PlatformOrigin(
                platform=Platform(d["origin"]["platform"]),
                **{k: v for k, v in d["origin"].items() if k != "platform"},
            )
        if d.get("destination"):
            d["destination"] = PlatformDestination(
                platform=Platform(d["destination"]["platform"]),
                **{k: v for k, v in d["destination"].items() if k != "platform"},
            )
        d["legacy_scope_ids"] = tuple(d.get("legacy_scope_ids") or ())
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class AgentResponse:
    """Response from agentd back to the bridge."""

    request_id: str
    agent_id: str

    # Content
    text: str = ""
    session_id: str = ""
    resumed: bool = False

    # Status
    success: bool = True
    error: str = ""

    # Handoff lines extracted from response (already resolved to platform format)
    handoff_lines: list[str] = field(default_factory=list)

    # Agent report lines
    report_lines: list[str] = field(default_factory=list)

    # Timing
    duration_ms: int = 0

    # Platform destination echo (so bridge knows where to send)
    destination: PlatformDestination | None = None

    # Metadata
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        d = asdict(self)
        if self.destination:
            d["destination"] = asdict(self.destination)
            d["destination"]["platform"] = self.destination.platform.value
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> AgentResponse:
        d = json.loads(data)
        if d.get("destination"):
            d["destination"] = PlatformDestination(
                platform=Platform(d["destination"]["platform"]),
                **{k: v for k, v in d["destination"].items() if k != "platform"},
            )
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
