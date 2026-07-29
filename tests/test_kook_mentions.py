"""Tests for KOOK mention routing."""

import unittest

from multinexus.kook.mentions import KookMentionRouter
from multinexus.models import KnownAgentMention


def _make_agent(**overrides):
    defaults = {
        "id": "mac-claude",
        "primary_name": "Claude",
        "names": {"mac-claude", "Claude", "Mac Claude"},
        "role_ids": {"67674453"},
    }
    defaults.update(overrides)
    return KnownAgentMention(**defaults)


class TestKookMentionRouter(unittest.TestCase):
    def setUp(self):
        self.router = KookMentionRouter()
        self.agents = [
            _make_agent(),
            _make_agent(id="mac-codex", primary_name="Codex", names={"mac-codex", "Codex", "Mac Codex"}, role_ids={"67674521"}),
        ]
        self.role_names = self.router.build_role_names(self.agents)

    def test_explicit_user_mention(self):
        mentions = self.router.explicit_mentions("(met)12345(met) hello")
        self.assertEqual(len(mentions), 1)
        self.assertEqual(mentions[0], ("user", "12345"))

    def test_explicit_role_mention(self):
        mentions = self.router.explicit_mentions("(rol)67674453(rol) test")
        self.assertEqual(len(mentions), 1)
        self.assertEqual(mentions[0], ("role", "67674453"))

    def test_mixed_mentions(self):
        mentions = self.router.explicit_mentions("(met)111(met) (rol)222(rol)")
        self.assertEqual(len(mentions), 2)

    def test_first_explicit_mention(self):
        result = self.router.first_explicit_mention("(met)111(met) (rol)222(rol)")
        self.assertEqual(result, ("user", "111"))

    def test_first_explicit_mention_none(self):
        self.assertIsNone(self.router.first_explicit_mention("no mentions"))

    def test_is_addressed_by_user_mention(self):
        self.assertTrue(
            self.router.is_addressed_to_this_bot(
                content="(met)999(met) hello",
                mentions=["999"],
                mention_roles=[],
                bot_id="999",
                bot_role_ids={"100"},
                aliases=set(),
            )
        )

    def test_is_addressed_by_role_mention(self):
        self.assertTrue(
            self.router.is_addressed_to_this_bot(
                content="(rol)100(rol) hello",
                mentions=[],
                mention_roles=[],
                bot_id="999",
                bot_role_ids={"100"},
                aliases=set(),
            )
        )

    def test_is_addressed_by_metadata(self):
        self.assertTrue(
            self.router.is_addressed_to_this_bot(
                content="hello",
                mentions=["999"],
                mention_roles=[],
                bot_id="999",
                bot_role_ids=set(),
                aliases=set(),
            )
        )

    def test_is_addressed_by_text_alias(self):
        self.assertTrue(
            self.router.is_addressed_to_this_bot(
                content="@Claude hello",
                mentions=[],
                mention_roles=[],
                bot_id="999",
                bot_role_ids={"100"},
                aliases={"Claude"},
            )
        )

    def test_not_addressed(self):
        self.assertFalse(
            self.router.is_addressed_to_this_bot(
                content="hello world",
                mentions=[],
                mention_roles=[],
                bot_id="999",
                bot_role_ids={"100"},
                aliases=set(),
            )
        )

    def test_clean_strips_self_mentions(self):
        result = self.router.clean_for_agent(
            content="(met)999(met) (rol)100(rol) hello",
            bot_id="999",
            bot_role_ids={"100"},
            aliases=set(),
            known_user_names={},
            known_role_names=self.role_names,
        )
        self.assertNotIn("(met)", result)
        self.assertNotIn("(rol)", result)
        self.assertIn("hello", result)

    def test_clean_replaces_other_user(self):
        result = self.router.clean_for_agent(
            content="(met)555(met) hello",
            bot_id="999",
            bot_role_ids=set(),
            aliases=set(),
            known_user_names={"555": "Alice"},
            known_role_names={},
        )
        self.assertIn("@Alice", result)

    def test_render_for_context(self):
        result = self.router.render_for_context(
            "(met)555(met) (rol)67674453(rol) hello",
            known_user_names={"555": "Alice"},
            known_role_names=self.role_names,
        )
        self.assertIn("@Alice", result)
        self.assertIn("@Claude", result)
        self.assertNotIn("(met)", result)

    def test_build_role_names(self):
        names = self.router.build_role_names(self.agents)
        self.assertEqual(names["67674453"], "Claude")
        self.assertEqual(names["67674521"], "Codex")

    def test_outbound_text_user(self):
        result = self.router.outbound_for_kook("@user:123 hello", self.agents)
        self.assertIn("(met)123(met)", result)

    def test_outbound_text_role(self):
        result = self.router.outbound_for_kook("@role:67674453 hello", self.agents)
        self.assertIn("(rol)67674453(rol)", result)

    def test_outbound_agent_name(self):
        result = self.router.outbound_for_kook("@Codex do something", self.agents)
        self.assertIn("(rol)67674521(rol)", result)

    def test_is_text_mention(self):
        self.assertTrue(self.router.is_text_mention("@Claude hi", {"Claude"}))
        self.assertFalse(self.router.is_text_mention("hello", {"Claude"}))


class TestKookMentionRouterEdgeCases(unittest.TestCase):
    def test_empty_content(self):
        router = KookMentionRouter()
        self.assertEqual(router.explicit_mentions(""), [])
        self.assertIsNone(router.first_explicit_mention(""))

    def test_no_known_agents(self):
        router = KookMentionRouter()
        self.assertEqual(router.build_role_names([]), {})

    def test_agent_without_roles(self):
        router = KookMentionRouter()
        agent = KnownAgentMention(id="test", primary_name="Test", names={"test"}, role_ids=set())
        result = router.outbound_for_kook("@Test hello", [agent])
        self.assertNotIn("(rol)", result)


if __name__ == "__main__":
    unittest.main()
