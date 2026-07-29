import asyncio
import os
import signal
import subprocess
import sys
from typing import Any

IS_WIN = sys.platform == "win32"
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {}


def async_subprocess_kwargs() -> dict[str, Any]:
    """Return kwargs for asyncio.create_subprocess_exec to own a process group.

    On POSIX this starts a new session so the child becomes a process group
    leader. On Windows a new process group is created with no console window.
    """
    if IS_WIN:
        return {
            "creationflags": subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP,
        }
    return {"start_new_session": True}


def filtered_env(*, cwd: str | None = None) -> dict[str, str]:
    """Strip message-bus secrets so spawned agents cannot echo bot tokens."""
    strip_prefixes = ("KOOK_", "DISCORD_")
    env = {
        key: value
        for key, value in os.environ.items()
        if not any(key.startswith(prefix) for prefix in strip_prefixes)
    }
    if cwd and not IS_WIN:
        env["PWD"] = cwd
    return env


async def terminate_owned_process_group(
    proc: asyncio.subprocess.Process,
    *,
    terminate_timeout: float = 2.0,
    kill_timeout: float = 2.0,
) -> None:
    """Terminate an owned process group created by async_subprocess_kwargs.

    On POSIX, signal the whole group and escalate to SIGKILL. On Windows,
    terminate the process tree with taskkill. Always wait for the leader.
    """
    pgid = proc.pid
    if pgid is None:
        raise RuntimeError("process has no pid")

    async def _group_alive() -> bool:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # A permission denial is not proof that the group is gone.  Keep
            # polling so a transient macOS group state can settle; if it does
            # not, the bounded timeout fails closed with the normal survivor
            # error instead of leaking a raw platform exception.
            return True

    async def _wait_group_gone(timeout: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while await _group_alive():
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    async def _cleanup() -> None:
        if IS_WIN:
            try:
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/PID", str(pgid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=kill_timeout,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    f"taskkill not found while terminating pid {pgid}"
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    f"taskkill timed out while terminating pid {pgid}"
                ) from exc

            if result.returncode != 0:
                raise RuntimeError(
                    f"taskkill failed for pid {pgid} with returncode "
                    f"{result.returncode}"
                )

            await asyncio.wait_for(proc.wait(), timeout=kill_timeout)
            if proc.returncode is None:
                raise RuntimeError(f"process leader {pgid} did not exit")
            return

        # Phase 1: graceful termination of the whole group.
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        gone = await _wait_group_gone(terminate_timeout)

        # Phase 2: escalate only if the group is still alive.
        if not gone:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if not await _wait_group_gone(kill_timeout):
                raise RuntimeError(f"process group {pgid} survived SIGKILL")

        # Phase 3: reap the leader.
        await asyncio.wait_for(proc.wait(), timeout=kill_timeout)

    cleanup_task = asyncio.create_task(_cleanup())
    try:
        await asyncio.shield(cleanup_task)
    except asyncio.CancelledError:
        await asyncio.shield(cleanup_task)
        raise
