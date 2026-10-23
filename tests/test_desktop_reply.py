"""Only worker-owned channel SDK results can settle an active request."""
import asyncio
import importlib
import json
import tempfile
from pathlib import Path

import pytest
import sys

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="native desktop acceptance is macOS only")
from test_desktop_bindings import make_profile, identity_fields

@pytest.fixture
def profile_files(tmp_path):
    return make_profile(tmp_path, "dsh")
from multinexus.desktop.bindings import load_profile


def ack_module():
    try:
        return importlib.import_module("multinexus.desktop.ack")
    except ModuleNotFoundError:
        pytest.fail("Scoped trusted reply endpoint is not implemented")


def test_ack_rejects_forged_receipts_and_scopes_then_sends_fixed_reply(profile_files):
    module = ack_module()
    profile = load_profile(profile_files[0])
    scope = {"job_id": "request:e1", "attempt_token": "1", "lease_id": "lease1", "native_request_id": "n1",
             "binding_digest": profile.bindings["dsh"].digest, "payload_digest": "a"*64,
             "native_content_digest": "b"*64, "recipient": "dsh"}
    sent = []
    def send(profile, alias, values):
        sent.append((alias, values))
        return {"delivery_id":"reply-d1", "message_id":"reply-m1", "status":"sent", "replayed":False, **identity_fields("dsh")}
    async def run():
        with tempfile.TemporaryDirectory(prefix="mn-", dir="/private/tmp") as root:
            async with module.ActiveReply(scope=scope, profile=profile, profile_path=profile_files[0],
                source_alias="codex", incoming_message_id="m1", runtime_dir=root, send=send,
                lease_lost=asyncio.Event()) as endpoint:
                packet = module.read_request(endpoint.request_file)
                assert endpoint.request_file.stat().st_mode & 0o777 == 0o600
                request = {"version":2, "capability":packet["capability"], "scope":scope, "text":"reply"}
                forged = await endpoint.handle({**request, "receipt":{"status":"sent"}})
                assert not forged["ok"] and not sent
                wrong = await endpoint.handle({**request, "scope":{**scope,"lease_id":"other"}})
                assert not wrong["ok"] and not sent
                first, replay = await asyncio.gather(endpoint.handle(request), endpoint.handle(request))
                assert first["ok"] and replay["ok"] and endpoint.acknowledged.is_set()
                assert len(sent) == 1
                assert sent[0] == ("dsh", {"send_key":"desktop-reply-v1:request:e1", "text":"reply", "reply_to_message_id":"m1"})
                conflict = await endpoint.handle({**request,"text":"different"})
                assert not conflict["ok"] and len(sent) == 1
                late_file = endpoint.request_file
            assert not late_file.exists()
            assert not (await endpoint.handle(request))["ok"]
    asyncio.run(run())


@pytest.mark.parametrize("status", ["unknown", "pending", "rejected"])
def test_unconfirmed_sdk_send_does_not_ack(profile_files, status):
    module = ack_module()
    profile = load_profile(profile_files[0])
    async def run():
        with tempfile.TemporaryDirectory(prefix="mn-", dir="/private/tmp") as root:
            async with module.ActiveReply(scope={"job_id":"j1"}, profile=profile, profile_path=profile_files[0],
                source_alias="codex", incoming_message_id="m1", runtime_dir=root,
                send=lambda *a: {"status":status}, lease_lost=asyncio.Event()) as endpoint:
                packet = module.read_request(endpoint.request_file)
                result = await endpoint.handle({"version":2,"capability":packet["capability"],"scope":{"job_id":"j1"},"text":"reply"})
                assert not result["ok"] and not endpoint.acknowledged.is_set()
    asyncio.run(run())


def test_reply_cli_does_not_reverse_without_explicit_flag(profile_files, monkeypatch, capsys):
    cli = importlib.import_module("multinexus.desktop.cli")
    module = ack_module()
    profile = load_profile(profile_files[0])
    packet = {"profile_path":str(profile_files[0]), "source_alias":"codex", "incoming_message_id":"m1",
              "scope":{"job_id":"request:e1", "recipient":"dsh", "binding_digest":profile.bindings["dsh"].digest}}
    monkeypatch.setattr(module, "reply_request", lambda path, text: (packet, {
        "ok":True,"data":{"status":"sent","delivery_id":"rd1","message_id":"rm1","replayed":False, **identity_fields("dsh")},"error":None}))
    calls = []
    def runtime(config, name, arguments):
        calls.append((name, arguments))
        return {"ok":True,"data":{"job_created":True,"job":{"id":"reverse-job","workspace_id":"workspace-a","assigned_agent":"desktop-codex","status":"pending"}},"error":None}
    monkeypatch.setattr(cli, "_call_runtime", runtime)
    channel_calls = []
    def transport(**kwargs):
        class Transport:
            def call_tool(self, name, arguments):
                channel_calls.append(arguments)
                return {"ok":True,"data":{"status":"sent","delivery_id":"sdk-reply","message_id":"sdk-message","replayed":True, **identity_fields("dsh")},"error":None}
        return Transport()
    monkeypatch.setattr(cli, "ChannelMCPTransport", transport)
    base = ["reply", "--request-file", "/fake/request.json", "--text", "reply"]
    assert cli.main(base) == 0
    assert not calls and not channel_calls
    assert json.loads(capsys.readouterr().out)["data"]["status"] == "sent"
    assert cli.main([*base, "--wake-source"]) == 0
    assert len(calls) == 1 and calls[0][1]["idempotency_key"] == "desktop-delivery-v1:sdk-reply"
    assert len(channel_calls) == 1 and channel_calls[0]["send_key"] == "desktop-reply-v1:request:e1"
    assert calls[0][1]["origin"]["desktop"]["source_alias"] == "dsh"
    assert calls[0][1]["origin"]["desktop"]["recipient"] == "codex"
    assert json.loads(capsys.readouterr().out)["data"]["status"] == "pending"


def test_absolute_cli_runs_from_other_cwd_without_pythonpath(tmp_path):
    import os
    import subprocess
    import sys
    cli_path = Path(__file__).resolve().parents[1] / "multinexus/desktop/cli.py"
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, str(cli_path), "reply", "--help"], cwd=tmp_path,
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "--request-file" in result.stdout and "--wake-source" in result.stdout


@pytest.mark.parametrize("failure", ["channel_unknown", "runtime_failed", "transport_error"])
def test_reverse_failure_reports_acknowledged_partial_without_false_job(profile_files, monkeypatch, capsys, failure):
    cli, ack = importlib.import_module("multinexus.desktop.cli"), ack_module()
    profile = load_profile(profile_files[0])
    receipt = {"status":"sent","delivery_id":"rd1","message_id":"rm1","replayed":False, **identity_fields("dsh")}
    packet = {"profile_path":str(profile_files[0]), "source_alias":"codex", "incoming_message_id":"m1",
              "scope":{"job_id":"request:e1","recipient":"dsh","binding_digest":profile.bindings["dsh"].digest}}
    monkeypatch.setattr(ack,"reply_request",lambda *args:(packet,{"ok":True,"data":receipt,"error":None}))
    runtime_calls = []
    def transport(**kw):
        class Transport:
            def call_tool(self,*args):
                if failure == "transport_error": raise RuntimeError("private failure")
                if failure == "channel_unknown":
                    return {"ok":False,"data":{"status":"unknown","delivery_id":"rd1"},"error":{"code":"delivery_uncertain"}}
                return {"ok":True,"data":receipt,"error":None}
        return Transport()
    def runtime(*args):
        runtime_calls.append(args)
        return {"ok":False,"data":None,"error":{"code":"upstream_unavailable"}}
    monkeypatch.setattr(cli,"ChannelMCPTransport",transport)
    monkeypatch.setattr(cli,"_call_runtime",runtime)
    assert cli.main(["reply","--request-file","/fake/packet","--text","reply","--wake-source"]) == 1
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["ok"] is False
    assert result["data"]["reply_acknowledged"] is True
    assert result["data"]["reverse_status"] == "unknown"
    assert result["data"]["reverse_send_key"] == "desktop-reply-v1:request:e1"
    assert "job_id" not in result["data"] and "private failure" not in output
    assert len(runtime_calls) == (1 if failure == "runtime_failed" else 0)


def test_lost_lease_while_sdk_inflight_cannot_settle_ack(profile_files):
    import threading
    module = ack_module()
    profile = load_profile(profile_files[0])
    started, release = threading.Event(), threading.Event()
    sent = []
    def send(*args):
        sent.append(args)
        started.set()
        release.wait(2)
        return {"delivery_id":"r1","message_id":"m2","status":"sent","replayed":False, **identity_fields("dsh")}
    async def run():
        lost = asyncio.Event()
        with tempfile.TemporaryDirectory(prefix="mn-", dir="/private/tmp") as root:
            async with module.ActiveReply(scope={"job_id":"j1","recipient":"dsh"}, profile=profile,
                profile_path=profile_files[0], source_alias="codex", incoming_message_id="m1", runtime_dir=root,
                send=send, lease_lost=lost) as endpoint:
                packet = module.read_request(endpoint.request_file)
                request = {"version":2,"scope":packet["scope"],"capability":packet["capability"],"text":"reply"}
                pending = asyncio.create_task(endpoint.handle(request))
                assert await asyncio.to_thread(started.wait, 2)
                lost.set()
                release.set()
                assert not (await pending)["ok"] and not endpoint.acknowledged.is_set()
                assert not (await endpoint.handle(request))["ok"]
                assert len(sent) == 1
    asyncio.run(run())


def test_endpoint_cleanup_preserves_unrelated_file(profile_files):
    module = ack_module()
    profile = load_profile(profile_files[0])
    async def run():
        with tempfile.TemporaryDirectory(prefix="mn-", dir="/private/tmp") as root:
            async with module.ActiveReply(scope={"job_id":"j1"}, profile=profile, profile_path=profile_files[0],
                source_alias="codex", incoming_message_id="m1", runtime_dir=root, lease_lost=asyncio.Event()) as endpoint:
                extra = endpoint.directory / "unrelated"
                extra.write_text("retain")
            assert extra.read_text() == "retain"
            assert not endpoint.request_file.exists()
    asyncio.run(run())


@pytest.mark.parametrize("mutation", [
    lambda r: r.pop("sender_identity"), lambda r: r.update(actual_author_bot_id="999"),
    lambda r: r.update(binding_current=False), lambda r: r.update(identity_status="legacy_unattributed"),
    lambda r: r["sender_identity"].update(agent_id="desktop-dsh"),
])
def test_ack_requires_current_own_sender_and_actual_author(profile_files, mutation):
    profile = load_profile(profile_files[0])
    receipt = {"delivery_id":"d1", "message_id":"m1", "status":"sent", "replayed":False, **identity_fields("dsh")}
    mutation(receipt)
    async def run():
        with tempfile.TemporaryDirectory(prefix="mn-", dir="/private/tmp") as root:
            async with ack_module().ActiveReply(scope={"job_id":"j1", "recipient":"dsh"}, profile=profile,
                profile_path=profile_files[0], source_alias="codex", incoming_message_id="m1", runtime_dir=root,
                send=lambda *a: receipt, lease_lost=asyncio.Event()) as endpoint:
                request = {"version":2,"capability":endpoint.capability,"scope":endpoint.scope,"text":"reply"}
                result = await endpoint.handle(request)
                assert not result["ok"] and not endpoint.acknowledged.is_set()
    asyncio.run(run())


def test_legacy_ack_rejected_before_send(profile_files):
    profile = load_profile(profile_files[0])
    async def run():
        with tempfile.TemporaryDirectory(prefix="mn-", dir="/private/tmp") as root:
            async with ack_module().ActiveReply(scope={"job_id":"j1", "recipient":"dsh"}, profile=profile,
                profile_path=profile_files[0], source_alias="codex", incoming_message_id="m1", runtime_dir=root,
                send=lambda *a: pytest.fail("legacy ACK sent"), lease_lost=asyncio.Event()) as endpoint:
                result = await endpoint.handle({"version":1,"capability":endpoint.capability,"scope":endpoint.scope,"text":"reply"})
                assert not result["ok"]
                packet = json.loads(endpoint.request_file.read_text()); packet["version"] = 1
                from test_desktop_bindings import private_json
                private_json(endpoint.request_file, packet)
                with pytest.raises(ValueError): ack_module().reply_request(endpoint.request_file, "reply")
    asyncio.run(run())
