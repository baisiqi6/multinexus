"""Supported read-only metadata and App queue transport; never loads a turn."""
from __future__ import annotations

import asyncio
import json
import os

from .bindings import DesktopBinding, NativeIdentityError, absolute_path

DEFAULT_CODEX_APP_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"


def native_environment():
    """OS and App configuration locations only; no credential or policy values."""
    keys = ("HOME", "USER", "LOGNAME", "PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
            "SHELL", "__CF_USER_TEXT_ENCODING", "CODEX_HOME", "XDG_CONFIG_HOME",
            "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR")
    return {key: os.environ[key] for key in keys if key in os.environ}


class CodexNativeTransport:
    def __init__(self, binary: str, binding: DesktopBinding):
        self.binary = str(absolute_path(binary))
        self.binding = binding

    async def startup_check(self):
        process = await asyncio.create_subprocess_exec(
            self.binary, "queue", "--help", stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=native_environment())
        try:
            async with asyncio.timeout(5):
                output, _ = await process.communicate()
            if process.returncode != 0 or b"--thread" not in output or b"--message" not in output:
                raise ValueError("Native queue capability unavailable.")
        finally:
            await self._stop_transport(process)

    async def inspect(self, binding: DesktopBinding) -> dict:
        if binding != self.binding or binding.kind != "codex":
            raise NativeIdentityError("Native binding mismatch.")
        thread = await self._read_thread()
        if thread.get("id") != binding.session_id or thread.get("cwd") != binding.cwd:
            raise NativeIdentityError("Native binding mismatch.")
        # A separate metadata reader cannot prove the Desktop has loaded it.
        return {"ready": True, "binding_digest": binding.digest,
                "session_id": binding.session_id, "cwd": binding.cwd}

    async def _read_thread(self) -> dict:
        process = await asyncio.create_subprocess_exec(
            self.binary, "app-server", "--listen", "stdio://",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=65536, env=native_environment())
        try:
            async with asyncio.timeout(10):
                async def write(value):
                    process.stdin.write(json.dumps(value).encode() + b"\n")
                    await process.stdin.drain()
                async def receive(identifier):
                    for _ in range(32):
                        line = await process.stdout.readline()
                        if not line or len(line) > 65536:
                            break
                        value = json.loads(line)
                        if value.get("id") == identifier:
                            if "error" in value or not isinstance(value.get("result"), dict):
                                break
                            return value["result"]
                    raise ValueError("Native metadata unavailable.")
                await write({"id": 1, "method": "initialize", "params": {
                    "clientInfo": {"name": "multinexus_desktop_metadata", "version": "1"}}})
                await receive(1)
                await write({"method": "initialized", "params": {}})
                await write({"id": 2, "method": "thread/read", "params": {
                    "threadId": self.binding.session_id, "includeTurns": False}})
                result = await receive(2)
                if not isinstance(result.get("thread"), dict):
                    raise ValueError("Native metadata unavailable.")
                return result["thread"]
        finally:
            await self._stop_transport(process)

    async def queue(self, scope: dict, content: str) -> dict:
        process = await asyncio.create_subprocess_exec(
            self.binary, "queue", "--thread", self.binding.session_id, "--message", content,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=native_environment())
        try:
            async with asyncio.timeout(10):
                await process.communicate()
            if process.returncode != 0:
                raise ValueError("Native queue delivery uncertain.")
            return {"state": "queued", "native_request_id": scope["native_request_id"]}
        finally:
            await self._stop_transport(process)

    @staticmethod
    async def _stop_transport(process):
        # Only the temporary CLI/metadata transport is terminated, never a turn.
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                process.kill()
                await process.wait()
