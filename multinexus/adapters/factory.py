from ..models import AgentConfig
from .base import AgentAdapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .hermes import HermesAdapter
from .omp import OmpAdapter
from .opencode import OpenCodeAdapter


def make_adapter(config: AgentConfig) -> AgentAdapter:
    adapter = config.adapter.lower()
    if adapter == "claude":
        return ClaudeAdapter(config)
    if adapter == "codex":
        return CodexAdapter(config)
    if adapter == "hermes":
        return HermesAdapter(config)
    if adapter == "omp":
        return OmpAdapter(config)
    if adapter == "opencode":
        return OpenCodeAdapter(config)
    raise SystemExit(
        f"Unsupported adapter: {config.adapter}. Available: claude, codex, hermes, omp, opencode."
    )
