import unittest
from unittest.mock import patch

from multinexus.adapters import utils


class FilteredEnvTests(unittest.TestCase):
    def test_filters_message_bus_secrets(self):
        with patch.dict(
            "os.environ",
            {
                "DISCORD_TOKEN": "discord-secret",
                "KOOK_TOKEN": "kook-secret",
                "SAFE_VALUE": "kept",
            },
            clear=True,
        ):
            env = utils.filtered_env()

        self.assertEqual(env, {"SAFE_VALUE": "kept"})

    def test_sets_pwd_on_posix(self):
        with patch.dict("os.environ", {"SAFE_VALUE": "kept"}, clear=True):
            with patch.object(utils, "IS_WIN", False):
                env = utils.filtered_env(cwd="/tmp/project")

        self.assertEqual(env["PWD"], "/tmp/project")
        self.assertEqual(env["SAFE_VALUE"], "kept")

    def test_does_not_set_pwd_on_windows(self):
        with patch.dict("os.environ", {"SAFE_VALUE": "kept"}, clear=True):
            with patch.object(utils, "IS_WIN", True):
                env = utils.filtered_env(cwd="C:\\Users\\example\\projects\\multinexus")

        self.assertNotIn("PWD", env)
        self.assertEqual(env["SAFE_VALUE"], "kept")
