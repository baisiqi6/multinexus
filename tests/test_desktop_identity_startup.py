"""Identity gates use a temporary synthetic registry and fake platform accounts."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from multinexus.config import load_config
from multinexus.models import AgentConfig


@pytest.fixture(autouse=True)
def synthetic_canonical_registry(tmp_path, monkeypatch):
    """Public tests own their registry and never require a deployed roster."""
    import multinexus.config as config
    registry = tmp_path / "canonical-registry.toml"
    registry.write_text('[registry]\nid="test.identity"\nversion=1\n'
                        '[[agents]]\nid="mac-codex"\ndisplay_name="Test Codex"\n'
                        'discord_user_id="100000000000000101"\nenabled=true\n')
    monkeypatch.setattr(config, "_CANONICAL_REGISTRY_PATH", registry)


def test_explicit_native_config_skips_dotenv_and_platform_token(tmp_path, monkeypatch):
    import multinexus.config as config
    path = tmp_path / "agents.toml"
    path.write_text('[[agents]]\nid="native"\nadapter="desktop-native"\ntoken_env="PEER_BOT_SECRET"\ntoken="inline-secret"\n')
    monkeypatch.setattr(config, "load_dotenv", lambda: pytest.fail("native loaded shared dotenv"))
    monkeypatch.setenv("PEER_BOT_SECRET", "peer-secret")
    result = load_config(["--config", str(path), "--agent", "native"], require_token=False)
    assert result.token == ""


def test_canonical_expected_bot_id_cannot_be_changed_by_runtime_config(tmp_path):
    path = tmp_path / "agents.toml"
    path.write_text('[[agents]]\nid="mac-codex"\nadapter="claude"\ntoken="fake"\ndiscord_user_id="999"\n')
    result = load_config(["--config", str(path), "--agent", "mac-codex"])
    assert getattr(result, "expected_discord_user_id", None) == 100000000000000101


@pytest.fixture
def client(tmp_path, monkeypatch):
    import multinexus.client as module
    monkeypatch.setattr(module, "make_adapter", lambda cfg: SimpleNamespace())
    config = AgentConfig(id="mac-codex", token="fake", context_db_path=str(tmp_path / "context.sqlite"))
    result = module.DiscordClient(config)
    result._connection.user = SimpleNamespace(id=999, bot=True, name="wrong", discriminator="0")
    return result


def test_wrong_bot_cannot_ready_publish_recover_or_route(client):
    client._register_slash_commands = Mock()
    client._start_reply_recovery = Mock()
    client.mention_router.update_discord_user_ids = Mock()
    client._bridge = SimpleNamespace(_on_client_ready=AsyncMock())
    async def run():
        await client.setup_hook()
        await client.on_ready()
        await client.on_message(SimpleNamespace())
        await client.on_message_edit(SimpleNamespace(), SimpleNamespace())
    asyncio.run(run())
    assert not client._register_slash_commands.called
    assert not client._start_reply_recovery.called
    assert not client._bridge._on_client_ready.called
    assert not client.mention_router.update_discord_user_ids.called


def test_wrong_and_pre_ready_bot_cannot_send_or_recover(client):
    channel = SimpleNamespace(send=AsyncMock())
    client.context_store.pending_runtime_replies = Mock(side_effect=AssertionError("recovery started"))
    client._coordinate_client = SimpleNamespace()
    async def run():
        await client._recover_runtime_replies()
        await client._send_text_chunks(channel, None, "hello", [], [])
        await client._send_adapter_response("hello", channel, None, context_channel_id="123", is_error=False)
    asyncio.run(run())
    assert not channel.send.called


def test_correct_canonical_bot_becomes_ready_once(client):
    client._connection.user.id = 100000000000000101
    client._register_slash_commands = Mock()
    client._start_reply_recovery = Mock()
    async def run():
        await client.setup_hook()
        await client.on_ready()
        await client.on_ready()
    asyncio.run(run())
    assert client._register_slash_commands.call_count == 1
    assert client._bot_user_id_map["mac-codex"] == 100000000000000101
    assert client._identity_ready() is True


@pytest.mark.parametrize("enabled", [True, False])
def test_new_registry_agent_uses_same_gate_without_executor_or_provider_switch(tmp_path, monkeypatch, enabled):
    import multinexus.config as config
    from multinexus.registry_authority import load_authority
    registry = tmp_path / "registry.toml"
    registry.write_text('[registry]\nid="test.registry"\nversion=1\n[[agents]]\nid="third-agent"\ndisplay_name="Third"\ndiscord_user_id="333"\nenabled=' + str(enabled).lower() + '\n')
    monkeypatch.setattr(config, "_CANONICAL_REGISTRY_PATH", registry)
    authority = load_authority(registry)
    assert authority.entries[0].enabled is enabled
    assert config.canonical_discord_user_id("third-agent") == (333 if enabled else None)


def test_unverified_client_cannot_update_router_from_peer_notice(client):
    client.mention_router.update_discord_user_ids = Mock()
    client.register_peer_bot("peer", 123)
    assert not client.mention_router.update_discord_user_ids.called


def test_ready_peer_map_converges_when_second_client_becomes_ready(client, tmp_path):
    from multinexus.client import DiscordBridge, DiscordClient
    other = DiscordClient(AgentConfig(id="mac-codex", token="fake", context_db_path=str(tmp_path / "other.sqlite")))
    # This test isolates bridge convergence after account verification.
    client._connection.user.id = client._expected_bot_id
    client._identity_verified = True
    other._connection.user = SimpleNamespace(id=123, bot=True)
    other._expected_bot_id = 123
    other.agent_config.id = "peer"
    bridge = DiscordBridge.__new__(DiscordBridge)
    bridge.clients = [client, other]
    asyncio.run(bridge._on_client_ready(client))
    assert not other._bot_user_id_map
    other._identity_verified = True
    asyncio.run(bridge._on_client_ready(other))
    assert client._bot_user_id_map["peer"] == 123
    assert other._bot_user_id_map["mac-codex"] == client.user.id


def test_config_expected_identity_mismatch_cannot_enable_client(tmp_path, monkeypatch):
    import multinexus.client as module
    monkeypatch.setattr(module, "make_adapter", lambda cfg: SimpleNamespace())
    cfg = AgentConfig(id="mac-codex", token="fake", expected_discord_user_id=999,
                      context_db_path=str(tmp_path / "mismatch.sqlite"))
    client = module.DiscordClient(cfg)
    client._connection.user = SimpleNamespace(id=100000000000000101, bot=True, name="correct", discriminator="0")
    client._register_slash_commands = Mock()
    asyncio.run(client.on_ready())
    assert not client._identity_ready()
    assert not client._register_slash_commands.called
