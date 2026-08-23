import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from multinexus.adapters.base import OUTCOME_FAILED, OUTCOME_TIMED_OUT
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
        self.assertEqual(result.outcome, OUTCOME_TIMED_OUT)
        self.assertEqual(result.error_category, "timeout")

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
        self.assertEqual(result.outcome, OUTCOME_TIMED_OUT)
        self.assertEqual(result.error_category, "timeout")
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

class TestCodexSettlement(unittest.IsolatedAsyncioTestCase):
    """Explicit outcome/category settlements for the closed failure paths."""

    async def test_total_timeout_is_timed_out(self):
        proc = _FakeProcess(hang=True)

        async def fake_exec(*args, **kwargs):
            return proc

        async def fake_cleanup(target):
            target.kill()
            await target.wait()

        adapter = CodexAdapter(_config(timeout=0))
        with (
            patch("multinexus.adapters.codex.asyncio.create_subprocess_exec", new=fake_exec),
            patch(
                "multinexus.adapters.codex.terminate_owned_process_group",
                new=fake_cleanup,
            ),
        ):
            result = await adapter.call("hang")
        self.assertIn("timed out", result.text)
        self.assertEqual(result.outcome, OUTCOME_TIMED_OUT)
        self.assertEqual(result.error_category, "timeout")

    async def test_empty_response_is_no_response(self):
        proc = _FakeProcess(
            [{"type": "thread.started", "thread_id": "t-1"}], returncode=0
        )

        async def fake_exec(*args, **kwargs):
            return proc

        with patch(
            "multinexus.adapters.codex.asyncio.create_subprocess_exec", new=fake_exec
        ):
            result = await CodexAdapter(_config()).call("x")
        self.assertEqual(result.text, "(no response)")
        self.assertEqual(result.session_id, "t-1")
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.error_category, "no_response")

    async def test_nonzero_exit_is_process_error(self):
        proc = _FakeProcess([], returncode=3)

        async def fake_exec(*args, **kwargs):
            return proc

        with patch(
            "multinexus.adapters.codex.asyncio.create_subprocess_exec", new=fake_exec
        ):
            result = await CodexAdapter(_config()).call("x")
        self.assertIn("Codex CLI failed (3)", result.text)
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.error_category, "process_error")

    async def test_capacity_error_is_provider_error(self):
        proc = _FakeProcess(
            [{"type": "error", "message": "selected model is at capacity"}],
            returncode=1,
        )

        async def fake_exec(*args, **kwargs):
            return proc

        with patch(
            "multinexus.adapters.codex.asyncio.create_subprocess_exec", new=fake_exec
        ):
            result = await CodexAdapter(_config()).call("x")
        self.assertIn("capacity", result.text.lower())
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.error_category, "provider_error")

    async def test_capacity_fallback_uses_fallback_model(self):
        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                return _FakeProcess(
                    [{"type": "error", "message": "selected model is at capacity"}],
                    returncode=1,
                )
            return _FakeProcess(
                [{"item": {"type": "agent_message", "text": "fallback ok"}}],
                returncode=0,
            )

        adapter = CodexAdapter(
            _config(model="primary", codex_fallback_model="fallback")
        )
        with patch(
            "multinexus.adapters.codex.asyncio.create_subprocess_exec", new=fake_exec
        ):
            result = await adapter.call("x")
        self.assertEqual(result.text, "fallback ok")
        self.assertEqual(len(calls), 2)
        self.assertIn("--model", calls[1])
        self.assertIn("fallback", calls[1])

    async def test_missing_cli_is_unavailable(self):
        async def fake_exec(*args, **kwargs):
            raise FileNotFoundError

        adapter = CodexAdapter(_config(codex_bin="/no/such/codex"))
        with patch(
            "multinexus.adapters.codex.asyncio.create_subprocess_exec", new=fake_exec
        ):
            result = await adapter.call("x")
        self.assertIn("Codex CLI not found", result.text)
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.error_category, "unavailable")

        with patch(
            "multinexus.adapters.codex.asyncio.create_subprocess_exec", new=fake_exec
        ):
            result = await adapter.resume("t-1", "x")
        self.assertIn("Codex CLI not found", result.text)
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.error_category, "unavailable")

    async def test_resume_nonzero_is_protocol_error(self):
        proc = _FakeProcess([], returncode=4)

        async def fake_exec(*args, **kwargs):
            return proc

        with patch(
            "multinexus.adapters.codex.asyncio.create_subprocess_exec", new=fake_exec
        ):
            result = await CodexAdapter(_config()).resume("t-1", "x")
        self.assertIn("Codex resume failed (4)", result.text)
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.error_category, "protocol_error")
        self.assertTrue(result.resumed)
