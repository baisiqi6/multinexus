"""Unit tests for mention routing and handoff parsing."""

import unittest

from multinexus.models import AgentConfig, KnownAgentMention
from multinexus.routing.mentions import MentionRouter


def _make_config(my_id="mac-claude", my_uid=None, peer_uid=None):
    return AgentConfig(
        id=my_id,
        token="fake",
        adapter="claude",
        aliases={"Claude", "Mac Claude"},
        known_agents=[
            KnownAgentMention(
                id="mac-claude",
                primary_name="Mac Claude",
                names={"claude", "mac claude"},
                discord_user_id=my_uid,
            ),
            KnownAgentMention(
                id="mac-codex",
                primary_name="Mac Codex",
                names={"codex", "mac codex"},
                discord_user_id=peer_uid,
            ),
        ],
    )


class TestHandoffDetection(unittest.TestCase):
    """is_handoff_message must recognise all three mention formats."""

    def test_text_mention(self):
        router = MentionRouter(_make_config())
        self.assertTrue(router.is_handoff_message("[handoff] @claude please review"))

    def test_text_mention_alias(self):
        router = MentionRouter(_make_config())
        self.assertTrue(router.is_handoff_message("[handoff] @Mac Claude do it"))

    def test_discord_mention_with_uid(self):
        router = MentionRouter(_make_config(my_uid=111))
        self.assertTrue(router.is_handoff_message("[handoff] <@111> please review"))

    def test_discord_mention_with_bang_uid(self):
        router = MentionRouter(_make_config(my_uid=111))
        self.assertTrue(router.is_handoff_message("[handoff] <@!111> please review"))

    def test_wrong_agent_not_matched(self):
        router = MentionRouter(_make_config(my_uid=111))
        self.assertFalse(router.is_handoff_message("[handoff] @codex run tests"))

    def test_wrong_uid_not_matched(self):
        router = MentionRouter(_make_config(my_uid=111))
        self.assertFalse(router.is_handoff_message("[handoff] <@222> run tests"))

    def test_no_handoff_prefix_ignored(self):
        router = MentionRouter(_make_config(my_uid=111))
        self.assertFalse(router.is_handoff_message("@claude what do you think"))

    def test_normal_bot_reply_no_trigger(self):
        router = MentionRouter(_make_config(my_uid=111))
        self.assertFalse(
            router.is_handoff_message(
                "I think @codex should handle this part of the task"
            )
        )


class TestHandoffResolution(unittest.TestCase):
    """resolve_handoff_mentions converts @Name to <@id>."""

    def test_text_to_discord_mention(self):
        router = MentionRouter(_make_config(peer_uid=999))
        text = "[handoff] @codex run the tests"
        result = router.resolve_handoff_mentions(text)
        self.assertIn("<@999>", result)
        self.assertIn("[handoff]", result)

    def test_discord_mention_unchanged(self):
        router = MentionRouter(_make_config(peer_uid=999))
        text = "[handoff] <@999> run the tests"
        result = router.resolve_handoff_mentions(text)
        self.assertEqual(result, text)

    def test_unknown_agent_left_as_text(self):
        router = MentionRouter(_make_config())
        text = "[handoff] @unknown-agent do something"
        result = router.resolve_handoff_mentions(text)
        self.assertIn("@unknown-agent", result)

    def test_mixed_content(self):
        router = MentionRouter(_make_config(peer_uid=999))
        text = "Here's the result.\n[handoff] @codex continue\nMore output"
        result = router.resolve_handoff_mentions(text)
        self.assertIn("<@999>", result)
        self.assertIn("Here's the result.", result)
        self.assertIn("More output", result)


class TestExtractHandoffs(unittest.TestCase):
    """extract_handoffs_from_response returns targets excluding self."""

    def test_extracts_peer(self):
        router = MentionRouter(_make_config())
        text = "Done.\n[handoff] @codex run tests"
        result = router.extract_handoffs_from_response(text, "mac-claude")
        self.assertEqual(result, [("mac-codex", "run tests")])

    def test_excludes_self(self):
        router = MentionRouter(_make_config())
        text = "[handoff] @claude do something"
        result = router.extract_handoffs_from_response(text, "mac-claude")
        self.assertEqual(result, [])

    def test_discord_mention_extracts(self):
        router = MentionRouter(_make_config(peer_uid=999))
        text = "[handoff] <@999> deploy it"
        result = router.extract_handoffs_from_response(text, "mac-claude")
        self.assertEqual(result, [("mac-codex", "deploy it")])


class TestBangCommands(unittest.TestCase):
    def test_matches_alias(self):
        router = MentionRouter(_make_config())
        self.assertTrue(router.matches_bang_command("!claude fix this"))
        self.assertTrue(router.matches_bang_command("!Claude fix this"))

    def test_no_match_other(self):
        router = MentionRouter(_make_config())
        self.assertFalse(router.matches_bang_command("!codex fix this"))

    def test_strip_prefix(self):
        router = MentionRouter(_make_config())
        self.assertEqual(router.strip_bang_prefix("!claude fix the bug"), "fix the bug")


class TestUpdateDiscordUserIds(unittest.TestCase):
    """update_discord_user_ids lets late-binding add IDs after startup."""

    def test_add_uid_enables_uid_lookup(self):
        config = _make_config()
        # No UID initially
        router = MentionRouter(config)
        self.assertFalse(router.is_handoff_message("[handoff] <@555> hello"))

        # Add UID
        router.update_discord_user_ids({"mac-claude": 555})
        self.assertTrue(router.is_handoff_message("[handoff] <@555> hello"))


if __name__ == "__main__":
    unittest.main()
