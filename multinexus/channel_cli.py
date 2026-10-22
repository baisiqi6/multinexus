"""Read, send, or wait through Coordinate's existing scoped Remote MCP."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from .channel_mcp_transport import ChannelMCPTransport, ChannelTransportError


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's default diagnostic repeats argv values, possibly secrets.
        raise ChannelTransportError("invalid_input", "Invalid command arguments. Run with --help for usage.")


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("read", "send", "wait"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--workspace", required=True)
        command.add_argument("--channel", required=True)
        command.add_argument("--platform", default="discord")
        command.add_argument("--url", default=os.environ.get("MULTINEXUS_CHANNEL_MCP_URL"))
        command.add_argument("--token-env", default="MULTINEXUS_CHANNEL_MCP_TOKEN")
        if name in {"read", "wait"}:
            command.add_argument("--after", required=name == "wait")
            command.add_argument("--limit", type=int, default=20)
        if name == "wait":
            command.add_argument("--timeout", type=int, default=20)
        if name == "send":
            source = command.add_mutually_exclusive_group(required=True)
            source.add_argument("--text")
            source.add_argument("--text-file")
            command.add_argument("--send-key", required=True)
            command.add_argument("--reply-to-message-id")
    return parser


def _arguments(args: argparse.Namespace) -> dict[str, Any]:
    values = {"workspace_id": args.workspace, "channel_id": args.channel, "platform": args.platform}
    if not args.workspace.strip() or not args.channel.strip() or not args.platform.strip() or not args.url:
        raise ChannelTransportError("invalid_input", "Workspace, channel, platform and MCP endpoint are required.")
    if args.command in {"read", "wait"}:
        if not 1 <= args.limit <= 100 or (args.after is not None and not args.after.strip()):
            raise ChannelTransportError("invalid_input", "Limit must be 1-100 and cursor must be nonempty.")
        values["limit"] = args.limit
        if args.after is not None:
            values["after_cursor"] = args.after
    if args.command == "wait":
        if not 0 <= args.timeout <= 30:
            raise ChannelTransportError("invalid_input", "Wait timeout must be 0-30 seconds.")
        values["timeout_seconds"] = args.timeout
    if args.command == "send":
        values.update(_send_arguments(args))
    return values


def _send_arguments(args: argparse.Namespace) -> dict[str, Any]:
    try:
        text = Path(args.text_file).read_bytes().decode("utf-8") if args.text_file is not None else args.text
        units = len(text.encode("utf-16-le")) // 2
    except (OSError, UnicodeError, ValueError):
        raise ChannelTransportError("invalid_input", "Message text must be readable UTF-8.") from None
    if not text.strip() or units > 1800:
        raise ChannelTransportError("invalid_input", "Message text must be nonblank and at most 1800 UTF-16 units.")
    if not args.send_key.isascii() or not 1 <= len(args.send_key) <= 128:
        raise ChannelTransportError("invalid_input", "Send key must contain 1-128 ASCII characters.")
    values = {"text": text, "send_key": args.send_key}
    if args.reply_to_message_id is not None:
        values["reply_to_message_id"] = args.reply_to_message_id
    return values


def main(argv: list[str] | None = None) -> int:
    args = None
    try:
        args = _parser().parse_args(argv)
        values = _arguments(args)
        transport = ChannelMCPTransport(url=args.url, token_env=args.token_env)
        envelope = transport.call_tool(f"coordinate.channel_{args.command}", values)
        code = 0 if envelope["ok"] else 1
    except ChannelTransportError as exc:
        envelope = exc.envelope()
        code = 2 if exc.code == "invalid_input" else 1
    except KeyboardInterrupt:
        if args is not None and args.command == "send":
            envelope = {"ok": False, "data": {"status": "unknown"}, "error": {
                "code": "delivery_uncertain",
                "message": "Delivery outcome is unknown; reuse the same send_key to inspect it.",
            }}
        else:
            envelope = {"ok": False, "data": None, "error": {"code": "upstream_unavailable", "message": "Channel call cancelled."}}
        code = 130
    except SystemExit as exc:
        return int(exc.code or 0)  # --help is the usual argparse text output
    except Exception:
        envelope = {"ok": False, "data": None, "error": {"code": "upstream_unavailable", "message": "Channel call failed."}}
        code = 1
    print(json.dumps(envelope, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
