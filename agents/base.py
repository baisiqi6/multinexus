"""Base agent abstract class and exceptions for multinexus.

All agent backends must inherit from BaseAgent and implement call() and health_check().
"""

from abc import ABC, abstractmethod


class BaseAgent(ABC):
    """Abstract base for all agent backends."""

    def __init__(self, name: str, timeout: int = 60):
        self.name = name
        self.timeout = timeout

    @abstractmethod
    async def call(self, messages: list[dict], system_prompt: str) -> str:
        """Send messages to the agent and return the response text.

        Args:
            messages: List of {"role": "user"|"assistant", "content": "..."} dicts.
            system_prompt: The system prompt for this agent.

        Returns:
            The agent's response as a string.

        Raises:
            AgentOfflineError: If the agent backend is unreachable.
            AgentTimeoutError: If the agent doesn't respond within timeout.
        """

    async def kill(self) -> None:
        """Interrupt an in-progress call. No-op by default; override for subprocess/HTTP agents."""

    @abstractmethod
    async def health_check(self) -> dict:
        """Check if the agent backend is reachable.

        Returns:
            {"status": "ok", "model": "model-name"} or
            {"status": "offline", "error": "reason"}
        """


class AgentOfflineError(Exception):
    """Raised when the agent backend is unreachable."""


class AgentRateLimitError(AgentOfflineError):
    """Raised when an agent's usage or rate limit is exceeded.

    Subclasses AgentOfflineError so existing offline handlers still catch it,
    but specific handlers can intercept first to trigger fallback routing.
    """


class AgentTimeoutError(Exception):
    """Raised when the agent doesn't respond within timeout."""
