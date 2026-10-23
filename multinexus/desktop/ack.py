"""Ephemeral per-request reply capability; only trusted channel send settles ACK."""
from __future__ import annotations

import asyncio
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile

from .bindings import absolute_path, read_private_json, verified_receipt
from .host_transport import MAX_FRAME, _socket_path
from .protocol import payload_digest


def send_channel_reply(profile, alias, values):
    from .cli import _CHANNEL_TOKEN, _credential_environment, _receipt_summary
    from .bindings import credential_config
    from ..channel_mcp_transport import ChannelMCPTransport
    config = credential_config(profile, alias)
    with _credential_environment(config, _CHANNEL_TOKEN):
        result = ChannelMCPTransport(url=config["url"], token_env=_CHANNEL_TOKEN).call_tool(
            "coordinate.channel_send", {"workspace_id": profile.workspace_id, "channel_id": profile.channel_id,
                                        "platform": "discord", **values})
    if result.get("ok") is not True:
        raise ValueError("Channel reply unconfirmed.")
    verified_receipt(profile, alias, result["data"])
    return _receipt_summary(result["data"])


def valid_text(text):
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-16-le")) // 2 > 1800:
        raise ValueError("Invalid reply text.")
    return text


class ActiveReply:
    def __init__(self, *, scope, profile, profile_path, source_alias, incoming_message_id,
                 runtime_dir, lease_lost, send=None, validate=None):
        self.scope = dict(scope)
        self.profile = profile
        self.profile_path = str(profile_path)
        self.source_alias = source_alias
        self.incoming_message_id = incoming_message_id
        self.lease_lost = lease_lost
        self.send = send or send_channel_reply
        self.validate = validate
        self.directory = Path(tempfile.mkdtemp(prefix="mn-", dir=absolute_path(runtime_dir)))
        self.directory_identity = self.directory.stat().st_ino
        self.file_identities = {}
        self.request_file = self.directory / "request.json"
        self.socket_path = self.directory / "ack.sock"
        self.capability = secrets.token_urlsafe(32)
        self.acknowledged = asyncio.Event()
        self.receipt = None
        self.reply_digest = None
        self.lock = asyncio.Lock()
        self.server = None
        self.active = False
        self.handlers = set()

    async def __aenter__(self):
        try:
            if len(os.fsencode(self.socket_path)) >= 104:
                raise ValueError("Native socket path too long.")
            self.server = await asyncio.start_unix_server(self._connection, path=str(self.socket_path), limit=MAX_FRAME)
            self.socket_path.chmod(0o600)
            self.file_identities[self.socket_path] = self.socket_path.lstat().st_ino
            packet = {"version": 2, "scope": self.scope, "capability": self.capability,
                      "socket_path": str(self.socket_path), "profile_path": self.profile_path,
                      "source_alias": self.source_alias, "incoming_message_id": self.incoming_message_id,
                      "python": sys.executable, "cli_path": str(Path(__file__).resolve().with_name("cli.py"))}
            descriptor = os.open(self.request_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump(packet, stream, ensure_ascii=False)
            self.file_identities[self.request_file] = self.request_file.lstat().st_ino
            self.active = True
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *args):
        self.active = False
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        # Allow an acknowledged client to receive its bounded receipt before
        # teardown; no new request can settle after active is cleared.
        if self.handlers:
            done, pending = await asyncio.wait(self.handlers, timeout=1)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        # Only remove artifacts whose inodes we created. Replacement paths and
        # unrelated files remain for manual inspection, including on shutdown.
        try:
            if self.directory.is_symlink() or self.directory.stat().st_ino != self.directory_identity:
                return
            for path, inode in self.file_identities.items():
                try:
                    if path.lstat().st_ino == inode:
                        path.unlink()
                except FileNotFoundError:
                    pass
            self.directory.rmdir()
        except OSError:
            pass

    async def handle(self, request):
        from .cli import _receipt_summary
        failure = {"ok": False, "data": None, "error": {"code": "delivery_uncertain", "message": "Reply not acknowledged."}}
        async with self.lock:
            try:
                if (not self.active or self.lease_lost.is_set() or not isinstance(request, dict)
                        or set(request) != {"version", "capability", "scope", "text"}
                        or type(request["version"]) is not int or request["version"] != 2
                        or not isinstance(request["capability"], str)
                        or not hmac.compare_digest(request["capability"], self.capability)
                        or request["scope"] != self.scope):
                    return failure
                if self.scope.get("recipient") != self.profile.local_alias:
                    return failure
                text = valid_text(request["text"])
                digest = payload_digest(text)
                if self.reply_digest is not None and digest != self.reply_digest:
                    return failure
                if self.receipt is None:
                    if self.validate:
                        self.validate()
                    # Fix content at first send attempt, including uncertain SDK
                    # replies. A retry may only replay this exact fixed key/body.
                    self.reply_digest = digest
                    receipt = await asyncio.to_thread(self.send, self.profile, self.scope["recipient"], {
                        "send_key": "desktop-reply-v1:" + self.scope["job_id"], "text": text,
                        "reply_to_message_id": self.incoming_message_id})
                    verified_receipt(self.profile, self.profile.local_alias, receipt)
                    receipt = _receipt_summary(receipt)
                    if not self.active or self.lease_lost.is_set():
                        return failure
                    self.receipt = receipt
                    self.acknowledged.set()
                return {"ok": True, "data": dict(self.receipt), "error": None}
            except Exception:
                return failure

    async def _connection(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            async with asyncio.timeout(30):
                frame = await reader.readline()
                if len(frame) > MAX_FRAME or not frame.endswith(b"\n"):
                    return
                response = await self.handle(json.loads(frame))
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
        except (ValueError, OSError, TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self.handlers.discard(task)


def read_request(path):
    packet = read_private_json(path)
    fields = {"version", "scope", "capability", "socket_path", "profile_path", "source_alias",
              "incoming_message_id", "python", "cli_path"}
    if (set(packet) != fields or type(packet["version"]) is not int or packet["version"] != 2
            or not isinstance(packet["scope"], dict) or not isinstance(packet["capability"], str)):
        raise ValueError("Invalid native request packet.")
    return packet


def reply_request(path, text):
    packet = read_request(path)
    target = absolute_path(packet["socket_path"])
    _socket_path(target)
    frame = json.dumps({"version": 2, "scope": packet["scope"], "capability": packet["capability"],
                        "text": valid_text(text)}).encode() + b"\n"
    if len(frame) > MAX_FRAME:
        raise ValueError("Invalid reply size.")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(30)
        client.connect(str(target))
        client.sendall(frame)
        with client.makefile("rb") as stream:
            raw = stream.readline(MAX_FRAME + 1)
        if len(raw) > MAX_FRAME or not raw.endswith(b"\n"):
            raise ValueError("Invalid native reply result.")
        result = json.loads(raw)
    return packet, result
