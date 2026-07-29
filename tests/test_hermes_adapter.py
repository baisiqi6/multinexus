import asyncio
import logging
import unittest
from unittest.mock import patch

from multinexus.adapters.base import AdapterResult
from multinexus.adapters.hermes import HermesAdapter
from multinexus.models import AgentConfig

logging.getLogger("multinexus.adapters.hermes").setLevel(logging.CRITICAL)


class FakeProcess:
    def __init__(
        self,
        stdout: bytes = b"hermes response\n",
        stderr: bytes = b"",
        returncode: int = 0,
    ):
        self.stdout_data = stdout
        self.stderr_data = stderr
        self.returncode = returncode
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout_data, self.stderr_data

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


def _make_config(**overrides) -> AgentConfig:
    values = {
        "id": "server-hermes",
        "token": "fake-token",
        "adapter": "hermes",
        "timeout": 30,
        "hermes_bin": "hermes",
    }
    values.update(overrides)
    return AgentConfig(**values)


class TestHermesSuccess(unittest.IsolatedAsyncioTestCase):
    async def test_basic_response(self):
        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeProcess(stdout=b"Hello from Hermes\n")

        adapter = HermesAdapter(_make_config())
        with (
            patch("multinexus.adapters.hermes.asyncio.create_subprocess_exec", new=fake_exec),
            patch(
                "multinexus.adapters.hermes.async_subprocess_kwargs",
                return_value={"start_new_session": True},
            ),
        ):
            result = await adapter.call("test prompt")

        self.assertIsInstance(result, AdapterResult)
        self.assertEqual(result.text, "Hello from Hermes")
        self.assertIs(calls[0][1]["start_new_session"], True)

    async def test_empty_stdout_returns_no_response(self):
        async def fake_exec(*args, **kwargs):
            return FakeProcess(stdout=b"   \n")

        adapter = HermesAdapter(_make_config())
        with patch("multinexus.adapters.hermes.asyncio.create_subprocess_exec", new=fake_exec):
            result = await adapter.call("test")

        self.assertIsInstance(result, AdapterResult)
        self.assertEqual(result.text, "(no response)")


class TestHermesFailure(unittest.IsolatedAsyncioTestCase):
    async def test_nonzero_exit_returns_stderr(self):
        async def fake_exec(*args, **kwargs):
            return FakeProcess(stderr=b"config error", returncode=2)

        adapter = HermesAdapter(_make_config())
        with patch("multinexus.adapters.hermes.asyncio.create_subprocess_exec", new=fake_exec):
            result = await adapter.call("test")

        self.assertIn("Hermes CLI failed (2)", result.text)
        self.assertIn("config error", result.text)

    async def test_nonzero_exit_falls_back_to_stdout(self):
        async def fake_exec(*args, **kwargs):
            return FakeProcess(stdout=b"some output", stderr=b"", returncode=1)

        adapter = HermesAdapter(_make_config())
        with patch("multinexus.adapters.hermes.asyncio.create_subprocess_exec", new=fake_exec):
            result = await adapter.call("test")

        self.assertIn("Hermes CLI failed (1)", result.text)
        self.assertIn("some output", result.text)


class TestHermesMissingCLI(unittest.IsolatedAsyncioTestCase):
    async def test_file_not_found(self):
        adapter = HermesAdapter(_make_config(hermes_bin="/no/such/hermes"))

        async def fake_exec(*args, **kwargs):
            raise FileNotFoundError

        with patch("multinexus.adapters.hermes.asyncio.create_subprocess_exec", new=fake_exec):
            result = await adapter.call("test")

        self.assertIn("Hermes CLI not found", result.text)
        self.assertIn("/no/such/hermes", result.text)


class TestHermesTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_kills_process(self):
        proc = FakeProcess()
        # Make communicate raise TimeoutError

        async def hanging_communicate():
            raise asyncio.TimeoutError

        proc.communicate = hanging_communicate

        async def fake_exec(*args, **kwargs):
            return proc

        cleanup_calls = []

        async def fake_cleanup(target):
            cleanup_calls.append(target)
            target.kill()
            await target.wait()

        adapter = HermesAdapter(_make_config(timeout=5))
        with (
            patch("multinexus.adapters.hermes.asyncio.create_subprocess_exec", new=fake_exec),
            patch(
                "multinexus.adapters.hermes.terminate_owned_process_group",
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

        async def hanging_communicate():
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

        adapter = HermesAdapter(_make_config())
        with (
            patch("multinexus.adapters.hermes.asyncio.create_subprocess_exec", new=fake_exec),
            patch(
                "multinexus.adapters.hermes.terminate_owned_process_group",
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

        async def hanging_communicate():
            raise asyncio.TimeoutError

        proc.communicate = hanging_communicate

        async def fake_exec(*args, **kwargs):
            return proc

        async def failing_cleanup(target):
            cleanup_calls.append(target)
            raise RuntimeError("process group cleanup failed")

        adapter = HermesAdapter(_make_config(timeout=5))
        with (
            patch("multinexus.adapters.hermes.asyncio.create_subprocess_exec", new=fake_exec),
            patch(
                "multinexus.adapters.hermes.terminate_owned_process_group",
                new=failing_cleanup,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "process group cleanup failed"):
                await adapter.call("test")

        self.assertEqual(cleanup_calls, [proc])


class TestHermesArgConstruction(unittest.IsolatedAsyncioTestCase):
    async def test_all_flags_present(self):
        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeProcess(stdout=b"ok\n")

        config = _make_config(
            model="openrouter/test",
            hermes_provider="openrouter",
            hermes_toolsets="search,browser",
            hermes_accept_hooks=True,
            work_dir="/home/ubuntu/projects",
        )
        adapter = HermesAdapter(config)
        with patch("multinexus.adapters.hermes.asyncio.create_subprocess_exec", new=fake_exec):
            await adapter.call("do stuff")

        args, kwargs = calls[0]
        self.assertEqual(args[0], "hermes")
        self.assertIn("--model", args)
        self.assertIn("openrouter/test", args)
        self.assertIn("--provider", args)
        self.assertIn("openrouter", args)
        self.assertIn("--toolsets", args)
        self.assertIn("search,browser", args)
        self.assertIn("--accept-hooks", args)
        self.assertIn("-z", args)
        self.assertEqual(kwargs["cwd"], "/home/ubuntu/projects")

    async def test_no_optional_flags(self):
        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeProcess(stdout=b"ok\n")

        adapter = HermesAdapter(_make_config())
        with patch("multinexus.adapters.hermes.asyncio.create_subprocess_exec", new=fake_exec):
            await adapter.call("hello")

        args, _kwargs = calls[0]
        self.assertNotIn("--model", args)
        self.assertNotIn("--provider", args)
        self.assertNotIn("--toolsets", args)
        self.assertNotIn("--accept-hooks", args)

    async def test_work_dir_override_in_call(self):
        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeProcess(stdout=b"ok\n")

        adapter = HermesAdapter(_make_config(work_dir="/default"))
        with patch("multinexus.adapters.hermes.asyncio.create_subprocess_exec", new=fake_exec):
            await adapter.call("test", work_dir="/override")

        _, kwargs = calls[0]
        self.assertEqual(kwargs["cwd"], "/override")


class TestHermesSystemPrompt(unittest.IsolatedAsyncioTestCase):
    async def test_system_prompt_prepended(self):
        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeProcess(stdout=b"ok\n")

        adapter = HermesAdapter(_make_config(system_prompt="You are Hermes."))
        with patch("multinexus.adapters.hermes.asyncio.create_subprocess_exec", new=fake_exec):
            await adapter.call("hello")

        args, _kwargs = calls[0]
        # -z is second to last, prompt is last
        z_idx = args.index("-z")
        prompt_arg = args[z_idx + 1]
        self.assertTrue(prompt_arg.startswith("You are Hermes."))
        self.assertIn("hello", prompt_arg)

    async def test_no_system_prompt_passes_raw(self):
        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeProcess(stdout=b"ok\n")

        adapter = HermesAdapter(_make_config(system_prompt=""))
        with patch("multinexus.adapters.hermes.asyncio.create_subprocess_exec", new=fake_exec):
            await adapter.call("just the prompt")

        args, _kwargs = calls[0]
        z_idx = args.index("-z")
        self.assertEqual(args[z_idx + 1], "just the prompt")


class TestHermesHealthCheck(unittest.IsolatedAsyncioTestCase):
    async def test_found(self):
        adapter = HermesAdapter(_make_config(hermes_bin="hermes"))
        with patch("multinexus.adapters.hermes.shutil.which", return_value="/usr/bin/hermes"):
            result = await adapter.health_check()

        self.assertEqual(result["adapter"], "hermes")
        self.assertTrue(result["available"])
        self.assertEqual(result["path"], "/usr/bin/hermes")

    async def test_not_found(self):
        adapter = HermesAdapter(_make_config(hermes_bin="hermes"))
        with patch("multinexus.adapters.hermes.shutil.which", return_value=None):
            result = await adapter.health_check()

        self.assertFalse(result["available"])


if __name__ == "__main__":
    unittest.main()
