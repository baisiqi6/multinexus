"""Two-tool lifecycle client with flat MCP arguments and static diagnostics."""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

from ..channel_mcp_transport import ChannelTransportError, _contains_token, _load_sdk, _validate_endpoint

CLIENT_TIMEOUT_SECONDS = 45
_TOOL_FIELDS = {
    "coordinate.runtime_request_submit": {"workspace_id", "target_agent", "prompt", "origin", "reply", "idempotency_key"},
    "coordinate.runtime_job_get": {"job_id"},
}


class LifecycleTransportError(ValueError):
    """Safe local failure; failed submit outcomes may be unknown."""


def _validate_arguments(name: str, arguments: dict) -> None:
    if name not in _TOOL_FIELDS or not isinstance(arguments, dict) or set(arguments) != _TOOL_FIELDS[name]:
        raise LifecycleTransportError("Only explicit flat lifecycle tool inputs are allowed.")
    string_fields = _TOOL_FIELDS[name] - {"origin", "reply"}
    if any(not isinstance(arguments[field], str) or not arguments[field].strip() for field in string_fields):
        raise LifecycleTransportError("Invalid lifecycle tool input.")
    if name == "coordinate.runtime_request_submit":
        for field in ("origin", "reply"):
            context = arguments[field]
            if (not isinstance(context, dict) or context.get("platform") != "none"
                    or not isinstance(context.get("destination"), str) or not context["destination"].strip()):
                raise LifecycleTransportError("Invalid lifecycle delivery scope.")
        scope = arguments["origin"].get("session_scope_id")
        if not isinstance(scope, str) or not scope.strip():
            raise LifecycleTransportError("Explicit lifecycle session scope is required.")


async def _call_remote(url: str, token: str, name: str, arguments: dict, sdk: tuple) -> Any:
    httpx, client_session, streamable_http_client = sdk
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"},
                                 timeout=CLIENT_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False) as client:
        async with streamable_http_client(url, http_client=client) as (reader, writer):
            async with client_session(reader, writer, read_timeout_seconds=CLIENT_TIMEOUT_SECONDS) as session:
                await session.initialize()
                return await session.call_tool(name, arguments, read_timeout_seconds=CLIENT_TIMEOUT_SECONDS)


def _translate_result(result: Any, token: str) -> dict:
    envelope = getattr(result, "structured_content", None)
    if envelope is None:
        for block in getattr(result, "content", None) or ():
            try:
                candidate = json.loads(block.text)
            except (AttributeError, ValueError, TypeError):
                continue
            if isinstance(candidate, dict) and isinstance(candidate.get("ok"), bool):
                envelope = candidate
                break
    valid = isinstance(envelope, dict) and set(envelope) == {"ok", "data", "error"}
    if valid:
        valid = type(envelope["ok"]) is bool
        if envelope["ok"] is True:
            valid = valid and isinstance(envelope["data"], dict) and envelope["error"] is None and not getattr(result, "is_error", False)
        else:
            error = envelope["error"]
            valid = valid and (envelope["data"] is None or isinstance(envelope["data"], dict))
            valid = valid and isinstance(error, dict) and set(error) == {"code", "message"}
            valid = valid and isinstance(error.get("code"), str) and bool(error["code"]) and isinstance(error.get("message"), str)
    try:
        json.dumps(envelope, ensure_ascii=False, allow_nan=False)
        valid = valid and not _contains_token(envelope, token)
    except (ValueError, TypeError, RecursionError):
        valid = False
    if not valid:
        raise LifecycleTransportError("Remote MCP returned an invalid lifecycle response.")
    return envelope


class LifecycleMCPTransport:
    def __init__(self, *, url: str, token_env: str):
        try:
            self._url = _validate_endpoint(url)
        except ChannelTransportError:
            raise LifecycleTransportError("Invalid lifecycle MCP endpoint.") from None
        if not isinstance(token_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
            raise LifecycleTransportError("Invalid token environment variable name.")
        self._token_env = token_env

    def call_tool(self, name: str, flat_arguments: dict) -> dict:
        _validate_arguments(name, flat_arguments)
        token = os.environ.get(self._token_env)
        if not token or not token.strip() or any(ord(c) < 32 or ord(c) == 127 for c in token):
            raise LifecycleTransportError("Lifecycle token environment variable is missing or invalid.")
        try:
            sdk = _load_sdk()
            async def run():
                async with asyncio.timeout(CLIENT_TIMEOUT_SECONDS):
                    result = await _call_remote(self._url, token, name, flat_arguments, sdk)
                    return _translate_result(result, token)
            return asyncio.run(run())
        except KeyboardInterrupt:
            raise
        except Exception:
            raise LifecycleTransportError("Lifecycle call failed; reuse the same delivery key to inspect uncertain submission.") from None
