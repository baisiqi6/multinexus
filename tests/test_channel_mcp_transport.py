"""Channel-only MCP boundary; no real credentials or external platform calls."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest


@pytest.fixture
def transport():
    return importlib.import_module("multinexus.channel_mcp_transport")


@pytest.mark.parametrize("url", [
    "http://example.com/mcp", "ftp://localhost/mcp", "https:///mcp",
    "https://user:secret@example.com/mcp", "https://example.com:bad/mcp",
    "https://example.com/mcp?token=secret", "https://example.com/mcp#secret",
])
def test_invalid_endpoints_fail_before_token_access(transport, monkeypatch, url):
    monkeypatch.setattr(transport.os.environ, "get", lambda *a: pytest.fail("read token"))
    with pytest.raises(transport.ChannelTransportError) as caught:
        transport.ChannelMCPTransport(url=url, token_env="TEST_TOKEN")
    assert caught.value.code == "invalid_input"
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("url", [
    "https://example.com/mcp", "http://localhost:8888/mcp", "http://127.0.0.2/mcp",
    "http://[::1]:8888/mcp",
])
def test_https_and_loopback_endpoints_are_accepted(transport, url):
    assert transport.ChannelMCPTransport(url=url, token_env="TEST_TOKEN") is not None


@pytest.mark.parametrize("tool, arguments", [
    ("coordinate.job_submit", {"workspace_id": "a", "channel_id": "123"}),
    ("coordinate.channel_read", {"workspace_id": "a", "channel_id": "123", "session_id": "bad"}),
    ("coordinate.channel_send", {"workspace_id": "a", "channel_id": "123", "token": "bad"}),
])
def test_disallowed_tools_and_fields_fail_before_token_access(transport, monkeypatch, tool, arguments):
    client = transport.ChannelMCPTransport(url="https://test.invalid/mcp", token_env="TEST_TOKEN")
    monkeypatch.setattr(transport.os.environ, "get", lambda *a: pytest.fail("read token"))
    with pytest.raises(transport.ChannelTransportError) as caught:
        client.call_tool(tool, arguments)
    assert caught.value.code == "invalid_input"


def test_missing_token_has_static_error(transport, monkeypatch):
    monkeypatch.delenv("TEST_TOKEN", raising=False)
    client = transport.ChannelMCPTransport(url="https://test.invalid/mcp", token_env="TEST_TOKEN")
    with pytest.raises(transport.ChannelTransportError) as caught:
        client.call_tool("coordinate.channel_read", {"workspace_id": "a", "channel_id": "123"})
    assert caught.value.code == "invalid_input"


def test_optional_sdk_import_failure_does_not_attempt_send(transport, monkeypatch):
    import builtins

    original_import = builtins.__import__

    def without_sdk(name, *args, **kwargs):
        if name == "mcp":
            raise ImportError("isolated optional dependency test")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_sdk)
    monkeypatch.setenv("TEST_TOKEN", "isolated-test-token")
    client = transport.ChannelMCPTransport(url="https://test.invalid/mcp", token_env="TEST_TOKEN")
    with pytest.raises(transport.ChannelTransportError) as caught:
        client.call_tool("coordinate.channel_send", {
            "workspace_id": "a", "channel_id": "123", "text": "hello", "send_key": "same-key",
        })
    assert caught.value.code == "upstream_unavailable"
    assert caught.value.data is None
    assert "isolated-test-token" not in str(caught.value)


def test_structured_and_text_envelopes_have_identical_results(transport):
    envelope = {"ok": True, "data": {"next_cursor": "c", "messages": []}, "error": None}
    assert transport._translate_result(SimpleNamespace(structured_content=envelope, content=[]), "secret") == envelope
    result = SimpleNamespace(structured_content=None, content=[SimpleNamespace(text=json.dumps(envelope))])
    assert transport._translate_result(result, "secret") == envelope


@pytest.mark.parametrize("envelope", [
    {"ok": "true", "data": {}, "error": None},
    {"ok": True, "data": None, "error": None},
    {"ok": False, "data": None, "error": None},
    {"ok": True, "data": {"echo": "test-secret-value"}, "error": None},
    {"ok": False, "data": None, "error": {"code": "forbidden", "message": "test-secret-value"}},
    {"ok": False, "data": None, "error": {"code": [], "message": "bad error code"}},
])
def test_invalid_or_credential_echoing_envelope_is_rejected(transport, envelope):
    with pytest.raises(transport.ChannelTransportError) as caught:
        transport._translate_result(SimpleNamespace(structured_content=envelope), "test-secret-value")
    assert caught.value.code == "upstream_unavailable"
    assert "test-secret-value" not in str(caught.value)


def test_json_escaped_credentials_are_rejected(transport):
    secret = 'test"secret\\value'
    envelope = {"ok": True, "data": {"text": f"echo: {secret}"}, "error": None}
    with pytest.raises(transport.ChannelTransportError):
        transport._translate_result(SimpleNamespace(structured_content=envelope), secret)


def test_mcp_error_marker_cannot_become_success(transport):
    envelope = {"ok": True, "data": {}, "error": None}
    with pytest.raises(transport.ChannelTransportError):
        transport._translate_result(SimpleNamespace(structured_content=envelope, is_error=True), "secret")


def test_uncertain_send_keeps_durable_receipt(transport):
    envelope = {"ok": False, "data": {"delivery_id": "d1", "status": "unknown"}, "error": {
        "code": "delivery_uncertain", "message": "Reuse the same send_key to inspect it.",
    }}
    assert transport._translate_result(SimpleNamespace(structured_content=envelope), "test-secret") == envelope


def test_read_timeout_is_bounded_and_does_not_echo_exception(transport, monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "test-secret")
    monkeypatch.setattr(transport, "CLIENT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(transport, "_load_sdk", lambda: None)

    async def hang(*args):
        await asyncio.sleep(1)

    monkeypatch.setattr(transport, "_call_remote", hang)
    client = transport.ChannelMCPTransport(url="https://test.invalid/mcp", token_env="TEST_TOKEN")
    with pytest.raises(transport.ChannelTransportError) as caught:
        client.call_tool("coordinate.channel_read", {"workspace_id": "a", "channel_id": "123"})
    assert caught.value.code == "upstream_unavailable"


def test_send_transport_loss_is_unknown_and_never_retries(transport, monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", "test-secret")
    monkeypatch.setattr(transport, "_load_sdk", lambda: None)
    calls = []

    async def disconnect(*args):
        calls.append(args)
        raise ConnectionError("test-secret https://sensitive.invalid/")

    monkeypatch.setattr(transport, "_call_remote", disconnect)
    client = transport.ChannelMCPTransport(url="https://test.invalid/mcp", token_env="TEST_TOKEN")
    with pytest.raises(transport.ChannelTransportError) as caught:
        client.call_tool("coordinate.channel_send", {
            "workspace_id": "a", "channel_id": "123", "text": "hello", "send_key": "same-key",
        })
    assert len(calls) == 1
    assert caught.value.code == "delivery_uncertain"
    assert caught.value.data == {"status": "unknown"}
    assert "test-secret" not in str(caught.value)


def test_real_sdk_and_cli_match_all_three_tools_on_loopback(monkeypatch, capsys):
    """Exercise the real optional SDK, HTTP framing, auth and CLI end to end."""
    pytest.importorskip("mcp")
    uvicorn = pytest.importorskip("uvicorn")
    from mcp.server.mcpserver import MCPServer
    from multinexus.channel_cli import main
    from multinexus.channel_mcp_transport import ChannelMCPTransport

    server = MCPServer("isolated-channel-test", log_level="ERROR")
    calls = []
    envelopes = {
        "read": {"ok": True, "data": {
            "messages": [], "next_cursor": "same-cursor", "has_more": False,
            "source": "discord_rest", "coverage": "after_cursor",
        }, "error": None},
        "send": {"ok": False, "data": {"delivery_id": "test-durable-id", "status": "unknown"}, "error": {
            "code": "delivery_uncertain", "message": "Reuse the same send_key to inspect it.",
        }},
        "wait": {"ok": True, "data": {
            "status": "timeout", "messages": [], "next_cursor": "same-cursor", "has_more": False,
            "source": "discord_rest", "coverage": "after_cursor",
        }, "error": None},
    }

    def register(operation):
        @server.tool(name=f"coordinate.channel_{operation}")
        async def operation_tool(input: dict) -> dict:
            calls.append((operation, input))
            if operation == "wait" and input.get("timeout_seconds") == 30:
                await asyncio.sleep(1)
            return envelopes[operation]

    for operation in envelopes:
        register(operation)
    app = server.streamable_http_app(stateless_http=True, json_response=True)
    seen_auth = []

    async def authenticated(scope, receive, send):
        if scope["type"] == "http":
            authorization = dict(scope["headers"]).get(b"authorization")
            seen_auth.append(authorization)
            assert authorization == b"Bearer isolated-test-credential"
        await app(scope, receive, send)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
        runner = uvicorn.Server(uvicorn.Config(authenticated, log_level="error", access_log=False))
        thread = threading.Thread(target=runner.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not runner.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runner.started
        monkeypatch.setenv("TEST_CHANNEL_TOKEN", "isolated-test-credential")
        try:
            client = ChannelMCPTransport(url=url, token_env="TEST_CHANNEL_TOKEN")
            common = {"workspace_id": "a", "channel_id": "123", "platform": "discord"}
            for operation, extra, flags, exit_code in [
                ("read", {"after_cursor": "same-cursor", "limit": 20}, ["--after", "same-cursor"], 0),
                ("send", {"text": "hello 😀", "send_key": "stable-key"}, ["--text", "hello 😀", "--send-key", "stable-key"], 1),
                ("wait", {"after_cursor": "same-cursor", "limit": 20, "timeout_seconds": 0}, ["--after", "same-cursor", "--timeout", "0"], 0),
            ]:
                direct = client.call_tool(f"coordinate.channel_{operation}", {**common, **extra})
                assert direct == envelopes[operation]
                assert main([
                    operation, "--workspace", "a", "--channel", "123", "--url", url,
                    "--token-env", "TEST_CHANNEL_TOKEN", *flags,
                ]) == exit_code
                output = capsys.readouterr()
                assert json.loads(output.out) == direct
                assert "isolated-test-credential" not in output.out + output.err
                assert calls[-2:] == [(operation, {**common, **extra})] * 2
            assert seen_auth
            module = importlib.import_module("multinexus.channel_mcp_transport")
            monkeypatch.setattr(module, "CLIENT_TIMEOUT_SECONDS", 0.05)
            started = time.monotonic()
            with pytest.raises(module.ChannelTransportError) as caught:
                client.call_tool("coordinate.channel_wait", {
                    **common, "after_cursor": "same-cursor", "limit": 20, "timeout_seconds": 30,
                })
            assert caught.value.code == "upstream_unavailable"
            assert time.monotonic() - started < 0.75
            if os.name == "posix":
                before = len(calls)
                process = subprocess.Popen([
                    sys.executable, "-m", "multinexus.channel_cli", "wait",
                    "--workspace", "a", "--channel", "123", "--after", "same-cursor",
                    "--timeout", "30", "--url", url, "--token-env", "TEST_CHANNEL_TOKEN",
                ], cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    deadline = time.monotonic() + 5
                    while len(calls) == before and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.01)
                    assert len(calls) > before
                    process.send_signal(signal.SIGINT)
                    stdout, stderr = process.communicate(timeout=5)
                    assert process.returncode == 130
                    assert json.loads(stdout)["ok"] is False
                    assert "isolated-test-credential" not in stdout + stderr
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)
        finally:
            runner.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()
