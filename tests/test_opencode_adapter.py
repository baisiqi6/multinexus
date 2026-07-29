import asyncio
import json
import unittest
from unittest.mock import patch

from multinexus.adapters.opencode import OpenCodeAdapter
from multinexus.models import AgentConfig


class _FakeStream:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        if not self._lines:
            return b""
        return self._lines.pop(0)

    async def read(self):
        return b""


class _HangingStream:
    def __init__(self, started: asyncio.Event | None = None):
        self._started = started

    async def readline(self):
        if self._started is not None:
            self._started.set()
        try:
            await asyncio.Event().wait()
        finally:
            # Ensure the started event is set even if cancellation races before
            # the first await point, so callers never block forever.
            if self._started is not None:
                self._started.set()

    async def read(self):
        return b""


class _FakeStdin:
    def write(self, data):
        self.data = data

    async def drain(self):
        return None

    def close(self):
        return None


class _FakeProcess:
    def __init__(self, events, returncode=0, hang=False, started: asyncio.Event | None = None):
        self.stdin = _FakeStdin()
        if hang:
            self.stdout = _HangingStream(started=started)
        else:
            self.stdout = _FakeStream(
                [(json.dumps(event) + "\n").encode("utf-8") for event in events]
            )
        self.stderr = _FakeStream([])
        self.returncode = returncode
        self.killed = False

    async def wait(self):
        return self.returncode

    def kill(self):
        self.killed = True
        if self.returncode is None:
            self.returncode = -9


def _config(**overrides):
    values = {
        "id": "win-opencode",
        "token": "",
        "adapter": "opencode",
        "opencode_bin": "opencode",
        "work_dir": ".",
        "timeout": 30,
        "first_byte_timeout": 5,
        "activity_timeout": 5,
    }
    values.update(overrides)
    return AgentConfig(**values)


class OpenCodeAdapterRetryTests(unittest.TestCase):
    def test_retries_empty_success_without_tool_use(self):
        calls = []
        processes = [
            _FakeProcess([{"type": "step_start", "sessionID": "s1"}]),
            _FakeProcess(
                [
                    {"type": "step_start", "sessionID": "s2"},
                    {"type": "text", "part": {"text": "ok"}},
                ]
            ),
        ]

        async def fake_create(*args, **kwargs):
            calls.append((args, kwargs))
            return processes.pop(0)

        async def run():
            adapter = OpenCodeAdapter(_config())
            with patch("multinexus.adapters.opencode.asyncio.create_subprocess_exec", fake_create):
                return await adapter.call("reply ok")

        result = asyncio.run(run())

        self.assertEqual(result.text, "ok")
        self.assertEqual(len(calls), 2)

    def test_does_not_retry_empty_success_after_tool_use(self):
        calls = []
        processes = [
            _FakeProcess(
                [
                    {"type": "step_start", "sessionID": "s1"},
                    {"type": "tool_use", "part": {"tool": "edit"}},
                ]
            )
        ]

        async def fake_create(*args, **kwargs):
            calls.append((args, kwargs))
            return processes.pop(0)

        async def run():
            adapter = OpenCodeAdapter(_config())
            with patch("multinexus.adapters.opencode.asyncio.create_subprocess_exec", fake_create):
                return await adapter.call("reply ok")

        result = asyncio.run(run())

        self.assertEqual(
            result.text,
            "OpenCode returned no text (events=step_start,tool_use)",
        )
        self.assertEqual(len(calls), 1)

    def test_empty_success_eventually_fails_after_retries(self):
        calls = []
        processes = [
            _FakeProcess([{"type": "step_start", "sessionID": f"s{i}"}])
            for i in range(5)
        ]

        async def fake_create(*args, **kwargs):
            calls.append((args, kwargs))
            return processes.pop(0)

        async def run():
            adapter = OpenCodeAdapter(_config())
            with patch("multinexus.adapters.opencode.asyncio.create_subprocess_exec", fake_create):
                return await adapter.call("reply ok")

        result = asyncio.run(run())

        self.assertEqual(result.text, "OpenCode returned no text (events=step_start)")
        self.assertEqual(len(calls), 5)


class OpenCodeAdapterCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_kills_subprocess(self):
        started = asyncio.Event()
        proc = _FakeProcess([], hang=True, returncode=None, started=started)

        async def fake_create(*args, **kwargs):
            return proc

        cleanup_calls = []

        async def fake_cleanup(target):
            cleanup_calls.append(target)
            target.kill()
            await target.wait()

        adapter = OpenCodeAdapter(_config())
        with (
            patch(
                "multinexus.adapters.opencode.asyncio.create_subprocess_exec", fake_create
            ),
            patch(
                "multinexus.adapters.opencode.terminate_owned_process_group",
                new=fake_cleanup,
            ),
        ):
            task = asyncio.create_task(adapter.call("hello"))
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(proc.killed)
        self.assertEqual(cleanup_calls, [proc])

    async def test_spawn_kwargs_and_first_byte_timeout_cleanup(self):
        proc = _FakeProcess([], hang=True, returncode=None)
        spawn_kwargs = {}
        cleanup_calls = []

        async def fake_create(*args, **kwargs):
            spawn_kwargs.update(kwargs)
            return proc

        async def fake_cleanup(target):
            cleanup_calls.append(target)
            target.kill()
            await target.wait()

        adapter = OpenCodeAdapter(_config(first_byte_timeout=0))
        with (
            patch(
                "multinexus.adapters.opencode.asyncio.create_subprocess_exec", fake_create
            ),
            patch(
                "multinexus.adapters.opencode.async_subprocess_kwargs",
                return_value={"start_new_session": True},
            ),
            patch(
                "multinexus.adapters.opencode.terminate_owned_process_group",
                new=fake_cleanup,
            ),
        ):
            result = await adapter.call("hello")

        self.assertIs(spawn_kwargs["start_new_session"], True)
        self.assertIs(spawn_kwargs["stdin"], asyncio.subprocess.PIPE)
        self.assertEqual(cleanup_calls, [proc])
        self.assertIn("no output", result.text)

    async def test_cleanup_failure_is_explicit_and_not_retried(self):
        proc = _FakeProcess([], hang=True, returncode=None)
        cleanup_calls = []

        async def fake_create(*args, **kwargs):
            return proc

        async def failing_cleanup(target):
            cleanup_calls.append(target)
            raise RuntimeError("process group cleanup failed")

        adapter = OpenCodeAdapter(_config(first_byte_timeout=0))
        with (
            patch(
                "multinexus.adapters.opencode.asyncio.create_subprocess_exec", fake_create
            ),
            patch(
                "multinexus.adapters.opencode.terminate_owned_process_group",
                new=failing_cleanup,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "process group cleanup failed"):
                await adapter.call("hello")

        self.assertEqual(cleanup_calls, [proc])


if __name__ == "__main__":
    unittest.main()
