"""Send authorization gates and explicit flat runtime status contracts."""
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_desktop_bindings import profile_files, private_json, make_profile, identity_fields


@pytest.fixture
def cli(monkeypatch, profile_files):
    module = importlib.import_module("multinexus.desktop.cli")
    calls = []
    state = {"channel": {"ok": True, "data": {"delivery_id": "d1", "message_id": "m1",
              "status": "sent", "replayed": False, **identity_fields("codex")}, "error": None},
             "runtime": {"ok": True, "data": {"event": {}, "event_created": True, "job_created": True,
                 "job": {"id": "j1", "workspace_id": "workspace-a", "assigned_agent": "desktop-dsh", "status": "pending"}}, "error": None}}
    def transport(kind):
        def construct(*, url, token_env):
            assert os.environ[token_env] == ("fake-codex-lifecycle-credential" if kind == "runtime" else "fake-codex-credential")
            def call(name, arguments):
                calls.append((kind, url, name, arguments))
                result = state[kind]
                if isinstance(result, BaseException):
                    raise result
                return result
            return SimpleNamespace(call_tool=call)
        return construct
    monkeypatch.setattr(module, "ChannelMCPTransport", transport("channel"))
    monkeypatch.setattr(module, "LifecycleMCPTransport", transport("runtime"))
    return module, calls, state, profile_files


def send(path, *extra):
    return ["send", "--profile", str(path), "--to", "dsh",
            "--send-key", "stable", *extra]


def test_sent_channel_receipt_submits_once_and_returns_job(cli, capsys, tmp_path):
    module, calls, _, (profile, _) = cli
    message = tmp_path / "message.txt"
    message.write_bytes("  中文 😀\r\n[handoff] @x\r\n".encode())
    assert module.main(send(profile, "--text-file", str(message), "--reply-to-message-id", "previous")) == 0
    assert len(calls) == 2
    assert calls[0] == ("channel", "https://channel.invalid/mcp", "coordinate.channel_send", {
        "workspace_id": "workspace-a", "channel_id": "123", "platform": "discord",
        "send_key": "stable", "text": "  中文 😀\r\n[handoff] @x\r\n", "reply_to_message_id": "previous"})
    assert calls[1][0:3] == ("runtime", "https://channel.invalid/mcp", "coordinate.runtime_request_submit")
    assert calls[1][3]["idempotency_key"] == "desktop-delivery-v1:d1"
    assert calls[1][3]["prompt"] == calls[0][3]["text"]
    assert "input" not in calls[1][3]
    output = capsys.readouterr()
    assert json.loads(output.out)["data"]["job_id"] == "j1"
    assert len(output.out.splitlines()) == 1 and not output.err
    assert "fake-" not in output.out


@pytest.mark.parametrize("status", ["unknown", "rejected", "pending"])
def test_unconfirmed_channel_send_never_submits_runtime(cli, capsys, status):
    module, calls, state, (profile, _) = cli
    state["channel"] = {"ok": False, "data": {"delivery_id": "d1", "status": status},
                        "error": {"code": "delivery_uncertain", "message": "uncertain"}}
    assert module.main(send(profile, "--text", "hello")) == 1
    assert len(calls) == 1
    assert json.loads(capsys.readouterr().out)["data"]["status"] == status


def test_status_is_job_id_only_and_does_not_read_channel_token(cli, capsys, monkeypatch):
    module, calls, state, (profile, _) = cli
    state["runtime"] = {"ok": True, "data": {"id": "j1", "workspace_id": "workspace-a", "assigned_agent": "desktop-dsh", "status": "pending"}, "error": None}
    original = os.open
    def guarded(path, *args, **kwargs):
        assert not str(path).endswith("codex.token") and not str(path).endswith("dsh.token")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", guarded)
    assert module.main(["status", "--profile", str(profile), "--job-id", "j1"]) == 0
    assert calls == [("runtime", "https://channel.invalid/mcp", "coordinate.runtime_job_get", {"job_id": "j1"})]
    assert json.loads(capsys.readouterr().out)["data"]["job_id"] == "j1"


@pytest.mark.parametrize("extra", [
    ["--text", " "], ["--text", "😀" * 901], ["--text", "hello", "--model", "secret"],
    ["--text", "hello", "--to", "unbound"], ["--text", "hello", "--client", "unbound"],
    ["--text", "hello", "--send-key", "中文"],
])
def test_invalid_input_precedes_any_credential_read(cli, capsys, monkeypatch, extra):
    module, calls, _, (profile, _) = cli
    original = os.open
    def guarded(path, *args, **kwargs):
        assert not str(path).endswith(".token"), "invalid input accessed credential"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", guarded)
    assert module.main(send(profile, *extra)) == 2
    assert calls == []
    output = capsys.readouterr()
    assert json.loads(output.out)["error"]["code"] == "invalid_input"
    assert "secret" not in output.out + output.err


@pytest.mark.parametrize("defect", ["symlink", "mode", "oversize"])
def test_guarded_tokens_never_reach_transport(cli, capsys, defect):
    module, calls, _, (profile, data) = cli
    config = json.loads(Path(data["channel_profiles"]["codex"]).read_text())
    token = Path(config["token_file"])
    if defect == "symlink":
        real = token.with_suffix(".real")
        token.rename(real)
        token.symlink_to(real)
    elif defect == "mode":
        token.chmod(0o644)
    else:
        token.write_bytes(b"x" * (1024 * 1024 + 1))
    assert module.main(send(profile, "--text", "hello")) == 2
    assert calls == []
    assert "fake-" not in capsys.readouterr().out


def test_scoped_token_environment_is_restored_on_failure(cli, monkeypatch, capsys):
    module, _, state, (profile, _) = cli
    monkeypatch.setenv("MULTINEXUS_DESKTOP_CHANNEL_TOKEN", "prior-channel")
    monkeypatch.setenv("MULTINEXUS_DESKTOP_LIFECYCLE_TOKEN", "prior-lifecycle")
    state["runtime"] = RuntimeError("fake-lifecycle-credential secret prompt")
    assert module.main(send(profile, "--text", "private body")) == 1
    assert os.environ["MULTINEXUS_DESKTOP_CHANNEL_TOKEN"] == "prior-channel"
    assert os.environ["MULTINEXUS_DESKTOP_LIFECYCLE_TOKEN"] == "prior-lifecycle"
    output = capsys.readouterr()
    assert "private body" not in output.out and "fake-" not in output.out


def test_self_send_rejected_before_tokens(cli, monkeypatch, capsys):
    module, calls, _, (profile, _) = cli
    original = os.open
    def guarded(path, *args, **kwargs):
        assert not str(path).endswith(".token")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", guarded)
    assert module.main(send(profile, "--to", "codex", "--text", "hello")) == 2
    assert calls == []
    capsys.readouterr()


def test_tailnet_exact_host_bypass_is_scoped_and_restored(cli, monkeypatch, capsys):
    from urllib.request import getproxies, proxy_bypass_environment
    module, calls, state, (path, data) = cli
    host = "service.example.ts.net"
    data["mcp_url"] = "https://" + host + "/mcp"
    private_json(path, data)
    for config_path in [*data["channel_profiles"].values(), data["lifecycle_profile"]]:
        config = json.loads(Path(config_path).read_text())
        config["url"] = data["mcp_url"]
        private_json(Path(config_path), config)
    monkeypatch.setenv("NO_PROXY", "prior.invalid")
    monkeypatch.setenv("no_proxy", "prior-lower.invalid")
    seen = []
    def construct(**kwargs):
        def call(name, args):
            proxies = getproxies()
            assert proxy_bypass_environment(host, proxies)
            assert not proxy_bypass_environment("unrelated.ts.net", proxies)
            seen.append(name)
            return state["channel" if "channel" in name else "runtime"]
        return SimpleNamespace(call_tool=call)
    monkeypatch.setattr(module, "ChannelMCPTransport", construct)
    monkeypatch.setattr(module, "LifecycleMCPTransport", construct)
    assert module.main(send(path, "--text", "hello")) == 0
    assert seen == ["coordinate.channel_send", "coordinate.runtime_request_submit"]
    assert os.environ["NO_PROXY"] == "prior.invalid"
    assert os.environ["no_proxy"] == "prior-lower.invalid"
    capsys.readouterr()


def test_status_never_prints_native_prompt_or_body(cli, capsys):
    module, _, state, (profile, _) = cli
    state["runtime"] = {"ok": True, "data": {"id": "j1", "workspace_id": "workspace-a", "assigned_agent": "desktop-dsh", "status": "done", "prompt": "private prompt",
        "result": {"response_text": "private native body", "desktop": {"status": "receiver_acknowledged"}}}, "error": None}
    assert module.main(["status", "--profile", str(profile), "--job-id", "j1"]) == 0
    output = capsys.readouterr()
    assert "private prompt" not in output.out and "private native body" not in output.out
    assert json.loads(output.out)["data"]["job_id"] == "j1"


@pytest.mark.parametrize("field,value", [("workspace_id", "other-workspace"), ("assigned_agent", "other-agent")])
def test_send_rejects_wrong_runtime_scope(cli, capsys, field, value):
    module, _, state, (profile, _) = cli
    state["runtime"]["data"]["job"][field] = value
    assert module.main(send(profile, "--text", "hello")) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert value not in json.dumps(output)


def test_status_rejects_wrong_workspace_or_job(cli, capsys):
    module, _, state, (profile, _) = cli
    state["runtime"] = {"ok": True, "data": {"id": "j1", "workspace_id": "other", "status": "done"}, "error": None}
    assert module.main(["status", "--profile", str(profile), "--job-id", "j1"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_runtime_error_does_not_echo_provider_message(cli, capsys):
    module, _, state, (profile, _) = cli
    state["runtime"] = {"ok": False, "data": None, "error": {"code": "forbidden", "message": "private body"}}
    assert module.main(["status", "--profile", str(profile), "--job-id", "j1"]) == 1
    assert "private body" not in capsys.readouterr().out


def test_channel_transport_uncertainty_preserves_receipt_and_never_submits(cli, capsys):
    from multinexus.channel_mcp_transport import ChannelTransportError
    module, calls, state, (profile, _) = cli
    state["channel"] = ChannelTransportError("delivery_uncertain", "private provider details",
                                            data={"status": "unknown", "delivery_id": "d1"})
    assert module.main(send(profile, "--text", "hello")) == 1
    assert len(calls) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["data"] == {"status": "unknown", "delivery_id": "d1"}
    assert "private provider" not in output.out


def test_bound_entry_reuses_parser_and_rejects_selectors(cli, capsys):
    module, calls, _, (path, _) = cli
    entry = getattr(module, "bound_main", None)
    assert callable(entry)
    assert entry(path, ["send", "--to", "dsh", "--send-key", "fixed", "--text", "hello"]) == 0
    capsys.readouterr(); calls.clear()
    for override in [["--profile", str(path)], ["--client=codex"]]:
        assert entry(path, ["send", "--to", "dsh", "--send-key", "fixed", "--text", "hello", *override]) == 2
        assert not calls
        capsys.readouterr()


@pytest.mark.parametrize("mutation", [
    lambda r: r.pop("sender_identity"), lambda r: r.update(binding_current=False),
    lambda r: r.update(actual_author_bot_id="999"),
])
def test_invalid_post_receipt_is_uncertain_and_never_submits(cli, capsys, mutation):
    module, calls, state, (path, _) = cli
    mutation(state["channel"]["data"])
    assert module.main(send(path, "--text", "hello")) == 1
    assert len(calls) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "delivery_uncertain"
    assert result["data"]["delivery_id"] == "d1"


def test_concurrent_own_capability_scopes_cannot_cross_tokens(profile_files):
    import concurrent.futures
    import threading
    import time
    from multinexus.desktop.cli import _credential_environment, _CHANNEL_TOKEN
    from multinexus.desktop.bindings import load_profile, credential_config
    source = credential_config(load_profile(profile_files[0]), "codex")
    receiver = credential_config(load_profile(make_profile(profile_files[0].parent / "peer", "dsh")[0]), "dsh")
    entered = threading.Event()
    def first():
        with _credential_environment(source, _CHANNEL_TOKEN):
            entered.set(); time.sleep(.04)
            return os.environ[_CHANNEL_TOKEN]
    def second():
        entered.wait()
        with _credential_environment(receiver, _CHANNEL_TOKEN):
            time.sleep(.08)
            return os.environ[_CHANNEL_TOKEN]
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        one, two = pool.submit(first), pool.submit(second)
        assert one.result() == "fake-codex-credential"
        assert two.result() == "fake-dsh-credential"


@pytest.mark.parametrize("override", [
    ["--profile=/other"], ["--profile", "/other", "--profile", "/again"],
    ["--prof", "/other"], ["--cli", "dsh"], ["--client", "dsh", "--client", "codex"],
])
def test_bound_entry_disallows_selector_duplicates_and_abbreviations(cli, capsys, override):
    module, calls, _, (path, _) = cli
    assert module.bound_main(path, ["send", "--to", "dsh", "--send-key", "stable", "--text", "hello", *override]) == 2
    assert not calls
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_input"


def test_bound_reply_rejects_packet_from_another_profile_before_ack(cli, monkeypatch, capsys):
    from multinexus.desktop import ack
    module, calls, _, (path, _) = cli
    monkeypatch.setattr(ack, "read_request", lambda _: {"profile_path": "/peer/profile.json"})
    monkeypatch.setattr(ack, "reply_request", lambda *a: pytest.fail("bound entry contacted peer ACK"))
    assert module.bound_main(path, ["reply", "--request-file", "/fake/request", "--text", "hello"]) == 2
    assert not calls
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_input"


def test_bound_status_uses_fixed_own_lifecycle(cli, capsys):
    module, calls, state, (path, _) = cli
    state["runtime"] = {"ok": True, "data": {"id": "j1", "workspace_id": "workspace-a", "status": "pending"}, "error": None}
    assert module.bound_main(path, ["status", "--job-id", "j1"]) == 0
    assert len(calls) == 1 and calls[0][0] == "runtime"
    capsys.readouterr()
