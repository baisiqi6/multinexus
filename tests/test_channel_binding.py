"""Focused channel binding tests for the S2B-2 MultiNexus half.

Covers:
- Coordinate client strict channel workspace resolve
- Discord bound / unbound / error / thread paths
- KOOK bound / unbound / error paths
- Managed handoff workspace mismatch gate
- Rebind / release isolation
- Legacy non-agentd path unchanged
- agentd worker claim does not call channel resolve
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from multinexus.agentd.coordinate_client import (
    CoordinateRuntimeClient,
    CoordinateRuntimeError,
)
from multinexus.client import DiscordClient
from multinexus.kook.bot import KookBridge
from multinexus.models import AgentConfig, KnownAgentMention
from multinexus.sessions.scope import (
    workspace_channel_context_scope,
    workspace_channel_session_scope,
    workspace_discord_thread_scope,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_config(**overrides):
    defaults = dict(
        id="mac-claude",
        token="fake-token",
        adapter="claude",
        display_name="Claude",
        known_agents=[
            KnownAgentMention(
                id="mac-claude",
                primary_name="Claude",
                kind="managed",
                discord_user_id=111,
            ),
        ],
        work_dir="/tmp/test",
        coordinator_bot_id=999,
        coordinator_cli_path="/fake/mac.sh",
        coordinator_db_path="/fake/db.sqlite3",
        coordinator_workspace_path="/fake/workspace",
        role_ids=["123"],
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _make_message(content="hello", channel_id=500, author_id=888, bot=False):
    msg = MagicMock(spec=discord.Message)
    msg.id = 1001
    msg.content = content
    msg.author = MagicMock()
    msg.author.id = author_id
    msg.author.bot = bot
    msg.author.display_name = "user"
    msg.channel = MagicMock(spec=discord.TextChannel)
    msg.channel.id = channel_id
    msg.channel.parent_id = channel_id
    mentioned = MagicMock()
    mentioned.id = 111
    msg.mentions = [mentioned]
    return msg


def _make_thread_message(content="hello", parent_id=500, thread_id=600, author_id=888):
    msg = MagicMock(spec=discord.Message)
    msg.id = 1002
    msg.content = content
    msg.author = MagicMock()
    msg.author.id = author_id
    msg.author.bot = False
    msg.author.display_name = "user"
    msg.channel = MagicMock(spec=discord.Thread)
    msg.channel.id = thread_id
    msg.channel.parent_id = parent_id
    mentioned = MagicMock()
    mentioned.id = 111
    msg.mentions = [mentioned]
    return msg


class TestCoordinateClientResolveChannelWorkspace(unittest.TestCase):
    def _client(self):
        return CoordinateRuntimeClient(
            cli_path="/fake/coord.sh", db_path="/fake/db.sqlite3"
        )

    def _patch_run(self, client, stdout_dict):
        return patch.object(client, "_run_cli", return_value=stdout_dict)

    def test_bound_returns_workspace_id(self):
        client = self._client()
        with self._patch_run(client, {
            "status": "bound",
            "binding": {
                "workspace_id": "multinexus",
                "platform": "discord",
                "channel_id": "500",
            },
        }):
            result = _run(client.resolve_channel_workspace(
                platform="discord", channel_id="500"
            ))
        self.assertEqual(result, "multinexus")

    def test_bound_platform_mismatch_raises(self):
        client = self._client()
        with self._patch_run(client, {
            "status": "bound",
            "binding": {
                "workspace_id": "multinexus",
                "platform": "kook",
                "channel_id": "500",
            },
        }):
            with self.assertRaises(CoordinateRuntimeError):
                _run(client.resolve_channel_workspace(
                    platform="discord", channel_id="500"
                ))

    def test_bound_channel_mismatch_raises(self):
        client = self._client()
        with self._patch_run(client, {
            "status": "bound",
            "binding": {
                "workspace_id": "multinexus",
                "platform": "discord",
                "channel_id": "501",
            },
        }):
            with self.assertRaises(CoordinateRuntimeError):
                _run(client.resolve_channel_workspace(
                    platform="discord", channel_id="500"
                ))

    def test_bound_empty_workspace_raises(self):
        client = self._client()
        with self._patch_run(client, {
            "status": "bound",
            "binding": {
                "workspace_id": "",
                "platform": "discord",
                "channel_id": "500",
            },
        }):
            with self.assertRaises(CoordinateRuntimeError):
                _run(client.resolve_channel_workspace(
                    platform="discord", channel_id="500"
                ))

    def test_bound_non_string_workspace_raises(self):
        client = self._client()
        with self._patch_run(client, {
            "status": "bound",
            "binding": {
                "workspace_id": 123,
                "platform": "discord",
                "channel_id": "500",
            },
        }):
            with self.assertRaises(CoordinateRuntimeError):
                _run(client.resolve_channel_workspace(
                    platform="discord", channel_id="500"
                ))

    def test_unbound_returns_none(self):
        client = self._client()
        with self._patch_run(client, {"status": "unbound", "binding": None}):
            result = _run(client.resolve_channel_workspace(
                platform="discord", channel_id="500"
            ))
        self.assertIsNone(result)

    def test_runtime_error_raises(self):
        client = self._client()
        with self._patch_run(client, {"error": "db locked"}):
            with self.assertRaises(CoordinateRuntimeError):
                _run(client.resolve_channel_workspace(
                    platform="discord", channel_id="500"
                ))

    def test_bound_without_workspace_id_raises(self):
        client = self._client()
        with self._patch_run(client, {"status": "bound", "binding": {}}):
            with self.assertRaises(CoordinateRuntimeError):
                _run(client.resolve_channel_workspace(
                    platform="discord", channel_id="500"
                ))

    def test_non_dict_result_raises(self):
        client = self._client()
        with self._patch_run(client, ["unexpected"]):
            with self.assertRaises(CoordinateRuntimeError):
                _run(client.resolve_channel_workspace(
                    platform="discord", channel_id="500"
                ))

    def test_unexpected_status_raises(self):
        client = self._client()
        with self._patch_run(client, {"status": "unknown"}):
            with self.assertRaises(CoordinateRuntimeError):
                _run(client.resolve_channel_workspace(
                    platform="discord", channel_id="500"
                ))


class TestDiscordResolveChannelWorkspace(unittest.TestCase):
    def _make_client(self, config):
        with patch.object(DiscordClient, "__init__", lambda self, *a, **kw: None):
            instance = DiscordClient.__new__(DiscordClient)
        instance.agent_config = config
        instance._agentd_mode = config.agentd_mode
        instance._coordinate_client = None
        return instance

    def test_legacy_mode_returns_none(self):
        config = _make_config(agentd_mode=False)
        client = self._make_client(config)
        result = _run(client._resolve_channel_workspace(
            platform="discord", channel_id=500
        ))
        self.assertIsNone(result)

    def test_agentd_mode_forwards_to_coordinate_client(self):
        config = _make_config(agentd_mode=True)
        client = self._make_client(config)
        client._coordinate_client = MagicMock()
        client._coordinate_client.resolve_channel_workspace = AsyncMock(
            return_value="multinexus"
        )
        result = _run(client._resolve_channel_workspace(
            platform="discord", channel_id=500
        ))
        self.assertEqual(result, "multinexus")
        client._coordinate_client.resolve_channel_workspace.assert_awaited_once_with(
            platform="discord", channel_id="500"
        )

    def test_agentd_mode_unbound_returns_none(self):
        config = _make_config(agentd_mode=True)
        client = self._make_client(config)
        client._coordinate_client = MagicMock()
        client._coordinate_client.resolve_channel_workspace = AsyncMock(
            return_value=None
        )
        result = _run(client._resolve_channel_workspace(
            platform="discord", channel_id=500
        ))
        self.assertIsNone(result)

    def test_agentd_mode_error_propagates(self):
        config = _make_config(agentd_mode=True)
        client = self._make_client(config)
        client._coordinate_client = MagicMock()
        client._coordinate_client.resolve_channel_workspace = AsyncMock(
            side_effect=CoordinateRuntimeError("db locked")
        )
        with self.assertRaises(CoordinateRuntimeError):
            _run(client._resolve_channel_workspace(
                platform="discord", channel_id=500
            ))


class TestDiscordOnMessageChannelBinding(unittest.TestCase):
    def _make_client(self, config):
        with patch.object(DiscordClient, "__init__", lambda self, *a, **kw: None):
            instance = DiscordClient.__new__(DiscordClient)
        instance.agent_config = config
        instance._agentd_mode = config.agentd_mode
        instance._coordinate_client = None
        mock_user = MagicMock()
        mock_user.id = 111
        instance._connection = MagicMock()
        instance._connection.user = mock_user
        instance.context_store = MagicMock()
        instance.mention_router = MagicMock()
        instance.mention_router.matches_bang_command.return_value = False
        instance.mention_router.is_handoff_message.return_value = False
        instance.adapter = MagicMock()
        instance.adapter.call = AsyncMock(
            return_value=MagicMock(text="ok", session_id=None)
        )
        instance._bot_user_id_map = {}
        instance._is_addressed_to_me = lambda _msg: True
        instance.session_store = MagicMock()
        return instance

    def test_human_message_unbound_drops_and_notifies(self):
        config = _make_config(agentd_mode=True)
        client = self._make_client(config)
        client._coordinate_client = MagicMock()
        client._coordinate_client.resolve_channel_workspace = AsyncMock(
            return_value=None
        )
        msg = _make_message(content="<@111> hello", channel_id=500)
        msg.channel.send = AsyncMock()

        _run(client.on_message(msg))

        client._coordinate_client.resolve_channel_workspace.assert_awaited_once_with(
            platform="discord", channel_id="500"
        )
        msg.channel.send.assert_awaited_once()
        sent = msg.channel.send.await_args.args[0]
        self.assertIn("未绑定", sent)
        client.adapter.call.assert_not_called()

    def test_human_message_bound_records_workspace_scope(self):
        config = _make_config(agentd_mode=True)
        client = self._make_client(config)
        client._coordinate_client = MagicMock()
        client._coordinate_client.resolve_channel_workspace = AsyncMock(
            return_value="multinexus"
        )
        client._coordinate_client.submit_request = AsyncMock(
            return_value={"result": {"job": {"id": "request:abc"}}}
        )
        client._coordinate_client.wait_for_job_result = AsyncMock(
            return_value={
                "id": "request:abc",
                "status": "done",
                "result": {"response_text": "hi"},
            }
        )
        msg = _make_message(content="<@111> hello", channel_id=500)
        msg.channel.send = AsyncMock()

        _run(client.on_message(msg))

        expected_scope = workspace_channel_context_scope(
            "multinexus", "discord", 500
        )
        call_kwargs = client.context_store.record_message.call_args.kwargs
        self.assertEqual(call_kwargs["channel_id"], expected_scope)

        # Managed addressed direct submit: legacy_scope_ids must be empty.
        submit_kwargs = client._coordinate_client.submit_request.await_args.kwargs
        origin = submit_kwargs["origin_json"]
        self.assertEqual(origin["legacy_scope_ids"], [])

    def test_legacy_human_message_unchanged(self):
        config = _make_config(agentd_mode=False)
        client = self._make_client(config)
        msg = _make_message(content="<@111> hello", channel_id=500)
        msg.channel.send = AsyncMock()

        _run(client.on_message(msg))

        call_kwargs = client.context_store.record_message.call_args.kwargs
        self.assertEqual(call_kwargs["channel_id"], "500")
        client.adapter.call.assert_awaited_once()

    def test_thread_uses_parent_channel_for_context_thread_for_session(self):
        config = _make_config(agentd_mode=True)
        client = self._make_client(config)
        client._coordinate_client = MagicMock()
        client._coordinate_client.resolve_channel_workspace = AsyncMock(
            return_value="multinexus"
        )
        client._coordinate_client.submit_request = AsyncMock(
            return_value={"result": {"job": {"id": "request:thread"}}}
        )
        client._coordinate_client.wait_for_job_result = AsyncMock(
            return_value={
                "id": "request:thread",
                "status": "done",
                "result": {"response_text": "thread reply"},
            }
        )
        msg = _make_thread_message(
            content="<@111> hello", parent_id=500, thread_id=600
        )
        msg.channel.send = AsyncMock()

        _run(client.on_message(msg))

        expected_context = workspace_channel_context_scope(
            "multinexus", "discord", 500
        )
        record_kwargs = client.context_store.record_message.call_args.kwargs
        self.assertEqual(record_kwargs["channel_id"], expected_context)

        submit_kwargs = client._coordinate_client.submit_request.await_args.kwargs
        origin = submit_kwargs["origin_json"]
        expected_session = workspace_discord_thread_scope("multinexus", 600)
        self.assertEqual(origin["session_scope_id"], expected_session)
        self.assertEqual(origin["thread_id"], "600")

    def test_unaddressed_bound_records_workspace_context_no_submit(self):
        """Managed unaddressed bound: record workspace-qualified context, no prompt/submit/visible."""
        config = _make_config(agentd_mode=True)
        client = self._make_client(config)
        # unaddressed — _is_addressed_to_me returns False
        client._is_addressed_to_me = lambda _msg: False
        client._coordinate_client = MagicMock()
        client._coordinate_client.resolve_channel_workspace = AsyncMock(
            return_value="multinexus"
        )
        # _resolve_silent uses the same client — mock it to return bound.
        client._resolve_silent = AsyncMock(return_value="multinexus")
        msg = _make_message(content="hello", channel_id=500)
        msg.channel.send = AsyncMock()

        _run(client.on_message(msg))

        # Context was recorded with workspace-qualified scope.
        expected_context = workspace_channel_context_scope(
            "multinexus", "discord", 500
        )
        record_kwargs = client.context_store.record_message.call_args.kwargs
        self.assertEqual(record_kwargs["channel_id"], expected_context)

        # No prompt built, no submit, no visible response.
        client.adapter.call.assert_not_called()
        if client._coordinate_client.submit_request.called:
            self.fail("submit_request should not be called for unaddressed")
        msg.channel.send.assert_not_called()


    def test_unaddressed_unbound_no_context_no_submit(self):
        """Managed unaddressed unbound: no context, no prompt, no submit, no visible."""
        config = _make_config(agentd_mode=True)
        client = self._make_client(config)
        client._is_addressed_to_me = lambda _msg: False
        client._coordinate_client = MagicMock()
        client._coordinate_client.resolve_channel_workspace = AsyncMock(
            return_value=None
        )
        client._resolve_silent = AsyncMock(return_value=None)
        msg = _make_message(content="hello", channel_id=500)
        msg.channel.send = AsyncMock()

        _run(client.on_message(msg))

        # No context recorded.
        client.context_store.record_message.assert_not_called()
        # No submit.
        if client._coordinate_client.submit_request.called:
            self.fail("submit_request should not be called for unaddressed unbound")
        msg.channel.send.assert_not_called()

    def test_unaddressed_lookup_error_no_context_no_submit(self):
        """Managed unaddressed lookup error: no context, no prompt, no submit, no visible."""
        config = _make_config(agentd_mode=True)
        client = self._make_client(config)
        client._is_addressed_to_me = lambda _msg: False
        client._coordinate_client = MagicMock()
        client._coordinate_client.resolve_channel_workspace = AsyncMock(
            side_effect=CoordinateRuntimeError("db locked")
        )
        client._resolve_silent = AsyncMock(return_value=None)
        msg = _make_message(content="hello", channel_id=500)
        msg.channel.send = AsyncMock()

        _run(client.on_message(msg))

        client.context_store.record_message.assert_not_called()
        if client._coordinate_client.submit_request.called:
            self.fail("submit_request should not be called for unaddressed error")
        msg.channel.send.assert_not_called()

    def _make_edit_message(self, content="[handoff]", channel_id=500, author_id=888):
        """Make a bot-authored edited message that is addressed+handoff."""
        msg = _make_message(content=content, channel_id=channel_id, author_id=author_id, bot=True)
        msg.author.id = author_id
        return msg

    def test_on_message_edit_managed_bound_continues(self):
        """Managed bound edit: records workspace-qualified context and continues to handle."""
        config = _make_config(agentd_mode=True)
        client = self._make_client(config)
        client.mention_router.is_handoff_message.return_value = True
        client._coordinate_client = MagicMock()
        client._coordinate_client.resolve_channel_workspace = AsyncMock(
            return_value="multinexus"
        )
        client._coordinate_client.submit_request = AsyncMock(
            return_value={"result": {"job": {"id": "request:edit-bound"}}}
        )
        client._coordinate_client.wait_for_job_result = AsyncMock(
            return_value={
                "id": "request:edit-bound",
                "status": "done",
                "result": {"response_text": "edit handled"},
            }
        )
        msg = self._make_edit_message(content="[handoff] task-123", channel_id=500)
        msg.channel.send = AsyncMock()
        before = MagicMock(spec=discord.Message)

        _run(client.on_message_edit(before, msg))

        expected_context = workspace_channel_context_scope(
            "multinexus", "discord", 500
        )
        record_kwargs = client.context_store.record_message.call_args.kwargs
        self.assertEqual(record_kwargs["channel_id"], expected_context)
        # Must have submitted.
        client._coordinate_client.submit_request.assert_awaited_once()

    def test_on_message_edit_managed_unbound_bounded_visible_no_submit(self):
        """Managed unbound edit: at most one bounded visible, no context, no submit."""
        config = _make_config(agentd_mode=True)
        client = self._make_client(config)
        client.mention_router.is_handoff_message.return_value = True
        client._coordinate_client = MagicMock()
        client._coordinate_client.resolve_channel_workspace = AsyncMock(
            return_value=None
        )
        msg = self._make_edit_message(content="[handoff] task-456", channel_id=500)
        msg.channel.send = AsyncMock()
        before = MagicMock(spec=discord.Message)

        _run(client.on_message_edit(before, msg))

        # At most one bounded visible message (unbound notification).
        self.assertLessEqual(msg.channel.send.await_count, 1)
        if msg.channel.send.await_count == 1:
            sent = msg.channel.send.await_args.args[0]
            self.assertIn("未绑定", sent)
        # No submit.
        if client._coordinate_client.submit_request.called:
            self.fail("submit_request should not be called for unbound edit")
        client.context_store.record_message.assert_not_called()

    def test_on_message_edit_managed_lookup_error_bounded_visible_no_submit(self):
        """Managed lookup-error edit: at most one bounded visible, no context, no submit."""
        config = _make_config(agentd_mode=True)
        client = self._make_client(config)
        client.mention_router.is_handoff_message.return_value = True
        client._coordinate_client = MagicMock()
        client._coordinate_client.resolve_channel_workspace = AsyncMock(
            side_effect=CoordinateRuntimeError("db locked")
        )
        msg = self._make_edit_message(content="[handoff] task-789", channel_id=500)
        msg.channel.send = AsyncMock()
        before = MagicMock(spec=discord.Message)

        _run(client.on_message_edit(before, msg))

        self.assertLessEqual(msg.channel.send.await_count, 1)
        if msg.channel.send.await_count == 1:
            sent = msg.channel.send.await_args.args[0]
            self.assertIn("查询失败", sent)
        if client._coordinate_client.submit_request.called:
            self.fail("submit_request should not be called for error edit")
        client.context_store.record_message.assert_not_called()

    def test_on_message_edit_legacy_continues_raw_context(self):
        """Legacy edit: records raw channel context and calls adapter."""
        config = _make_config(agentd_mode=False)
        client = self._make_client(config)
        client.mention_router.is_handoff_message.return_value = True
        msg = self._make_edit_message(content="[handoff] task-legacy", channel_id=500)
        msg.channel.send = AsyncMock()
        before = MagicMock(spec=discord.Message)

        _run(client.on_message_edit(before, msg))

        # Raw channel context (legacy path).
        record_kwargs = client.context_store.record_message.call_args.kwargs
        self.assertEqual(record_kwargs["channel_id"], "500")
        # Adapter was called.
        client.adapter.call.assert_awaited_once()

class TestKookChannelBinding(unittest.TestCase):
    def _make_bridge(self, agentd_mode=True):
        config = _make_config(agentd_mode=agentd_mode, token="fake-kook-token")
        with patch.object(KookBridge, "__init__", lambda self, cfg: None):
            bridge = KookBridge.__new__(KookBridge)
        bridge.config = config
        bridge.bot_id = "777"
        bridge.bot_role_ids = {"123"}
        bridge.aliases = set()
        bridge.known_user_names = {}
        bridge.known_role_names = {}
        bridge.context_store = MagicMock()
        bridge.router = MagicMock()
        bridge.router.render_for_context = lambda c, *_: c
        bridge.router.is_addressed_to_this_bot.return_value = True
        bridge.router.clean_for_agent = lambda content, **kw: content
        bridge.router.outbound_for_kook = lambda c, *_: c
        bridge._coordinate_client = None
        bridge.seen_message_ids = set()
        bridge.seen_message_order = []
        bridge.poll_error_keys = set()
        bridge.poll_error_last_logged = {}
        return bridge

    def test_unbound_drops_inbound(self):
        bridge = self._make_bridge(agentd_mode=True)
        bridge.router.is_addressed_to_this_bot.return_value = False
        bridge._coordinate_client = MagicMock()
        bridge._coordinate_client.resolve_channel_workspace = AsyncMock(
            return_value=None
        )
        bridge.send_channel_message = AsyncMock()

        _run(bridge._handle_text_message(
            message_id="m1",
            channel_id="900",
            author_id="888",
            author_name="user",
            author_is_bot=False,
            created_at_ms=1,
            content="just chatting",
            mentions=[],
            mention_roles=[],
            source="ws",
        ))

        bridge.send_channel_message.assert_not_called()
        bridge._coordinate_client.resolve_channel_workspace.assert_awaited_once_with(
            platform="kook", channel_id="900"
        )

    def test_addressed_unbound_sends_visible_error(self):
        bridge = self._make_bridge(agentd_mode=True)
        bridge._coordinate_client = MagicMock()
        bridge._coordinate_client.resolve_channel_workspace = AsyncMock(
            return_value=None
        )
        bridge.send_channel_message = AsyncMock()

        _run(bridge._handle_text_message(
            message_id="m1",
            channel_id="900",
            author_id="888",
            author_name="user",
            author_is_bot=False,
            created_at_ms=1,
            content="@Claude hello",
            mentions=[],
            mention_roles=[],
            source="ws",
        ))

        bridge.send_channel_message.assert_awaited_once()
        sent = bridge.send_channel_message.await_args.args
        self.assertIn("未绑定", sent[1])

    def test_unaddressed_unbound_silent(self):
        bridge = self._make_bridge(agentd_mode=True)
        bridge.router.is_addressed_to_this_bot.return_value = False
        bridge._coordinate_client = MagicMock()
        bridge._coordinate_client.resolve_channel_workspace = AsyncMock(
            return_value=None
        )
        bridge.send_channel_message = AsyncMock()

        _run(bridge._handle_text_message(
            message_id="m1",
            channel_id="900",
            author_id="888",
            author_name="user",
            author_is_bot=False,
            created_at_ms=1,
            content="just chatting",
            mentions=[],
            mention_roles=[],
            source="ws",
        ))

        bridge.send_channel_message.assert_not_called()

    def test_addressed_lookup_error_sends_visible_error(self):
        bridge = self._make_bridge(agentd_mode=True)
        bridge._coordinate_client = MagicMock()
        bridge._coordinate_client.resolve_channel_workspace = AsyncMock(
            side_effect=CoordinateRuntimeError("coord down")
        )
        bridge.send_channel_message = AsyncMock()

        _run(bridge._handle_text_message(
            message_id="m3",
            channel_id="900",
            author_id="888",
            author_name="user",
            author_is_bot=False,
            created_at_ms=1,
            content="@Claude hello",
            mentions=[],
            mention_roles=[],
            source="ws",
        ))

        bridge.send_channel_message.assert_awaited_once()
        sent = bridge.send_channel_message.await_args.args
        self.assertIn("查询失败", sent[1])

    def test_unaddressed_lookup_error_silent(self):
        bridge = self._make_bridge(agentd_mode=True)
        bridge.router.is_addressed_to_this_bot.return_value = False
        bridge._coordinate_client = MagicMock()
        bridge._coordinate_client.resolve_channel_workspace = AsyncMock(
            side_effect=CoordinateRuntimeError("coord down")
        )
        bridge.send_channel_message = AsyncMock()

        _run(bridge._handle_text_message(
            message_id="m3",
            channel_id="900",
            author_id="888",
            author_name="user",
            author_is_bot=False,
            created_at_ms=1,
            content="just chatting",
            mentions=[],
            mention_roles=[],
            source="ws",
        ))

        bridge.send_channel_message.assert_not_called()

    def test_bound_uses_workspace_scope(self):
        bridge = self._make_bridge(agentd_mode=True)
        bridge._coordinate_client = MagicMock()
        bridge._coordinate_client.resolve_channel_workspace = AsyncMock(
            return_value="multinexus"
        )
        bridge._coordinate_client.submit_request = AsyncMock(
            return_value={"result": {"job": {"id": "request:kook"}}}
        )
        bridge._coordinate_client.wait_for_job_result = AsyncMock(
            return_value={
                "id": "request:kook",
                "status": "done",
                "result_json": '{"response_text":"kook reply"}',
            }
        )
        bridge.send_channel_message = AsyncMock()

        _run(bridge._handle_text_message(
            message_id="m2",
            channel_id="900",
            author_id="888",
            author_name="user",
            author_is_bot=False,
            created_at_ms=1,
            content="@Claude hello",
            mentions=[],
            mention_roles=[],
            source="ws",
        ))

        expected_context = workspace_channel_context_scope(
            "multinexus", "kook", "900"
        )
        record_kwargs = bridge.context_store.record_message.call_args.kwargs
        self.assertEqual(record_kwargs["channel_id"], expected_context)

        submit_kwargs = bridge._coordinate_client.submit_request.await_args.kwargs
        self.assertEqual(submit_kwargs["workspace_id"], "multinexus")
        origin = submit_kwargs["origin_json"]
        self.assertEqual(
            origin["session_scope_id"],
            workspace_channel_session_scope("multinexus", "kook", "900"),
        )

    def test_configured_poll_channel_ids_skips_guild_discovery(self):
        """When kook_poll_channel_ids is set, _discover_text_channels returns them directly."""
        bridge = self._make_bridge(agentd_mode=False)
        bridge.config.kook_poll_channel_ids = [100, 200, 300]

        bot = MagicMock()
        result = _run(bridge._discover_text_channels(bot))

        self.assertEqual(result, ["100", "200", "300"])
        bot.client.fetch_guild_list.assert_not_called()


class TestHandoffWorkspaceMismatch(unittest.TestCase):
    def _make_client(self, config):
        with patch.object(DiscordClient, "__init__", lambda self, *a, **kw: None):
            instance = DiscordClient.__new__(DiscordClient)
        instance.agent_config = config
        instance._agentd_mode = True
        mock_user = MagicMock()
        mock_user.id = 111
        instance._connection = MagicMock()
        instance._connection.user = mock_user
        instance.context_store = MagicMock()
        instance.mention_router = MagicMock()
        instance.mention_router.resolve_handoff_mentions = lambda t: t
        instance._coordinate_client = MagicMock()
        instance.adapter = MagicMock()
        return instance

    def test_assignment_handoff_mismatch_emits_blocker(self):
        config = _make_config(agentd_mode=True)
        client = self._make_client(config)
        content = (
            "[handoff] <@111> workspace_id=other "
            "task_id=phase-5.1 action=assignment.accept "
            "bootstrap=docs/project-harness/tasks/phase-5.1/worker-bootstrap.md "
            "context_version=1 workspace_path=/fake/workspace "
            "harness_root=/fake/harness branch=main"
        )
        msg = _make_message(content=content, channel_id=500, author_id=999, bot=True)
        msg.channel.send = AsyncMock()

        _run(client._try_coordinator_handoff(msg, resolved_workspace="multinexus"))

        client._coordinate_client.submit_request.assert_not_called()
        sent = msg.channel.send.await_args.args[0]
        self.assertIn("action=blocker", sent)
        self.assertIn("workspace_id=other", sent)
        self.assertIn("channel binding workspace_id=multinexus", sent)


class TestAgentdWorkerClaimNoResolve(unittest.TestCase):
    def test_claim_job_does_not_call_channel_resolve(self):
        client = CoordinateRuntimeClient(
            cli_path="/fake/coord.sh", db_path="/fake/db.sqlite3"
        )
        with patch.object(client, "_run_cli", return_value={
            "result": {"claimed": True, "job": {"id": "job:abc"}}
        }) as run_cli:
            result = _run(client.claim_job(agent_id="mac-claude"))

        self.assertTrue(result["claimed"])
        # Ensure no workspace channel resolve command was issued.
        commands = [call.args[0] for call in run_cli.call_args_list]
        for cmd in commands:
            self.assertNotIn("channel", cmd)
            self.assertNotIn("resolve", cmd)


class TestRebindIsolation(unittest.TestCase):
    def test_different_channel_same_platform_resolves_differently(self):
        client = CoordinateRuntimeClient(
            cli_path="/fake/coord.sh", db_path="/fake/db.sqlite3"
        )
        with patch.object(client, "_run_cli") as run_cli:
            run_cli.side_effect = [
                {
                    "status": "bound",
                    "binding": {
                        "workspace_id": "alpha",
                        "platform": "discord",
                        "channel_id": "100",
                    },
                },
                {
                    "status": "bound",
                    "binding": {
                        "workspace_id": "beta",
                        "platform": "discord",
                        "channel_id": "200",
                    },
                },
            ]
            alpha = _run(client.resolve_channel_workspace(
                platform="discord", channel_id="100"
            ))
            beta = _run(client.resolve_channel_workspace(
                platform="discord", channel_id="200"
            ))
        self.assertEqual(alpha, "alpha")
        self.assertEqual(beta, "beta")

    def test_release_then_unbound(self):
        client = CoordinateRuntimeClient(
            cli_path="/fake/coord.sh", db_path="/fake/db.sqlite3"
        )
        with patch.object(client, "_run_cli", return_value={
            "status": "unbound", "binding": None
        }):
            result = _run(client.resolve_channel_workspace(
                platform="discord", channel_id="100"
            ))
        self.assertIsNone(result)

    def test_bound_channel_id_as_int_string(self):
        client = CoordinateRuntimeClient(
            cli_path="/fake/coord.sh", db_path="/fake/db.sqlite3"
        )
        with patch.object(client, "_run_cli", return_value={
            "status": "bound",
            "binding": {
                "workspace_id": "gamma",
                "platform": "discord",
                "channel_id": 300,
            },
        }):
            result = _run(client.resolve_channel_workspace(
                platform="discord", channel_id="300"
            ))
        self.assertEqual(result, "gamma")


if __name__ == "__main__":
    unittest.main()
