"""Channel-only Streamable HTTP client for the existing Coordinate MCP server.

The SDK is optional, credentials come only from a named environment variable,
and one call (including connection and cleanup) has a 45 second deadline.
No retries or background work are started here.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
from typing import Any
from urllib.parse import urlsplit

CLIENT_TIMEOUT_SECONDS = 45
_COMMON_FIELDS = {"workspace_id", "channel_id", "platform"}
_TOOL_FIELDS = {
    "coordinate.channel_read": _COMMON_FIELDS | {"after_cursor", "limit"},
    "coordinate.channel_send": _COMMON_FIELDS | {"text", "send_key", "reply_to_message_id"},
    "coordinate.channel_wait": _COMMON_FIELDS | {"after_cursor", "limit", "timeout_seconds"},
}
_ERROR_CODES = frozenset({
    "sender_unbound", "sender_unavailable", "sender_identity_mismatch",
    "invalid_input", "forbidden", "unsupported_platform", "unsupported_channel_type",
    "cursor_scope_mismatch", "invalid_cursor", "idempotency_conflict", "busy",
    "rate_limited", "upstream_unavailable", "content_unavailable", "delivery_rejected",
    "delivery_uncertain",
})
_UNKNOWN_MESSAGE = "Delivery outcome is unknown; reuse the same send_key to inspect it."


class ChannelTransportError(Exception):
    """A local failure with a static, credential-free error envelope."""

    def __init__(self, code: str, message: str, *, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.data = data

    def envelope(self) -> dict[str, Any]:
        return {"ok": False, "data": self.data, "error": {"code": self.code, "message": str(self)}}


def _validate_endpoint(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port  # also rejects malformed ports
        valid = (
            parts.scheme in {"https", "http"} and host and not parts.query and not parts.fragment
            and parts.username is None and parts.password is None
            and not any(character.isspace() or ord(character) < 32 for character in url)
            and (port is None or 0 < port < 65536)
        )
        if valid and parts.scheme == "http":
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host == "localhost"
            valid = loopback
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ChannelTransportError("invalid_input", "Invalid Remote MCP endpoint URL.")
    return url


def _load_sdk() -> tuple[Any, Any, Any]:
    try:
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError:
        raise ChannelTransportError(
            "upstream_unavailable", "Install requirements-channel-tools.txt to use the MCP client."
        ) from None
    return httpx2, ClientSession, streamable_http_client


async def _call_remote(url: str, token: str, name: str, arguments: dict[str, Any], sdk: tuple) -> Any:
    httpx, client_session, streamable_http_client = sdk
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=CLIENT_TIMEOUT_SECONDS,
        follow_redirects=False,
    ) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (reader, writer):
            async with client_session(reader, writer, read_timeout_seconds=CLIENT_TIMEOUT_SECONDS) as session:
                await session.initialize()
                return await session.call_tool(name, {"input": arguments}, read_timeout_seconds=CLIENT_TIMEOUT_SECONDS)


def _translate_result(result: Any, token: str) -> dict[str, Any]:
    envelope = getattr(result, "structured_content", None)
    if envelope is None:
        for block in getattr(result, "content", None) or ():
            try:
                envelope = json.loads(block.text)
            except (AttributeError, TypeError, ValueError):
                continue
            if isinstance(envelope, dict) and isinstance(envelope.get("ok"), bool):
                break
    valid = isinstance(envelope, dict) and set(envelope) == {"ok", "data", "error"}
    if valid:
        valid = type(envelope["ok"]) is bool
        if envelope["ok"] is True:
            valid = valid and isinstance(envelope["data"], dict) and envelope["error"] is None
            valid = valid and not getattr(result, "is_error", False)
        else:
            error = envelope["error"]
            valid = valid and (envelope["data"] is None or isinstance(envelope["data"], dict))
            valid = valid and isinstance(error, dict) and isinstance(error.get("code"), str)
            valid = valid and error.get("code") in _ERROR_CODES
            valid = valid and isinstance(error.get("message"), str)
    try:
        json.dumps(envelope, ensure_ascii=False, allow_nan=False)
        valid = valid and not _contains_token(envelope, token)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ChannelTransportError("upstream_unavailable", "Remote MCP returned an invalid channel response.")
    return envelope


def _contains_token(value: Any, token: str) -> bool:
    if isinstance(value, str):
        return token in value
    if isinstance(value, dict):
        return any(_contains_token(item, token) for pair in value.items() for item in pair)
    if isinstance(value, list):
        return any(_contains_token(item, token) for item in value)
    return False


class ChannelMCPTransport:
    """An allowlisted channel client; never a general Coordinate tool gateway."""

    def __init__(self, *, url: str, token_env: str):
        self._url = _validate_endpoint(url)
        if not isinstance(token_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
            raise ChannelTransportError("invalid_input", "Invalid token environment variable name.")
        self._token_env = token_env

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in _TOOL_FIELDS or not isinstance(arguments, dict) or not set(arguments) <= _TOOL_FIELDS[name]:
            raise ChannelTransportError("invalid_input", "Only channel tool inputs are allowed.")
        token = os.environ.get(self._token_env)
        if not token or not token.strip() or any(ord(c) < 32 or ord(c) == 127 for c in token):
            raise ChannelTransportError("invalid_input", "Remote MCP token environment variable is missing or invalid.")
        sdk = _load_sdk()  # a missing dependency cannot have sent a message

        async def run() -> dict[str, Any]:
            async with asyncio.timeout(CLIENT_TIMEOUT_SECONDS):
                result = await _call_remote(self._url, token, name, arguments, sdk)
                return _translate_result(result, token)

        try:
            return asyncio.run(run())
        except KeyboardInterrupt:
            raise
        except Exception:
            # A send may have reached the server even when its response was
            # lost or malformed. Never retry it or claim it was rejected.
            if name == "coordinate.channel_send":
                raise ChannelTransportError("delivery_uncertain", _UNKNOWN_MESSAGE, data={"status": "unknown"}) from None
            raise ChannelTransportError("upstream_unavailable", "Remote MCP channel call failed.") from None
