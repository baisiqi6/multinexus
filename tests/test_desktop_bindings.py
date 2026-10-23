"""Desktop contracts are tested with private, fake configuration only."""
import hashlib
import importlib
import json
import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest


def private_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)
    return path


def make_profile(tmp_path, local_alias="codex"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    channels = {}
    for alias in (local_alias,):
        token = tmp_path / f"{alias}.token"
        token.write_text(f"fake-{alias}-credential")
        token.chmod(0o600)
        channels[alias] = str(private_json(tmp_path / f"{alias}.json", {
            "workspace_id": "workspace-a", "channel_id": "123", "client": alias,
            "client_id": f"private-{alias}", "url": "https://channel.invalid/mcp",
            "token_file": str(token), "tools": ["coordinate.channel_send"], "purpose": "fixture",
        }))
    token = tmp_path / "lifecycle.token"
    token.write_text(f"fake-{local_alias}-lifecycle-credential")
    token.chmod(0o600)
    lifecycle = private_json(tmp_path / "lifecycle.json", {
        "workspace_ids": ["workspace-a"], "platforms": ["none"],
        "tools": ["coordinate.runtime_request_submit", "coordinate.runtime_job_get"],
        "url": "https://channel.invalid/mcp", "token_file": str(token),
        "client_id": f"private-{local_alias}-lifecycle", "role": "native submit/get fixture",
    })
    data = {"version": 2, "local_alias": local_alias, "sender_expectations": EXPECTATIONS, "workspace_id": "workspace-a", "channel_id": "123",
            "mcp_url": "https://channel.invalid/mcp", "channel_profiles": channels,
            "lifecycle_profile": str(lifecycle), "bindings": {
                "codex": {"kind": "codex", "agent_id": "desktop-codex", "session_id": "11111111-1111-1111-1111-111111111111",
                          "cwd": str(tmp_path), "revision": "r1"},
                "dsh": {"kind": "dsh", "agent_id": "desktop-dsh", "session_id": "session-22222222-2222-2222-2222-222222222222",
                        "cwd": str(tmp_path), "revision": "r1"},
            }}
    path = private_json(tmp_path / "profile.json", data)
    return path, data


EXPECTATIONS = {
    "codex": {"client_id": "private-codex", "agent_id": "mac-codex", "discord_user_id": "100000000000000101"},
    "dsh": {"client_id": "private-dsh", "agent_id": "example-dsh-agent", "discord_user_id": "100000000000000102"},
}

def identity_fields(alias):
    return {"sender_identity": {"version": 1, **EXPECTATIONS[alias], "registry_source_id": "multinexus",
            "registry_source_version": 8, "registry_source_hash": "a" * 64},
            "identity_status": "verified", "binding_current": True,
            "actual_author_bot_id": EXPECTATIONS[alias]["discord_user_id"]}

@pytest.fixture
def profile_files(tmp_path):
    return make_profile(tmp_path)


def test_own_only_profile_never_reads_peer(profile_files, monkeypatch):
    from multinexus.desktop.bindings import load_profile, credential_config
    original = os.open
    def guarded(path, *args, **kw):
        assert str(path) not in {"dsh.json", "dsh.token"}
        return original(path, *args, **kw)
    monkeypatch.setattr(os, "open", guarded)
    profile = load_profile(profile_files[0])
    assert profile.local_alias == "codex"
    with pytest.raises(ValueError):
        credential_config(profile, "dsh")
    assert profile.sender_expectations["dsh"]["agent_id"] != profile.bindings["dsh"].agent_id

@pytest.mark.parametrize("mutation", [
    lambda d: d.update(local_alias="dsh"),
    lambda d: d["channel_profiles"].update(dsh="/absent/peer.json"),
    lambda d: d["sender_expectations"]["codex"].update(client_id="wrong"),
])
def test_own_identity_profile_rejects_swap(profile_files, mutation):
    from copy import deepcopy
    from multinexus.desktop.bindings import load_profile
    path, raw = profile_files
    data = deepcopy(raw); mutation(data); private_json(path, data)
    with pytest.raises(ValueError): load_profile(path)

@pytest.mark.parametrize("mutation", [
    lambda r: r.pop("sender_identity"), lambda r: r.update(binding_current=False),
    lambda r: r.update(actual_author_bot_id="999"), lambda r: r.update(identity_status="legacy_unattributed"),
    lambda r: r["sender_identity"].update(agent_id="desktop-codex"),
])
def test_source_identity_required_before_submission(profile_files, mutation):
    from multinexus.desktop.bindings import load_profile
    from multinexus.desktop.protocol import build_submission
    receipt = {"status":"sent", "delivery_id":"d1", "message_id":"m1", **identity_fields("codex")}
    mutation(receipt)
    with pytest.raises(ValueError): build_submission(load_profile(profile_files[0]), "codex", "dsh", "hello", receipt)


def test_profile_is_deeply_immutable_and_digest_is_canonical(profile_files):
    module = importlib.import_module("multinexus.desktop.bindings")
    path, data = profile_files
    profile = module.load_profile(path)
    binding = profile.bindings["codex"]
    assert binding.alias == "codex"
    assert binding.digest == module.binding_digest(binding.as_dict())
    assert module.binding_digest({"a": "汉", "b": 1}) == module.binding_digest({"b": 1, "a": "汉"})
    with pytest.raises((FrozenInstanceError, AttributeError)):
        binding.cwd = "/other"
    with pytest.raises(TypeError):
        profile.bindings["other"] = binding
    with pytest.raises(TypeError):
        profile.channel_profiles["other"] = Path("/other")
    assert set(profile.channel_profiles) == {"codex"}


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(version=True), lambda d: d.update(model="secret"),
    lambda d: d.update(workspace_id=""), lambda d: d.update(channel_id=123),
    lambda d: d.update(lifecycle_profile="relative.json"),
    lambda d: d["bindings"]["codex"].update(command="secret-command"),
    lambda d: d["bindings"]["codex"].update(kind="dsh"),
    lambda d: d["bindings"]["dsh"].update(session_id="not-a-native-id"),
    lambda d: d["bindings"]["codex"].update(revision=""),
    lambda d: d["bindings"]["codex"].update(cwd="relative"),
    lambda d: d["bindings"]["codex"].update(agent_id="desktop-dsh"),
    lambda d: d["bindings"]["codex"].update(session_id="session-22222222-2222-2222-2222-222222222222"),
])
def test_invalid_profile_rejected_without_reading_tokens(profile_files, monkeypatch, mutation):
    module = importlib.import_module("multinexus.desktop.bindings")
    path, data = profile_files
    mutation(data)
    private_json(path, data)
    original = os.open
    def guarded(path, *args, **kwargs):
        assert not str(path).endswith(".token"), "profile validation read credentials"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", guarded)
    with pytest.raises(ValueError):
        module.load_profile(path)


@pytest.mark.parametrize("which", ["profile", "channel", "lifecycle"])
@pytest.mark.parametrize("defect", ["symlink", "mode", "owner", "oversize"])
def test_private_config_guards(profile_files, monkeypatch, which, defect):
    module = importlib.import_module("multinexus.desktop.bindings")
    profile, data = profile_files
    path = {"profile": profile, "channel": Path(data["channel_profiles"]["codex"]),
            "lifecycle": Path(data["lifecycle_profile"])}[which]
    if defect == "symlink":
        real = path.with_suffix(".real")
        path.rename(real)
        path.symlink_to(real)
    elif defect == "mode":
        path.chmod(0o644)
    elif defect == "oversize":
        path.write_bytes(b" " * (1024 * 1024 + 1))
    else:
        monkeypatch.setattr(module.os, "getuid", lambda: os.stat(path).st_uid + 1)
    with pytest.raises(ValueError):
        module.load_profile(profile)


@pytest.mark.parametrize("which,field,value", [
    ("channel", "workspace_id", "other"), ("channel", "channel_id", "456"),
    ("channel", "client", "other"), ("lifecycle", "workspace_ids", ["other"]),
    ("lifecycle", "platforms", ["discord"]), ("lifecycle", "tools", ["coordinate.channel_send"]),
])
def test_referenced_config_scope_must_match(profile_files, which, field, value):
    module = importlib.import_module("multinexus.desktop.bindings")
    path, data = profile_files
    config_path = Path(data["channel_profiles"]["codex"] if which == "channel" else data["lifecycle_profile"])
    config = json.loads(config_path.read_text())
    config[field] = value
    private_json(config_path, config)
    with pytest.raises(ValueError):
        module.load_profile(path)


def test_duplicate_json_alias_rejected(profile_files):
    module = importlib.import_module("multinexus.desktop.bindings")
    path, _ = profile_files
    path.write_text('{"version":1,"version":1}')
    with pytest.raises(ValueError):
        module.load_profile(path)


def test_submission_key_is_delivery_only_and_target_change_changes_exact_replay(profile_files):
    bindings = importlib.import_module("multinexus.desktop.bindings")
    protocol = importlib.import_module("multinexus.desktop.protocol")
    profile = bindings.load_profile(profile_files[0])
    text = "  中文 😀\r\n"
    receipt = {**identity_fields("codex"), "delivery_id": "d1", "message_id": "m1", "status": "sent", "replayed": False}
    one = protocol.build_submission(profile, "codex", "dsh", text, receipt)
    from dataclasses import replace
    from types import MappingProxyType
    alternate = replace(profile.bindings["dsh"], alias="other", agent_id="mac-other",
                        session_id="session-33333333-3333-3333-3333-333333333333")
    profile = replace(profile, bindings=MappingProxyType({**profile.bindings, "other": alternate}))
    two = protocol.build_submission(profile, "codex", "other", text, receipt)
    assert protocol.runtime_key("d1") == "desktop-delivery-v1:d1"
    assert one["idempotency_key"] == two["idempotency_key"] == "desktop-delivery-v1:d1"
    assert one != two
    assert one["origin"]["desktop"]["source_alias"] == two["origin"]["desktop"]["source_alias"]
    assert one["prompt"] == text
    assert one["origin"]["platform"] == one["reply"]["platform"] == "none"
    assert one["origin"]["destination"] and one["reply"]["destination"]
    assert one["origin"]["session_scope_id"]
    assert "task_id" not in one and "input" not in one
    desktop = one["origin"]["desktop"]
    assert desktop["binding"] == profile.bindings["dsh"].as_dict()
    assert desktop["binding_digest"] == profile.bindings["dsh"].digest
    assert desktop["payload_digest"] == hashlib.sha256(text.encode()).hexdigest()
    assert protocol.payload_digest(text) == hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.parametrize("status", ["unknown", "rejected", "pending"])
def test_submission_requires_confirmed_channel_receipt(profile_files, status):
    bindings = importlib.import_module("multinexus.desktop.bindings")
    protocol = importlib.import_module("multinexus.desktop.protocol")
    with pytest.raises(ValueError):
        protocol.build_submission(bindings.load_profile(profile_files[0]), "codex", "dsh", "hello",
                                  {"delivery_id": "d1", "message_id": "m1", "status": status})


@pytest.mark.parametrize("field,value", [("kind", []), ("session_id", 7), ("cwd", None), ("agent_id", True)])
def test_binding_types_never_coerce(profile_files, field, value):
    module = importlib.import_module("multinexus.desktop.bindings")
    path, data = profile_files
    data["bindings"]["codex"][field] = value
    private_json(path, data)
    with pytest.raises(ValueError):
        module.load_profile(path)


def test_aliases_are_not_native_kind_names(profile_files):
    module = importlib.import_module("multinexus.desktop.bindings")
    path, data = profile_files
    data["local_alias"] = "sender"
    data["sender_expectations"] = {**data["sender_expectations"], "sender": data["sender_expectations"]["codex"]}
    del data["sender_expectations"]["codex"]
    data["bindings"]["sender"] = data["bindings"].pop("codex")
    data["channel_profiles"]["sender"] = data["channel_profiles"].pop("codex")
    private_json(path, data)
    assert module.load_profile(path).bindings["sender"].kind == "codex"


def test_lifecycle_endpoint_must_match_common_endpoint_without_token_read(profile_files):
    module = importlib.import_module("multinexus.desktop.bindings")
    path, data = profile_files
    lifecycle = Path(data["lifecycle_profile"])
    config = json.loads(lifecycle.read_text())
    config["url"] = "https://other.invalid/mcp"
    private_json(lifecycle, config)
    with pytest.raises(ValueError):
        module.load_profile(path)


def test_legacy_profile_remains_inspectable_but_native_use_requires_upgrade(profile_files):
    from multinexus.desktop.bindings import load_profile, read_private_json
    path, data = profile_files
    data["version"] = 1
    data.pop("local_alias"); data.pop("sender_expectations")
    data["channel_profiles"]["dsh"] = "/historical/absent-peer.json"
    private_json(path, data)
    assert read_private_json(path) == data
    with pytest.raises(ValueError, match="upgrade"):
        load_profile(path)
    assert read_private_json(path) == data


def test_receipt_accepts_canonical_registry_version_zero(profile_files):
    from multinexus.desktop.bindings import load_profile
    from multinexus.desktop.protocol import build_submission
    receipt = {"status":"sent", "delivery_id":"d1", "message_id":"m1", **identity_fields("codex")}
    receipt["sender_identity"]["registry_source_version"] = 0
    result = build_submission(load_profile(profile_files[0]), "codex", "dsh", "hello", receipt)
    assert result["origin"]["desktop"]["source_sender_identity"]["registry_source_version"] == 0
