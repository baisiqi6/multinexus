"""Channel CLI wire contract; all external calls use an isolated test double."""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest


PAGE = {
    "ok": True,
    "data": {
        "messages": [], "next_cursor": "opaque-cursor", "has_more": False,
        "source": "discord_rest", "coverage": "after_cursor",
    },
    "error": None,
}


@pytest.fixture
def cli(monkeypatch):
    module = importlib.import_module("multinexus.channel_cli")
    calls = []
    state = {"result": PAGE, "exception": None}

    def construct(*, url, token_env):
        calls.append(("connect", url, token_env))

        def call_tool(name, arguments):
            calls.append((name, arguments))
            if state["exception"] is not None:
                raise state["exception"]
            return state["result"]

        return SimpleNamespace(call_tool=call_tool)

    monkeypatch.setattr(module, "ChannelMCPTransport", construct)
    monkeypatch.setenv("MULTINEXUS_CHANNEL_MCP_URL", "https://test.invalid/mcp")
    return module, calls, state


def test_read_maps_only_channel_fields_and_preserves_envelope(cli, capsys):
    module, calls, _ = cli
    assert module.main([
        "read", "--workspace", "project-a", "--channel", "123456789012345678",
        "--after", "opaque-cursor", "--limit", "7",
    ]) == 0
    assert calls == [
        ("connect", "https://test.invalid/mcp", "MULTINEXUS_CHANNEL_MCP_TOKEN"),
        ("coordinate.channel_read", {
            "workspace_id": "project-a", "channel_id": "123456789012345678",
            "platform": "discord", "after_cursor": "opaque-cursor", "limit": 7,
        }),
    ]
    output = capsys.readouterr()
    assert json.loads(output.out) == PAGE
    assert len(output.out.splitlines()) == 1
    assert output.err == ""


def test_initial_read_omits_cursor_and_uses_default_limit(cli, capsys):
    module, calls, _ = cli
    assert module.main(["read", "--workspace", "a", "--channel", "123"]) == 0
    assert calls[-1] == ("coordinate.channel_read", {
        "workspace_id": "a", "channel_id": "123", "platform": "discord", "limit": 20,
    })
    assert json.loads(capsys.readouterr().out) == PAGE


def test_send_preserves_utf8_file_whitespace_and_reply(cli, capsys, tmp_path):
    module, calls, state = cli
    text_file = tmp_path / "message.txt"
    text_file.write_text("  中文 😀\n[handoff] @Codex\n", encoding="utf-8")
    state["result"] = {"ok": True, "data": {
        "delivery_id": "d1", "message_id": "999", "status": "sent", "replayed": False,
    }, "error": None}
    assert module.main([
        "send", "--workspace", "a", "--channel", "123", "--text-file", str(text_file),
        "--send-key", "batch-1", "--reply-to-message-id", "456",
    ]) == 0
    assert calls[-1] == ("coordinate.channel_send", {
        "workspace_id": "a", "channel_id": "123", "platform": "discord",
        "text": "  中文 😀\n[handoff] @Codex\n", "send_key": "batch-1",
        "reply_to_message_id": "456",
    })
    assert json.loads(capsys.readouterr().out) == state["result"]


def test_text_file_preserves_crlf_for_send_fingerprint(cli, capsys, tmp_path):
    module, calls, _ = cli
    text_file = tmp_path / "windows-message.txt"
    text_file.write_bytes(b"  first\r\nsecond\r\n")
    assert module.main([
        "send", "--workspace", "a", "--channel", "123", "--send-key", "same-key",
        "--text-file", str(text_file),
    ]) == 0
    assert calls[-1][1]["text"] == "  first\r\nsecond\r\n"
    capsys.readouterr()


def test_wait_zero_timeout_is_sent_as_nonblocking_check(cli, capsys):
    module, calls, state = cli
    state["result"] = {"ok": True, "data": {**PAGE["data"], "status": "timeout"}, "error": None}
    assert module.main([
        "wait", "--workspace", "a", "--channel", "123", "--after", "cursor",
        "--timeout", "0", "--url", "https://other.invalid/mcp", "--token-env", "TEST_TOKEN",
    ]) == 0
    assert calls == [
        ("connect", "https://other.invalid/mcp", "TEST_TOKEN"),
        ("coordinate.channel_wait", {
            "workspace_id": "a", "channel_id": "123", "platform": "discord",
            "after_cursor": "cursor", "limit": 20, "timeout_seconds": 0,
        }),
    ]
    assert json.loads(capsys.readouterr().out) == state["result"]


@pytest.mark.parametrize("extra", [
    ["--limit", "0"], ["--limit", "101"], ["--limit", "1.5"],
    ["--session-id", "secret-session"], ["--model", "some-model"],
    ["--token", "secret-plain-token"],
])
def test_invalid_arguments_fail_before_transport_and_do_not_echo_values(cli, capsys, extra):
    module, calls, _ = cli
    assert module.main(["read", "--workspace", "a", "--channel", "123", *extra]) == 2
    output = capsys.readouterr()
    assert calls == []
    assert json.loads(output.out)["error"]["code"] == "invalid_input"
    assert "secret" not in output.out + output.err


@pytest.mark.parametrize("args", [
    ["wait", "--workspace", "a", "--channel", "123"],
    ["wait", "--workspace", "a", "--channel", "123", "--after", "c", "--timeout", "31"],
    ["send", "--workspace", "a", "--channel", "123", "--text", "hello"],
    ["send", "--workspace", "a", "--channel", "123", "--text", " ", "--send-key", "k"],
    ["send", "--workspace", "a", "--channel", "123", "--text", "😀" * 901, "--send-key", "k"],
    ["send", "--workspace", "a", "--channel", "123", "--text", "hi", "--send-key", "中文"],
])
def test_missing_and_out_of_range_inputs_are_parameter_errors(cli, capsys, args):
    module, calls, _ = cli
    assert module.main(args) == 2
    assert calls == []
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "invalid_input"


def test_delivery_unknown_is_failure_with_original_durable_receipt(cli, capsys):
    module, _, state = cli
    state["result"] = {"ok": False, "data": {"delivery_id": "d1", "status": "unknown"},
                       "error": {"code": "delivery_uncertain", "message": "Reuse the same send_key."}}
    assert module.main([
        "send", "--workspace", "a", "--channel", "123", "--text", "hello", "--send-key", "same-key",
    ]) == 1
    assert json.loads(capsys.readouterr().out) == state["result"]


def test_user_cancellation_has_130_exit_code(cli, capsys):
    module, _, state = cli
    state["exception"] = KeyboardInterrupt()
    assert module.main(["read", "--workspace", "a", "--channel", "123"]) == 130
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_cancelled_send_is_unknown_and_keeps_130_exit_code(cli, capsys):
    module, _, state = cli
    state["exception"] = KeyboardInterrupt()
    assert module.main([
        "send", "--workspace", "a", "--channel", "123", "--text", "hello", "--send-key", "same-key",
    ]) == 130
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["data"] == {"status": "unknown"}
    assert envelope["error"]["code"] == "delivery_uncertain"


def test_unreadable_text_file_is_safe_parameter_error(cli, capsys, tmp_path):
    module, calls, _ = cli
    assert module.main([
        "send", "--workspace", "a", "--channel", "123", "--send-key", "k",
        "--text-file", str(tmp_path / "missing-secret-file.txt"),
    ]) == 2
    output = capsys.readouterr()
    assert calls == []
    assert "missing-secret" not in output.out + output.err
