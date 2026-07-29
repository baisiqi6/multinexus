"""Embed builders for slash command operator output."""

import time

import discord

from .sessions.scope import (
    describe_scope,
    legacy_scope_for_channel_id,
    scope_for_channel_id,
)


def build_agents_embed(config) -> discord.Embed:
    embed = discord.Embed(title="可用 Agent", color=discord.Color.blurple())
    managed = [a for a in config.known_agents if a.kind == "managed"]
    external = [a for a in config.known_agents if a.kind == "external"]
    if managed:
        lines = []
        for a in managed:
            ident = f"discord_id: `{a.discord_user_id}`" if a.discord_user_id else "无 Discord ID"
            lines.append(f"{a.id} ({a.primary_name}) — {ident}")
        embed.add_field(name="托管 Agent", value="\n".join(lines), inline=False)
    if external:
        lines = []
        for a in external:
            ident = f"discord_id: `{a.discord_user_id}`" if a.discord_user_id else "无 Discord ID"
            lines.append(f"{a.id} ({a.primary_name}) — {ident}")
        embed.add_field(name="外部 Gateway Agent", value="\n".join(lines), inline=False)
    return embed


def build_health_embed(config, health: dict) -> discord.Embed:
    agentd_mode = health.get("agentd_mode", False)
    available = health.get("available", False)
    if agentd_mode:
        color = discord.Color.blurple()
    elif available:
        color = discord.Color.green()
    else:
        color = discord.Color.red()
    suffix = "（agentd 模式）" if agentd_mode else ""
    embed = discord.Embed(
        title=f"健康检查{suffix} — {config.id}",
        color=color,
    )
    embed.add_field(name="适配器", value=health.get("adapter", "?"), inline=True)
    embed.add_field(name="可执行文件", value=health.get("bin", "?"), inline=True)
    if agentd_mode:
        embed.add_field(name="状态", value="由 coordinate runtime 管理", inline=True)
    else:
        embed.add_field(name="可用", value="是" if available else "否", inline=True)
    embed.add_field(name="工作目录", value=config.work_dir or "(none)", inline=True)
    embed.add_field(name="模型", value=config.model or "(default)", inline=True)
    embed.add_field(name="超时", value=f"{config.timeout}s", inline=True)
    if not agentd_mode:
        path = health.get("path") or health.get("bin", "?")
        embed.add_field(name="路径", value=f"`{path}`", inline=False)
    if health.get("error"):
        embed.add_field(name="错误", value=str(health["error"])[:1024], inline=False)
    return embed


def build_session_status_embed(
    client, channel_id: int, *, is_thread: bool = False
) -> discord.Embed:
    scope_id = scope_for_channel_id(channel_id, is_thread=is_thread)
    legacy_scope_id = legacy_scope_for_channel_id(channel_id)
    agent_id = client.agent_config.id
    if client.session_store is None:
        scope_desc = describe_scope(scope_id)
        embed = discord.Embed(
            title=f"会话状态（agentd 模式） — {agent_id}",
            description="agentd 模式下会话由 coordinate runtime 管理，本地不维护 session store。",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="scope", value=scope_id, inline=True)
        embed.add_field(name="scope_type", value=scope_desc.label, inline=True)
        return embed
    current = client.session_store.get_first_active(
        scope_ids=(scope_id, legacy_scope_id),
        agent_id=agent_id,
    )
    all_sessions = client.session_store.list_by_agent(agent_id=agent_id)
    scope_desc = describe_scope(current["scope_id"] if current else scope_id)

    if current:
        embed = discord.Embed(
            title=f"会话状态 — {agent_id}",
            color=discord.Color.green(),
        )
        sid = current["session_id"]
        embed.add_field(name="scope", value=current["scope_id"], inline=True)
        embed.add_field(name="scope_type", value=scope_desc.label, inline=True)
        embed.add_field(
            name="session_id",
            value=f"`{sid[:16]}...`" if len(sid) > 16 else f"`{sid}`",
            inline=True,
        )
        embed.add_field(name="适配器", value=current["adapter"], inline=True)
        embed.add_field(name="工作目录", value=current["work_dir"] or "(none)", inline=True)
        embed.add_field(name="状态", value=current["status"], inline=True)
        embed.add_field(name="轮次", value=str(current["turn_count"]), inline=True)
        embed.add_field(
            name="更新时间",
            value=_fmt_time(current["updated_at"]),
            inline=False,
        )
        embed.add_field(name="活跃会话", value=f"共 {len(all_sessions)} 个", inline=False)
    else:
        embed = discord.Embed(
            title=f"会话状态 — {agent_id}",
            description="当前 scope 没有活跃会话。",
            color=discord.Color.gold(),
        )
        embed.add_field(name="scope", value=scope_id, inline=True)
        embed.add_field(name="scope_type", value=scope_desc.label, inline=True)
        embed.add_field(name="活跃会话", value=f"共 {len(all_sessions)} 个", inline=True)

    return embed


def _fmt_time(ts: float) -> str:
    if not ts:
        return "(未知)"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
