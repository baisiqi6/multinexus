import os
import tempfile
import unittest

from multinexus.config import load_config
from multinexus.models import AgentConfig, KnownAgentMention
from multinexus.routing.mentions import MentionRouter


def _write_toml(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".toml")
    os.write(fd, content.encode("utf-8"))
    os.close(fd)
    return path


TOML_WITH_EXTERNALS = """
[defaults]
timeout = 60
channels = [999]

[[agents]]
id = "mac-claude"
adapter = "claude"
token = "fake-token"
aliases = ["Claude"]
channels = [999]
work_dir = "/tmp"

[[agents]]
id = "mac-codex"
adapter = "codex"
token = "fake-token"
aliases = ["Codex"]
channels = [999]

[[external_agents]]
id = "mac-openclaw"
display_name = "小龙虾"
aliases = ["小龙虾", "OpenClaw"]
discord_user_id = 111222

[[external_agents]]
id = "server-hermes"
display_name = "Hermes"
aliases = ["Hermes", "爱马仕"]
discord_user_id = 333444
"""


class TestExternalAgentsConfig(unittest.TestCase):
    def setUp(self):
        self.toml_path = _write_toml(TOML_WITH_EXTERNALS)

    def tearDown(self):
        os.unlink(self.toml_path)

    def test_known_agents_includes_externals(self):
        config = load_config(["--config", self.toml_path, "--agent", "mac-claude"])
        ids = [a.id for a in config.known_agents]
        self.assertIn("mac-claude", ids)
        self.assertIn("mac-codex", ids)
        self.assertIn("mac-openclaw", ids)
        self.assertIn("server-hermes", ids)
        self.assertEqual(len(config.known_agents), 4)

    def test_external_has_correct_fields(self):
        config = load_config(["--config", self.toml_path, "--agent", "mac-claude"])
        hermes = next(a for a in config.known_agents if a.id == "server-hermes")
        self.assertEqual(hermes.primary_name, "Hermes")
        self.assertIn("爱马仕", hermes.names)
        self.assertEqual(hermes.discord_user_id, 333444)

    def test_cannot_select_external_agent(self):
        with self.assertRaises(SystemExit) as ctx:
            load_config(["--config", self.toml_path, "--agent", "server-hermes"])
        self.assertIn("not found", str(ctx.exception))

    def test_cannot_select_openclaw(self):
        with self.assertRaises(SystemExit) as ctx:
            load_config(["--config", self.toml_path, "--agent", "mac-openclaw"])
        self.assertIn("not found", str(ctx.exception))

    def test_error_lists_only_managed_agents(self):
        with self.assertRaises(SystemExit) as ctx:
            load_config(["--config", self.toml_path, "--agent", "server-hermes"])
        msg = str(ctx.exception)
        self.assertIn("mac-claude", msg)
        self.assertIn("mac-codex", msg)
        self.assertNotIn("server-hermes", msg.split("Available:")[1])


TOML_NO_EXTERNALS = """
[defaults]
timeout = 60

[[agents]]
id = "mac-claude"
adapter = "claude"
token = "fake-token"
channels = [999]
work_dir = "/tmp"
"""


class TestNoExternalAgents(unittest.TestCase):
    def setUp(self):
        self.toml_path = _write_toml(TOML_NO_EXTERNALS)

    def tearDown(self):
        os.unlink(self.toml_path)

    def test_works_without_externals(self):
        config = load_config(["--config", self.toml_path, "--agent", "mac-claude"])
        ids = [a.id for a in config.known_agents]
        self.assertEqual(ids, ["mac-claude"])


class TestExternalAgentsRouting(unittest.TestCase):
    """External agents should be routable via MentionRouter."""

    def _make_config(self):
        return AgentConfig(
            id="mac-claude",
            token="fake",
            adapter="claude",
            channels=[999],
            known_agents=[
                KnownAgentMention(
                    id="mac-claude",
                    primary_name="Mac Claude",
                    names={"claude", "mac claude"},
                ),
                KnownAgentMention(
                    id="mac-openclaw",
                    primary_name="小龙虾",
                    names={"小龙虾", "openclaw"},
                    discord_user_id=111222,
                ),
                KnownAgentMention(
                    id="server-hermes",
                    primary_name="Hermes",
                    names={"hermes", "爱马仕"},
                    discord_user_id=333444,
                ),
            ],
        )

    def test_resolve_external_chinese_name(self):
        router = MentionRouter(self._make_config())
        result = router.resolve_handoff_mentions("[handoff] @小龙虾 请搜索")
        self.assertIn("<@111222>", result)

    def test_resolve_external_by_uid_in_extract(self):
        router = MentionRouter(self._make_config())
        text = "[handoff] <@111222> 请搜索"
        results = router.extract_handoffs_from_response(text, "mac-claude")
        self.assertEqual(results, [("mac-openclaw", "请搜索")])

    def test_resolve_external_name_to_uid(self):
        router = MentionRouter(self._make_config())
        result = router.resolve_handoff_mentions("[handoff] @Hermes 部署一下")
        self.assertIn("<@333444>", result)

    def test_resolve_chinese_alias(self):
        router = MentionRouter(self._make_config())
        result = router.resolve_handoff_mentions("[handoff] @爱马仕 部署一下")
        self.assertIn("<@333444>", result)

    def test_extract_handoff_to_external(self):
        router = MentionRouter(self._make_config())
        text = "[handoff] @小龙虾 搜索资料"
        results = router.extract_handoffs_from_response(text, "mac-claude")
        self.assertEqual(results, [("mac-openclaw", "搜索资料")])


if __name__ == "__main__":
    unittest.main()
