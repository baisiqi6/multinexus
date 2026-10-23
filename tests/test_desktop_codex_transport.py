"""The only native mutation is App-bundled queue with exact original thread."""
import asyncio
import importlib
from types import SimpleNamespace

import pytest
import sys

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="native desktop acceptance is macOS only")
from test_desktop_bindings import profile_files
from multinexus.desktop.bindings import load_profile


def transport_module():
    try:
        return importlib.import_module("multinexus.desktop.codex_transport")
    except ModuleNotFoundError:
        pytest.fail("Native Codex queue transport is not implemented")


def test_queue_exact_original_thread_and_no_model_or_policy_overrides(profile_files, monkeypatch):
    module = transport_module()
    binding = load_profile(profile_files[0]).bindings["codex"]
    calls = []
    async def spawn(*argv, **kw):
        calls.append((argv, kw))
        async def communicate(): return b"queued private output", b"private stderr"
        return SimpleNamespace(communicate=communicate, returncode=0)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    transport = module.CodexNativeTransport("/fake/Codex.app/bin/codex", binding)
    result = asyncio.run(transport.queue({"native_request_id":"n1"}, "agent data"))
    assert calls[0][0] == ("/fake/Codex.app/bin/codex", "queue", "--thread", binding.session_id, "--message", "agent data")
    assert "cwd" not in calls[0][1]
    assert result == {"state":"queued", "native_request_id":"n1"}
    assert "private" not in str(result)


def test_inspect_not_loaded_is_only_readonly_metadata(profile_files, monkeypatch):
    module = transport_module()
    binding = load_profile(profile_files[0]).bindings["codex"]
    transport = module.CodexNativeTransport("/fake/codex", binding)
    calls = []
    async def read():
        calls.append("read")
        return {"id":binding.session_id, "cwd":binding.cwd, "status":{"type":"notLoaded"}}
    monkeypatch.setattr(transport, "_read_thread", read)
    result = asyncio.run(transport.inspect(binding))
    assert result["ready"] is True and calls == ["read"]
    assert "awake" not in result and "receiver_acknowledged" not in str(result)
    async def wrong(): return {"id":binding.session_id, "cwd":"/other"}
    monkeypatch.setattr(transport, "_read_thread", wrong)
    with pytest.raises(ValueError): asyncio.run(transport.inspect(binding))


def test_readonly_stdio_uses_only_initialize_and_thread_read(tmp_path, profile_files):
    import json
    import sys
    module = transport_module()
    binding = load_profile(profile_files[0]).bindings["codex"]
    log = tmp_path / "calls.jsonl"
    script = tmp_path / "fake-codex"
    script.write_text(f'''#!{sys.executable}
import sys, json
with open({str(log)!r}, "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
for line in sys.stdin:
    value = json.loads(line)
    with open({str(log)!r}, "a") as stream:
        stream.write(json.dumps(value) + "\\n")
    if value.get("method") == "initialize":
        print(json.dumps({{"id":1,"result":{{}}}}), flush=True)
    if value.get("method") == "thread/read":
        print(json.dumps({{"id":2,"result":{{"thread":{{"id":{binding.session_id!r},"cwd":{binding.cwd!r},"status":{{"type":"notLoaded"}}}}}}}}), flush=True)
''')
    script.chmod(0o700)
    result = asyncio.run(module.CodexNativeTransport(str(script), binding).inspect(binding))
    frames = [json.loads(line) for line in log.read_text().splitlines()]
    assert frames[0] == ["app-server", "--listen", "stdio://"]
    assert [frame["method"] for frame in frames[1:]] == ["initialize", "initialized", "thread/read"]
    assert frames[-1]["params"] == {"threadId":binding.session_id,"includeTurns":False}
    assert result["ready"] is True and "state" not in result


def test_startup_rejects_binary_without_queue_capability(profile_files, monkeypatch):
    module = transport_module()
    binding = load_profile(profile_files[0]).bindings["codex"]
    async def spawn(*argv, **kw):
        async def communicate(): return b"no supported options", b""
        return SimpleNamespace(communicate=communicate, returncode=0)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    transport = module.CodexNativeTransport("/fake/codex", binding)
    assert hasattr(transport, "startup_check"), "native queue capability must be probed"
    with pytest.raises(ValueError): asyncio.run(transport.startup_check())


def test_cancel_kills_only_queue_transport(profile_files, monkeypatch):
    module = transport_module()
    binding = load_profile(profile_files[0]).bindings["codex"]
    events = []
    class Process:
        returncode = None
        async def communicate(self):
            events.append("waiting")
            await asyncio.Event().wait()
        def terminate(self):
            events.append("terminate-transport")
            self.returncode = -15
        async def wait(self): return self.returncode
    async def spawn(*argv, **kw):
        events.append(argv[1])
        return Process()
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    async def run():
        task = asyncio.create_task(module.CodexNativeTransport("/fake/codex", binding).queue({"native_request_id":"n1"}, "body"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(run())
    assert events == ["queue", "waiting", "terminate-transport"]


@pytest.mark.parametrize("operation", ["startup_check", "_read_thread", "queue"])
def test_every_native_spawn_has_minimal_path_environment(profile_files, monkeypatch, operation):
    module = transport_module()
    for key in ("DISCORD_TOKEN", "PEER_BOT_SECRET", "OPENAI_API_KEY", "MULTINEXUS_DESKTOP_CHANNEL_TOKEN",
                "MULTINEXUS_DESKTOP_LIFECYCLE_TOKEN", "MULTINEXUS_COORDINATE_HTTP_TOKEN", "CODEX_MODEL", "CODEX_SANDBOX"):
        monkeypatch.setenv(key, "secret-marker")
    monkeypatch.setenv("CODEX_HOME", "/fake/app-config")
    calls = []
    async def spawn(*argv, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("fake stop before process")
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    transport = module.CodexNativeTransport("/fake/codex", load_profile(profile_files[0]).bindings["codex"])
    with pytest.raises(RuntimeError):
        asyncio.run(getattr(transport, operation)(*([{"native_request_id":"n"}, "body"] if operation == "queue" else [])))
    assert calls[0].get("env") is not None
    assert "secret-marker" not in calls[0]["env"].values()
    assert calls[0]["env"]["CODEX_HOME"] == "/fake/app-config"
    assert "HOME" in calls[0]["env"]
