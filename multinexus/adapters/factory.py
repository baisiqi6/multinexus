from ..models import AgentConfig
from .acp import ACPAdapter
from .base import AgentAdapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .grok import GrokAdapter
from .hermes import HermesAdapter
from .omp import OmpAdapter
from .opencode import OpenCodeAdapter
from .qoder import QoderAdapter
from .zcode import ZCodeAdapter


def make_adapter(config: AgentConfig) -> AgentAdapter:
    adapter = config.adapter.lower()
    if adapter == "acp":
        return ACPAdapter(config)
    if adapter == "claude":
        return ClaudeAdapter(config)
    if adapter == "codex":
        return CodexAdapter(config)
    if adapter == "grok":
        return GrokAdapter(config)
    if adapter == "hermes":
        return HermesAdapter(config)
    if adapter == "omp":
        return OmpAdapter(config)
    if adapter == "opencode":
        return OpenCodeAdapter(config)
    if adapter == "qoder":
        return QoderAdapter(config)
    if adapter == "zcode":
        return ZCodeAdapter(config)
    raise SystemExit(
        f"Unsupported adapter: {config.adapter}. Available: acp, claude, codex, grok, hermes, omp, opencode, qoder, zcode."
    )
