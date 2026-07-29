import asyncio
import shutil
import logging

from ..models import AgentConfig
from .base import AdapterResult, AgentAdapter
from .utils import async_subprocess_kwargs, filtered_env, terminate_owned_process_group

log = logging.getLogger(__name__)


class HermesAdapter(AgentAdapter):
    """Hermes one-shot CLI adapter. No session support."""

    def __init__(self, config: AgentConfig):
        super().__init__(name="hermes", timeout=config.timeout)
        self.config = config

    def _with_system_prompt(self, prompt: str) -> str:
        if not self.config.system_prompt.strip():
            return prompt
        return f"{self.config.system_prompt.strip()}\n\nUSER: {prompt}"

    async def call(
        self,
        prompt: str,
        *,
        timeout: int | None = None,
        work_dir: str | None = None,
        on_progress=None,
    ) -> AdapterResult:
        timeout = timeout or self.config.timeout
        cmd = [self.config.hermes_bin]
        if self.config.model:
            cmd += ["--model", self.config.model]
        if self.config.hermes_provider:
            cmd += ["--provider", self.config.hermes_provider]
        if self.config.hermes_toolsets:
            cmd += ["--toolsets", self.config.hermes_toolsets]
        if self.config.hermes_accept_hooks:
            cmd.append("--accept-hooks")
        cmd += ["-z", self._with_system_prompt(prompt)]

        cwd = work_dir or self.config.work_dir

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=filtered_env(cwd=cwd),
                limit=10 * 1024 * 1024,
                **async_subprocess_kwargs(),
            )
        except FileNotFoundError:
            return AdapterResult(text=f"Hermes CLI not found: {self.config.hermes_bin}")

        cleanup_attempted = False

        async def cleanup() -> None:
            nonlocal cleanup_attempted
            if cleanup_attempted:
                return
            cleanup_attempted = True
            await terminate_owned_process_group(proc)

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await cleanup()
            return AdapterResult(text=f"Hermes timed out after {timeout}s")
        except asyncio.CancelledError:
            await cleanup()
            raise
        except Exception:
            await cleanup()
            raise

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            log.error(
                "Hermes CLI failed: rc=%s stderr=%s stdout=%s",
                proc.returncode,
                stderr_text[-1000:],
                stdout_text[-1000:],
            )
            detail = stderr_text or stdout_text
            return AdapterResult(
                text=f"Hermes CLI failed ({proc.returncode}): {detail[:500]}"
            )

        return AdapterResult(text=stdout_text or "(no response)")

    async def health_check(self) -> dict:
        found = shutil.which(self.config.hermes_bin)
        return {
            "adapter": "hermes",
            "bin": self.config.hermes_bin,
            "available": found is not None,
            "path": found,
        }
