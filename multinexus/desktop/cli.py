"""Explicit channel send plus existing-session delivery, or runtime job status."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ipaddress
import json
import os
import re
import sys
import threading
from urllib.parse import urlsplit
from pathlib import Path
from types import SimpleNamespace

# A fixed absolute script works from the original native business cwd without
# inheriting the worker's environment or changing that session's cwd.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "multinexus.desktop"

from ..channel_cli import _send_arguments
from ..channel_mcp_transport import ChannelMCPTransport, ChannelTransportError
from .bindings import credential_config, load_profile, read_private_bytes, required_string, verified_receipt
from .lifecycle_mcp import LifecycleMCPTransport, LifecycleTransportError
from .protocol import build_submission

_CHANNEL_TOKEN = "MULTINEXUS_DESKTOP_CHANNEL_TOKEN"
_LIFECYCLE_TOKEN = "MULTINEXUS_DESKTOP_LIFECYCLE_TOKEN"
_CREDENTIAL_ENV_LOCK = threading.RLock()


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("Invalid desktop command arguments. Run with --help for usage.")


def _parser():
    parser = _Parser(description=__doc__, allow_abbrev=False)
    subcommands = parser.add_subparsers(dest="command", required=True)
    send = subcommands.add_parser("send", allow_abbrev=False)
    send.add_argument("--profile", required=True)
    send.add_argument("--to", required=True)
    send.add_argument("--send-key", required=True)
    source = send.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--text-file")
    send.add_argument("--reply-to-message-id")
    reply = subcommands.add_parser("reply", allow_abbrev=False)
    reply.add_argument("--request-file", required=True)
    source = reply.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--text-file")
    reply.add_argument("--wake-source", action="store_true")
    status = subcommands.add_parser("status", allow_abbrev=False)
    status.add_argument("--profile", required=True)
    status.add_argument("--job-id", required=True)
    return parser


def _is_tailnet(host: str) -> bool:
    if host.endswith(".ts.net"):
        return True
    try:
        return ipaddress.ip_address(host) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


@contextmanager
def _scoped_environment(values: dict[str, str]):
    # The legacy SDK takes an environment variable name; serialize the whole
    # synchronous call so concurrent worker reply threads cannot swap principals.
    with _CREDENTIAL_ENV_LOCK:
        previous = {key: os.environ.get(key) for key in values}
        try:
            os.environ.update(values)
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


@contextmanager
def _credential_environment(config: dict, variable: str):
    try:
        token = read_private_bytes(config["token_file"]).decode("utf-8").rstrip("\r\n")
    except UnicodeError:
        raise ValueError("Invalid private credential file.") from None
    if not token or not token.strip() or any(ord(c) < 32 or ord(c) == 127 for c in token):
        raise ValueError("Invalid private credential file.")
    values = {variable: token}
    host = urlsplit(config["url"]).hostname
    if host and _is_tailnet(host):
        # httpx uses stdlib proxy discovery on macOS: both spellings override
        # the system setting only for this exact endpoint during this call.
        values.update(NO_PROXY=host, no_proxy=host)
    with _scoped_environment(values):
        yield


def _call_runtime(config: dict, name: str, arguments: dict) -> dict:
    with _credential_environment(config, _LIFECYCLE_TOKEN):
        return LifecycleMCPTransport(url=config["url"], token_env=_LIFECYCLE_TOKEN).call_tool(name, arguments)


def _send(profile, args, values: dict) -> dict:
    channel_config = credential_config(profile, profile.local_alias)
    lifecycle_config = credential_config(profile)
    with _credential_environment(channel_config, _CHANNEL_TOKEN):
        receipt = ChannelMCPTransport(url=channel_config["url"], token_env=_CHANNEL_TOKEN).call_tool(
            "coordinate.channel_send", {"workspace_id": profile.workspace_id, "channel_id": profile.channel_id,
                                        "platform": "discord", **values})
    if not receipt["ok"] or receipt["data"].get("status") != "sent":
        return _channel_failure(receipt)
    channel_receipt = _confirmed_receipt(profile, receipt["data"])
    submission = build_submission(profile, profile.local_alias, args.to, values["text"], receipt["data"], args.reply_to_message_id)
    result = _call_runtime(lifecycle_config, "coordinate.runtime_request_submit", submission)
    if not result["ok"]:
        return _safe_failure(result)
    data = result["data"]
    job = _job_summary(data.get("job"), profile.workspace_id, target_agent=profile.bindings[args.to].agent_id)
    if type(data.get("job_created")) is not bool:
        raise LifecycleTransportError("Invalid runtime job receipt.")
    return {"ok": True, "data": {"channel_receipt": channel_receipt, **job, "job_created": data["job_created"]}, "error": None}


def _safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", value):
        raise LifecycleTransportError("Invalid runtime identifier.")
    return value


def _job_summary(job, workspace_id, *, target_agent=None, job_id=None):
    if (not isinstance(job, dict) or job.get("workspace_id") != workspace_id
            or (target_agent is not None and job.get("assigned_agent") != target_agent)
            or (job_id is not None and job.get("id") != job_id)):
        raise LifecycleTransportError("Runtime job scope mismatch.")
    status = job.get("status")
    if not isinstance(status, str) or status not in {"pending", "running", "done", "failed", "cancelled", "timed_out"}:
        raise LifecycleTransportError("Invalid runtime job status.")
    return {"job_id": _safe_id(job.get("id")), "status": status}


def _receipt_summary(data):
    if not isinstance(data, dict) or data.get("status") != "sent" or type(data.get("replayed")) is not bool:
        raise LifecycleTransportError("Invalid channel receipt.")
    return {"delivery_id": _safe_id(data.get("delivery_id")), "message_id": _safe_id(data.get("message_id")),
            "status": "sent", "replayed": data["replayed"],
            **{k: data.get(k) for k in ("sender_identity", "actual_author_bot_id", "identity_status", "binding_current")}}


def _confirmed_receipt(profile, data):
    try:
        verified_receipt(profile, profile.local_alias, data)
        return _receipt_summary(data)
    except (ValueError, TypeError, KeyError):
        safe = {"status": "unknown"}
        if isinstance(data, dict):
            try:
                safe["delivery_id"] = _safe_id(data.get("delivery_id"))
            except ValueError:
                pass
        raise ChannelTransportError("delivery_uncertain", "Sender receipt unconfirmed; inspect the same send key.", data=safe) from None


def _safe_failure(envelope):
    error = envelope.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    allowed = {"invalid_input", "forbidden", "not_found", "idempotency_conflict", "busy", "rate_limited",
               "upstream_unavailable", "delivery_rejected", "delivery_uncertain",
               "sender_unbound", "sender_unavailable", "sender_identity_mismatch"}
    if not isinstance(code, str) or code not in allowed:
        code = "upstream_unavailable"
    return _error(code, "Desktop request failed; inspect the existing delivery or job before retrying.")


def _channel_failure(envelope):
    result = _safe_failure(envelope)
    data = envelope.get("data")
    if isinstance(data, dict) and data.get("status") in {"unknown", "rejected", "pending"}:
        safe = {"status": data["status"]}
        if data.get("delivery_id") is not None:
            safe["delivery_id"] = _safe_id(data["delivery_id"])
        result["data"] = safe
    return result


def _validate_send(profile, args):
    if args.to not in profile.bindings or profile.local_alias == args.to:
        raise ValueError("Distinct configured source and recipient aliases are required.")
    values = _send_arguments(args)
    if args.reply_to_message_id is not None:
        required_string(args.reply_to_message_id)
    return values


def _reply(args):
    from .ack import reply_request, valid_text
    text = Path(args.text_file).read_bytes().decode("utf-8") if args.text_file is not None else args.text
    packet, result = reply_request(args.request_file, valid_text(text))
    if result.get("ok") is not True:
        return _safe_failure(result)
    profile = load_profile(packet["profile_path"])
    receipt = _confirmed_receipt(profile, result["data"])
    if not args.wake_source:
        return {"ok": True, "data": receipt, "error": None}
    send_key = "desktop-reply-v1:" + packet["scope"]["job_id"]
    try:
        profile = load_profile(packet["profile_path"])
        recipient = packet["scope"]["recipient"]
        if recipient != profile.local_alias or profile.bindings[recipient].digest != packet["scope"]["binding_digest"]:
            raise ValueError("Native reply binding changed.")
        # SDK replay independently confirms delivery before reverse submission.
        reverse = SimpleNamespace(to=packet["source_alias"],
                                  reply_to_message_id=packet["incoming_message_id"])
        result = _send(profile, reverse, {"send_key": send_key, "text": text,
                                         "reply_to_message_id": packet["incoming_message_id"]})
        if result["ok"]:
            return result
    except Exception:
        pass
    # ACK and reverse are two steps. A failure in the latter cannot erase the
    # confirmed reply or falsely claim a reverse job was created.
    return {"ok": False, "data": {"reply_acknowledged": True, "channel_receipt": receipt,
            "reverse_status": "unknown", "reverse_send_key": send_key},
            "error": {"code": "delivery_uncertain", "message": "Reply acknowledged; reverse delivery is uncertain. Retry explicit desktop send with the same reply key and text."}}



def bound_main(profile_path, argv: list[str] | None = None) -> int:
    """Private native launchers fix the profile; this is not OS isolation."""
    return main(argv, _bound_profile=str(profile_path))


def main(argv: list[str] | None = None, *, _bound_profile=None) -> int:
    args = None
    try:
        values = list(sys.argv[1:] if argv is None else argv)
        if _bound_profile is not None:
            if any(value.split("=", 1)[0] in {"--profile", "--client"} for value in values):
                raise ValueError("Fixed native profile cannot be overridden.")
            if values and values[0] in {"send", "status"}:
                values.extend(["--profile", _bound_profile])
        args = _parser().parse_args(values)
        if _bound_profile is not None and args.command == "reply":
            from .ack import read_request
            if read_request(args.request_file)["profile_path"] != _bound_profile:
                raise ValueError("Fixed native reply profile mismatch.")
        profile = None if args.command == "reply" else load_profile(args.profile)
        if args.command == "reply":
            envelope = _reply(args)
        elif args.command == "send":
            values = _validate_send(profile, args)
            envelope = _send(profile, args, values)
        else:
            required_string(args.job_id)
            result = _call_runtime(credential_config(profile), "coordinate.runtime_job_get", {"job_id": args.job_id})
            envelope = ({"ok": True, "data": _job_summary(result["data"], profile.workspace_id, job_id=args.job_id), "error": None}
                        if result["ok"] else _safe_failure(result))
        code = 0 if envelope["ok"] else 1
    except ChannelTransportError as exc:
        envelope = _channel_failure(exc.envelope())
        code = 2 if exc.code == "invalid_input" else 1
    except LifecycleTransportError:
        envelope = _error("upstream_unavailable", "Lifecycle call failed; submission may be uncertain. Reuse the same send key.")
        code = 1
    except ValueError:
        envelope = _error("invalid_input", "Invalid desktop input or private configuration.")
        code = 2
    except KeyboardInterrupt:
        envelope = _error("delivery_uncertain" if args and args.command == "send" else "upstream_unavailable",
                          "Desktop call cancelled; delivery may be uncertain.")
        code = 130
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception:
        envelope = _error("upstream_unavailable", "Desktop call failed; delivery may be uncertain. Reuse the same send key.")
        code = 1
    print(json.dumps(envelope, ensure_ascii=False, allow_nan=False))
    return code


def _error(code: str, message: str) -> dict:
    return {"ok": False, "data": None, "error": {"code": code, "message": message}}


if __name__ == "__main__":
    sys.exit(main())
