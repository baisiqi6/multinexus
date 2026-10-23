"""Explicit native adapter configuration. No provider/session-store interface."""
from __future__ import annotations

import asyncio
import os
import sys

from .bindings import NativeIdentityError, absolute_path, credential_config, load_profile
from .codex_transport import CodexNativeTransport, DEFAULT_CODEX_APP_BIN
from .host_transport import DSHHostTransport


class DesktopNativeAdapter:
    def __init__(self, config):
        self.config = config
        if sys.platform != "darwin":
            raise ValueError("Native desktop workers currently require macOS.")
        self.profile_path = absolute_path(config.desktop_profile_file)
        self.profile = load_profile(self.profile_path)
        if config.desktop_recipient != self.profile.local_alias:
            raise ValueError("Invalid native recipient.")
        self.binding = self.profile.bindings[config.desktop_recipient]
        if self.binding.agent_id != config.id or (config.work_dir is not None and config.work_dir != self.binding.cwd):
            raise ValueError("Native worker binding mismatch.")
        if config.model or config.system_prompt:
            raise ValueError("Native workers cannot override model or instructions.")
        self.runtime_dir = absolute_path(config.desktop_runtime_dir)
        if self.runtime_dir.resolve(strict=True) != self.runtime_dir or not self.runtime_dir.is_dir():
            raise ValueError("Native runtime directory must not contain symlinks.")
        if len(os.fsencode(self.runtime_dir)) > 65:
            raise ValueError("Native runtime directory must be a short absolute path.")
        self.credentials = {alias: credential_config(self.profile, alias) for alias in (self.profile.local_alias, None)}
        self.lock = asyncio.Lock()
        self.attempted_jobs: set[str] = set()
        if self.binding.kind == "codex":
            self.transport = CodexNativeTransport(config.desktop_codex_bin or DEFAULT_CODEX_APP_BIN, self.binding)
        else:
            self.transport = DSHHostTransport(config.desktop_host_socket, config.desktop_host_token_file)

    def validate_configuration(self):
        try:
            if load_profile(self.profile_path) != self.profile:
                raise ValueError("Native profile changed.")
            for alias, value in self.credentials.items():
                if credential_config(self.profile, alias) != value:
                    raise ValueError("Native channel profile changed.")
        except (ValueError, OSError):
            raise NativeIdentityError("Native configuration mismatch.") from None

    async def preflight(self):
        self.validate_configuration()
        result = await self.transport.inspect(self.binding)
        expected = {"ready": True, "binding_digest": self.binding.digest,
                    "session_id": self.binding.session_id, "cwd": self.binding.cwd}
        if not isinstance(result, dict) or any(result.get(k) != v for k, v in expected.items()):
            raise NativeIdentityError("Native session identity mismatch.")

    async def startup_check(self):
        check = getattr(self.transport, "startup_check", None)
        if check:
            await check()
        await self.preflight()
        return {"runtime_ready": True}
