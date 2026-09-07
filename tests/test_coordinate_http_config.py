"""R2B focused tests: coordinate transport config, dotenv authority, URL and
token-file validation.

Covers plan §6.1/6.2/6.3/6.13: default/legacy config keeps constructing the
CLI profile, the four MULTINEXUS_COORDINATE_* env keys override TOML but never
arrive from a shared .env, missing HTTP fields fail closed only at consumer
construction, and the loopback URL / token-file rules reject every non-loopback
or world-visible shape.
"""

from __future__ import annotations

import os
import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from multinexus.agentd.coordinate_client import (
    CoordinateHttpConfigError,
    validate_http_base_url,
    validate_http_token_file,
)
from multinexus.config import (
    _COORDINATE_ENV_KEYS,
    _load_dotenv_preserving_coordinate_env,
    load_config,
)
from multinexus.models import AgentConfig


def _write_toml(tmp: Path, **agent_fields) -> Path:
    toml = tmp / "agents.toml"
    lines = [
        "[defaults]",
        'token_env = "DISCORD_TOKEN"',
        'work_dir = "/tmp/ws"',
        "",
        "[[agents]]",
        'id = "test-agent"',
        'adapter = "claude"',
    ]
    for key, value in agent_fields.items():
        lines.append(f'{key} = "{value}"')
    toml.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return toml


class DotenvAuthorityTests(unittest.TestCase):
    """The four coordinate env keys never come from a shared .env."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = {
            key: os.environ.get(key)
            for key in _COORDINATE_ENV_KEYS
            if key in os.environ
        }
        for key in _COORDINATE_ENV_KEYS:
            os.environ.pop(key, None)
        # DISCORD_TOKEN is written by the fake dotenv in some tests; keep this
        # class hermetic so no residue leaks into later tests.
        self.old_discord_token = os.environ.get("DISCORD_TOKEN")
        os.environ.pop("DISCORD_TOKEN", None)

    def tearDown(self):
        for key in _COORDINATE_ENV_KEYS:
            os.environ.pop(key, None)
        for key, value in self.old.items():
            os.environ[key] = value
        if self.old_discord_token is None:
            os.environ.pop("DISCORD_TOKEN", None)
        else:
            os.environ["DISCORD_TOKEN"] = self.old_discord_token

    def test_dotenv_injected_coordinate_keys_are_removed(self):
        def fake_load_dotenv():
            os.environ["MULTINEXUS_COORDINATE_TRANSPORT"] = "http"
            os.environ["MULTINEXUS_COORDINATE_HTTP_BASE_URL"] = "http://127.0.0.1:8765"
            os.environ["MULTINEXUS_COORDINATE_HTTP_CLIENT_ID"] = "evil-bridge"
            os.environ["MULTINEXUS_COORDINATE_HTTP_TOKEN_FILE"] = "/tmp/evil-token"
            os.environ["DISCORD_TOKEN"] = "from-dotenv"

        with patch("multinexus.config.load_dotenv", side_effect=fake_load_dotenv):
            _load_dotenv_preserving_coordinate_env()
        for key in _COORDINATE_ENV_KEYS:
            self.assertNotIn(key, os.environ, f".env must not inject {key}")
        # Non-coordinate keys from dotenv still load.
        self.assertEqual(os.environ.get("DISCORD_TOKEN"), "from-dotenv")

    def test_process_env_keys_survive_dotenv_load_verbatim(self):
        def fake_load_dotenv():
            os.environ["MULTINEXUS_COORDINATE_TRANSPORT"] = "http"

        os.environ["MULTINEXUS_COORDINATE_TRANSPORT"] = "cli"
        with patch("multinexus.config.load_dotenv", side_effect=fake_load_dotenv):
            _load_dotenv_preserving_coordinate_env()
        self.assertEqual(os.environ["MULTINEXUS_COORDINATE_TRANSPORT"], "cli")

    def test_dotenv_raise_still_restores_process_env(self):
        os.environ["MULTINEXUS_COORDINATE_TRANSPORT"] = "cli"
        with patch(
            "multinexus.config.load_dotenv",
            side_effect=RuntimeError("dotenv boom"),
        ):
            with self.assertRaises(RuntimeError):
                _load_dotenv_preserving_coordinate_env()
        self.assertEqual(os.environ["MULTINEXUS_COORDINATE_TRANSPORT"], "cli")

    def test_other_dotenv_keys_still_load(self):
        def fake_load_dotenv():
            os.environ["DISCORD_TOKEN"] = "from-dotenv"

        with patch("multinexus.config.load_dotenv", side_effect=fake_load_dotenv):
            _load_dotenv_preserving_coordinate_env()
        self.assertEqual(os.environ.get("DISCORD_TOKEN"), "from-dotenv")


class TransportConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.toml = _write_toml(Path(self.tmp.name))
        self.old = {
            key: os.environ.get(key)
            for key in _COORDINATE_ENV_KEYS
            if key in os.environ
        }
        for key in _COORDINATE_ENV_KEYS:
            os.environ.pop(key, None)
        self.old_token = os.environ.get("DISCORD_TOKEN")
        os.environ["DISCORD_TOKEN"] = "fake-token"

    def tearDown(self):
        for key in _COORDINATE_ENV_KEYS:
            os.environ.pop(key, None)
        for key, value in self.old.items():
            os.environ[key] = value
        if self.old_token is None:
            os.environ.pop("DISCORD_TOKEN", None)
        else:
            os.environ["DISCORD_TOKEN"] = self.old_token

    def _load(self) -> AgentConfig:
        return load_config(
            argv=["--config", str(self.toml), "--agent", "test-agent"]
        )

    def test_default_transport_is_cli_and_legacy_fields_work(self):
        cfg = self._load()
        self.assertEqual(cfg.coordinate_transport, "cli")
        self.assertEqual(cfg.coordinate_http_base_url, "")
        self.assertEqual(cfg.coordinate_http_client_id, "")
        self.assertEqual(cfg.coordinate_http_token_file, "")
        # Legacy compat fields are untouched.
        self.assertIsInstance(cfg, AgentConfig)

    def test_toml_http_profile_parses(self):
        toml = _write_toml(
            Path(self.tmp.name),
            coordinate_transport="http",
            coordinate_http_base_url="http://127.0.0.1:8765",
            coordinate_http_client_id="discord-bridge",
            coordinate_http_token_file="/tmp/bridge-token",
        )
        cfg = load_config(argv=["--config", str(toml), "--agent", "test-agent"])
        self.assertEqual(cfg.coordinate_transport, "http")
        self.assertEqual(cfg.coordinate_http_base_url, "http://127.0.0.1:8765")
        self.assertEqual(cfg.coordinate_http_client_id, "discord-bridge")
        self.assertEqual(cfg.coordinate_http_token_file, "/tmp/bridge-token")

    def test_process_env_overrides_toml(self):
        toml = _write_toml(
            Path(self.tmp.name), coordinate_transport="http"
        )
        os.environ["MULTINEXUS_COORDINATE_TRANSPORT"] = "cli"
        os.environ["MULTINEXUS_COORDINATE_HTTP_BASE_URL"] = "http://127.0.0.1:9999"
        cfg = load_config(argv=["--config", str(toml), "--agent", "test-agent"])
        self.assertEqual(cfg.coordinate_transport, "cli")
        self.assertEqual(cfg.coordinate_http_base_url, "http://127.0.0.1:9999")

    def test_dotenv_never_reaches_runtime_config(self):
        # .env tries to flip the transport; process env is clean.
        Path(self.tmp.name, ".env").write_text(
            "MULTINEXUS_COORDINATE_TRANSPORT=http\n", encoding="utf-8"
        )
        cfg = self._load()
        self.assertEqual(cfg.coordinate_transport, "cli")


class HttpBaseUrlValidationTests(unittest.TestCase):
    """Plan §6.2: loopback-only, explicit port, no userinfo/path/query;
    canonical form has no trailing slash."""

    def _assert_valid(self, url: str, expected: str | None = None) -> None:
        self.assertEqual(
            validate_http_base_url(url), expected if expected is not None else url
        )

    def _assert_invalid(self, url: str) -> None:
        with self.assertRaises(CoordinateHttpConfigError):
            validate_http_base_url(url)

    def test_valid_loopback_urls(self):
        for url in (
            "http://127.0.0.1:8765",
            "http://127.0.0.1:1",
            "http://127.0.0.1:65535",
            "http://127.8.8.8:8765",  # 127/8 numeric loopback
            "http://127.0.0.1:8765/",  # trailing slash canonicalized away
            "http://[::1]:8765",
            "http://[::1]:65535",
            "http://[::1]:8765/",  # trailing slash canonicalized away
        ):
            expected = url[:-1] if url.endswith("/") else url
            self._assert_valid(url, expected)

    def test_canonical_form_drops_trailing_slash(self):
        self.assertEqual(
            validate_http_base_url("http://127.0.0.1:8765/"),
            "http://127.0.0.1:8765",
        )
        self.assertEqual(
            validate_http_base_url("http://[::1]:8765/"),
            "http://[::1]:8765",
        )

    def test_rejected_urls(self):
        for url in (
            "https://127.0.0.1:8765",          # https
            "http://localhost:8765",           # DNS name
            "http://example.com:8765",         # DNS name
            "http://127.0.0.1",                # no explicit port
            "http://127.0.0.1:",               # empty port
            "http://127.0.0.1:0",              # port 0
            "http://127.0.0.1:65536",          # port overflow
            "http://127.0.0.1:8765/path",      # non-root path
            "http://127.0.0.1:8765?x=1",       # query
            "http://127.0.0.1:8765#frag",      # fragment
            "http://user:pass@127.0.0.1:8765", # userinfo
            "http://192.168.1.1:8765",         # non-loopback
            "http://10.0.0.1:8765",            # non-loopback
            "http://[fe80::1]:8765",           # non-loopback IPv6
            "ftp://127.0.0.1:8765",            # wrong scheme
            "http://[::1]:8765/path",          # path with IPv6
            "http://",                         # empty
            "127.0.0.1:8765",                  # missing scheme
            "http://127.0.0.1:port",           # non-numeric port
        ):
            self._assert_invalid(url)


class HttpTokenFileValidationTests(unittest.TestCase):
    """Plan §6.3: absolute regular non-symlink, not world-readable, not
    group/world-writable; 0640 root:multinexus style is allowed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.token = Path(self.tmp.name, "token")
        self.token.write_text("secret-token-value\n", encoding="utf-8")
        os.chmod(self.token, 0o600)

    def _read(self, path: Path | None = None) -> str:
        return validate_http_token_file(str(path or self.token))

    def test_valid_0600_reads_token(self):
        self.assertEqual(self._read(), "secret-token-value")

    def test_windows_private_acl_is_accepted_without_posix_mode_bits(self):
        acl = {
            "owner": "S-1-5-21-1000",
            "current": "S-1-5-18",
            "rules": [
                {"sid": "S-1-5-21-1000", "type": "Allow", "rights": 2032127},
                {"sid": "S-1-5-18", "type": "Allow", "rights": 2032127},
                {"sid": "S-1-5-32-544", "type": "Allow", "rights": 2032127},
            ],
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps(acl), "")
        with patch.dict(os.environ, {"COORDINATE_REMOTE_MCP_TOKEN": "must-not-inherit"}), patch(
            "multinexus.agentd.coordinate_client.sys.platform", "win32"
        ), patch(
            "multinexus.agentd.coordinate_client.subprocess.run",
            return_value=completed,
        ) as run:
            self.assertEqual(self._read(), "secret-token-value")
        child_env = run.call_args.kwargs["env"]
        self.assertEqual(child_env["COORDINATE_TOKEN_PATH"], str(self.token))
        self.assertNotIn("COORDINATE_REMOTE_MCP_TOKEN", child_env)

    def test_windows_acl_rejects_read_grant_to_untrusted_principal(self):
        acl = {
            "owner": "S-1-5-21-1000",
            "current": "S-1-5-18",
            "rules": [
                {"sid": "S-1-5-18", "type": "Allow", "rights": 2032127},
                {"sid": "S-1-5-32-545", "type": "Allow", "rights": 131209},
            ],
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps(acl), "")
        with patch("multinexus.agentd.coordinate_client.sys.platform", "win32"), patch(
            "multinexus.agentd.coordinate_client.subprocess.run",
            return_value=completed,
        ):
            with self.assertRaises(CoordinateHttpConfigError):
                self._read()

    def test_windows_replacement_after_acl_check_is_rejected(self):
        replacement = Path(self.tmp.name, "replacement")
        replacement.write_text("different-token\n", encoding="utf-8")

        def replace_after_acl(_path):
            os.replace(replacement, self.token)

        with patch("multinexus.agentd.coordinate_client.sys.platform", "win32"), patch(
            "multinexus.agentd.coordinate_client._validate_windows_private_acl",
            side_effect=replace_after_acl,
        ):
            with self.assertRaises(CoordinateHttpConfigError):
                self._read()

    def test_0640_group_readable_is_allowed(self):
        os.chmod(self.token, 0o640)
        self.assertEqual(self._read(), "secret-token-value")

    def test_relative_path_rejected(self):
        with self.assertRaises(CoordinateHttpConfigError):
            validate_http_token_file("relative/token")

    def test_missing_file_rejected(self):
        with self.assertRaises(CoordinateHttpConfigError):
            validate_http_token_file(str(Path(self.tmp.name, "absent")))

    def test_directory_rejected(self):
        with self.assertRaises(CoordinateHttpConfigError):
            validate_http_token_file(self.tmp.name)

    def test_symlink_rejected(self):
        link = Path(self.tmp.name, "link")
        os.symlink(self.token, link)
        with self.assertRaises(CoordinateHttpConfigError):
            validate_http_token_file(str(link))

    def test_path_replacement_between_lstat_and_open_is_rejected(self):
        replacement = Path(self.tmp.name, "replacement")
        replacement.write_text("different-token\n", encoding="utf-8")
        os.chmod(replacement, 0o600)
        real_open = os.open

        def replace_then_open(path, flags):
            os.replace(replacement, self.token)
            return real_open(path, flags)

        with patch(
            "multinexus.agentd.coordinate_client.os.open",
            side_effect=replace_then_open,
        ):
            with self.assertRaises(CoordinateHttpConfigError):
                self._read()

    def test_world_readable_rejected(self):
        for mode in (0o644, 0o444, 0o604):
            os.chmod(self.token, mode)
            with self.assertRaises(CoordinateHttpConfigError):
                self._read()

    def test_group_or_world_writable_rejected(self):
        for mode in (0o660, 0o606, 0o666, 0o662, 0o620):
            os.chmod(self.token, mode)
            with self.assertRaises(CoordinateHttpConfigError):
                self._read()

    def test_empty_token_rejected(self):
        empty = Path(self.tmp.name, "empty")
        empty.write_text("", encoding="utf-8")
        os.chmod(empty, 0o600)
        with self.assertRaises(CoordinateHttpConfigError):
            validate_http_token_file(str(empty))

    def test_whitespace_only_token_rejected(self):
        ws = Path(self.tmp.name, "ws")
        ws.write_text("   \n", encoding="utf-8")
        os.chmod(ws, 0o600)
        with self.assertRaises(CoordinateHttpConfigError):
            validate_http_token_file(str(ws))


if __name__ == "__main__":
    unittest.main()
