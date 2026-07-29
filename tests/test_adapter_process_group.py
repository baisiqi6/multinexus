"""Regression tests for multinexus.adapters.utils process-group helpers.

Covers:
- async_subprocess_kwargs: POSIX `start_new_session=True`
- terminate_owned_process_group: real tree termination + cancellation/SIGKILL path
"""

import asyncio
import os
import signal
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from multinexus.adapters import utils

_IS_POSIX = sys.platform != "win32"
_NEEDS_POSIX = unittest.skipUnless(_IS_POSIX, "POSIX-only test")

# ---------------------------------------------------------------------------
# Child scripts executed as subprocess trees
# ---------------------------------------------------------------------------

# Spawns a grandchild, prints "child_pid:gc_pid", then waits.
_CHILD_TREE_SCRIPT = (
    "import os,sys,subprocess,time;"
    'gc=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);'
    "print(f'{os.getpid()}:{gc.pid}',flush=True);"
    "gc.wait()"
)

# Spawns a grandchild; both parent and grandchild ignore SIGTERM.
_CHILD_SIGTERM_IGNORE_SCRIPT = (
    "import os,sys,signal,subprocess,time;"
    "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
    'gc=subprocess.Popen([sys.executable,"-c",'
    '"import signal,time;'
    'signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"]);'
    "print(f'{os.getpid()}:{gc.pid}',flush=True);"
    "gc.wait()"
)


class AsyncSubprocessKwargsTests(unittest.TestCase):
    """Unit tests for async_subprocess_kwargs()."""

    def test_returns_dict(self):
        self.assertIsInstance(utils.async_subprocess_kwargs(), dict)

    @_NEEDS_POSIX
    def test_posix_spawn_kwargs_includes_start_new_session(self):
        kwargs = utils.async_subprocess_kwargs()
        self.assertTrue(kwargs["start_new_session"])


class _FakeWindowsProcess:
    def __init__(self, *, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.wait_calls = 0

    async def wait(self) -> int:
        self.wait_calls += 1
        self.returncode = -9
        return self.returncode


class WindowsTerminateProcessGroupTests(unittest.IsolatedAsyncioTestCase):
    async def test_taskkill_success_uses_tree_force_flags_and_waits(self):
        proc = _FakeWindowsProcess()
        completed = SimpleNamespace(returncode=0)

        with (
            patch.object(utils, "IS_WIN", True),
            patch.object(
                utils.subprocess,
                "CREATE_NO_WINDOW",
                0x08000000,
                create=True,
            ),
            patch.object(utils.subprocess, "run", return_value=completed) as run,
        ):
            await utils.terminate_owned_process_group(proc, kill_timeout=0.5)

        run.assert_called_once_with(
            ["taskkill", "/PID", "4242", "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
            timeout=0.5,
            creationflags=0x08000000,
        )
        self.assertEqual(proc.wait_calls, 1)
        self.assertEqual(proc.returncode, -9)

    async def test_taskkill_failure_is_safe_and_does_not_wait(self):
        proc = _FakeWindowsProcess()
        completed = SimpleNamespace(
            returncode=5,
            stdout="sensitive stdout",
            stderr="sensitive stderr",
        )

        with (
            patch.object(utils, "IS_WIN", True),
            patch.object(
                utils.subprocess,
                "CREATE_NO_WINDOW",
                0x08000000,
                create=True,
            ),
            patch.object(utils.subprocess, "run", return_value=completed),
        ):
            with self.assertRaises(RuntimeError) as raised:
                await utils.terminate_owned_process_group(proc, kill_timeout=0.5)

        message = str(raised.exception)
        self.assertIn("returncode 5", message)
        self.assertNotIn("sensitive stdout", message)
        self.assertNotIn("sensitive stderr", message)
        self.assertEqual(proc.wait_calls, 0)

    async def test_cancellation_waits_for_same_windows_cleanup_task(self):
        proc = _FakeWindowsProcess()
        entered = asyncio.Event()
        release = asyncio.Event()
        cleanup_finished = asyncio.Event()

        async def controlled_to_thread(func, *args, **kwargs):
            self.assertIs(func, utils.subprocess.run)
            entered.set()
            await release.wait()
            cleanup_finished.set()
            return SimpleNamespace(returncode=0)

        with (
            patch.object(utils, "IS_WIN", True),
            patch.object(
                utils.subprocess,
                "CREATE_NO_WINDOW",
                0x08000000,
                create=True,
            ),
            patch.object(utils.asyncio, "to_thread", new=controlled_to_thread),
        ):
            task = asyncio.create_task(
                utils.terminate_owned_process_group(proc, kill_timeout=0.5)
            )
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            task.cancel()
            release.set()

            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1.0)

        self.assertTrue(cleanup_finished.is_set())
        self.assertEqual(proc.wait_calls, 1)


class TerminateProcessGroupTests(unittest.IsolatedAsyncioTestCase):
    """Integration / regression tests for terminate_owned_process_group()."""

    async def asyncSetUp(self):
        self._proc: asyncio.subprocess.Process | None = None
        self._pids: list[int] = []

    async def asyncTearDown(self):
        """Backstop leak guard (runs even if test finally somehow skipped)."""
        await self._cleanup_leaks()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # PID exists but we lack permission to signal it.
            return True

    async def _cleanup_leaks(self) -> None:
        """Best-effort cleanup: kill process group, wait leader, nuke known PIDs."""
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
            except Exception:
                pass
        for pid in self._pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass

    async def _spawn_tree(
        self, script: str
    ) -> asyncio.subprocess.Process:
        """Spawn a child with *script*, read its PID line, return proc."""
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **utils.async_subprocess_kwargs(),
        )
        self._proc = proc
        stdout_bytes = await asyncio.wait_for(
            proc.stdout.readline(), timeout=5.0
        )
        line = stdout_bytes.decode().strip()
        if not line:
            raise RuntimeError("child produced no output")
        child_pid_str, gc_pid_str = line.split(":", 1)
        self._pids = [int(child_pid_str), int(gc_pid_str)]
        return proc

    # ------------------------------------------------------------------
    # test cases
    # ------------------------------------------------------------------

    @_NEEDS_POSIX
    async def test_terminates_real_process_tree(self):
        """Spawn tree → terminate → leader reaped, all PIDs gone."""
        proc = await self._spawn_tree(_CHILD_TREE_SCRIPT)
        child_pid, gc_pid = self._pids

        self.assertTrue(self._pid_exists(child_pid),
                        f"child PID {child_pid} should exist before termination")
        self.assertTrue(self._pid_exists(gc_pid),
                        f"grandchild PID {gc_pid} should exist before termination")

        try:
            await utils.terminate_owned_process_group(
                proc, terminate_timeout=1.0, kill_timeout=1.0
            )

            # Leader reaped
            self.assertIsNotNone(proc.returncode,
                                 "process should have been waited/reaped")

            # Both PIDs gone
            self.assertFalse(self._pid_exists(child_pid),
                             f"child PID {child_pid} still exists")
            self.assertFalse(self._pid_exists(gc_pid),
                             f"grandchild PID {gc_pid} still exists")
        finally:
            await self._cleanup_leaks()

    @_NEEDS_POSIX
    async def test_transient_permission_denial_does_not_abort_cleanup(self):
        """A denied liveness probe is retried rather than treated as gone."""
        proc = await self._spawn_tree(_CHILD_TREE_SCRIPT)
        real_killpg = os.killpg
        denied_once = False

        def flaky_killpg(pgid: int, sig: int) -> None:
            nonlocal denied_once
            if sig == 0 and not denied_once:
                denied_once = True
                raise PermissionError("transient process-group probe denial")
            real_killpg(pgid, sig)

        try:
            with patch.object(utils.os, "killpg", side_effect=flaky_killpg):
                await utils.terminate_owned_process_group(
                    proc, terminate_timeout=1.0, kill_timeout=1.0
                )

            self.assertTrue(denied_once)
            self.assertIsNotNone(proc.returncode)
            for pid in self._pids:
                self.assertFalse(self._pid_exists(pid), f"PID {pid} survived cleanup")
        finally:
            await self._cleanup_leaks()

    @_NEEDS_POSIX
    async def test_cancellation_sigkill_cleanup_and_propagate(self):
        """Cancel during termination → SIGKILL cleanup → CancelledError."""
        proc = await self._spawn_tree(_CHILD_SIGTERM_IGNORE_SCRIPT)
        child_pid, gc_pid = self._pids

        self.assertTrue(self._pid_exists(child_pid))
        self.assertTrue(self._pid_exists(gc_pid))

        try:
            # Start termination in a task so we can cancel it mid-flight.
            term_task = asyncio.create_task(
                utils.terminate_owned_process_group(
                    proc, terminate_timeout=0.5, kill_timeout=1.0
                )
            )

            # Give _cleanup time to send SIGTERM before we cancel.
            await asyncio.sleep(0.2)

            term_task.cancel()

            # Helper must complete SIGKILL cleanup, then re-raise.
            with self.assertRaises(asyncio.CancelledError):
                await term_task

            self.assertFalse(self._pid_exists(child_pid),
                             f"child PID {child_pid} survived SIGKILL")
            self.assertFalse(self._pid_exists(gc_pid),
                             f"grandchild PID {gc_pid} survived SIGKILL")
        finally:
            await self._cleanup_leaks()
