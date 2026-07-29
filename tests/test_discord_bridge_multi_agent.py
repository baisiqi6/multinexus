"""Tests for ``DiscordBridge`` (Phase 7.1.1 single-platform bridge).

These tests exercise the in-process bridge without connecting to Discord.
They verify:

* N ``DiscordClient`` instances are constructed from N ``AgentConfig`` entries.
* ``agent_ids`` exposes the registered agent IDs in input order.
* ``_on_client_ready`` propagates user_id to peer clients via
  ``register_peer_bot`` so the shared mention map converges.
* Legacy single-agent mode (``DiscordClient(config)``) still constructs a
  valid object, so backward compat holds.
* Loading a config with all four managed agents does not raise, given a
  valid token environment (we mock by passing tokens directly to a TOML
  in a temp dir).
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from multinexus.client import DiscordBridge, DiscordClient
from multinexus.config import load_all_configs_for_platform, load_config
from multinexus.models import AgentConfig


def _make_cfg(
    agent_id: str = "mac-test",
    *,
    token: str = "fake-token",
    adapter: str = "claude",
) -> AgentConfig:
    """Minimal ``AgentConfig`` for bridge construction (no TOML parse)."""
    return AgentConfig(
        id=agent_id,
        token=token,
        adapter=adapter,
        display_name=agent_id,
        aliases={agent_id},
        role_ids=set(),
        channels=[],
        respond_to_bots=True,
        context_db_path=f"data/test_{agent_id}.sqlite3",
        context_recent_messages=40,
        context_budget_chars=12000,
        context_max_message_chars=500,
        context_ttl_seconds=86400,
        handoff_dedupe_seconds=600,
        timeout=360,
        first_byte_timeout=120,
        activity_timeout=120,
        system_prompt="",
        known_agents=[],
        work_dir=None,
        model=None,
        openclaw_agent_id="main",
        openclaw_bin="openclaw",
        hermes_bin="hermes",
        hermes_provider=None,
        hermes_toolsets=None,
        hermes_accept_hooks=False,
        claude_bin="claude",
        claude_dangerously_skip_permissions=False,
        codex_bin="codex",
        codex_sandbox="danger-full-access",
        codex_dangerously_bypass_approvals_and_sandbox=False,
        codex_fallback_model=None,
        opencode_bin="opencode",
        opencode_dangerously_skip_permissions=False,
        omp_bin="omp",
        omp_model=None,
        omp_thinking=None,
        omp_auto_approve=True,
        wiki_enabled=False,
        wiki_path="wiki",
        discoveries_channel_id=None,
        allowed_user_ids=[],
        coordinator_bot_id=None,
        coordinator_cli_path="",
        coordinator_db_path="",
        coordinator_workspace_path="",
        agentd_mode=False,
        agentd_port=0,
        agentd_host="127.0.0.1",
        kook_poll_channel_ids=[],
        kook_poll_interval_seconds=5,
        kook_poll_page_size=50,
    )


class DiscordBridgeConstructionTests(unittest.TestCase):
    def test_constructs_n_clients(self):
        cfgs = [
            _make_cfg("mac-claude"),
            _make_cfg("mac-codex"),
            _make_cfg("mac-omp"),
        ]
        bridge = DiscordBridge(cfgs)
        self.assertEqual(len(bridge.clients), 3)
        self.assertEqual(
            bridge.agent_ids,
            ["mac-claude", "mac-codex", "mac-omp"],
        )
        for c, cfg in zip(bridge.clients, cfgs):
            self.assertIs(c.agent_config, cfg)

    def test_constructs_one_client(self):
        cfgs = [_make_cfg("mac-claude")]
        bridge = DiscordBridge(cfgs)
        self.assertEqual(len(bridge.clients), 1)
        self.assertEqual(bridge.agent_ids, ["mac-claude"])

    def test_empty_configs_raises(self):
        with self.assertRaises(SystemExit):
            DiscordBridge([])


class DiscordBridgeMentionPropagationTests(unittest.TestCase):
    """Verify peer registration populates the shared mention map."""

    def _build_bridge_with_mocked_users(self):
        cfgs = [
            _make_cfg("mac-claude"),
            _make_cfg("mac-codex"),
        ]
        bridge = DiscordBridge(cfgs)

        # Inject fake user objects (discord.User) into each client. The
        # ``user`` attribute on ``discord.Client`` is a read-only property,
        # so we patch it via ``PropertyMock`` on a per-instance subclass.
        from unittest.mock import PropertyMock, patch

        self._patchers = []
        for c, uid in zip(bridge.clients, (1001, 1002)):
            fake = type("FakeUser", (), {"id": uid, "name": f"bot-{uid}", "discriminator": "0001"})()
            # Patch the *instance* via a per-instance subclass that overrides
            # ``user``. This is more reliable than class-level patching when
            # we have multiple clients that all need different fakes.
            sub = type(f"Client_{uid}", (type(c),), {
                "user": PropertyMock(return_value=fake),
            })
            patcher = patch.object(c, "__class__", sub)
            patcher.start()
            self._patchers.append(patcher)

        return bridge

    def tearDown(self):
        for patcher in getattr(self, "_patchers", []):
            try:
                patcher.stop()
            except Exception:
                pass

    def _teardown_bridge_user_patches(self, bridge):
        for c in bridge.clients:
            patcher = getattr(c, "_user_patcher", None)
            if patcher is not None:
                patcher.stop()

    def test_on_client_ready_propagates_user_id(self):
        bridge = self._build_bridge_with_mocked_users()
        try:
            asyncio.run(bridge._on_client_ready(bridge.clients[0]))

            # mac-codex's mention map should now know mac-claude's user_id.
            codex_map = bridge.clients[1]._bot_user_id_map
            self.assertEqual(codex_map.get("mac-claude"), 1001)
            # mac-claude should NOT have called register_peer_bot on itself
            # (the loop skips self), so its own map is empty.
            claude_map = bridge.clients[0]._bot_user_id_map
            self.assertNotIn("mac-claude", claude_map)
        finally:
            self._teardown_bridge_user_patches(bridge)

    def test_both_clients_ready_yields_full_map(self):
        bridge = self._build_bridge_with_mocked_users()
        try:
            asyncio.run(bridge._on_client_ready(bridge.clients[0]))
            asyncio.run(bridge._on_client_ready(bridge.clients[1]))

            # After both have called _on_client_ready, each side knows the
            # other.
            claude_map = bridge.clients[0]._bot_user_id_map
            codex_map = bridge.clients[1]._bot_user_id_map
            self.assertEqual(claude_map.get("mac-codex"), 1002)
            self.assertEqual(codex_map.get("mac-claude"), 1001)
        finally:
            self._teardown_bridge_user_patches(bridge)

    def test_is_all_ready_reflects_user_state(self):
        bridge = self._build_bridge_with_mocked_users()
        try:
            self.assertTrue(bridge.is_all_ready())
            # Verify the function is callable and returns a sensible bool.
            self.assertIsInstance(bridge.is_all_ready(), bool)
        finally:
            self._teardown_bridge_user_patches(bridge)


class LoadAllConfigsForPlatformTests(unittest.TestCase):
    """Verify ``load_all_configs_for_platform`` returns N configs."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False, encoding="utf-8"
        )
        self.tmp.write(
            '[defaults]\n'
            'timeout = 360\n'
            'context_db_path = "data/test_bridge.sqlite3"\n'
            'coordinator_cli_path = "/tmp/mac.sh"\n'
            'coordinator_db_path = "/tmp/coordinator.sqlite3"\n'
            '\n'
            '[[agents]]\n'
            'id = "mac-claude"\n'
            'adapter = "claude"\n'
            'token = "fake-claude-token"\n'
            '\n'
            '[[agents]]\n'
            'id = "mac-codex"\n'
            'adapter = "codex"\n'
            'token = "fake-codex-token"\n'
            '\n'
            '[[agents]]\n'
            'id = "mac-omp"\n'
            'adapter = "omp"\n'
            'token = "fake-omp-token"\n'
        )
        self.tmp.close()
        self.path = self.tmp.name

    def tearDown(self):
        os.unlink(self.path)

    def test_loads_all_three_agents(self):
        cfgs = load_all_configs_for_platform(config_path=self.path, require_token=True)
        self.assertEqual([c.id for c in cfgs], ["mac-claude", "mac-codex", "mac-omp"])
        for c in cfgs:
            self.assertTrue(c.token)
            self.assertIn(c.adapter, ("claude", "codex", "omp"))

    def test_load_missing_path_raises(self):
        with self.assertRaises(SystemExit):
            load_all_configs_for_platform(config_path="/nonexistent/agents.toml")

    def test_load_empty_agents_raises(self):
        empty = tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False, encoding="utf-8"
        )
        empty.write("[defaults]\ntimeout = 60\n")
        empty.close()
        try:
            with self.assertRaises(SystemExit):
                load_all_configs_for_platform(config_path=empty.name)
        finally:
            os.unlink(empty.name)


class LegacySingleAgentModeStillWorksTests(unittest.TestCase):
    """``load_config`` and ``DiscordClient(config)`` are unchanged for legacy
    callers using ``--agent``."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False, encoding="utf-8"
        )
        self.tmp.write(
            '[defaults]\n'
            'timeout = 360\n'
            'context_db_path = "data/test_legacy.sqlite3"\n'
            '\n'
            '[[agents]]\n'
            'id = "mac-claude"\n'
            'adapter = "claude"\n'
            'token = "fake-claude-token"\n'
        )
        self.tmp.close()
        self.path = self.tmp.name

    def tearDown(self):
        os.unlink(self.path)

    def test_load_config_legacy(self):
        cfg = load_config(["--config", self.path, "--agent", "mac-claude"])
        self.assertEqual(cfg.id, "mac-claude")
        self.assertEqual(cfg.token, "fake-claude-token")

    def test_load_config_require_token_false_skips_validation(self):
        # Write a config without token to test the new opt-out path.
        tmp2 = tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False, encoding="utf-8"
        )
        tmp2.write(
            '[defaults]\n'
            'context_db_path = "data/test_no_token.sqlite3"\n'
            '\n'
            '[[agents]]\n'
            'id = "mac-claude"\n'
            'adapter = "claude"\n'
        )
        tmp2.close()
        try:
            # require_token=True (default): should fail.
            with self.assertRaises(SystemExit):
                load_config(
                    ["--config", tmp2.name, "--agent", "mac-claude"],
                    require_token=True,
                )
            # require_token=False: should succeed (agentd path).
            cfg = load_config(
                ["--config", tmp2.name, "--agent", "mac-claude"],
                require_token=False,
            )
            self.assertEqual(cfg.id, "mac-claude")
            # agentd path does not need a real token; the empty/missing
            # value is left intact (config.py coerces via str() so we just
            # verify it didn't raise and the agent id is correct).
            self.assertNotEqual(cfg.id, "")
        finally:
            os.unlink(tmp2.name)


import os as _os


class ResolveBridgeProxyUrlTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: _os.environ.get(k) for k in _os.environ}

    def tearDown(self):
        for k in list(_os.environ.keys()):
            if k not in self._saved:
                del _os.environ[k]
        for k, v in self._saved.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v

    @staticmethod
    def _clear_vars():
        for v in (
            "MULTINEXUS_HTTP_PROXY",
            "HTTPS_PROXY", "https_proxy",
            "HTTP_PROXY", "http_proxy",
        ):
            _os.environ.pop(v, None)

    def test_no_env_returns_none(self):
        self._clear_vars()
        from multinexus.client import _resolve_bridge_proxy_url
        self.assertIsNone(_resolve_bridge_proxy_url())

    def test_multinexus_http_proxy_takes_priority(self):
        self._clear_vars()
        _os.environ["MULTINEXUS_HTTP_PROXY"] = "http://127.0.0.1:7890"
        _os.environ["HTTPS_PROXY"] = "http://other:3128"
        from multinexus.client import _resolve_bridge_proxy_url
        self.assertEqual(_resolve_bridge_proxy_url(), "http://127.0.0.1:7890")

    def test_fallback_to_https_proxy(self):
        self._clear_vars()
        _os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"
        from multinexus.client import _resolve_bridge_proxy_url
        self.assertEqual(_resolve_bridge_proxy_url(), "http://127.0.0.1:7890")

    def test_fallback_to_http_proxy(self):
        self._clear_vars()
        _os.environ["HTTP_PROXY"] = "http://127.0.0.1:7890"
        from multinexus.client import _resolve_bridge_proxy_url
        self.assertEqual(_resolve_bridge_proxy_url(), "http://127.0.0.1:7890")

    def test_lowercase_fallback(self):
        self._clear_vars()
        _os.environ["http_proxy"] = "http://127.0.0.1:7890"
        from multinexus.client import _resolve_bridge_proxy_url
        self.assertEqual(_resolve_bridge_proxy_url(), "http://127.0.0.1:7890")


if __name__ == "__main__":
    unittest.main()
