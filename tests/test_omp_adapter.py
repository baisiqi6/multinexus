import asyncio
import unittest
from unittest.mock import patch

from multinexus.adapters.base import AdapterResult
from multinexus.adapters.factory import make_adapter
from multinexus.adapters.omp import OmpAdapter
from multinexus.models import AgentConfig


class FakeProcess:
    def __init__(
        self,
        stdout: bytes = b"omp response\n",
        stderr: bytes = b"",
        returncode: int = 0,
    ):
        self.stdout_data = stdout
        self.stderr_data = stderr
        self.returncode = returncode
        self.killed = False

    async def communicate(self, _input=None):
        return self.stdout_data, self.stderr_data

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def _make_config(**overrides) -> AgentConfig:
    values = {
        "id": "mac-omp",
        "token": "fake-token",
        "adapter": "omp",
        "timeout": 30,
        "omp_bin": "omp",
    }
    values.update(overrides)
    return AgentConfig(**values)


class TestOmpBuildCmd(unittest.TestCase):
    def test_auto_approve_present_by_default(self):
        config = _make_config()
        adapter = OmpAdapter(config)
        cmd = adapter._build_cmd()
        self.assertIn("--auto-approve", cmd)

    def test_auto_approve_omitted_when_disabled(self):
        config = _make_config(omp_auto_approve=False)
        adapter = OmpAdapter(config)
        cmd = adapter._build_cmd()
        self.assertNotIn("--auto-approve", cmd)

    def test_model_included_when_set(self):
        config = _make_config(omp_model="opus")
        adapter = OmpAdapter(config)
        cmd = adapter._build_cmd()
        self.assertIn("--model", cmd)
        self.assertIn("opus", cmd)

    def test_no_model_flag_when_unset(self):
        config = _make_config()
        adapter = OmpAdapter(config)
        cmd = adapter._build_cmd()
        self.assertNotIn("--model", cmd)

    def test_thinking_included_when_set(self):
        config = _make_config(omp_thinking="high")
        adapter = OmpAdapter(config)
        cmd = adapter._build_cmd()
        self.assertIn("--thinking", cmd)
        self.assertIn("high", cmd)

    def test_no_thinking_flag_when_unset(self):
        config = _make_config()
        adapter = OmpAdapter(config)
        cmd = adapter._build_cmd()
        self.assertNotIn("--thinking", cmd)

    def test_resume_uses_session_id(self):
        config = _make_config()
        adapter = OmpAdapter(config)
        cmd = adapter._build_cmd(resume_session_id="abc-123")
        self.assertIn("--resume", cmd)
        idx = cmd.index("--resume")
        self.assertEqual(cmd[idx + 1], "abc-123")


class TestOmpCall(unittest.IsolatedAsyncioTestCase):
    async def test_basic_response(self):
        spawn_kwargs = {}

        async def fake_exec(*args, **kwargs):
            spawn_kwargs.update(kwargs)
            return FakeProcess(stdout=b"Hello from omp\n")

        adapter = OmpAdapter(_make_config())
        with (
            patch("multinexus.adapters.omp.asyncio.create_subprocess_exec", new=fake_exec),
            patch(
                "multinexus.adapters.omp.async_subprocess_kwargs",
                return_value={"start_new_session": True},
            ),
        ):
            result = await adapter.call("test prompt")

        self.assertIsInstance(result, AdapterResult)
        self.assertEqual(result.text, "Hello from omp")
        self.assertFalse(result.resumed)
        self.assertIs(spawn_kwargs["start_new_session"], True)

    async def test_empty_stdout_returns_no_response(self):
        async def fake_exec(*args, **kwargs):
            return FakeProcess(stdout=b"   \n")

        adapter = OmpAdapter(_make_config())
        with patch("multinexus.adapters.omp.asyncio.create_subprocess_exec", new=fake_exec):
            result = await adapter.call("test")

        self.assertEqual(result.text, "(no response)")

    async def test_nonzero_exit_returns_stderr(self):
        async def fake_exec(*args, **kwargs):
            return FakeProcess(stdout=b"", stderr=b"config error", returncode=2)

        adapter = OmpAdapter(_make_config())
        with patch("multinexus.adapters.omp.asyncio.create_subprocess_exec", new=fake_exec):
            result = await adapter.call("test")

        self.assertIn("omp CLI failed (2)", result.text)
        self.assertIn("config error", result.text)

    async def test_nonzero_exit_with_stdout_returns_error(self):
        proc = FakeProcess(returncode=1, stdout=b"partial output\n", stderr=b"fatal error")

        async def fake_exec(*args, **kwargs):
            return proc

        adapter = OmpAdapter(_make_config())
        with patch("multinexus.adapters.omp.asyncio.create_subprocess_exec", new=fake_exec):
            result = await adapter.call("test")

        self.assertIn("omp CLI failed (1)", result.text)
        self.assertIn("fatal error", result.text)


class TestOmpResume(unittest.IsolatedAsyncioTestCase):
    async def test_resume_sets_flag(self):
        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            return FakeProcess(stdout=b"resumed\n")

        adapter = OmpAdapter(_make_config())
        with patch("multinexus.adapters.omp.asyncio.create_subprocess_exec", new=fake_exec):
            result = await adapter.resume("sess-1", "continue")

        self.assertTrue(result.resumed)
        self.assertEqual(result.session_id, "sess-1")
        cmd_args = calls[0]
        self.assertIn("--resume", cmd_args)


class TestOmpMissingCLI(unittest.IsolatedAsyncioTestCase):
    async def test_file_not_found(self):
        async def fake_exec(*args, **kwargs):
            raise FileNotFoundError

        adapter = OmpAdapter(_make_config(omp_bin="/no/such/omp"))
        with patch("multinexus.adapters.omp.asyncio.create_subprocess_exec", new=fake_exec):
            result = await adapter.call("test")

        self.assertIn("omp CLI not found", result.text)
        self.assertIn("/no/such/omp", result.text)


class TestOmpTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_kills_process(self):
        proc = FakeProcess()

        async def hanging_communicate(_input=None):
            raise asyncio.TimeoutError

        proc.communicate = hanging_communicate

        async def fake_exec(*args, **kwargs):
            return proc

        cleanup_calls = []

        async def fake_cleanup(target):
            cleanup_calls.append(target)
            target.kill()
            await target.wait()

        adapter = OmpAdapter(_make_config(timeout=5))
        with (
            patch("multinexus.adapters.omp.asyncio.create_subprocess_exec", new=fake_exec),
            patch(
                "multinexus.adapters.omp.terminate_owned_process_group",
                new=fake_cleanup,
            ),
        ):
            result = await adapter.call("test")

        self.assertIn("timed out after 5s", result.text)
        self.assertTrue(proc.killed)
        self.assertEqual(cleanup_calls, [proc])

    async def test_cancellation_kills_process(self):
        proc = FakeProcess()
        started = asyncio.Event()

        async def hanging_communicate(_input=None):
            started.set()
            await asyncio.Event().wait()

        proc.communicate = hanging_communicate

        async def fake_exec(*args, **kwargs):
            return proc

        cleanup_calls = []

        async def fake_cleanup(target):
            cleanup_calls.append(target)
            target.kill()
            await target.wait()

        adapter = OmpAdapter(_make_config())
        with (
            patch("multinexus.adapters.omp.asyncio.create_subprocess_exec", new=fake_exec),
            patch(
                "multinexus.adapters.omp.terminate_owned_process_group",
                new=fake_cleanup,
            ),
        ):
            task = asyncio.create_task(adapter.call("test"))
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(proc.killed)
        self.assertEqual(cleanup_calls, [proc])

    async def test_cleanup_failure_is_explicit_and_not_retried(self):
        proc = FakeProcess()
        cleanup_calls = []

        async def hanging_communicate(_input=None):
            raise asyncio.TimeoutError

        proc.communicate = hanging_communicate

        async def fake_exec(*args, **kwargs):
            return proc

        async def failing_cleanup(target):
            cleanup_calls.append(target)
            raise RuntimeError("process group cleanup failed")

        adapter = OmpAdapter(_make_config(timeout=5))
        with (
            patch("multinexus.adapters.omp.asyncio.create_subprocess_exec", new=fake_exec),
            patch(
                "multinexus.adapters.omp.terminate_owned_process_group",
                new=failing_cleanup,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "process group cleanup failed"):
                await adapter.call("test")

        self.assertEqual(cleanup_calls, [proc])


class TestOmpHealthCheck(unittest.IsolatedAsyncioTestCase):
    async def test_found(self):
        proc = FakeProcess()

        async def fake_exec(*args, **kwargs):
            return proc

        adapter = OmpAdapter(_make_config(omp_bin="omp"))
        with (
            patch("multinexus.adapters.omp.shutil.which", return_value="/usr/local/bin/omp"),
            patch("multinexus.adapters.omp.asyncio.create_subprocess_exec", new=fake_exec),
        ):
            result = await adapter.health_check()

        self.assertEqual(result["adapter"], "omp")
        self.assertEqual(result["bin"], "omp")
        self.assertTrue(result["available"])
        self.assertEqual(result["path"], "/usr/local/bin/omp")

    async def test_not_found(self):
        async def fake_exec(*args, **kwargs):
            raise FileNotFoundError

        adapter = OmpAdapter(_make_config(omp_bin="omp"))
        with (
            patch("multinexus.adapters.omp.shutil.which", return_value=None),
            patch("multinexus.adapters.omp.asyncio.create_subprocess_exec", new=fake_exec),
        ):
            result = await adapter.health_check()

        self.assertFalse(result["available"])


class TestOmpFactory(unittest.TestCase):
    def test_make_adapter_returns_omp(self):
        config = _make_config()
        adapter = make_adapter(config)
        self.assertIsInstance(adapter, OmpAdapter)


if __name__ == "__main__":
    unittest.main()
