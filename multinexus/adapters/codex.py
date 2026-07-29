import asyncio
import json
import logging
import shutil
from dataclasses import dataclass
from typing import Any

from ..models import AgentConfig
from .base import AdapterResult, AgentAdapter
from .utils import async_subprocess_kwargs, filtered_env, terminate_owned_process_group

log = logging.getLogger(__name__)

NON_TEXT_CODEX_EVENTS = {
    "thread.started",
    "turn.started",
    "turn.completed",
    "item.started",
    "item.completed",
}

CODEX_CAPACITY_ERROR = "selected model is at capacity"


@dataclass
class CodexRunResult:
    text: str
    session_id: str | None = None
    capacity_error: bool = False


def extract_codex_text(event: dict[str, Any]) -> str:
    item = event.get("item", {})
    if isinstance(item, dict) and item.get("type") == "agent_message":
        text = item.get("text", "")
        if text:
            return text

    msg = event.get("message") or item
    content = msg.get("content", []) if isinstance(msg, dict) else []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("text", "output_text"):
                text = block.get("text", "")
                if text:
                    return text

    flat = event.get("content")
    if isinstance(flat, str) and flat:
        return flat

    for out_item in event.get("response", {}).get("output", []):
        for block in out_item.get("content", []):
            if isinstance(block, dict):
                text = block.get("text", "")
                if text:
                    return text
    return ""


def extract_codex_error(event: dict[str, Any]) -> str:
    if event.get("type") == "error":
        return str(event.get("message") or event.get("error") or event)[:1000]

    error = event.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)[:1000]
    if isinstance(error, str):
        return error[:1000]
    return ""


def is_capacity_error(text: str) -> bool:
    return CODEX_CAPACITY_ERROR in text.lower()


class CodexAdapter(AgentAdapter):
    """Codex CLI adapter with streaming JSON output and capacity fallback."""

    def __init__(self, config: AgentConfig):
        super().__init__(name="codex", timeout=config.timeout)
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
        full_prompt = self._with_system_prompt(prompt)
        effective_work_dir = work_dir or self.config.work_dir
        primary = await self._run_once(full_prompt, model=self.config.model, work_dir=effective_work_dir)
        if not primary.capacity_error:
            return AdapterResult(
                text=primary.text, session_id=primary.session_id
            )

        fallback_model = self.config.codex_fallback_model
        if fallback_model and fallback_model != self.config.model:
            log.warning(
                "Codex model at capacity; retrying with fallback: primary=%s fallback=%s",
                self.config.model or "default",
                fallback_model,
            )
            fallback = await self._run_once(full_prompt, model=fallback_model, work_dir=effective_work_dir)
            if not fallback.capacity_error:
                return AdapterResult(
                    text=fallback.text, session_id=fallback.session_id
                )
            return AdapterResult(
                text=self._capacity_failure_message(self.config.model, fallback_model)
            )

        return AdapterResult(
            text=self._capacity_failure_message(self.config.model, None)
        )

    async def resume(
        self,
        session_id: str,
        prompt: str,
        *,
        timeout: int | None = None,
        work_dir: str | None = None,
        on_progress=None,
    ) -> AdapterResult:
        full_prompt = self._with_system_prompt(prompt)
        effective_timeout = timeout or self.config.timeout
        effective_work_dir = work_dir or self.config.work_dir
        cmd = [
            self.config.codex_bin,
            "exec",
            "resume",
            session_id,
            "--json",
            "--skip-git-repo-check",
        ]
        self._append_permission_flags(cmd, for_resume=True)
        if self.config.model:
            cmd += ["--model", self.config.model]
        cmd.append("-")

        result = await self._exec_cmd(cmd, full_prompt, timeout=effective_timeout, work_dir=effective_work_dir)
        result.resumed = True
        return result

    def _build_cmd(self, model: str | None) -> list[str]:
        cmd = [self.config.codex_bin, "exec"]
        cwd = self.config.work_dir
        if cwd:
            cmd += ["-C", cwd]
        cmd.append("--skip-git-repo-check")
        self._append_permission_flags(cmd, for_resume=False)
        cmd.append("--json")
        if model:
            cmd += ["--model", model]
        cmd.append("-")
        return cmd

    def _append_permission_flags(self, cmd: list[str], *, for_resume: bool) -> None:
        if self.config.codex_dangerously_bypass_approvals_and_sandbox:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
            return
        if not self.config.codex_sandbox:
            return
        if for_resume:
            # exec resume does not accept --sandbox, so use a config override.
            cmd += ["-c", f"sandbox_permissions=[\"{self.config.codex_sandbox}\"]"]
        else:
            cmd += ["--sandbox", self.config.codex_sandbox]

    @staticmethod
    def _capacity_failure_message(
        primary_model: str | None, fallback_model: str | None
    ) -> str:
        primary_label = primary_model or "default model"
        if fallback_model:
            return (
                f"Codex model capacity: {primary_label} and fallback {fallback_model} "
                "are both at capacity. Aborted, no handoff."
            )
        return (
            f"Codex model capacity: {primary_label} is at capacity. "
            "Configure codex_fallback_model to auto-retry."
        )

    async def _run_once(self, full_prompt: str, model: str | None, *, work_dir: str | None = None) -> CodexRunResult:
        cmd = self._build_cmd(model)
        timeout = self.config.timeout
        effective_cwd = work_dir or self.config.work_dir

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=effective_cwd,
                env=filtered_env(cwd=effective_cwd),
                limit=10 * 1024 * 1024,
                **async_subprocess_kwargs(),
            )
        except FileNotFoundError:
            return CodexRunResult(f"Codex CLI not found: {self.config.codex_bin}")

        return await _run_codex_process(proc, full_prompt, timeout, self.config.activity_timeout)

    async def _exec_cmd(
        self,
        cmd: list[str],
        full_prompt: str,
        *,
        timeout: int | None = None,
        work_dir: str | None = None,
    ) -> AdapterResult:
        timeout = timeout or self.config.timeout
        effective_cwd = work_dir or self.config.work_dir
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=effective_cwd,
                env=filtered_env(cwd=effective_cwd),
                limit=10 * 1024 * 1024,
                **async_subprocess_kwargs(),
            )
        except FileNotFoundError:
            return AdapterResult(text=f"Codex CLI not found: {self.config.codex_bin}")

        cleanup_attempted = False

        async def cleanup() -> None:
            nonlocal cleanup_attempted
            if cleanup_attempted:
                return
            cleanup_attempted = True
            await terminate_owned_process_group(proc)

        try:
            assert proc.stdin is not None
            proc.stdin.write(full_prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

            response_text = ""
            error_text = ""
            session_id: str | None = None
            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout
            last_activity = loop.time()

            while True:
                now = loop.time()
                if now >= deadline:
                    await cleanup()
                    return AdapterResult(text=f"Codex timed out after {timeout}s")

                assert proc.stdout is not None
                read_timeout = min(self.config.activity_timeout, deadline - now)
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=read_timeout)
                except asyncio.TimeoutError:
                    if loop.time() - last_activity >= self.config.activity_timeout:
                        await cleanup()
                        return AdapterResult(
                            text=f"Codex stopped responding for {self.config.activity_timeout}s"
                        )
                    continue
                if not raw:
                    break
                last_activity = loop.time()
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "thread.started" and event.get("thread_id"):
                    session_id = event["thread_id"]
                error_text = extract_codex_error(event) or error_text
                partial = extract_codex_text(event)
                if partial:
                    response_text = partial

            await proc.wait()

            # Check for resume failure
            if proc.returncode != 0:
                assert proc.stderr is not None
                stderr = (await proc.stderr.read()).decode("utf-8", errors="replace").strip()
                detail = stderr or error_text or f"exit code {proc.returncode}"
                return AdapterResult(
                    text=f"Codex resume failed ({proc.returncode}): {detail[:500]}",
                    session_id=session_id,
                    resumed=True,
                )

            return AdapterResult(
                text=response_text.strip() or "(no response)",
                session_id=session_id,
                resumed=True,
            )
        except asyncio.CancelledError:
            await cleanup()
            raise
        except Exception:
            await cleanup()
            raise

    async def health_check(self) -> dict:
        found = shutil.which(self.config.codex_bin)
        return {
            "adapter": "codex",
            "bin": self.config.codex_bin,
            "available": found is not None,
            "path": found,
        }


async def _run_codex_process(
    proc: asyncio.subprocess.Process,
    full_prompt: str,
    timeout: int,
    activity_timeout: int,
) -> CodexRunResult:
    response_text = ""
    session_id: str | None = None
    stdout_tail: list[str] = []
    error_text = ""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    last_activity = loop.time()
    cleanup_attempted = False

    async def cleanup() -> None:
        nonlocal cleanup_attempted
        if cleanup_attempted:
            return
        cleanup_attempted = True
        await terminate_owned_process_group(proc)

    try:
        assert proc.stdin is not None
        proc.stdin.write(full_prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        while True:
            now = loop.time()
            if now >= deadline:
                await cleanup()
                return CodexRunResult(f"Codex timed out after {timeout}s", session_id=session_id)

            assert proc.stdout is not None
            read_timeout = min(activity_timeout, deadline - now)
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=read_timeout)
            except asyncio.TimeoutError:
                if loop.time() - last_activity >= activity_timeout:
                    await cleanup()
                    return CodexRunResult(
                        f"Codex stopped responding for {activity_timeout}s",
                        session_id=session_id,
                    )
                continue
            if not raw:
                break
            last_activity = loop.time()
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            stdout_tail.append(line)
            stdout_tail = stdout_tail[-20:]
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Capture thread_id from thread.started event
            if event.get("type") == "thread.started" and event.get("thread_id"):
                session_id = event["thread_id"]
            error_text = extract_codex_error(event) or error_text
            partial = extract_codex_text(event)
            if partial:
                response_text = partial
            elif event.get("type") and event.get("type") not in NON_TEXT_CODEX_EVENTS:
                log.debug("Codex unmatched event: %s", str(event)[:300])

        await proc.wait()
        if not response_text and proc.returncode != 0:
            assert proc.stderr is not None
            stderr = (await proc.stderr.read()).decode("utf-8", errors="replace")
            detail = stderr.strip() or error_text.strip() or "\n".join(stdout_tail)[-1000:]
            capacity_error = is_capacity_error(detail)
            log.error(
                "Codex CLI failed: model= rc=%s capacity=%s",
                proc.returncode,
                capacity_error,
            )
            return CodexRunResult(
                f"Codex CLI failed ({proc.returncode}): {detail[:500]}",
                session_id=session_id,
                capacity_error=capacity_error,
            )
        return CodexRunResult(response_text.strip() or "(no response)", session_id=session_id)
    except asyncio.CancelledError:
        await cleanup()
        raise
    except Exception:
        await cleanup()
        raise
