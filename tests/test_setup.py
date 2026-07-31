import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv

from multinexus.config import load_config
from multinexus.setup import (
    CHECK_INVALID,
    CHECK_OK,
    _atomic_write_text,
    check_configuration,
    run_setup,
)


class _Answers:
    def __init__(self, *values: str):
        self._values = iter(values)

    def __call__(self, _prompt: str) -> str:
        return next(self._values)


class TestStandaloneSetup(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.work_dir = self.root / "workspace"
        self.work_dir.mkdir()

    def tearDown(self):
        self.tempdir.cleanup()

    def _run_fresh(self, *, token: str = "secret.discord.token") -> tuple[int, str]:
        output = io.StringIO()
        answers = _Answers(
            "demo-agent",
            "演示 Agent",
            "claude",
            str(self.work_dir),
            "111111111111111111",
            "222222222222222222",
        )
        with patch("multinexus.setup.shutil.which", return_value="/usr/local/bin/claude"):
            code = run_setup(
                self.root,
                input_fn=answers,
                secret_fn=lambda _prompt: token,
                out=output,
            )
        return code, output.getvalue()

    def test_fresh_setup_writes_loadable_secret_safe_config(self):
        token = "secret.discord.token"
        code, output = self._run_fresh(token=token)

        self.assertEqual(code, CHECK_OK)
        self.assertNotIn(token, output)
        toml_text = (self.root / "agents.toml").read_text(encoding="utf-8")
        self.assertNotIn(token, toml_text)
        self.assertIn("agentd_mode = false", toml_text)
        self.assertNotIn("coordinator_", toml_text)
        self.assertEqual(
            stat.S_IMODE((self.root / ".env").stat().st_mode),
            0o600,
        )

        old_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("DISCORD_DEMO_AGENT_TOKEN", None)
                load_dotenv(self.root / ".env", override=True)
                config = load_config(
                    ["--config", "agents.toml", "--agent", "demo-agent"],
                    require_token=True,
                )
        finally:
            os.chdir(old_cwd)

        self.assertEqual(config.id, "demo-agent")
        self.assertEqual(config.adapter, "claude")
        self.assertEqual(config.token, token)
        self.assertFalse(config.agentd_mode)
        self.assertEqual(config.channels, [111111111111111111])
        self.assertEqual(config.allowed_user_ids, [222222222222222222])

    def test_existing_config_is_never_overwritten(self):
        config_path = self.root / "agents.toml"
        original = "# user-owned config\n"
        config_path.write_text(original, encoding="utf-8")
        output = io.StringIO()

        code = run_setup(
            self.root,
            input_fn=lambda _prompt: self.fail("must not prompt"),
            secret_fn=lambda _prompt: self.fail("must not request secret"),
            out=output,
        )

        self.assertEqual(code, CHECK_INVALID)
        self.assertEqual(config_path.read_text(encoding="utf-8"), original)
        self.assertFalse((self.root / ".env").exists())
        self.assertIn("不会覆盖", output.getvalue())

    def test_env_upsert_preserves_unrelated_lines(self):
        (self.root / ".env").write_text(
            "# keep this\nOTHER_SETTING=yes\nDISCORD_DEMO_AGENT_TOKEN=old\n",
            encoding="utf-8",
        )

        code, _ = self._run_fresh(token="replacement.token")

        self.assertEqual(code, CHECK_OK)
        env_text = (self.root / ".env").read_text(encoding="utf-8")
        self.assertIn("# keep this", env_text)
        self.assertIn("OTHER_SETTING=yes", env_text)
        self.assertEqual(env_text.count("DISCORD_DEMO_AGENT_TOKEN="), 1)
        self.assertIn("DISCORD_DEMO_AGENT_TOKEN=replacement.token", env_text)

    def test_invalid_ids_and_work_dir_are_reprompted(self):
        output = io.StringIO()
        answers = _Answers(
            "bad id",
            "good-id",
            "Good Agent",
            "not-an-executor",
            "omp",
            "relative/path",
            "/does/not/exist",
            str(self.work_dir),
            "not-a-channel",
            "123456789012345678",
            "not-a-user",
            "223456789012345678",
        )
        with patch("multinexus.setup.shutil.which", return_value="/usr/local/bin/omp"):
            code = run_setup(
                self.root,
                input_fn=answers,
                secret_fn=lambda _prompt: "valid.token",
                out=output,
            )

        self.assertEqual(code, CHECK_OK)
        text = output.getvalue()
        self.assertIn("格式无效", text)
        self.assertIn("必须是绝对路径", text)
        self.assertIn("目录不存在", text)
        self.assertIn("必须是纯数字", text)

    def test_missing_executor_aborts_before_requesting_token(self):
        output = io.StringIO()
        answers = _Answers(
            "demo-agent",
            "Demo",
            "claude",
            str(self.work_dir),
            "123456789012345678",
            "223456789012345678",
        )
        with patch("multinexus.setup.shutil.which", return_value=None):
            code = run_setup(
                self.root,
                input_fn=answers,
                secret_fn=lambda _prompt: self.fail("must not request token"),
                out=output,
            )

        self.assertEqual(code, CHECK_INVALID)
        self.assertFalse((self.root / "agents.toml").exists())
        self.assertFalse((self.root / ".env").exists())
        self.assertIn("未找到 executor", output.getvalue())

    def test_all_public_executors_generate_their_runtime_binary_field(self):
        expected_fields = {
            "claude": "claude_bin",
            "codex": "codex_bin",
            "opencode": "opencode_bin",
            "omp": "omp_bin",
            "hermes": "hermes_bin",
        }
        for executor, field in expected_fields.items():
            with self.subTest(executor=executor), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = io.StringIO()
                answers = _Answers(
                    f"demo-{executor}",
                    f"Demo {executor}",
                    executor,
                    str(self.work_dir),
                    "123456789012345678",
                    "223456789012345678",
                )
                with patch(
                    "multinexus.setup.shutil.which",
                    side_effect=lambda binary: f"/usr/local/bin/{binary}",
                ):
                    code = run_setup(
                        root,
                        input_fn=answers,
                        secret_fn=lambda _prompt: "valid.token",
                        out=output,
                    )

                self.assertEqual(code, CHECK_OK)
                config_text = (root / "agents.toml").read_text(encoding="utf-8")
                self.assertIn(f'adapter = "{executor}"', config_text)
                self.assertIn(f"{field} = ", config_text)

    def test_atomic_write_failure_preserves_original_and_cleans_temp(self):
        target = self.root / ".env"
        target.write_text("ORIGINAL=yes\n", encoding="utf-8")

        with patch("multinexus.setup.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                _atomic_write_text(target, "REPLACEMENT=yes\n", mode=0o600)

        self.assertEqual(target.read_text(encoding="utf-8"), "ORIGINAL=yes\n")
        self.assertEqual(list(self.root.glob(".env.*.tmp")), [])


class TestSetupCheck(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.work_dir = self.root / "workspace"
        self.work_dir.mkdir()

    def tearDown(self):
        self.tempdir.cleanup()

    def _write_config(self, *, token: str = "present.token", binary: str = "claude") -> None:
        (self.root / "agents.toml").write_text(
            "[defaults]\n"
            "agentd_mode = false\n\n"
            "[[agents]]\n"
            'id = "demo"\n'
            'adapter = "claude"\n'
            'display_name = "Demo"\n'
            'token_env = "DISCORD_DEMO_TOKEN"\n'
            f'work_dir = "{self.work_dir}"\n'
            f'claude_bin = "{binary}"\n'
            "channels = [123456789012345678]\n"
            "allowed_user_ids = [223456789012345678]\n",
            encoding="utf-8",
        )
        (self.root / ".env").write_text(
            f"DISCORD_DEMO_TOKEN={token}\n",
            encoding="utf-8",
        )
        os.chmod(self.root / ".env", 0o600)

    def test_valid_configuration_returns_zero_without_secret(self):
        token = "present.token"
        self._write_config(token=token)
        output = io.StringIO()

        with patch("multinexus.setup.shutil.which", return_value="/usr/local/bin/claude"):
            code = check_configuration(self.root, out=output)

        self.assertEqual(code, CHECK_OK)
        self.assertIn("CHECK_OK", output.getvalue())
        self.assertNotIn(token, output.getvalue())

    def test_missing_token_returns_nonzero(self):
        self._write_config(token="")
        output = io.StringIO()

        with patch.dict(os.environ, {}, clear=True), patch(
            "multinexus.setup.shutil.which", return_value="/usr/local/bin/claude"
        ):
            code = check_configuration(self.root, out=output)

        self.assertEqual(code, CHECK_INVALID)
        self.assertIn("token: missing", output.getvalue())

    def test_missing_executor_returns_nonzero(self):
        self._write_config()
        output = io.StringIO()

        with patch("multinexus.setup.shutil.which", return_value=None):
            code = check_configuration(self.root, out=output)

        self.assertEqual(code, CHECK_INVALID)
        self.assertIn("executor: missing", output.getvalue())

    def test_invalid_toml_returns_nonzero(self):
        (self.root / "agents.toml").write_text("[[agents]\n", encoding="utf-8")
        output = io.StringIO()

        code = check_configuration(self.root, out=output)

        self.assertEqual(code, CHECK_INVALID)
        self.assertIn("TOML 无效", output.getvalue())

    def test_missing_config_returns_nonzero(self):
        output = io.StringIO()

        code = check_configuration(self.root, out=output)

        self.assertEqual(code, CHECK_INVALID)
        self.assertIn("agents.toml 不存在", output.getvalue())

    def test_missing_allowlists_warn_but_do_not_fail(self):
        self._write_config()
        config_path = self.root / "agents.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            .replace("channels = [123456789012345678]\n", "")
            .replace("allowed_user_ids = [223456789012345678]\n", ""),
            encoding="utf-8",
        )
        output = io.StringIO()

        with patch("multinexus.setup.shutil.which", return_value="/usr/local/bin/claude"):
            code = check_configuration(self.root, out=output)

        self.assertEqual(code, CHECK_OK)
        self.assertIn("WARNING channels", output.getvalue())
        self.assertIn("WARNING allowed_user_ids", output.getvalue())


if __name__ == "__main__":
    unittest.main()
