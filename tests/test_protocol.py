"""Tests for the platform-agnostic request/response envelope."""

import json
import unittest

from multinexus.protocol import (
    AgentRequest,
    AgentResponse,
    Platform,
    PlatformDestination,
    PlatformOrigin,
)


class TestPlatformOrigin(unittest.TestCase):
    def test_discord_origin(self):
        o = PlatformOrigin(
            platform=Platform.DISCORD,
            channel_id="123",
            thread_id="456",
            message_id="789",
            guild_id="000",
        )
        self.assertEqual(o.platform, Platform.DISCORD)
        self.assertEqual(o.channel_id, "123")
        self.assertIsNone(o.role_id)

    def test_kook_origin_with_role(self):
        o = PlatformOrigin(
            platform=Platform.KOOK,
            channel_id="abc",
            message_id="def",
            role_id="999",
        )
        self.assertEqual(o.platform, Platform.KOOK)
        self.assertEqual(o.role_id, "999")
        self.assertIsNone(o.thread_id)


class TestAgentRequestSerialization(unittest.TestCase):
    def test_roundtrip_minimal(self):
        req = AgentRequest(request_id="r1", agent_id="mac-claude", prompt="hello")
        j = req.to_json()
        parsed = json.loads(j)
        self.assertEqual(parsed["request_id"], "r1")
        self.assertEqual(parsed["prompt"], "hello")
        self.assertIsNone(parsed["origin"])

        restored = AgentRequest.from_json(j)
        self.assertEqual(restored.request_id, "r1")
        self.assertEqual(restored.agent_id, "mac-claude")
        self.assertEqual(restored.prompt, "hello")
        self.assertEqual(restored.legacy_scope_ids, ())

    def test_roundtrip_with_origin_and_destination(self):
        req = AgentRequest(
            request_id="r2",
            agent_id="mac-codex",
            prompt="test",
            origin=PlatformOrigin(
                platform=Platform.DISCORD,
                channel_id="ch1",
                message_id="msg1",
            ),
            destination=PlatformDestination(
                platform=Platform.DISCORD,
                channel_id="ch1",
                reply_to_message_id="msg1",
            ),
            author_id="user1",
            author_name="Alice",
            session_scope="channel:ch1",
            legacy_scope_ids=("legacy:ch1",),
        )
        j = req.to_json()
        restored = AgentRequest.from_json(j)
        self.assertEqual(restored.origin.platform, Platform.DISCORD)
        self.assertEqual(restored.origin.channel_id, "ch1")
        self.assertEqual(restored.destination.reply_to_message_id, "msg1")
        self.assertEqual(restored.legacy_scope_ids, ("legacy:ch1",))

    def test_kook_request_roundtrip(self):
        req = AgentRequest(
            request_id="r3",
            agent_id="mac-claude",
            prompt="kook test",
            origin=PlatformOrigin(
                platform=Platform.KOOK,
                channel_id="kch1",
                role_id="role1",
            ),
            destination=PlatformDestination(
                platform=Platform.KOOK,
                channel_id="kch1",
                quote_message_id="qm1",
            ),
        )
        j = req.to_json()
        restored = AgentRequest.from_json(j)
        self.assertEqual(restored.origin.platform, Platform.KOOK)
        self.assertEqual(restored.origin.role_id, "role1")
        self.assertEqual(restored.destination.quote_message_id, "qm1")

    def test_handoff_fields(self):
        req = AgentRequest(
            request_id="r4",
            agent_id="mac-claude",
            prompt="do task",
            handoff_workspace_id="ws1",
            handoff_task_id="task-1",
            handoff_bootstrap_content="bootstrap here",
        )
        j = req.to_json()
        restored = AgentRequest.from_json(j)
        self.assertEqual(restored.handoff_workspace_id, "ws1")
        self.assertEqual(restored.handoff_task_id, "task-1")

    def test_created_at_ms_set(self):
        req = AgentRequest(request_id="r5", agent_id="a", prompt="p")
        self.assertGreater(req.created_at_ms, 0)


class TestAgentResponseSerialization(unittest.TestCase):
    def test_roundtrip_success(self):
        resp = AgentResponse(
            request_id="r1",
            agent_id="mac-claude",
            text="Hello!",
            session_id="sess1",
            success=True,
            handoff_lines=["[handoff] @Codex do something"],
            report_lines=["[agent-report] action=progress summary=ok"],
            duration_ms=1500,
        )
        j = resp.to_json()
        restored = AgentResponse.from_json(j)
        self.assertEqual(restored.request_id, "r1")
        self.assertEqual(restored.text, "Hello!")
        self.assertEqual(restored.session_id, "sess1")
        self.assertTrue(restored.success)
        self.assertEqual(len(restored.handoff_lines), 1)
        self.assertEqual(len(restored.report_lines), 1)
        self.assertEqual(restored.duration_ms, 1500)

    def test_roundtrip_error(self):
        resp = AgentResponse(
            request_id="r2",
            agent_id="mac-codex",
            success=False,
            error="Codex CLI failed: timeout",
        )
        j = resp.to_json()
        restored = AgentResponse.from_json(j)
        self.assertFalse(restored.success)
        self.assertIn("timeout", restored.error)

    def test_roundtrip_with_destination(self):
        resp = AgentResponse(
            request_id="r3",
            agent_id="mac-claude",
            text="done",
            destination=PlatformDestination(
                platform=Platform.KOOK,
                channel_id="kch1",
                quote_message_id="qm1",
            ),
        )
        j = resp.to_json()
        restored = AgentResponse.from_json(j)
        self.assertEqual(restored.destination.platform, Platform.KOOK)
        self.assertEqual(restored.destination.quote_message_id, "qm1")


if __name__ == "__main__":
    unittest.main()
