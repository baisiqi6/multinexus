import sys
from dataclasses import dataclass, field

DEFAULT_OPENCLAW_BIN = "openclaw"
DEFAULT_HERMES_BIN = "hermes"
CODEX_CMD = "codex.cmd" if sys.platform == "win32" else "codex"


@dataclass
class KnownAgentMention:
    id: str
    primary_name: str = ""
    names: set[str] = field(default_factory=set)
    role_ids: set[str] = field(default_factory=set)
    discord_user_id: int | None = None
    kind: str = "managed"


@dataclass
class AgentConfig:
    id: str
    token: str
    adapter: str = "openclaw"
    display_name: str = ""
    aliases: set[str] = field(default_factory=set)
    role_ids: set[str] = field(default_factory=set)
    channels: list[int] = field(default_factory=list)
    respond_to_bots: bool = True
    context_db_path: str = "data/discord_context.sqlite3"
    context_recent_messages: int = 40
    context_budget_chars: int = 12000
    context_max_message_chars: int = 500
    context_ttl_seconds: int = 24 * 60 * 60
    handoff_dedupe_seconds: int = 10 * 60
    timeout: int = 360
    first_byte_timeout: int = 120
    activity_timeout: int = 120
    system_prompt: str = ""
    known_agents: list[KnownAgentMention] = field(default_factory=list)
    work_dir: str | None = None
    model: str | None = None
    openclaw_agent_id: str = "main"
    openclaw_bin: str = DEFAULT_OPENCLAW_BIN
    hermes_bin: str = DEFAULT_HERMES_BIN
    hermes_provider: str | None = None
    hermes_toolsets: str | None = None
    hermes_accept_hooks: bool = False
    claude_bin: str = "claude"
    claude_dangerously_skip_permissions: bool = False
    codex_bin: str = CODEX_CMD
    codex_sandbox: str = "danger-full-access"
    codex_dangerously_bypass_approvals_and_sandbox: bool = False
    codex_fallback_model: str | None = None
    opencode_bin: str = "opencode"
    opencode_dangerously_skip_permissions: bool = False
    omp_bin: str = "omp"
    omp_model: str | None = None
    omp_thinking: str | None = None
    omp_auto_approve: bool = True
    qoder_bin: str = "qodercli"
    qoder_reasoning_effort: str | None = None
    qoder_permission_mode: str = "dont_ask"
    grok_bin: str = "grok"
    grok_reasoning_effort: str | None = None
    grok_permission_mode: str = "dontAsk"
    acp_command: str = ""
    acp_args: list[str] = field(default_factory=list)
    allowed_user_ids: list[int] = field(default_factory=list)
    wiki_enabled: bool = False
    wiki_path: str = "wiki"
    discoveries_channel_id: int | None = None
    coordinator_bot_id: int | None = None
    coordinator_cli_path: str = ""
    coordinator_db_path: str = ""
    coordinator_workspace_path: str = ""

    # Agentd (local daemon mode)
    agentd_mode: bool = False
    agentd_port: int = 0  # 0 = auto-assign
    agentd_host: str = "127.0.0.1"

    # KOOK bridge fields
    kook_poll_channel_ids: list[int] = field(default_factory=list)
    kook_poll_interval_seconds: int = 5
    kook_poll_page_size: int = 50
