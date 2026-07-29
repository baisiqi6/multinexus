import asyncio
import shutil

from ..models import AgentConfig
from .base import AdapterResult, AgentAdapter
from .utils import NO_WINDOW, async_subprocess_kwargs, filtered_env, terminate_owned_process_group


class OmpAdapter(AgentAdapter):
    """Oh My Pi (omp) CLI adapter with --auto-approve for headless use."""

    def __init__(self, config: AgentConfig):
        super().__init__(name="omp", timeout=config.timeout)
        self.config = config

    def _with_system_prompt(self, prompt: str) -> str:
        if not self.config.system_prompt.strip():
            return prompt
        return f"{self.config.system_prompt.strip()}\n\nUSER: {prompt}"

    def _build_cmd(self, *, resume_session_id: str | None = None, no_session: bool = False) -> list[str]:
        cmd = [self.config.omp_bin, "-p"]
        if no_session:
            cmd.append("--no-session")
        if resume_session_id:
            cmd += ["--resume", resume_session_id]
        if self.config.omp_auto_approve:
            cmd.append("--auto-approve")
        if self.config.omp_model:
            cmd += ["--model", self.config.omp_model]
        if self.config.omp_thinking:
            cmd += ["--thinking", self.config.omp_thinking]
        return cmd

    async def call(
        self,
        prompt: str,
        *,
        timeout: int | None = None,
        work_dir: str | None = None,
        on_progress=None,
    ) -> AdapterResult:
        result = await self._run(prompt, timeout=timeout, work_dir=work_dir, no_session=True)
        result.session_id = None
        return result

    async def resume(
        self,
        session_id: str,
        prompt: str,
        *,
        timeout: int | None = None,
        work_dir: str | None = None,
        on_progress=None,
    ) -> AdapterResult:
        result = await self._run(
            prompt,
            timeout=timeout,
            work_dir=work_dir,
            resume_session_id=session_id,
        )
        result.resumed = True
        return result

    async def _run(
        self,
        prompt: str,
        *,
        timeout: int | None = None,
        work_dir: str | None = None,
        resume_session_id: str | None = None,
        no_session: bool = False,
    ) -> AdapterResult:
        timeout = timeout or self.config.timeout
        full_prompt = self._with_system_prompt(prompt)
        cmd = self._build_cmd(resume_session_id=resume_session_id, no_session=no_session)
        cmd.append(full_prompt)
        cwd = work_dir or self.config.work_dir

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=filtered_env(cwd=cwd),
                limit=10 * 1024 * 1024,
                **async_subprocess_kwargs(),
            )
        except FileNotFoundError:
            return AdapterResult(text=f"omp CLI not found: {self.config.omp_bin}")

        cleanup_attempted = False

        async def cleanup() -> None:
            nonlocal cleanup_attempted
            if cleanup_attempted:
                return
            cleanup_attempted = True
            await terminate_owned_process_group(proc)

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(None), timeout=timeout
            )
        except asyncio.TimeoutError:
            await cleanup()
            return AdapterResult(text=f"omp timed out after {timeout}s")
        except asyncio.CancelledError:
            await cleanup()
            raise
        except Exception:
            await cleanup()
            raise

        response_text = stdout.decode("utf-8", errors="replace").strip()
        session_id = resume_session_id

        if proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            detail = stderr_text or f"exit code {proc.returncode}"
            return AdapterResult(
                text=f"omp CLI failed ({proc.returncode}): {detail[:500]}"
            )

        return AdapterResult(
            text=response_text or "(no response)",
            session_id=session_id,
        )

    async def health_check(self) -> dict:
        bin_path = self.config.omp_bin
        try:
            proc = await asyncio.create_subprocess_exec(
                bin_path, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=filtered_env(),
                **NO_WINDOW,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            available = proc.returncode == 0
        except Exception:
            available = False

        found = shutil.which(bin_path)
        return {
            "adapter": "omp",
            "bin": bin_path,
            "available": available,
            "path": found,
        }
