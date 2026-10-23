"""One-request private Unix transport; no native cancellation or business retries."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
from typing import Any

from .bindings import DesktopBinding, absolute_path, read_private_bytes

MAX_FRAME = 65536
TIMEOUT_SECONDS = 5
_SCOPE_FIELDS = {"job_id", "attempt_token", "lease_id", "native_request_id", "binding_digest", "payload_digest", "native_content_digest", "recipient"}


class DSHHostError(RuntimeError):
    def __init__(self):
        super().__init__("Desktop host unavailable.")


def _socket_path(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if stat.S_ISLNK(current.lstat().st_mode):
            raise DSHHostError()
    parent, info = path.parent.stat(), path.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700 or not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
        raise DSHHostError()


class DSHHostTransport:
    def __init__(self, socket_path: str | Path, worker_token_file: str | Path):
        try:
            self.socket_path = absolute_path(socket_path)
            self.worker_token_file = absolute_path(worker_token_file)
        except ValueError:
            raise DSHHostError() from None

    async def inspect(self, binding: DesktopBinding) -> dict[str, Any]:
        if not isinstance(binding, DesktopBinding) or binding.kind != "dsh":
            raise DSHHostError()
        return await self._request("inspect", binding=binding.as_dict())

    async def register_scope(self, scope: dict) -> dict[str, Any]:
        return await self._request("register", scope=self._scope(scope))

    async def queue(self, scope: dict, content: str) -> dict[str, Any]:
        """Trusted worker calls only after awaited fenced latch and exact readback."""
        scope = self._scope(scope)
        if not isinstance(content, str) or not content.strip():
            raise DSHHostError()
        return await self._request("queue", scope=scope, content=content, confirmed_latch=dict(scope))

    async def query(self, scope: dict) -> dict[str, Any]:
        return await self._request("query", scope=self._scope(scope))

    async def close_scope(self, scope: dict) -> dict[str, Any]:
        return await self._request("close", scope=self._scope(scope))

    @staticmethod
    def _scope(scope: dict) -> dict:
        if (not isinstance(scope, dict) or set(scope) != _SCOPE_FIELDS
                or not all(isinstance(v, str) and v.strip() for v in scope.values())):
            raise DSHHostError()
        return dict(scope)

    async def _request(self, operation: str, **fields: Any) -> dict[str, Any]:
        writer = None
        try:
            token = read_private_bytes(self.worker_token_file).decode("utf-8").strip()
            if not token or any(ord(c) < 32 for c in token):
                raise DSHHostError()
            _socket_path(self.socket_path)
            frame = json.dumps({"version": 1, "op": operation, "worker_token": token, **fields}, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
            if len(frame) > MAX_FRAME:
                raise DSHHostError()
            async with asyncio.timeout(TIMEOUT_SECONDS):
                reader, writer = await asyncio.open_unix_connection(str(self.socket_path), limit=MAX_FRAME)
                writer.write(frame)
                await writer.drain()
                response = await reader.readline()
                if len(response) > MAX_FRAME or not response.endswith(b"\n"):
                    raise DSHHostError()
                data = json.loads(response)
                if (not isinstance(data, dict) or set(data) != {"version", "ok", "result"}
                        or type(data["version"]) is not int or data["version"] != 1
                        or data["ok"] is not True or not isinstance(data["result"], dict)):
                    raise DSHHostError()
                return data["result"]
        except (OSError, ValueError, TypeError, UnicodeError, TimeoutError, RecursionError):
            raise DSHHostError() from None
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
