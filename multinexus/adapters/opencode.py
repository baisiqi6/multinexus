import asyncio
import json
import logging
import shutil
from collections.abc import Callable

from ..models import AgentConfig
from .base import AdapterResult, AgentAdapter
from .utils import async_subprocess_kwargs, filtered_env, terminate_owned_process_group

log = logging.getLogger(__name__)

EMPTY_TEXT_RETRIES = 4


class OpenCodeAdapter(AgentAdapter):
    """OpenCode CLI adapter with streaming JSON output."""

    def __init__(self, config: AgentConfig):
        super().__init__(name="opencode", timeout=config.timeout)
        self.config = config

    def _with_system_prompt(self, prompt: str) -> str:
        if not self.config.system_prompt.strip():
            return prompt
        return f"{self.config.system_prompt.strip()}\n\nUSER: {prompt}"

    def _build_cmd(self, *, resume_session_id: str | None = None) -> list[str]:
        cmd = [self.config.opencode_bin, "run", "--format", "json"]
        if resume_session_id:
            cmd += ["--session", resume_session_id]
        if self.config.opencode_dangerously_skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if self.config.model:
            cmd += ["--model", self.config.model]
        return cmd

    async def call(
        self,
        prompt: str,
        *,
        timeout: int | None = None,
        work_dir: str | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> AdapterResult:
        return await self._run(prompt, timeout=timeout, work_dir=work_dir, on_progress=on_progress)

    async def resume(
        self,
        session_id: str,
        prompt: str,
        *,
        timeout: int | None = None,
        work_dir: str | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> AdapterResult:
        result = await self._run(
            prompt,
            timeout=timeout,
            work_dir=work_dir,
            on_progress=on_progress,
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
        on_progress: Callable[[str], None] | None = None,
        resume_session_id: str | None = None,
        empty_text_retries: int = EMPTY_TEXT_RETRIES,
    ) -> AdapterResult:
        timeout = timeout or self.config.timeout
        full_prompt = self._with_system_prompt(prompt)
        cmd = self._build_cmd(resume_session_id=resume_session_id)

        cwd = work_dir or self.config.work_dir
        log.info("OpenCode cmd: %s (prompt %d chars)", cmd, len(full_prompt))

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
            return AdapterResult(text=f"OpenCode CLI not found: {self.config.opencode_bin}")

        cleanup_attempted = False

        async def cleanup() -> None:
            nonlocal cleanup_attempted
            if cleanup_attempted:
                return
            cleanup_attempted = True
            await terminate_owned_process_group(proc)

        try:
            # Write prompt to stdin, then close
            assert proc.stdin is not None
            proc.stdin.write(full_prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout
            last_activity = loop.time()
            saw_output = False
            response_parts: list[str] = []
            session_id: str | None = resume_session_id
            event_types_seen: set[str] = set()

            while True:
                now = loop.time()
                if now >= deadline:
                    await cleanup()
                    return AdapterResult(text=f"OpenCode timed out after {timeout}s")

                assert proc.stdout is not None
                idle_timeout = (
                    self.config.activity_timeout if saw_output else self.config.first_byte_timeout
                )
                read_timeout = min(idle_timeout, deadline - now)
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=read_timeout)
                except asyncio.TimeoutError:
                    elapsed = loop.time() - last_activity
                    if not saw_output and elapsed >= self.config.first_byte_timeout:
                        await cleanup()
                        return AdapterResult(
                            text=f"OpenCode timed out: no output after {self.config.first_byte_timeout}s"
                        )
                    if saw_output and elapsed >= self.config.activity_timeout:
                        await cleanup()
                        return AdapterResult(
                            text=f"OpenCode timed out: no activity for {self.config.activity_timeout}s"
                        )
                    continue
                if not raw:
                    break
                last_activity = loop.time()
                saw_output = True
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Capture sessionID from any event
                if not session_id and event.get("sessionID"):
                    session_id = event["sessionID"]

                event_type = event.get("type", "")
                if event_type:
                    event_types_seen.add(event_type)
                if event_type == "text":
                    text = event.get("part", {}).get("text", "")
                    if text:
                        response_parts.append(text)
                        if on_progress:
                            try:
                                on_progress("\n".join(response_parts))
                            except Exception:
                                pass
                elif on_progress and event_type == "tool_use":
                    tool = event.get("part", {}).get("tool", "")
                    if tool:
                        on_progress(
                            f"[using tool: {tool}]"
                            if not response_parts
                            else "\n".join(response_parts)
                        )

            await proc.wait()
        except asyncio.CancelledError:
            await cleanup()
            raise
        except Exception:
            await cleanup()
            raise

        stderr_text = ""
        if proc.returncode != 0:
            assert proc.stderr is not None
            stderr_text = (await proc.stderr.read()).decode("utf-8", errors="replace").strip()

        if not response_parts and proc.returncode != 0:
            log.error(
                "OpenCode CLI failed: rc=%s stderr=%s",
                proc.returncode,
                stderr_text[-1000:],
            )
            detail = stderr_text or f"exit code {proc.returncode}"
            return AdapterResult(
                text=f"OpenCode CLI failed ({proc.returncode}): {detail[:500]}"
            )

        if (
            not response_parts
            and proc.returncode == 0
            and empty_text_retries > 0
            and "tool_use" not in event_types_seen
        ):
            log.warning(
                "OpenCode returned no text with rc=0; retrying (%d remaining, events=%s)",
                empty_text_retries,
                sorted(event_types_seen),
            )
            return await self._run(
                prompt,
                timeout=timeout,
                work_dir=work_dir,
                on_progress=on_progress,
                resume_session_id=resume_session_id,
                empty_text_retries=empty_text_retries - 1,
            )

        if not response_parts and proc.returncode == 0:
            return AdapterResult(
                text=(
                    "OpenCode returned no text"
                    f" (events={','.join(sorted(event_types_seen)) or 'none'})"
                ),
                session_id=session_id,
            )

        return AdapterResult(
            text="\n".join(response_parts).strip() or "(no response)",
            session_id=session_id,
        )

    async def health_check(self) -> dict:
        found = shutil.which(self.config.opencode_bin)
        return {
            "adapter": "opencode",
            "bin": self.config.opencode_bin,
            "available": found is not None,
            "path": found,
        }
