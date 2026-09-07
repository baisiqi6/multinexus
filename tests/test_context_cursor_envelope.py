import tempfile
import time
import unittest

from multinexus.context.envelope import messages_after_cursor, parse_context_envelope
from multinexus.context.prompt import build_agent_prompt_with_context, delta_prompt
from multinexus.context.store import ChatContextStore
from multinexus.models import AgentConfig


class ContextEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ChatContextStore(f"{self.tmp.name}/context.sqlite3")
        self.config = AgentConfig(
            id="agent-1",
            token="token",
            display_name="Agent 1",
            context_db_path=f"{self.tmp.name}/context.sqlite3",
        )
        base = int(time.time() * 1000)
        for message_id, content, created_at in (
            ("m1", "first", base - 2000),
            ("m2", "second", base - 1000),
            ("m3", "current", base),
        ):
            self.store.record_message(
                message_id=message_id,
                channel_id="scope",
                author_id="human",
                author_name="human",
                author_is_bot=False,
                content=content,
                created_at_ms=created_at,
                source="discord",
                ttl_seconds=3600,
            )

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_and_delta_preserve_current_message(self):
        full, envelope = build_agent_prompt_with_context(
            context_store=self.store,
            config=self.config,
            bot_id=1,
            channel_id="scope",
            message_id="m3",
            current_text="current",
            scope_id="scope",
            session_scope_id="session",
            recipient={"id": "agent-1"},
        )
        self.assertIn("first", full)
        parsed = parse_context_envelope(envelope)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        first = parsed.messages[0]
        delta = messages_after_cursor(
            parsed, str(first["context_order"]), first["message_id"]
        )
        self.assertEqual([item["message_id"] for item in delta or ()], ["m2"])
        rendered = delta_prompt(
            envelope,
            str(first["context_order"]),
            first["message_id"],
            self.config,
        )
        self.assertNotIn("first", rendered)
        self.assertIn("second", rendered)
        self.assertIn("current", rendered)

    def test_missing_anchor_falls_back_to_full_prompt(self):
        _, envelope = build_agent_prompt_with_context(
            context_store=self.store,
            config=self.config,
            bot_id=1,
            channel_id="scope",
            message_id="m3",
            current_text="current",
            scope_id="scope",
            session_scope_id="session",
            recipient={"id": "agent-1"},
        )
        rendered = delta_prompt(envelope, "999999", "missing", self.config)
        self.assertIn("first", rendered)
        self.assertIn("second", rendered)

    def test_invalid_envelope_falls_back(self):
        self.assertIsNone(parse_context_envelope({"version": 2}))


if __name__ == "__main__":
    unittest.main()
