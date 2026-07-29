from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AdapterResult:
    """Unified return type for all agent adapter calls."""

    text: str
    session_id: str | None = None
    resumed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class AgentAdapter(ABC):
    """Base class for agent backends (CLI subprocess wrappers)."""

    def __init__(self, name: str, timeout: int = 360):
        self.name = name
        self.timeout = timeout

    @abstractmethod
    async def call(
        self,
        prompt: str,
        *,
        timeout: int | None = None,
        work_dir: str | None = None,
        on_progress: Callable[[str | dict[str, Any]], None] | None = None,
    ) -> AdapterResult:
        """Send prompt to the agent and return an AdapterResult."""
        ...

    async def resume(
        self,
        session_id: str,
        prompt: str,
        *,
        timeout: int | None = None,
        work_dir: str | None = None,
        on_progress: Callable[[str | dict[str, Any]], None] | None = None,
    ) -> AdapterResult:
        """Resume a previous session. Default: fallback to fresh call()."""
        return await self.call(
            prompt, timeout=timeout, work_dir=work_dir, on_progress=on_progress
        )

    @abstractmethod
    async def health_check(self) -> dict:
        """Check if the agent backend is available. Returns status dict."""
        ...
