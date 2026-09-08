import asyncio
import json
import logging
import shutil

from ..models import AgentConfig
from .base import AdapterResult, AgentAdapter, failed_result, timed_out_result
from .utils import async_subprocess_kwargs, filtered_env, terminate_owned_process_group

log = logging.getLogger(__name__)


STARTUP_TIMEOUT_SECONDS = 15
STARTUP_OUTPUT_LIMIT = 1024 * 1024
_STARTUP_REQUEST_ID = "multinexus-startup"
_RUNTIME_DIRECTORY_HINT = (
    "OMP runtime and SQLite state must be writable (default: ~/.omp; "
    "profiles and environment overrides may change the location)."
)


class _StartupOutputLimit(Exception):
    pass


async def _read_startup_output(reader: asyncio.StreamReader) -> bytes:
    output = bytearray()
    while chunk := await reader.read(8192):
        if len(output) + len(chunk) > STARTUP_OUTPUT_LIMIT:
            raise _StartupOutputLimit
        output.extend(chunk)
    return bytes(output)


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
            return failed_result(
                text=f"omp CLI not found: {self.config.omp_bin}",
                category="unavailable",
            )

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
            return timed_out_result(text=f"omp timed out after {timeout}s")
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
            return failed_result(
                text=f"omp CLI failed ({proc.returncode}): {detail[:500]}",
                category="process_error",
            )

        if not response_text:
            return failed_result(
                text="(no response)",
                category="no_response",
                session_id=session_id,
            )

        return AdapterResult(text=response_text, session_id=session_id)

    async def startup_check(self) -> dict:
        """Initialize RPC locally without sending a prompt or retaining a session.

        Unlike --version, this exercises OMP's runtime/SQLite initialization.
        It can write local runtime state. Optional extensions/tools are disabled;
        a successful probe is not a provider or per-job workspace acceptance.
        """
        cmd = [arg for arg in self._build_cmd(no_session=True)
               if arg not in ("-p", "--auto-approve")]
        cmd += [
            "--mode", "rpc", "--no-tools", "--no-extensions", "--no-skills",
            "--no-rules", "--no-lsp", "--no-title", "--no-pty",
        ]
        result = {
            "runtime_ready": False,
            "provider_checked": False,
            "reason_code": "runtime_startup_failed",
            "runtime_directory_hint": _RUNTIME_DIRECTORY_HINT,
        }
        proc = None
        cleanup_attempted = False

        async def cleanup() -> None:
            nonlocal cleanup_attempted
            if proc is not None and not cleanup_attempted:
                cleanup_attempted = True
                await terminate_owned_process_group(proc)

        async def exchange() -> tuple[bytes, bytes]:
            readers = [
                asyncio.create_task(_read_startup_output(proc.stdout)),
                asyncio.create_task(_read_startup_output(proc.stderr)),
                asyncio.create_task(proc.wait()),
            ]
            try:
                request = {"id": _STARTUP_REQUEST_ID, "type": "get_state"}
                try:
                    proc.stdin.write((json.dumps(request) + "\n").encode())
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    # A failed initialization may close stdin before reading it.
                    # Still drain its bounded stderr to classify the local failure.
                    pass
                finally:
                    proc.stdin.close()
                stdout, stderr, _ = await asyncio.gather(*readers)
                return stdout, stderr
            finally:
                for reader in readers:
                    if not reader.done():
                        reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.config.work_dir,
                env=filtered_env(cwd=self.config.work_dir),
                **async_subprocess_kwargs(),
            )
            stdout, stderr = await asyncio.wait_for(exchange(), STARTUP_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            await cleanup()
            raise
        except Exception as exc:
            if isinstance(exc, asyncio.TimeoutError):
                result["reason_code"] = "runtime_timeout"
            elif isinstance(exc, _StartupOutputLimit):
                result["reason_code"] = "runtime_output_limit"
            # Spawn errors also include a missing cwd. Only the separate
            # --version check can identify binary availability unambiguously.
            try:
                if proc is not None:
                    await cleanup()
            except Exception:
                result["reason_code"] = "runtime_cleanup_failed"
                log.error("OMP startup probe process cleanup failed")
            return result

        if proc.returncode != 0:
            if any(marker in stderr.lower() for marker in (
                b"sqlite_readonly", b"readonly database", b"read-only file system",
                b"eacces", b"erofs", b"permission denied",
            )):
                result["reason_code"] = "runtime_not_writable"
            return result

        # Never forward raw state or stderr: state can contain private provider,
        # session and configuration facts. Validate only the required RPC frames.
        ready = False
        responses = []
        try:
            for line in stdout.splitlines():
                if not line.strip():
                    continue
                frame = json.loads(line)
                if not isinstance(frame, dict):
                    raise ValueError("not an RPC object")
                if frame.get("type") == "ready":
                    ready = type(frame.get("protocolVersion")) is int and frame["protocolVersion"] == 1
                if frame.get("type") == "response" and frame.get("id") == _STARTUP_REQUEST_ID:
                    responses.append(frame)
            valid = (
                ready and len(responses) == 1
                and responses[0].get("command") == "get_state"
                and responses[0].get("success") is True
            )
        except (ValueError, UnicodeError, RecursionError):
            valid = False
        result["runtime_ready"] = bool(valid)
        result["reason_code"] = "ready" if valid else "runtime_protocol_error"
        return result

    async def health_check(self) -> dict:
        bin_path = self.config.omp_bin
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                bin_path, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=filtered_env(),
                **async_subprocess_kwargs(),
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            available = proc.returncode == 0
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                await terminate_owned_process_group(proc)
            raise
        except Exception:
            if proc is not None and proc.returncode is None:
                try:
                    await terminate_owned_process_group(proc)
                except Exception as cleanup_error:
                    log.warning(
                        "omp health-check cleanup failed: %s",
                        type(cleanup_error).__name__,
                    )
            available = False

        found = shutil.which(bin_path)
        runtime = await self.startup_check() if available else {
            "runtime_ready": False,
            "provider_checked": False,
            "reason_code": "binary_unavailable",
            "runtime_directory_hint": _RUNTIME_DIRECTORY_HINT,
        }
        return {
            "adapter": "omp",
            "bin": bin_path,
            "binary_available": available,
            "available": available and runtime["runtime_ready"] is True,
            "path": found,
            **runtime,
        }
