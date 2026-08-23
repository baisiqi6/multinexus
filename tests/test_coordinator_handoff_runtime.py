"""Runtime tests for DiscordClient._try_coordinator_handoff.

Tests the full handoff auto-accept flow: accept failure (blocker report),
accept success (with/without bootstrap), and adapter invocation.
"""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from multinexus.adapters.base import AdapterResult
from multinexus.models import AgentConfig, KnownAgentMention
from multinexus.sessions.scope import task_scope
from multinexus.sessions.store import SessionStore


def _make_config(**overrides):
    defaults = dict(
        id="mac-claude",
        token="fake-token",
        adapter="claude",
        display_name="Claude",
        known_agents=[
            KnownAgentMention(
                id="mac-claude", primary_name="Claude",
                kind="managed", discord_user_id=111,
            ),
        ],
        work_dir="/tmp/test",
        coordinator_bot_id=999,
        coordinator_cli_path="/fake/mac.sh",
        coordinator_db_path="/fake/db.sqlite3",
        coordinator_workspace_path="/fake/workspace",
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _make_handoff_message(content=None, author_id=999, channel_id=500):
    """Build a mock Discord message that looks like a coordinator handoff."""
    if content is None:
        content = (
            "[handoff] <@111> workspace_id=multinexus "
            "task_id=phase-5.1 action=assignment.accept "
            "bootstrap=docs/project-harness/tasks/phase-5.1/worker-bootstrap.md "
            "context_version=1 workspace_path=/fake/workspace "
            "harness_root=/fake/harness branch=main"
        )
    msg = MagicMock(spec=discord.Message)
    msg.id = 123456
    msg.content = content
    msg.author.id = author_id
    msg.author.bot = True
    msg.channel = MagicMock(spec=discord.TextChannel)
    msg.channel.id = channel_id
    msg.channel.send = AsyncMock()
    placeholder = MagicMock()
    placeholder.edit = AsyncMock()
    msg.channel.send.return_value = placeholder
    return msg


def _make_runtime_client(config=None):
    """Create a DiscordClient-like object with all handoff dependencies mocked."""
    from multinexus.client import DiscordClient

    config = config or _make_config()

    with patch.object(DiscordClient, "__init__", lambda self, *a, **kw: None):
        instance = DiscordClient.__new__(DiscordClient)

    instance.agent_config = config
    instance._agentd_mode = config.agentd_mode
    instance._coordinate_client = None
    instance.adapter = MagicMock()
    instance.adapter.call = AsyncMock(
        return_value=AdapterResult(text="ok", session_id=None)
    )
    instance.adapter.resume = AsyncMock(
        return_value=AdapterResult(text="resumed", session_id=None)
    )
    # discord.Client.user reads from _connection.user
    mock_user = MagicMock()
    mock_user.id = 111
    instance._connection = MagicMock()
    instance._connection.user = mock_user
    instance._bot_user_id_map = {}
    instance.mention_router = MagicMock()
    instance.mention_router.resolve_handoff_mentions = lambda t: t
    instance.context_store = MagicMock()
    tmpdir = tempfile.mkdtemp()
    instance.session_store = SessionStore(os.path.join(tmpdir, "sessions.sqlite3"))

    # Bind the static helper so tests that check _resolve_channel_id continue to work.
    instance._resolve_channel_id = DiscordClient._resolve_channel_id

    return instance


class TestCoordinatorHandoffAcceptFailure(unittest.TestCase):
    """When assignment accept fails, a blocker report is sent and adapter is NOT called."""

    def test_sends_blocker_report_on_accept_failure(self):
        config = _make_config()
        msg = _make_handoff_message()
        instance = _make_runtime_client(config)

        with (
            patch(
                "multinexus.client.execute_assignment_accept",
                return_value=(False, "lease conflict"),
            ),
            patch("multinexus.client.read_bootstrap", return_value=None),
        ):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    instance._try_coordinator_handoff(msg)
                )
            finally:
                loop.close()

        self.assertTrue(result, "Should return True (handled)")

        sends = msg.channel.send.call_args_list
        self.assertTrue(len(sends) >= 1, "Should have sent at least one message")
        first_send = sends[0]
        sent_text = first_send[0][0]
        self.assertIn("[agent-report]", sent_text)
        self.assertIn("action=blocker", sent_text)
        self.assertIn("workspace_id=multinexus", sent_text)
        self.assertIn("task_id=phase-5.1", sent_text)
        self.assertIn("lease conflict", sent_text)

        # Adapter must NOT have been called
        instance.adapter.call.assert_not_called()

    def test_blocker_report_uses_allowed_mentions_none(self):
        config = _make_config()
        msg = _make_handoff_message()
        instance = _make_runtime_client(config)

        with (
            patch(
                "multinexus.client.execute_assignment_accept",
                return_value=(False, "error"),
            ),
            patch("multinexus.client.read_bootstrap", return_value=None),
        ):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(
                    instance._try_coordinator_handoff(msg)
                )
            finally:
                loop.close()

        sends = msg.channel.send.call_args_list
        self.assertTrue(len(sends) >= 1)
        first_send = sends[0]
        allowed = first_send[1].get("allowed_mentions")
        self.assertIsNotNone(allowed, "allowed_mentions must be set")
        self.assertIsInstance(allowed, discord.AllowedMentions)
        self.assertFalse(allowed.everyone)
        self.assertFalse(allowed.users)
        self.assertFalse(allowed.roles)


class TestCoordinatorHandoffAcceptSuccess(unittest.TestCase):
    """When accept succeeds, accept report is sent and adapter is called with bootstrap prompt."""

    def test_sends_accept_report_and_calls_adapter(self):
        config = _make_config()
        msg = _make_handoff_message()
        instance = _make_runtime_client(config)
        bootstrap_content = "# Worker Bootstrap\nStep 1: Read the plan."

        with (
            patch(
                "multinexus.client.execute_assignment_accept",
                return_value=(True, "accepted"),
            ),
            patch(
                "multinexus.client.read_bootstrap",
                return_value=bootstrap_content,
            ),
        ):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    instance._try_coordinator_handoff(msg)
                )
            finally:
                loop.close()

        self.assertTrue(result)

        # Verify adapter was called
        instance.adapter.call.assert_called_once()
        prompt = instance.adapter.call.call_args[0][0]

        # Prompt must contain bootstrap content
        self.assertIn("Worker Bootstrap", prompt)
        self.assertIn("Step 1: Read the plan.", prompt)
        # Prompt must contain handoff context
        self.assertIn("phase-5.1", prompt)
        self.assertIn("multinexus", prompt)

        # Verify accept report was sent
        sends = msg.channel.send.call_args_list
        accept_send = None
        for s in sends:
            text = s[0][0]
            if "[agent-report]" in text and "action=accept" in text:
                accept_send = s
                break
        self.assertIsNotNone(accept_send, "Should have sent an accept report")
        accept_text = accept_send[0][0]
        self.assertIn("workspace_id=multinexus", accept_text)
        self.assertIn("task_id=phase-5.1", accept_text)

    def test_agentd_mode_submits_handoff_prompt_to_runtime(self):
        config = _make_config(agentd_mode=True)
        msg = _make_handoff_message()
        instance = _make_runtime_client(config)
        instance._agentd_mode = True
        instance.session_store = None
        instance._coordinate_client = MagicMock()
        instance._coordinate_client.submit_request = AsyncMock(
            return_value={"result": {"job": {"id": "request:abc"}}}
        )
        instance._coordinate_client.wait_for_job_result = AsyncMock(
            return_value={
                "id": "request:abc",
                "status": "done",
                "result": {
                    "response_text": (
                        "Implementation finished.\n"
                        "[agent-report] action=done workspace_id=multinexus "
                        "task_id=phase-5.1 summary='tests OK'"
                    )
                },
            }
        )

        with (
            patch(
                "multinexus.client.execute_assignment_accept",
                return_value=(True, "accepted"),
            ),
            patch("multinexus.client.read_bootstrap", return_value="bootstrap"),
        ):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    instance._try_coordinator_handoff(msg, resolved_workspace="multinexus")
                )
            finally:
                loop.close()

        self.assertTrue(result)
        instance.adapter.call.assert_not_called()
        instance._coordinate_client.submit_request.assert_awaited_once()
        submit_kwargs = instance._coordinate_client.submit_request.await_args.kwargs
        self.assertEqual(submit_kwargs["workspace_id"], "multinexus")
        self.assertEqual(submit_kwargs["task_id"], "phase-5.1")
        self.assertEqual(submit_kwargs["target_agent"], "mac-claude")
        self.assertEqual(submit_kwargs["reply_json"]["platform"], "none")
        self.assertIn("bootstrap", submit_kwargs["prompt"])
        self.assertIn("phase-5.1", submit_kwargs["prompt"])
        instance._coordinate_client.wait_for_job_result.assert_awaited_once_with(
            job_id="request:abc",
            workspace_id="multinexus",
            timeout=config.timeout,
        )

        sent_texts = [call.args[0] for call in msg.channel.send.call_args_list]
        done_reports = [
            text for text in sent_texts
            if text.startswith("[agent-report]") and "action=done" in text
        ]
        self.assertEqual(done_reports, [])
        edited_texts = [
            call.kwargs["content"]
            for call in msg.channel.send.return_value.edit.await_args_list
            if "content" in call.kwargs
        ]
        self.assertIn("Implementation finished.", edited_texts)

    def test_agentd_mode_does_not_emit_missing_report_fallback(self):
        config = _make_config(agentd_mode=True)
        msg = _make_handoff_message()
        instance = _make_runtime_client(config)
        instance._agentd_mode = True
        instance.session_store = None
        instance._coordinate_client = MagicMock()
        instance._coordinate_client.submit_request = AsyncMock(
            return_value={"result": {"job": {"id": "request:no-report"}}}
        )
        instance._coordinate_client.wait_for_job_result = AsyncMock(
            return_value={
                "id": "request:no-report",
                "status": "done",
                "result": {"response_text": "Implementation finished."},
            }
        )

        with (
            patch(
                "multinexus.client.execute_assignment_accept",
                return_value=(True, "accepted"),
            ),
            patch("multinexus.client.read_bootstrap", return_value="bootstrap"),
        ):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    instance._try_coordinator_handoff(msg, resolved_workspace="multinexus")
                )
            finally:
                loop.close()

        self.assertTrue(result)
        sent_texts = [call.args[0] for call in msg.channel.send.call_args_list]
        progress_reports = [
            text for text in sent_texts
            if text.startswith("[agent-report]") and "action=progress" in text
        ]
        self.assertEqual(progress_reports, [])

    def test_accept_output_bootstrap_text_skips_workspace_file_read(self):
        config = _make_config(agentd_mode=True)
        msg = _make_handoff_message()
        instance = _make_runtime_client(config)
        instance._agentd_mode = True
        instance.session_store = None
        instance._coordinate_client = MagicMock()
        instance._coordinate_client.submit_request = AsyncMock(
            return_value={"result": {"job": {"id": "request:abc"}}}
        )
        instance._coordinate_client.wait_for_job_result = AsyncMock(
            return_value={
                "id": "request:abc",
                "status": "done",
                "result": {"response_text": "done"},
            }
        )
        accept_output = (
            '{"result":{"bootstrap_text":"# Worker Bootstrap\\n'
            'cd /home/example/multinexus"}}'
        )

        with (
            patch(
                "multinexus.client.execute_assignment_accept",
                return_value=(True, accept_output),
            ),
            patch("multinexus.client.read_bootstrap", return_value="server bootstrap") as read_bootstrap,
        ):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    instance._try_coordinator_handoff(msg, resolved_workspace="multinexus")
                )
            finally:
                loop.close()

        self.assertTrue(result)
        read_bootstrap.assert_not_called()
        submit_kwargs = instance._coordinate_client.submit_request.await_args.kwargs
        self.assertIn("/home/example/multinexus", submit_kwargs["prompt"])
        self.assertNotIn("server bootstrap", submit_kwargs["prompt"])


def _make_legacy_handoff_message():
    """Legacy handoff without v1 execution context fields."""
    return _make_handoff_message(
        content=(
            "[handoff] <@111> workspace_id=multinexus "
            "task_id=phase-5.1 action=assignment.accept "
            "bootstrap=docs/project-harness/tasks/phase-5.1/worker-bootstrap.md"
        )
    )


def _make_partial_v1_handoff_message():
    """v1 handoff missing the required harness_root field."""
    return _make_handoff_message(
        content=(
            "[handoff] <@111> workspace_id=multinexus "
            "task_id=phase-5.1 action=assignment.accept "
            "bootstrap=docs/project-harness/tasks/phase-5.1/worker-bootstrap.md "
            "context_version=1 workspace_path=/fake/workspace branch=main"
        )
    )


def _make_partial_v1_review_handoff_message():
    """v1 review handoff missing the required harness_root field."""
    return _make_handoff_message(
        content=(
            "[handoff] <@111> workspace_id=multinexus "
            "task_id=phase-5.1 action=review.begin "
            "bootstrap=docs/project-harness/tasks/phase-5.1/reviewer-bootstrap.md "
            "context_version=1 workspace_path=/fake/workspace branch=main"
        )
    )


class TestCoordinatorHandoffV1Authority(unittest.TestCase):
    """Managed agentd_mode must reject legacy/partial handoffs before any authority use."""

    def _run_handoff(self, instance, msg):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(instance._try_coordinator_handoff(msg))
        finally:
            loop.close()

    def test_agentd_mode_rejects_legacy_worker_handoff_before_assignment_accept(self):
        config = _make_config(agentd_mode=True)
        msg = _make_legacy_handoff_message()
        instance = _make_runtime_client(config)
        instance._agentd_mode = True
        instance.session_store = None
        instance._coordinate_client = MagicMock()
        instance._coordinate_client.submit_request = AsyncMock(
            return_value={"result": {"job": {"id": "request:legacy"}}}
        )

        with (
            patch(
                "multinexus.client.execute_assignment_accept",
                return_value=(True, "accepted"),
            ) as accept,
            patch("multinexus.client.read_bootstrap", return_value="bootstrap") as read_bootstrap,
            patch(
                "multinexus.client.resolve_workspace_path",
                return_value="/sqlite/workspace",
            ) as resolve_workspace,
        ):
            result = self._run_handoff(instance, msg)

        self.assertTrue(result, "Should return True (handled)")
        accept.assert_not_called()
        read_bootstrap.assert_not_called()
        resolve_workspace.assert_not_called()
        instance._coordinate_client.submit_request.assert_not_called()
        instance.adapter.call.assert_not_called()

        sends = msg.channel.send.call_args_list
        self.assertTrue(len(sends) >= 1)
        sent_text = sends[0][0][0]
        self.assertIn("[agent-report]", sent_text)
        self.assertIn("action=blocker", sent_text)
        self.assertIn("managed mode requires a complete v1 handoff authority", sent_text)

    def test_agentd_mode_rejects_legacy_review_handoff_before_bootstrap_read(self):
        config = _make_config(agentd_mode=True)
        msg = _make_handoff_message(
            content=(
                "[handoff] <@111> workspace_id=multinexus "
                "task_id=phase-5.1 action=review.begin "
                "bootstrap=docs/project-harness/tasks/phase-5.1/reviewer-bootstrap.md"
            )
        )
        instance = _make_runtime_client(config)
        instance._agentd_mode = True
        instance.session_store = None
        instance._coordinate_client = MagicMock()
        instance._coordinate_client.submit_request = AsyncMock(
            return_value={"result": {"job": {"id": "request:review-legacy"}}}
        )

        with (
            patch("multinexus.client.read_bootstrap", return_value="review bootstrap") as read_bootstrap,
            patch(
                "multinexus.client.resolve_workspace_path",
                return_value="/sqlite/workspace",
            ) as resolve_workspace,
        ):
            result = self._run_handoff(instance, msg)

        self.assertTrue(result, "Should return True (handled)")
        read_bootstrap.assert_not_called()
        resolve_workspace.assert_not_called()
        instance._coordinate_client.submit_request.assert_not_called()
        instance.adapter.call.assert_not_called()

        sends = msg.channel.send.call_args_list
        self.assertTrue(len(sends) >= 1)
        sent_text = sends[0][0][0]
        self.assertIn("[agent-report]", sent_text)
        self.assertIn("action=blocker", sent_text)
        self.assertIn("managed mode requires a complete v1 handoff authority", sent_text)

    def test_agentd_mode_partial_v1_handoff_emits_blocker_before_assignment_accept(self):
        config = _make_config(agentd_mode=True)
        msg = _make_partial_v1_handoff_message()
        instance = _make_runtime_client(config)
        instance._agentd_mode = True
        instance.session_store = None
        instance._coordinate_client = MagicMock()

        with (
            patch(
                "multinexus.client.execute_assignment_accept",
                return_value=(True, "accepted"),
            ) as accept,
            patch("multinexus.client.read_bootstrap", return_value="bootstrap") as read_bootstrap,
            patch(
                "multinexus.client.resolve_workspace_path",
                return_value="/sqlite/workspace",
            ) as resolve_workspace,
        ):
            result = self._run_handoff(instance, msg)

        # Malformed v1 is recognized as a candidate in diagnostic mode, then
        # rejected by the managed authority gate with exactly one visible blocker.
        self.assertTrue(result, "Should return True (handled)")
        accept.assert_not_called()
        read_bootstrap.assert_not_called()
        resolve_workspace.assert_not_called()
        instance._coordinate_client.submit_request.assert_not_called()
        instance.adapter.call.assert_not_called()

        sends = msg.channel.send.call_args_list
        self.assertEqual(len(sends), 1)
        sent_text = sends[0][0][0]
        self.assertIn("[agent-report]", sent_text)
        self.assertIn("action=blocker", sent_text)
        self.assertIn("managed mode requires a complete v1 handoff authority", sent_text)

    def test_agentd_mode_partial_v1_review_handoff_emits_blocker_before_bootstrap_read(self):
        config = _make_config(agentd_mode=True)
        msg = _make_partial_v1_review_handoff_message()
        instance = _make_runtime_client(config)
        instance._agentd_mode = True
        instance.session_store = None
        instance._coordinate_client = MagicMock()

        with (
            patch("multinexus.client.read_bootstrap", return_value="review bootstrap") as read_bootstrap,
            patch(
                "multinexus.client.resolve_workspace_path",
                return_value="/sqlite/workspace",
            ) as resolve_workspace,
        ):
            result = self._run_handoff(instance, msg)

        self.assertTrue(result, "Should return True (handled)")
        read_bootstrap.assert_not_called()
        resolve_workspace.assert_not_called()
        instance._coordinate_client.submit_request.assert_not_called()
        instance.adapter.call.assert_not_called()

        sends = msg.channel.send.call_args_list
        self.assertEqual(len(sends), 1)
        sent_text = sends[0][0][0]
        self.assertIn("[agent-report]", sent_text)
        self.assertIn("action=blocker", sent_text)
        self.assertIn("managed mode requires a complete v1 handoff authority", sent_text)

    def test_accept_report_uses_allowed_mentions_none(self):
        config = _make_config()
        msg = _make_handoff_message()
        instance = _make_runtime_client(config)

        with (
            patch(
                "multinexus.client.execute_assignment_accept",
                return_value=(True, "accepted"),
            ),
            patch("multinexus.client.read_bootstrap", return_value="bootstrap"),
        ):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(
                    instance._try_coordinator_handoff(msg)
                )
            finally:
                loop.close()

        sends = msg.channel.send.call_args_list
        for s in sends:
            text = s[0][0]
            if "[agent-report]" in text and "action=accept" in text:
                allowed = s[1].get("allowed_mentions")
                self.assertIsNotNone(allowed, "allowed_mentions must be set")
                self.assertIsInstance(allowed, discord.AllowedMentions)
                self.assertFalse(allowed.everyone)
                self.assertFalse(allowed.users)
                self.assertFalse(allowed.roles)
                return
        self.fail("No accept report found")

    def test_sends_progress_fallback_when_adapter_omits_agent_report(self):
        config = _make_config()
        msg = _make_handoff_message()
        instance = _make_runtime_client(config)
        instance.adapter.call = AsyncMock(
            return_value=AdapterResult(text="Implementation finished.", session_id=None)
        )

        with (
            patch(
                "multinexus.client.execute_assignment_accept",
                return_value=(True, "accepted"),
            ),
            patch("multinexus.client.read_bootstrap", return_value="bootstrap"),
        ):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(instance._try_coordinator_handoff(msg))
            finally:
                loop.close()

        sent_texts = [call.args[0] for call in msg.channel.send.call_args_list]
        fallback = [
            text for text in sent_texts
            if "[agent-report]" in text and "action=progress" in text
        ]
        self.assertEqual(len(fallback), 1)
        self.assertIn("without a structured agent-report", fallback[0])

    def test_sends_progress_fallback_when_adapter_only_echoes_accept_report(self):
        config = _make_config()
        msg = _make_handoff_message()
        instance = _make_runtime_client(config)
        instance.adapter.call = AsyncMock(
            return_value=AdapterResult(
                text=(
                    "[agent-report] action=accept workspace_id=multinexus "
                    "task_id=phase-5.1 summary='auto accepted by mac-claude'\n"
                    "Round 3 rework complete. Requesting review."
                ),
                session_id=None,
            )
        )

        with (
            patch(
                "multinexus.client.execute_assignment_accept",
                return_value=(True, "accepted"),
            ),
            patch("multinexus.client.read_bootstrap", return_value="bootstrap"),
        ):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(instance._try_coordinator_handoff(msg))
            finally:
                loop.close()

        sent_texts = [call.args[0] for call in msg.channel.send.call_args_list]
        fallback = [
            text for text in sent_texts
            if "[agent-report]" in text and "action=progress" in text
        ]
        self.assertEqual(len(fallback), 1)
        self.assertIn("without a structured agent-report", fallback[0])

    def test_does_not_send_progress_fallback_when_adapter_reports_done(self):
        config = _make_config()
        msg = _make_handoff_message()
        instance = _make_runtime_client(config)
        instance.adapter.call = AsyncMock(
            return_value=AdapterResult(
                text=(
                    "Done.\n"
                    "[agent-report] action=done workspace_id=multinexus "
                    "task_id=phase-5.1 summary='tests OK'"
                ),
                session_id=None,
            )
        )

        with (
            patch(
                "multinexus.client.execute_assignment_accept",
                return_value=(True, "accepted"),
            ),
            patch("multinexus.client.read_bootstrap", return_value="bootstrap"),
        ):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(instance._try_coordinator_handoff(msg))
            finally:
                loop.close()

        sent_texts = [call.args[0] for call in msg.channel.send.call_args_list]
        fallback = [
            text for text in sent_texts
            if "[agent-report]" in text and "action=progress" in text
        ]
        self.assertEqual(fallback, [])
        done_reports = [
            text for text in sent_texts
            if text.startswith("[agent-report]") and "action=done" in text
        ]
        self.assertEqual(len(done_reports), 1)
        self.assertIn("summary='tests OK'", done_reports[0])


class TestManagedHandoffResponseContextAndV1Reject(unittest.TestCase):
    """Managed handoff: response writes qualified context; v1+resolved_workspace=None rejects."""

    def test_managed_handoff_response_writes_qualified_context(self):
        """Managed handoff response must write back to workspace-qualified context scope."""
        config = _make_config(agentd_mode=True)
        msg = _make_handoff_message()
        instance = _make_runtime_client(config)
        instance._agentd_mode = True
        instance.session_store = None
        instance._coordinate_client = MagicMock()
        instance._coordinate_client.submit_request = AsyncMock(
            return_value={"result": {"job": {"id": "request:ctx"}}}
        )
        instance._coordinate_client.wait_for_job_result = AsyncMock(
            return_value={
                "id": "request:ctx",
                "status": "done",
                "result": {"response_text": "done"},
            }
        )

        with (
            patch(
                "multinexus.client.execute_assignment_accept",
                return_value=(True, "accepted"),
            ),
            patch("multinexus.client.read_bootstrap", return_value="bootstrap"),
        ):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(
                    instance._try_coordinator_handoff(msg, resolved_workspace="multinexus")
                )
            finally:
                loop.close()

        # Response was recorded with workspace-qualified context scope.
        from multinexus.sessions.scope import workspace_channel_context_scope

        record_calls = instance.context_store.record_message.call_args_list
        # Find the response message record (author_is_bot=True).
        response_record = None
        for call in record_calls:
            kwargs = call.kwargs
            if kwargs.get("author_is_bot") and "response:" in kwargs.get("message_id", ""):
                response_record = kwargs
                break
        self.assertIsNotNone(response_record, "Response should be recorded to context store")
        expected = workspace_channel_context_scope("multinexus", "discord", 500)
        self.assertEqual(response_record["channel_id"], expected)

    def test_v1_complete_but_no_resolved_workspace_rejects_before_accept(self):
        """Complete v1 handoff with resolved_workspace=None rejects before accept/bootstrap/submit."""
        config = _make_config(agentd_mode=True)
        msg = _make_handoff_message()
        instance = _make_runtime_client(config)
        instance._agentd_mode = True
        instance.session_store = None
        instance._coordinate_client = MagicMock()

        with (
            patch(
                "multinexus.client.execute_assignment_accept",
                return_value=(True, "accepted"),
            ) as accept,
            patch("multinexus.client.read_bootstrap", return_value="bootstrap") as read_bootstrap,
            patch(
                "multinexus.client.resolve_workspace_path",
                return_value="/sqlite/workspace",
            ) as resolve_workspace,
        ):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    instance._try_coordinator_handoff(msg, resolved_workspace=None)
                )
            finally:
                loop.close()

        self.assertTrue(result, "Should return True (handled)")
        accept.assert_not_called()
        read_bootstrap.assert_not_called()
        resolve_workspace.assert_not_called()
        instance._coordinate_client.submit_request.assert_not_called()

        sends = msg.channel.send.call_args_list
        self.assertTrue(len(sends) >= 1)
        sent_text = sends[0][0][0]
        self.assertIn("[agent-report]", sent_text)
        self.assertIn("action=blocker", sent_text)
        self.assertIn("requires a resolved channel workspace", sent_text)


class TestCoordinatorReviewHandoffReports(unittest.TestCase):
    def _review_message(self):
        return _make_handoff_message(
            content=(
                "[handoff] <@111> workspace_id=multinexus "
                "task_id=phase-5.1 action=review.begin "
                "bootstrap=docs/project-harness/tasks/phase-5.1/reviewer-bootstrap.md "
                "context_version=1 workspace_path=/fake/workspace "
                "harness_root=/fake/harness branch=main"
            )
        )

    def test_agentd_mode_keeps_review_report_out_of_discord(self):
        config = _make_config(agentd_mode=True)
        msg = self._review_message()
        instance = _make_runtime_client(config)
        instance._agentd_mode = True
        instance.session_store = None
        instance._coordinate_client = MagicMock()
        instance._coordinate_client.submit_request = AsyncMock(
            return_value={"result": {"job": {"id": "request:review"}}}
        )
        instance._coordinate_client.wait_for_job_result = AsyncMock(
            return_value={
                "id": "request:review",
                "status": "done",
                "result": {
                    "response_text": (
                        "Review approved.\n"
                        "[agent-report] decision=approve workspace_id=multinexus "
                        "task_id=phase-5.1 summary='looks good'"
                    )
                },
            }
        )

        with (
            patch("multinexus.client.read_bootstrap", return_value="review bootstrap"),
        ):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    instance._try_coordinator_handoff(msg, resolved_workspace="multinexus")
                )
            finally:
                loop.close()

        self.assertTrue(result)
        submit_kwargs = instance._coordinate_client.submit_request.await_args.kwargs
        self.assertEqual(submit_kwargs["reply_json"]["platform"], "none")
        self.assertEqual(submit_kwargs["workspace_id"], "multinexus")
        sent_texts = [call.args[0] for call in msg.channel.send.call_args_list]
        decision_reports = [
            text for text in sent_texts
            if text.startswith("[agent-report]") and "decision=approve" in text
        ]
        self.assertEqual(decision_reports, [])
        edited_texts = [
            call.kwargs["content"]
            for call in msg.channel.send.return_value.edit.await_args_list
            if "content" in call.kwargs
        ]
        self.assertIn("Review approved.", edited_texts)

    def test_direct_mode_still_emits_review_report(self):
        config = _make_config()
        msg = self._review_message()
        instance = _make_runtime_client(config)
        instance.adapter.call = AsyncMock(
            return_value=AdapterResult(
                text=(
                    "Review approved.\n"
                    "[agent-report] decision=approve workspace_id=multinexus "
                    "task_id=phase-5.1 summary='looks good'"
                ),
                session_id=None,
            )
        )

        with patch("multinexus.client.read_bootstrap", return_value="review bootstrap"):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(instance._try_coordinator_handoff(msg))
            finally:
                loop.close()

        self.assertTrue(result)
        sent_texts = [call.args[0] for call in msg.channel.send.call_args_list]
        decision_reports = [
            text for text in sent_texts
            if text.startswith("[agent-report]") and "decision=approve" in text
        ]
        self.assertEqual(len(decision_reports), 1)
        self.assertIn("summary='looks good'", decision_reports[0])


class TestCoordinatorHandoffBootstrapMissing(unittest.TestCase):
    """When bootstrap is missing, adapter is still called with a prompt noting bootstrap is missing."""

    def test_calls_adapter_with_bootstrap_missing_message(self):
        config = _make_config()
        msg = _make_handoff_message()
        instance = _make_runtime_client(config)

        with (
            patch(
                "multinexus.client.execute_assignment_accept",
                return_value=(True, "accepted"),
            ),
            patch(
                "multinexus.client.read_bootstrap",
                return_value=None,
            ),
        ):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    instance._try_coordinator_handoff(msg)
                )
            finally:
                loop.close()

        self.assertTrue(result)
        # Adapter must still be called
        instance.adapter.call.assert_called_once()
        prompt = instance.adapter.call.call_args[0][0]
        # Prompt must indicate bootstrap is missing
        self.assertIn("未找到 bootstrap", prompt)


class TestCoordinatorHandoffTaskScope(unittest.TestCase):
    """Coordinator handoffs use task-scoped sessions instead of channel sessions."""

    def _run_handoff(self, instance, msg):
        with (
            patch(
                "multinexus.client.execute_assignment_accept",
                return_value=(True, "accepted"),
            ),
            patch("multinexus.client.read_bootstrap", return_value="bootstrap"),
        ):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(
                    instance._try_coordinator_handoff(msg)
                )
            finally:
                loop.close()

    def test_handoff_saves_task_scope_not_channel_scope(self):
        config = _make_config()
        msg = _make_handoff_message(channel_id=500)
        instance = _make_runtime_client(config)
        instance.adapter.call = AsyncMock(
            return_value=AdapterResult(text="ok", session_id="sess-task")
        )

        result = self._run_handoff(instance, msg)

        self.assertTrue(result)
        scoped = instance.session_store.get(
            scope_id=task_scope("multinexus", "phase-5.1"),
            agent_id="mac-claude",
        )
        self.assertIsNotNone(scoped)
        self.assertEqual(scoped["session_id"], "sess-task")
        self.assertIsNone(
            instance.session_store.get(scope_id="channel:500", agent_id="mac-claude")
        )
        self.assertIsNone(
            instance.session_store.get(scope_id="500", agent_id="mac-claude")
        )

    def test_same_task_resumes_existing_session(self):
        config = _make_config()
        msg = _make_handoff_message()
        instance = _make_runtime_client(config)
        instance.adapter.call = AsyncMock(
            return_value=AdapterResult(text="first", session_id="sess-task")
        )
        instance.adapter.resume = AsyncMock(
            return_value=AdapterResult(text="second", session_id="sess-task")
        )

        self._run_handoff(instance, msg)
        self._run_handoff(instance, msg)

        instance.adapter.call.assert_called_once()
        instance.adapter.resume.assert_called_once()
        self.assertEqual(instance.adapter.resume.call_args.args[0], "sess-task")
        scoped = instance.session_store.get(
            scope_id=task_scope("multinexus", "phase-5.1"),
            agent_id="mac-claude",
        )
        self.assertEqual(scoped["turn_count"], 2)

    def test_different_tasks_do_not_reuse_session(self):
        config = _make_config()
        first = _make_handoff_message()
        second = _make_handoff_message(
            content=(
                "[handoff] <@111> workspace_id=multinexus "
                "task_id=phase-5.2 action=assignment.accept "
                "bootstrap=docs/project-harness/tasks/phase-5.2/worker-bootstrap.md"
            )
        )
        instance = _make_runtime_client(config)
        instance.adapter.call = AsyncMock(
            side_effect=[
                AdapterResult(text="first", session_id="sess-5.1"),
                AdapterResult(text="second", session_id="sess-5.2"),
            ]
        )

        self._run_handoff(instance, first)
        self._run_handoff(instance, second)

        self.assertEqual(instance.adapter.call.call_count, 2)
        instance.adapter.resume.assert_not_called()
        self.assertEqual(
            instance.session_store.get(
                scope_id=task_scope("multinexus", "phase-5.1"),
                agent_id="mac-claude",
            )["session_id"],
            "sess-5.1",
        )
        self.assertEqual(
            instance.session_store.get(
                scope_id=task_scope("multinexus", "phase-5.2"),
                agent_id="mac-claude",
            )["session_id"],
            "sess-5.2",
        )

    def test_closeout_archives_task_session_before_next_handoff(self):
        config = _make_config()
        handoff_msg = _make_handoff_message()
        closeout_msg = _make_handoff_message(
            content=(
                "[handoff] <@111> workspace_id=multinexus "
                "task_id=phase-5.1 action=assignment.closeout"
            )
        )
        instance = _make_runtime_client(config)
        instance.adapter.call = AsyncMock(
            side_effect=[
                AdapterResult(text="first", session_id="sess-old"),
                AdapterResult(text="fresh", session_id="sess-new"),
            ]
        )
        instance.adapter.resume = AsyncMock(
            return_value=AdapterResult(text="should not resume", session_id="sess-old")
        )

        self._run_handoff(instance, handoff_msg)
        loop = asyncio.new_event_loop()
        try:
            handled = loop.run_until_complete(
                instance._try_coordinator_lifecycle(closeout_msg)
            )
        finally:
            loop.close()
        self.assertTrue(handled)
        self.assertIsNone(
            instance.session_store.get(
                scope_id=task_scope("multinexus", "phase-5.1"),
                agent_id="mac-claude",
            )
        )

        self._run_handoff(instance, handoff_msg)

        self.assertEqual(instance.adapter.call.call_count, 2)
        instance.adapter.resume.assert_not_called()
        scoped = instance.session_store.get(
            scope_id=task_scope("multinexus", "phase-5.1"),
            agent_id="mac-claude",
        )
        self.assertEqual(scoped["session_id"], "sess-new")


class TestCoordinatorHandoffNotCoordinatorMessage(unittest.TestCase):
    """Non-coordinator messages are not handled."""

    def test_returns_false_for_non_matching_content(self):
        config = _make_config()
        # Content without proper handoff format
        msg = _make_handoff_message(content="<@111> hello there")
        instance = _make_runtime_client(config)

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                instance._try_coordinator_handoff(msg)
            )
        finally:
            loop.close()

        self.assertFalse(result)
        instance.adapter.call.assert_not_called()


class TestActionScopeConstraint(unittest.TestCase):
    """Only assignment.accept is auto-executed. Other actions must be rejected."""

    def test_rejects_mark_done_action(self):
        handoff_msg = (
            "[handoff] <@111> workspace_id=multinexus "
            "task_id=phase-5.1 action=assignment.mark-done"
        )
        from multinexus.handoff_handler import parse_coordinator_handoff
        result = parse_coordinator_handoff(
            handoff_msg, my_discord_user_id=111
        )
        self.assertIsNone(result)

    def test_rejects_closeout_action(self):
        handoff_msg = (
            "[handoff] <@111> workspace_id=multinexus "
            "task_id=phase-5.1 action=assignment.closeout"
        )
        from multinexus.handoff_handler import parse_coordinator_handoff
        result = parse_coordinator_handoff(
            handoff_msg, my_discord_user_id=111
        )
        self.assertIsNone(result)

    def test_rejects_merge_action(self):
        handoff_msg = (
            "[handoff] <@111> workspace_id=multinexus "
            "task_id=phase-5.1 action=merge"
        )
        from multinexus.handoff_handler import parse_coordinator_handoff
        result = parse_coordinator_handoff(
            handoff_msg, my_discord_user_id=111
        )
        self.assertIsNone(result)

    def test_rejects_deploy_action(self):
        handoff_msg = (
            "[handoff] <@111> workspace_id=multinexus "
            "task_id=phase-5.1 action=deploy"
        )
        from multinexus.handoff_handler import parse_coordinator_handoff
        result = parse_coordinator_handoff(
            handoff_msg, my_discord_user_id=111
        )
        self.assertIsNone(result)

    def test_rejects_pr_action(self):
        handoff_msg = (
            "[handoff] <@111> workspace_id=multinexus "
            "task_id=phase-5.1 action=pr.create"
        )
        from multinexus.handoff_handler import parse_coordinator_handoff
        result = parse_coordinator_handoff(
            handoff_msg, my_discord_user_id=111
        )
        self.assertIsNone(result)

    def test_accepts_only_assignment_accept(self):
        handoff_msg = (
            "[handoff] <@111> workspace_id=multinexus "
            "task_id=phase-5.1 action=assignment.accept"
        )
        from multinexus.handoff_handler import parse_coordinator_handoff
        result = parse_coordinator_handoff(
            handoff_msg, my_discord_user_id=111
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.action, "assignment.accept")


class TestLifecycleSessionStoreNone(unittest.TestCase):
    """agentd_mode bridge with session_store=None must not traceback on lifecycle."""

    def test_lifecycle_no_session_store_returns_handled(self):
        instance = _make_runtime_client()
        instance.session_store = None
        lifecycle_msg = _make_handoff_message(
            content=(
                "[lifecycle] <@111> workspace_id=multinexus "
                "task_id=phase-5.1 action=task.done"
            )
        )
        loop = asyncio.new_event_loop()
        try:
            handled = loop.run_until_complete(
                instance._try_coordinator_lifecycle(lifecycle_msg)
            )
        finally:
            loop.close()
        self.assertTrue(handled)


class TestCoordinatorHandoffOutcomeContract(unittest.TestCase):
    """R2: handoff is_error reads the machine outcome, never the visible text.

    Legacy (direct adapter) path uses AdapterResult.effective_outcome();
    agentd path keeps Coordinate status as terminal authority, then prefers
    a structured outcome projection over the text seam for done jobs.
    """

    def _run_handoff(self, instance, msg, *, resolved_workspace=None):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                instance._try_coordinator_handoff(
                    msg, resolved_workspace=resolved_workspace
                )
            )
        finally:
            loop.close()

    def _legacy_instance(self, call_result):
        config = _make_config()
        msg = _make_handoff_message()
        instance = _make_runtime_client(config)
        instance.adapter.call = AsyncMock(return_value=call_result)
        return instance, msg

    def _run_legacy_handoff(self, instance, msg):
        with (
            patch(
                "multinexus.client.execute_assignment_accept",
                return_value=(True, "accepted"),
            ),
            patch("multinexus.client.read_bootstrap", return_value="# b"),
        ):
            self._run_handoff(instance, msg)

    def _agentd_instance(self, *, completed):
        config = _make_config(agentd_mode=True)
        msg = _make_handoff_message()
        instance = _make_runtime_client(config)
        instance._agentd_mode = True
        instance.session_store = None
        instance._coordinate_client = MagicMock()
        instance._coordinate_client.submit_request = AsyncMock(
            return_value={"result": {"job": {"id": "request:agentd"}}}
        )
        instance._coordinate_client.wait_for_job_result = AsyncMock(
            return_value=completed
        )
        return instance, msg

    def _run_agentd_handoff(self, instance, msg):
        with (
            patch(
                "multinexus.client.execute_assignment_accept",
                return_value=(True, "accepted"),
            ),
            patch("multinexus.client.read_bootstrap", return_value="bootstrap"),
        ):
            self._run_handoff(instance, msg, resolved_workspace="multinexus")

    def test_legacy_is_error_reads_outcome_not_text(self):
        from multinexus.adapters.base import OUTCOME_FAILED, OUTCOME_SUCCESS

        cases = [
            # (name, reply text, outcome, context should be recorded)
            ("success outcome with error-like text", "OpenCode CLI failed", OUTCOME_SUCCESS, True),
            ("failed outcome with plain text", "looks fine", OUTCOME_FAILED, False),
        ]
        for name, text, outcome, records in cases:
            with self.subTest(name=name):
                instance, msg = self._legacy_instance(
                    AdapterResult(
                        text=text,
                        outcome=outcome,
                        error_category=(
                            "provider_error" if outcome == OUTCOME_FAILED else None
                        ),
                        session_id=None,
                    )
                )
                self._run_legacy_handoff(instance, msg)
                if records:
                    instance.context_store.record_message.assert_called()
                else:
                    instance.context_store.record_message.assert_not_called()

    def test_agentd_structured_outcome_governs_done_jobs(self):
        cases = [
            # (name, completed dict, context should be recorded)
            (
                "failed outcome with plain text",
                {
                    "id": "request:sf",
                    "status": "done",
                    "result": {
                        "response_text": "looks fine",
                        "outcome": "failed",
                        "error_category": "provider_error",
                    },
                },
                False,
            ),
            (
                "success outcome with error-like text",
                {
                    "id": "request:ss",
                    "status": "done",
                    "result": {
                        "response_text": "OpenCode CLI failed",
                        "outcome": "success",
                    },
                },
                True,
            ),
        ]
        for name, completed, records in cases:
            with self.subTest(name=name):
                instance, msg = self._agentd_instance(completed=completed)
                self._run_agentd_handoff(instance, msg)
                instance._coordinate_client.submit_request.assert_awaited_once()
                if records:
                    instance.context_store.record_message.assert_called()
                else:
                    instance.context_store.record_message.assert_not_called()

    def test_agentd_non_done_status_is_error_regardless_of_outcome(self):
        """Coordinate job status is terminal authority over any projection."""
        instance, msg = self._agentd_instance(
            completed={
                "id": "request:conflict",
                "status": "failed",
                "result": {"response_text": "plain", "outcome": "success"},
            }
        )
        self._run_agentd_handoff(instance, msg)
        instance._coordinate_client.submit_request.assert_awaited_once()
        instance.context_store.record_message.assert_not_called()

    def test_agentd_malformed_outcome_falls_back_to_text_seam(self):
        cases = [
            # malformed outcome + error-like text => error via text seam
            ({"response_text": "OpenCode CLI failed", "outcome": "bogus"}, False),
            # malformed outcome + plain text => legacy seam decides success
            ({"response_text": "plain reply", "outcome": "bogus"}, True),
        ]
        for result, records in cases:
            with self.subTest(result=result):
                instance, msg = self._agentd_instance(
                    completed={
                        "id": "request:malformed",
                        "status": "done",
                        "result": result,
                    }
                )
                self._run_agentd_handoff(instance, msg)
                if records:
                    instance.context_store.record_message.assert_called()
                else:
                    instance.context_store.record_message.assert_not_called()

    def test_agentd_legacy_payload_keeps_status_text_seam(self):
        instance, msg = self._agentd_instance(
            completed={
                "id": "request:legacy",
                "status": "failed",
                "result": {"response_text": "plain reply"},
            }
        )
        self._run_agentd_handoff(instance, msg)
        instance._coordinate_client.submit_request.assert_awaited_once()
        instance.context_store.record_message.assert_not_called()


class TestSendAdapterResponseContextDecision(unittest.TestCase):
    """R2: legacy context persistence follows the machine outcome, not text."""

    def _send(self, instance, response_text, is_error):
        channel = MagicMock()
        channel.send = AsyncMock(return_value=MagicMock())
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                instance._send_adapter_response(
                    response_text,
                    channel,
                    None,
                    context_channel_id="ch-ctx",
                    is_error=is_error,
                )
            )
        finally:
            loop.close()

    def test_context_persistence_follows_outcome_not_text(self):
        cases = [
            ("error-like text with success outcome", "OpenCode CLI failed", False, True),
            ("plain text with failed outcome", "looks fine", True, False),
        ]
        for name, text, is_error, records in cases:
            with self.subTest(name=name):
                instance = _make_runtime_client()
                self._send(instance, text, is_error)
                if records:
                    instance.context_store.record_message.assert_called()
                else:
                    instance.context_store.record_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
