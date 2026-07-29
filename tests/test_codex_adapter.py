import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from multinexus.adapters.codex import CodexAdapter
from multinexus.config import _load_toml_agent
from multinexus.models import AgentConfig


class _FakeStream:
    def __init__(self, lines=None, *, hang=False):
        self._lines = list(lines or [])
        self._hang = hang

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        if self._hang:
            await asyncio.Event().wait()
        return b""

    async def read(self):
        return b""


class _FakeStdin:
    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data

    async def drain(self):
        return None

    def close(self):
        return None


class _FakeProcess:
    def __init__(self, events=None, *, hang=False, returncode=0):
        lines = [(json.dumps(event) + "\n").encode("utf-8") for event in (events or [])]
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(lines, hang=hang)
        self.stderr = _FakeStream([])
        self.returncode = None
        self._final_returncode = returncode
        self.killed = False

    async def wait(self):
        self.returncode = self._final_returncode
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def _config(**overrides):
    values = {
        "id": "mac-codex",
        "token": "token",
        "adapter": "codex",
        "codex_bin": "codex",
        "codex_sandbox": "danger-full-access",
        "work_dir": "/tmp",
        "timeout": 30,
        "activity_timeout": 5,
    }
    values.update(overrides)
    return AgentConfig(**values)


class TestCodexAdapterPermissions(unittest.TestCase):
    def test_build_cmd_uses_bypass_flag_when_enabled(self):
        config = AgentConfig(
            id="mac-codex",
            token="token",
            adapter="codex",
            codex_bin="codex",
            codex_sandbox="danger-full-access",
            codex_dangerously_bypass_approvals_and_sandbox=True,
            work_dir="/tmp",
        )
        adapter = CodexAdapter(config)

        cmd = adapter._build_cmd(model=None)

        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertNotIn("--sandbox", cmd)

    def test_build_cmd_uses_sandbox_when_bypass_disabled(self):
        config = AgentConfig(
            id="mac-codex",
            token="token",
            adapter="codex",
            codex_bin="codex",
            codex_sandbox="danger-full-access",
            work_dir="/tmp",
        )
        adapter = CodexAdapter(config)

        cmd = adapter._build_cmd(model=None)

        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertIn("--sandbox", cmd)
        self.assertIn("danger-full-access", cmd)

    def test_config_loads_codex_bypass_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "agents.toml"
            path.write_text(
                """
[[agents]]
id = "mac-codex"
token = "token"
adapter = "codex"
codex_dangerously_bypass_approvals_and_sandbox = true
""",
                encoding="utf-8",
            )

            config = _load_toml_agent(path, "mac-codex")

        self.assertTrue(config.codex_dangerously_bypass_approvals_and_sandbox)


class TestCodexResumePermissions(unittest.TestCase):
    def test_resume_uses_bypass_flag_when_enabled(self):
        config = AgentConfig(
            id="mac-codex",
            token="token",
            adapter="codex",
            codex_bin="codex",
            codex_sandbox="danger-full-access",
            codex_dangerously_bypass_approvals_and_sandbox=True,
            work_dir="/tmp",
        )
        adapter = CodexAdapter(config)
        cmd: list[str] = []

        adapter._append_permission_flags(cmd, for_resume=True)

        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertNotIn("sandbox_permissions=[\"danger-full-access\"]", cmd)

    def test_resume_uses_sandbox_config_when_bypass_disabled(self):
        config = AgentConfig(
            id="mac-codex",
            token="token",
            adapter="codex",
            codex_bin="codex",
            codex_sandbox="danger-full-access",
            work_dir="/tmp",
        )
        adapter = CodexAdapter(config)
        cmd: list[str] = []

        adapter._append_permission_flags(cmd, for_resume=True)

        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertIn("-c", cmd)
        self.assertIn("sandbox_permissions=[\"danger-full-access\"]", cmd)


class TestCodexCancellation(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_kills_process(self):
        proc = _FakeProcess(hang=True)

        async def fake_exec(*args, **kwargs):
            return proc

        cleanup_calls = []

        async def fake_cleanup(target):
            cleanup_calls.append(target)
            target.kill()
            await target.wait()

        adapter = CodexAdapter(_config())
        with (
            patch("multinexus.adapters.codex.asyncio.create_subprocess_exec", new=fake_exec),
            patch(
                "multinexus.adapters.codex.terminate_owned_process_group",
                new=fake_cleanup,
            ),
        ):
            task = asyncio.create_task(adapter.call("hang"))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(proc.killed)
        self.assertEqual(cleanup_calls, [proc])


class TestCodexProcessGroups(unittest.IsolatedAsyncioTestCase):
    async def test_call_spawns_owned_group_and_cleans_activity_timeout_once(self):
        proc = _FakeProcess(hang=True)
        spawn_kwargs = {}
        cleanup_calls = []

        async def fake_exec(*args, **kwargs):
            spawn_kwargs.update(kwargs)
            return proc

        async def fake_cleanup(target):
            cleanup_calls.append(target)
            target.kill()
            await target.wait()

        adapter = CodexAdapter(_config(activity_timeout=0))
        with (
            patch("multinexus.adapters.codex.asyncio.create_subprocess_exec", new=fake_exec),
            patch(
                "multinexus.adapters.codex.async_subprocess_kwargs",
                return_value={"start_new_session": True},
            ),
            patch(
                "multinexus.adapters.codex.terminate_owned_process_group",
                new=fake_cleanup,
            ),
        ):
            result = await adapter.call("hang")

        self.assertIs(spawn_kwargs["start_new_session"], True)
        self.assertEqual(cleanup_calls, [proc])
        self.assertIn("stopped responding", result.text)

    async def test_resume_spawns_owned_group_and_cleans_activity_timeout_once(self):
        proc = _FakeProcess(hang=True)
        spawn_kwargs = {}
        cleanup_calls = []

        async def fake_exec(*args, **kwargs):
            spawn_kwargs.update(kwargs)
            return proc

        async def fake_cleanup(target):
            cleanup_calls.append(target)
            target.kill()
            await target.wait()

        adapter = CodexAdapter(_config(activity_timeout=0))
        with (
            patch("multinexus.adapters.codex.asyncio.create_subprocess_exec", new=fake_exec),
            patch(
                "multinexus.adapters.codex.async_subprocess_kwargs",
                return_value={"start_new_session": True},
            ),
            patch(
                "multinexus.adapters.codex.terminate_owned_process_group",
                new=fake_cleanup,
            ),
        ):
            result = await adapter.resume("thread-1", "hang")

        self.assertIs(spawn_kwargs["start_new_session"], True)
        self.assertEqual(cleanup_calls, [proc])
        self.assertIn("stopped responding", result.text)
        self.assertTrue(result.resumed)

    async def test_cleanup_failure_is_explicit_and_not_retried(self):
        proc = _FakeProcess(hang=True)
        cleanup_calls = []

        async def fake_exec(*args, **kwargs):
            return proc

        async def failing_cleanup(target):
            cleanup_calls.append(target)
            raise RuntimeError("process group cleanup failed")

        adapter = CodexAdapter(_config(activity_timeout=0))
        with (
            patch("multinexus.adapters.codex.asyncio.create_subprocess_exec", new=fake_exec),
            patch(
                "multinexus.adapters.codex.terminate_owned_process_group",
                new=failing_cleanup,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "process group cleanup failed"):
                await adapter.call("hang")

        self.assertEqual(cleanup_calls, [proc])
