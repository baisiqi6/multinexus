import os
import tempfile
import unittest
from unittest.mock import MagicMock

from multinexus.embeds import (
    build_agents_embed,
    build_health_embed,
    build_session_status_embed,
)
from multinexus.models import AgentConfig, KnownAgentMention
from multinexus.sessions.store import SessionStore

import discord


def _make_config(**overrides):
    defaults = dict(
        id="test-agent",
        token="fake-token",
        adapter="claude",
        display_name="Test Agent",
        aliases={"test"},
        known_agents=[
            KnownAgentMention(id="test-agent", primary_name="Test", kind="managed", discord_user_id=111),
            KnownAgentMention(id="ext-agent", primary_name="Ext", kind="external", discord_user_id=222),
        ],
        work_dir="/tmp/test",
        model="sonnet",
        timeout=360,
        allowed_user_ids=[100],
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _make_client(config=None, session_store=None):
    config = config or _make_config()
    client = MagicMock()
    client.agent_config = config
    if session_store is None:
        tmpdir = tempfile.mkdtemp()
        session_store = SessionStore(os.path.join(tmpdir, "test.sqlite3"))
    client.session_store = session_store
    return client


class TestBuildAgentsEmbed(unittest.TestCase):
    def test_has_managed_and_external_fields(self):
        config = _make_config()
        embed = build_agents_embed(config)
        names = [f.name for f in embed.fields]
        self.assertIn("托管 Agent", names)
        self.assertIn("外部 Gateway Agent", names)

    def test_contains_discord_id(self):
        config = _make_config()
        embed = build_agents_embed(config)
        text = embed.fields[0].value + embed.fields[1].value
        self.assertIn("discord_id:", text)
        self.assertIn("`111`", text)
        self.assertIn("`222`", text)

    def test_no_ping_mentions(self):
        config = _make_config()
        embed = build_agents_embed(config)
        text = embed.fields[0].value + embed.fields[1].value
        self.assertNotIn("<@", text)

    def test_blurple_color(self):
        config = _make_config()
        embed = build_agents_embed(config)
        self.assertEqual(embed.color.value, discord.Color.blurple().value)


class TestBuildHealthEmbed(unittest.TestCase):
    def test_available_true_green(self):
        config = _make_config()
        health = {"adapter": "claude", "bin": "claude", "available": True, "path": "/usr/bin/claude"}
        embed = build_health_embed(config, health)
        self.assertEqual(embed.color.value, discord.Color.green().value)

    def test_available_false_red(self):
        config = _make_config()
        health = {"adapter": "claude", "bin": "claude", "available": False, "path": "/usr/bin/claude"}
        embed = build_health_embed(config, health)
        self.assertEqual(embed.color.value, discord.Color.red().value)

    def test_fields_present(self):
        config = _make_config()
        health = {"adapter": "claude", "bin": "claude", "available": True, "path": "/usr/bin/claude"}
        embed = build_health_embed(config, health)
        names = [f.name for f in embed.fields]
        for expected in ["适配器", "可执行文件", "可用", "工作目录", "模型", "超时"]:
            self.assertIn(expected, names)

    def test_available_shows_yes(self):
        config = _make_config()
        health = {"adapter": "claude", "bin": "claude", "available": True, "path": "/usr/bin/claude"}
        embed = build_health_embed(config, health)
        avail_field = next(f for f in embed.fields if f.name == "可用")
        self.assertEqual(avail_field.value, "是")

    def test_unavailable_shows_no(self):
        config = _make_config()
        health = {"adapter": "claude", "bin": "claude", "available": False, "path": "/usr/bin/claude"}
        embed = build_health_embed(config, health)
        avail_field = next(f for f in embed.fields if f.name == "可用")
        self.assertEqual(avail_field.value, "否")

    def test_title_contains_agent_id(self):
        config = _make_config()
        health = {"adapter": "claude", "bin": "claude", "available": True, "path": "/usr/bin/claude"}
        embed = build_health_embed(config, health)
        self.assertIn("test-agent", embed.title)

    def test_error_included(self):
        config = _make_config()
        health = {"adapter": "claude", "bin": "?", "available": False, "error": "connection refused"}
        embed = build_health_embed(config, health)
        names = [f.name for f in embed.fields]
        self.assertIn("错误", names)
        error_field = next(f for f in embed.fields if f.name == "错误")
        self.assertIn("connection refused", error_field.value)

    def test_agentd_mode_blurple_and_managed_status(self):
        config = _make_config()
        health = {"adapter": "codex", "bin": "(agentd)", "available": True, "agentd_mode": True}
        embed = build_health_embed(config, health)
        self.assertEqual(embed.color.value, discord.Color.blurple().value)
        self.assertIn("agentd 模式", embed.title)
        names = [f.name for f in embed.fields]
        self.assertNotIn("可用", names)
        self.assertIn("状态", names)
        self.assertNotIn("路径", names)


class TestBuildSessionStatusEmbed(unittest.TestCase):
    def test_active_session_green(self):
        client = _make_client()
        client.session_store.upsert(
            scope_id="999", agent_id="test-agent",
            adapter="claude", session_id="sess-abc123456789", work_dir="/tmp/test",
        )
        embed = build_session_status_embed(client, 999)
        self.assertEqual(embed.color.value, discord.Color.green().value)

    def test_active_session_fields(self):
        client = _make_client()
        client.session_store.upsert(
            scope_id="999", agent_id="test-agent",
            adapter="claude", session_id="sess-abc123456789", work_dir="/tmp/test",
        )
        embed = build_session_status_embed(client, 999)
        names = [f.name for f in embed.fields]
        for expected in ["scope", "scope_type", "session_id", "适配器", "工作目录", "轮次", "活跃会话"]:
            self.assertIn(expected, names)

    def test_scope_type_field_for_channel(self):
        client = _make_client()
        client.session_store.upsert(
            scope_id="channel:999", agent_id="test-agent",
            adapter="claude", session_id="sess-abc123456789", work_dir="/tmp/test",
        )
        embed = build_session_status_embed(client, 999)
        scope_type = next(f for f in embed.fields if f.name == "scope_type")
        self.assertEqual(scope_type.value, "channel scope")

    def test_scope_type_field_for_thread(self):
        client = _make_client()
        client.session_store.upsert(
            scope_id="thread:888", agent_id="test-agent",
            adapter="claude", session_id="sess-abc123456789", work_dir="/tmp/test",
        )
        embed = build_session_status_embed(client, 888, is_thread=True)
        scope_type = next(f for f in embed.fields if f.name == "scope_type")
        self.assertEqual(scope_type.value, "thread scope")

    def test_session_id_truncated(self):
        client = _make_client()
        client.session_store.upsert(
            scope_id="999", agent_id="test-agent",
            adapter="claude", session_id="sess-abc123456789def012345678", work_dir="/tmp/test",
        )
        embed = build_session_status_embed(client, 999)
        sid_field = next(f for f in embed.fields if f.name == "session_id")
        self.assertIn("...", sid_field.value)

    def test_no_session_gold(self):
        client = _make_client()
        embed = build_session_status_embed(client, 999)
        self.assertEqual(embed.color.value, discord.Color.gold().value)

    def test_no_session_description(self):
        client = _make_client()
        embed = build_session_status_embed(client, 999)
        self.assertIn("没有活跃会话", embed.description)

    def test_no_session_shows_active_count(self):
        client = _make_client()
        embed = build_session_status_embed(client, 999)
        names = [f.name for f in embed.fields]
        self.assertIn("活跃会话", names)

    def test_title_contains_agent_id(self):
        client = _make_client()
        embed = build_session_status_embed(client, 999)
        self.assertIn("test-agent", embed.title)

    def test_agentd_mode_none_session_store(self):
        config = _make_config()
        client = MagicMock()
        client.agent_config = config
        client.session_store = None
        embed = build_session_status_embed(client, 999)
        self.assertEqual(embed.color.value, discord.Color.blurple().value)
        self.assertIn("agentd 模式", embed.title)
        self.assertIn("coordinate runtime", embed.description)


if __name__ == "__main__":
    unittest.main()
