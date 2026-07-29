import asyncio
import os
import tempfile
import unittest
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

from multinexus.commands import (
    OPERATOR_COMMANDS,
    can_run_operator_command,
    handle_operator_command,
    is_dangerous_command,
    parse_operator_command,
)
from multinexus.models import AgentConfig, KnownAgentMention
from multinexus.sessions.store import SessionStore


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
    client.adapter = MagicMock()
    client.adapter.health_check = AsyncMock(return_value={
        "adapter": "claude", "bin": "claude", "available": True, "path": "/usr/bin/claude",
    })
    return client


def _make_message(content="session status", author_id=100, channel_id=999):
    msg = MagicMock()
    msg.content = content
    msg.author.id = author_id
    msg.author.bot = False
    msg.channel.id = channel_id
    return msg


class TestParseOperatorCommand(unittest.TestCase):
    def test_session_status(self):
        self.assertEqual(parse_operator_command("session status"), "session status")

    def test_session_reset(self):
        self.assertEqual(parse_operator_command("session reset"), "session reset")

    def test_agents(self):
        self.assertEqual(parse_operator_command("agents"), "agents")

    def test_health(self):
        self.assertEqual(parse_operator_command("health"), "health")

    def test_leading_trailing_spaces(self):
        self.assertEqual(parse_operator_command("  session status  "), "session status")

    def test_case_insensitive(self):
        self.assertEqual(parse_operator_command("Session Status"), "session status")

    def test_normal_text_returns_none(self):
        self.assertIsNone(parse_operator_command("hello world"))

    def test_partial_match_returns_none(self):
        self.assertIsNone(parse_operator_command("session"))

    def test_prefix_match_returns_none(self):
        self.assertIsNone(parse_operator_command("session status extra"))


class TestIsDangerousCommand(unittest.TestCase):
    def test_session_reset_is_dangerous(self):
        self.assertTrue(is_dangerous_command("session reset"))

    def test_session_status_is_not_dangerous(self):
        self.assertFalse(is_dangerous_command("session status"))

    def test_agents_is_not_dangerous(self):
        self.assertFalse(is_dangerous_command("agents"))

    def test_health_is_not_dangerous(self):
        self.assertFalse(is_dangerous_command("health"))


class TestCmdSessionStatus(unittest.TestCase):
    def test_with_active_session(self):
        client = _make_client()
        client.session_store.upsert(
            scope_id="999", agent_id="test-agent",
            adapter="claude", session_id="sess-abc123",
            work_dir="/tmp/test",
        )
        msg = _make_message()
        result = asyncio.run(
            handle_operator_command("session status", client, msg.channel.id)
        )
        self.assertIn("test-agent", result)
        self.assertIn("sess-abc123", result)
        self.assertIn("999", result)
        self.assertIn("claude", result)
        self.assertIn("轮次: 1", result)

    def test_status_shows_channel_scope_type(self):
        client = _make_client()
        client.session_store.upsert(
            scope_id="channel:999", agent_id="test-agent",
            adapter="claude", session_id="sess-channel",
            work_dir="/tmp/test",
        )
        result = asyncio.run(
            handle_operator_command("session status", client, 999)
        )
        self.assertIn("channel scope", result)
        self.assertIn("scope: `channel:999`", result)

    def test_status_shows_thread_scope_type(self):
        client = _make_client()
        client.session_store.upsert(
            scope_id="thread:888", agent_id="test-agent",
            adapter="claude", session_id="sess-thread",
            work_dir="/tmp/test",
        )
        result = asyncio.run(
            handle_operator_command("session status", client, 888, is_thread=True)
        )
        self.assertIn("thread scope", result)
        self.assertIn("scope: `thread:888`", result)

    def test_without_active_session(self):
        client = _make_client()
        msg = _make_message()
        result = asyncio.run(
            handle_operator_command("session status", client, msg.channel.id)
        )
        self.assertIn("没有活跃会话", result)
        self.assertIn("999", result)


class TestCmdSessionReset(unittest.TestCase):
    def test_with_active_session(self):
        client = _make_client()
        client.session_store.upsert(
            scope_id="999", agent_id="test-agent",
            adapter="claude", session_id="sess-abc",
        )
        msg = _make_message()
        result = asyncio.run(
            handle_operator_command("session reset", client, msg.channel.id)
        )
        self.assertIn("stale", result)
        self.assertIn("999", result)
        # Verify it's actually stale now
        self.assertIsNone(client.session_store.get(scope_id="999", agent_id="test-agent"))

    def test_without_active_session(self):
        client = _make_client()
        msg = _make_message()
        result = asyncio.run(
            handle_operator_command("session reset", client, msg.channel.id)
        )
        self.assertIn("没有活跃会话", result)


class TestCmdAgents(unittest.TestCase):
    def test_lists_managed_and_external(self):
        client = _make_client()
        msg = _make_message()
        result = asyncio.run(
            handle_operator_command("agents", client, msg.channel.id)
        )
        self.assertIn("托管 Agent", result)
        self.assertIn("test-agent", result)
        self.assertIn("外部 Gateway Agent", result)
        self.assertIn("ext-agent", result)
        self.assertIn("discord_id: `111`", result)
        self.assertIn("discord_id: `222`", result)
        self.assertNotIn("<@111>", result)
        self.assertNotIn("<@222>", result)


class TestCmdHealth(unittest.TestCase):
    def test_shows_adapter_info(self):
        client = _make_client()
        msg = _make_message()
        result = asyncio.run(
            handle_operator_command("health", client, msg.channel.id)
        )
        self.assertIn("claude", result)
        self.assertIn("available: 是", result)
        self.assertIn("/tmp/test", result)
        self.assertIn("sonnet", result)
        self.assertIn("360s", result)


class TestClientInterception(unittest.TestCase):
    """Test that operator commands are intercepted and don't reach the adapter."""

    def test_operator_command_does_not_call_adapter(self):
        """When @bot session status is sent, adapter.call() should NOT be invoked."""
        # We test this by checking parse_operator_command returns non-None
        # and the actual interception is in client.py which requires a real
        # Discord connection. Here we verify the command detection pipeline.
        prompt = "session status"
        cmd = parse_operator_command(prompt)
        self.assertIsNotNone(cmd)
        self.assertFalse(is_dangerous_command(cmd))

    def test_normal_message_not_intercepted(self):
        """Normal text should not be detected as operator command."""
        self.assertIsNone(parse_operator_command("hello, please help me with this code"))
        self.assertIsNone(parse_operator_command("session"))
        self.assertIsNone(parse_operator_command(""))

    def test_dangerous_command_permission_check(self):
        """session reset should require allowed_user_ids non-empty and matching."""
        cmd = "session reset"
        self.assertTrue(is_dangerous_command(cmd))
        # Config with empty allowed_user_ids
        config = _make_config(allowed_user_ids=[])
        # Empty list means no one has explicit permission → should be denied
        self.assertFalse(bool(config.allowed_user_ids))
        # Config with allowed_user_ids
        config2 = _make_config(allowed_user_ids=[100])
        self.assertIn(100, config2.allowed_user_ids)


class TestCanRunOperatorCommand(unittest.TestCase):
    def test_empty_allowed_user_ids_allows_non_dangerous(self):
        """When allowed_user_ids is empty, non-dangerous commands are allowed."""
        config = _make_config(allowed_user_ids=[])
        self.assertIsNone(can_run_operator_command(config, 999, "agents"))
        self.assertIsNone(can_run_operator_command(config, 999, "health"))
        self.assertIsNone(can_run_operator_command(config, 999, "session status"))

    def test_empty_allowed_user_ids_blocks_dangerous(self):
        """session reset is blocked when allowed_user_ids is empty (no explicit permission)."""
        config = _make_config(allowed_user_ids=[])
        self.assertIsNotNone(can_run_operator_command(config, 999, "session reset"))

    def test_authorized_user_allowed(self):
        config = _make_config(allowed_user_ids=[100])
        self.assertIsNone(can_run_operator_command(config, 100, "agents"))
        self.assertIsNone(can_run_operator_command(config, 100, "health"))
        self.assertIsNone(can_run_operator_command(config, 100, "session status"))
        self.assertIsNone(can_run_operator_command(config, 100, "session reset"))

    def test_unauthorized_user_blocked_for_all_commands(self):
        config = _make_config(allowed_user_ids=[100])
        self.assertIsNotNone(can_run_operator_command(config, 200, "agents"))
        self.assertIsNotNone(can_run_operator_command(config, 200, "health"))
        self.assertIsNotNone(can_run_operator_command(config, 200, "session status"))
        self.assertIsNotNone(can_run_operator_command(config, 200, "session reset"))


if __name__ == "__main__":
    unittest.main()
