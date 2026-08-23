from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Canonical error/timeout prefixes produced by adapter wrappers and the
# bridge itself. This is only the legacy/external compatibility seam; built-in
# adapters written against Contract V1 settle an explicit machine outcome.
ERROR_PREFIXES = (
    "Agent error:",
    "OpenCode CLI failed",
    "OpenCode timed out",
    "OpenCode returned no text",
    "OpenCode CLI not found",
    "Codex CLI failed",
    "Codex timed out",
    "Codex stopped responding",
    "Codex resume failed",
    "Codex CLI not found",
    "Codex model capacity",
    "Hermes CLI failed",
    "Hermes timed out",
    "Hermes CLI not found",
    "Claude CLI failed",
    "Claude error:",
    "Claude timeout",
    "Claude CLI not found",
    "omp CLI failed",
    "omp timed out",
    "omp CLI not found",
    "Qoder CLI failed",
    "Qoder timeout after",
    "Qoder error:",
    "Qoder resume failed:",
    "Qoder CLI not found",
    "Grok CLI failed",
    "Grok timeout after",
    "Grok error:",
    "Grok resume failed:",
    "Grok CLI not found",
    "ACP error:",
    "ACP timeout after",
    "ACP command not found",
    "ACP protocol mismatch",
    "ACP resume failed closed",
)


# Exact sentinel adapters emit when a provider turn produced no deliverable
# text (e.g. a permission-cancelled turn with empty output). A turn without a
# deliverable result must never become a success terminal state, so the exact
# match (whitespace-insensitive) is classified as an error. It intentionally
# lives OUTSIDE ERROR_PREFIXES: a prefix match would misclassify legitimate
# text that merely starts with the sentinel.
NO_RESPONSE_SENTINEL = "(no response)"


def is_error_text(text: str) -> bool:
    """True for a canonical adapter/bridge error prefix, or the exact
    empty-response sentinel ``(no response)`` (whitespace-insensitive)."""
    return text.strip() == NO_RESPONSE_SENTINEL or any(
        text.startswith(prefix) for prefix in ERROR_PREFIXES
    )


# Machine outcome vocabulary (Contract V1). ``None`` is accepted only as a
# legacy compatibility input; consumers must read ``AdapterResult.
# effective_outcome()`` instead of reclassifying user-visible text.
OUTCOME_SUCCESS = "success"
OUTCOME_FAILED = "failed"
OUTCOME_TIMED_OUT = "timed_out"
OUTCOMES = (OUTCOME_SUCCESS, OUTCOME_FAILED, OUTCOME_TIMED_OUT)

# Stable, non-empty failure categories (Contract V1). Small closed set;
# provider wording is never used as a category.
FAILURE_CATEGORIES = (
    "unavailable",
    "timeout",
    "process_error",
    "protocol_error",
    "provider_error",
    "no_response",
    "internal_error",
)
CATEGORY_TIMEOUT = "timeout"

# Bounded diagnostic contract: at most DIAGNOSTIC_MAX_BYTES UTF-8 bytes,
# with the fixed truncation marker reserved at the tail.
DIAGNOSTIC_MAX_BYTES = 4096
DIAGNOSTIC_TRUNCATION_MARKER = "\n[diagnostic truncated]"


def bounded_diagnostic(text: str, *, max_bytes: int = DIAGNOSTIC_MAX_BYTES) -> str:
    """Return ``text`` bounded to ``max_bytes`` UTF-8 bytes.

    Unchanged when it already fits; otherwise truncate on a UTF-8 byte
    boundary while reserving room for the fixed truncation marker. Never
    splits a multi-byte character and never emits replacement characters.
    """
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    marker = DIAGNOSTIC_TRUNCATION_MARKER.encode("utf-8")
    if max_bytes <= len(marker):
        # Marker cannot fit inside the budget; still return valid UTF-8.
        return data[:max_bytes].decode("utf-8", errors="ignore")
    limit = max_bytes - len(marker)
    return data[:limit].decode("utf-8", errors="ignore") + DIAGNOSTIC_TRUNCATION_MARKER


def safe_exception_diagnostic(exc: BaseException) -> str:
    """Return a non-secret diagnostic for an unexpected exception.

    Exception messages can contain command arguments, paths, provider payloads,
    or credentials. Full details belong in the protected process log; durable
    result envelopes expose only the exception type.
    """
    return type(exc).__name__


@dataclass
class AdapterResult:
    """Unified return type for all agent adapter calls.

    ``outcome`` is the machine settlement (success/failed/timed_out);
    ``None`` is accepted only as a legacy compatibility input. Consumers
    must read ``effective_outcome()`` instead of reclassifying ``text``.
    """

    text: str
    session_id: str | None = None
    resumed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    outcome: str | None = None
    error_category: str | None = None
    diagnostic: str = ""

    def __post_init__(self) -> None:
        if self.outcome is not None and self.outcome not in OUTCOMES:
            raise ValueError(
                f"invalid outcome {self.outcome!r}; expected one of {OUTCOMES}"
            )
        if (
            self.error_category is not None
            and self.error_category not in FAILURE_CATEGORIES
        ):
            raise ValueError(
                f"invalid error_category {self.error_category!r}; "
                f"expected one of {FAILURE_CATEGORIES}"
            )
        if self.outcome is None:
            if self.error_category is not None:
                raise ValueError(
                    "error_category requires an explicit failed/timed_out outcome"
                )
        elif self.outcome == OUTCOME_SUCCESS:
            if self.error_category is not None:
                raise ValueError("success outcome must not carry an error_category")
        elif self.outcome == OUTCOME_TIMED_OUT:
            if self.error_category is not None and self.error_category != CATEGORY_TIMEOUT:
                raise ValueError(
                    "timed_out outcome requires error_category 'timeout'"
                )
            self.error_category = CATEGORY_TIMEOUT
        elif self.error_category is None:
            raise ValueError("failed outcome requires a non-empty error_category")
        self.diagnostic = bounded_diagnostic(self.diagnostic)

    def effective_outcome(self) -> str:
        """Machine settlement for this result (single normalization seam).

        Priority: explicit outcome; legacy ``metadata["timeout"]``; legacy
        ``Claude timeout:`` text; other legacy error prefixes; otherwise
        success.
        """
        if self.outcome is not None:
            return self.outcome
        if self.metadata.get("timeout"):
            return OUTCOME_TIMED_OUT
        if self.text.startswith("Claude timeout:"):
            return OUTCOME_TIMED_OUT
        if is_error_text(self.text):
            return OUTCOME_FAILED
        return OUTCOME_SUCCESS


def failed_result(text: str, *, category: str, **kwargs: Any) -> AdapterResult:
    """Explicit ``failed`` settlement with a stable category.

    Helper for adapter failure paths: the machine terminal state is written
    explicitly and never derived from the user-visible text. ``text`` stays
    the safe user-visible summary; extra AdapterResult fields (session_id,
    metadata, ...) pass through as keyword arguments.
    """
    return AdapterResult(
        text=text, outcome=OUTCOME_FAILED, error_category=category, **kwargs
    )


def timed_out_result(text: str, **kwargs: Any) -> AdapterResult:
    """Explicit ``timed_out`` settlement (category fixed to ``timeout``)."""
    return AdapterResult(text=text, outcome=OUTCOME_TIMED_OUT, **kwargs)


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
