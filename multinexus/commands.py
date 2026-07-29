"""Operator commands: session status/reset, agents listing, health check."""

import time

from .sessions.scope import (
    describe_scope,
    legacy_scope_for_channel_id,
    scope_for_channel_id,
)

OPERATOR_COMMANDS = {"session status", "session reset", "agents", "health"}


def parse_operator_command(text: str) -> str | None:
    cleaned = text.strip().lower()
    for cmd in OPERATOR_COMMANDS:
        if cleaned == cmd:
            return cmd
    return None


def is_dangerous_command(cmd: str) -> bool:
    return cmd == "session reset"


def can_run_operator_command(config, user_id: int, cmd: str) -> str | None:
    """Return None if allowed, otherwise a denial reason string."""
    if config.allowed_user_ids and user_id not in config.allowed_user_ids:
        return "无权限：此命令需要 operator 权限。"
    if is_dangerous_command(cmd):
        if not config.allowed_user_ids or user_id not in config.allowed_user_ids:
            return "无权限：此命令需要显式 operator 权限。"
    return None


async def handle_operator_command(
    cmd: str, client, channel_id: int, *, is_thread: bool = False
) -> str:
    if cmd == "session status":
        return _cmd_session_status(client, channel_id, is_thread=is_thread)
    if cmd == "session reset":
        return _cmd_session_reset(client, channel_id, is_thread=is_thread)
    if cmd == "agents":
        return _cmd_agents(client)
    if cmd == "health":
        return await _cmd_health(client)
    return "未知命令。"


def _cmd_session_status(client, channel_id: int, *, is_thread: bool = False) -> str:
    scope_id = scope_for_channel_id(channel_id, is_thread=is_thread)
    legacy_scope_id = legacy_scope_for_channel_id(channel_id)
    agent_id = client.agent_config.id
    current = client.session_store.get_first_active(
        scope_ids=(scope_id, legacy_scope_id),
        agent_id=agent_id,
    )
    all_sessions = client.session_store.list_by_agent(agent_id=agent_id)
    scope_desc = describe_scope(current["scope_id"] if current else scope_id)

    lines = [f"**会话状态** — {agent_id}\n"]
    if current:
        sid = current["session_id"]
        lines.append(f"**当前 scope**（{scope_desc.label} `{scope_desc.detail}`）：")
        lines.append(f"  scope: `{current['scope_id']}`")
        lines.append(f"  scope_type: {scope_desc.label}")
        lines.append(f"  session_id: `{sid[:16]}...`" if len(sid) > 16 else f"  session_id: `{sid}`")
        lines.append(f"  adapter: {current['adapter']}")
        lines.append(f"  work_dir: {current['work_dir'] or '(none)'}")
        lines.append(f"  status: {current['status']}")
        lines.append(f"  轮次: {current['turn_count']}")
        lines.append(f"  更新时间: {_fmt_time(current['updated_at'])}")
    else:
        lines.append(
            f"当前 scope 没有活跃会话（{scope_desc.label} `{scope_id}`）。"
        )

    lines.append(f"\n活跃会话：共 {len(all_sessions)} 个")
    return "\n".join(lines)


def _cmd_session_reset(client, channel_id: int, *, is_thread: bool = False) -> str:
    scope_id = scope_for_channel_id(channel_id, is_thread=is_thread)
    legacy_scope_id = legacy_scope_for_channel_id(channel_id)
    agent_id = client.agent_config.id
    current = client.session_store.get_first_active(
        scope_ids=(scope_id, legacy_scope_id),
        agent_id=agent_id,
    )
    scope_desc = describe_scope(current["scope_id"] if current else scope_id)
    if not current:
        return f"当前 scope 没有活跃会话（{scope_desc.label} `{scope_id}`）。"

    client.session_store.mark_stale(scope_id=current["scope_id"], agent_id=agent_id)
    return (
        f"**会话已重置** — {agent_id}\n\n"
        f"已将 {scope_desc.label} `{current['scope_id']}` 的会话标记为 stale。\n"
        f"下一次调用会启动新会话。"
    )


def _cmd_agents(client) -> str:
    known = client.agent_config.known_agents
    managed = [a for a in known if a.kind == "managed"]
    external = [a for a in known if a.kind == "external"]

    lines = ["**可用 Agent**\n"]
    if managed:
        lines.append("**托管 Agent：**")
        for a in managed:
            ident = f"discord_id: `{a.discord_user_id}`" if a.discord_user_id else "无 Discord ID"
            lines.append(f"  {a.id} ({a.primary_name}) — {ident}")
    if external:
        lines.append("\n**外部 Gateway Agent：**")
        for a in external:
            ident = f"discord_id: `{a.discord_user_id}`" if a.discord_user_id else "无 Discord ID"
            lines.append(f"  {a.id} ({a.primary_name}) — {ident}")
    return "\n".join(lines)


async def _cmd_health(client) -> str:
    health = await client.adapter.health_check()
    cfg = client.agent_config
    available = health.get("available", False)
    status = "是" if available else "否"
    path = health.get("path") or health.get("bin", "?")
    lines = [
        f"**健康检查** — {cfg.id}\n",
        f"adapter: {health.get('adapter', '?')}",
        f"bin: {health.get('bin', '?')}",
        f"available: {status} (`{path}`)",
        f"work_dir: {cfg.work_dir or '(none)'}",
        f"model: {cfg.model or '(default)'}",
        f"timeout: {cfg.timeout}s",
    ]
    return "\n".join(lines)


def _fmt_time(ts: float) -> str:
    if not ts:
        return "(未知)"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
