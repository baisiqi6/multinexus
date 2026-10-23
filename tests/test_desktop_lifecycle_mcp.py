"""Lifecycle transport has an independent, flat, two-tool SDK boundary."""
import asyncio
import importlib
from types import SimpleNamespace

import pytest


@pytest.fixture
def module():
    return importlib.import_module("multinexus.desktop.lifecycle_mcp")


@pytest.mark.parametrize("name,args", [
    ("coordinate.channel_send", {}), ("coordinate.runtime_job_get", {"input": {"job_id": "j1"}}),
    ("coordinate.runtime_job_get", {"job_id": "j1", "workspace_id": "w"}),
    ("coordinate.runtime_request_submit", {"command": "secret"}),
])
def test_only_flat_runtime_tools_before_token_access(module, monkeypatch, name, args):
    client = module.LifecycleMCPTransport(url="https://test.invalid/mcp", token_env="TEST_TOKEN")
    monkeypatch.setattr(module.os.environ, "get", lambda *a: pytest.fail("token read"))
    with pytest.raises(ValueError):
        client.call_tool(name, args)


@pytest.mark.parametrize("url", ["http://remote.invalid/mcp", "https://user:secret@host/mcp", "https://host/mcp?secret=x"])
def test_bad_endpoint_is_safe_before_credentials(module, monkeypatch, url):
    monkeypatch.setattr(module.os.environ, "get", lambda *a: pytest.fail("token read"))
    with pytest.raises(ValueError) as caught:
        module.LifecycleMCPTransport(url=url, token_env="TEST_TOKEN")
    assert "secret" not in str(caught.value)


def test_sdk_initializes_and_receives_flat_arguments_with_redirects_off(module):
    calls = []
    class Manager:
        def __init__(self, value): self.value = value
        async def __aenter__(self): return self.value
        async def __aexit__(self, *args): return None
    class Session:
        async def initialize(self): calls.append("initialize")
        async def call_tool(self, name, arguments, **kwargs):
            calls.append((name, arguments))
            return "result"
    def http_client(**kwargs):
        assert kwargs["follow_redirects"] is False
        assert kwargs["headers"] == {"Authorization": "Bearer isolated-test-token"}
        return Manager("http")
    sdk = (SimpleNamespace(AsyncClient=http_client), lambda *a, **k: Manager(Session()),
           lambda *a, **k: Manager(("reader", "writer")))
    result = asyncio.run(module._call_remote("https://test.invalid/mcp", "isolated-test-token",
        "coordinate.runtime_job_get", {"job_id": "j1"}, sdk))
    assert result == "result"
    assert calls == ["initialize", ("coordinate.runtime_job_get", {"job_id": "j1"})]


@pytest.mark.parametrize("envelope", [
    {"ok": "true", "data": {}, "error": None}, {"ok": True, "data": None, "error": None},
    {"ok": True, "data": {"echo": "fake-secret"}, "error": None},
    {"ok": False, "data": None, "error": {"code": "bad", "message": "fake-secret"}},
])
def test_malformed_and_credential_echoing_results_are_static_errors(module, envelope):
    with pytest.raises(ValueError) as caught:
        module._translate_result(SimpleNamespace(structured_content=envelope), "fake-secret")
    assert "fake-secret" not in str(caught.value)


def test_submit_disconnect_is_one_attempt_with_static_failure(module, monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "fake-secret")
    monkeypatch.setattr(module, "_load_sdk", lambda: None)
    calls = []
    async def disconnect(*args):
        calls.append(args)
        raise OSError("fake-secret sensitive")
    monkeypatch.setattr(module, "_call_remote", disconnect)
    client = module.LifecycleMCPTransport(url="https://test.invalid/mcp", token_env="TEST_TOKEN")
    with pytest.raises(ValueError) as caught:
        client.call_tool("coordinate.runtime_request_submit", {
            "workspace_id": "w", "target_agent": "a", "prompt": "hello", "idempotency_key": "k",
            "origin": {"platform": "none", "destination": "desktop", "session_scope_id": "s"},
            "reply": {"platform": "none", "destination": "desktop"}})
    assert len(calls) == 1
    assert "fake-secret" not in str(caught.value)
