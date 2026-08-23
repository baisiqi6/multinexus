import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from multinexus.adapters.factory import make_adapter
from multinexus.adapters.qoder import QoderAdapter
from multinexus.config import _load_toml_agent
from multinexus.models import AgentConfig
from multinexus.adapters.base import OUTCOME_FAILED, OUTCOME_TIMED_OUT


def _config(**overrides):
    values = {
        "id": "mac-qoder",
        "token": "",
        "adapter": "qoder",
        "qoder_bin": "qodercli",
        "work_dir": ".",
        "timeout": 5,
    }
    values.update(overrides)
    return AgentConfig(**values)


class _FakeProcess:
    def __init__(self, payload=None, *, stdout=None, returncode=0, hang=False):
        if stdout is None:
            stdout = json.dumps(payload or {}).encode()
        self._stdout = stdout
        self.returncode = returncode
        self.pid = 4101
        self.hang = hang
        self.started = asyncio.Event()
        self.input = None
        self.killed = False

    async def communicate(self, input=None):
        self.input = input
        self.started.set()
        if self.hang:
            await asyncio.Event().wait()
        return self._stdout, b""

    async def wait(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def _success(**overrides):
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "hello",
        "session_id": "q-session",
        "stop_reason": "end_turn",
        "modelUsage": {"qmodel_preview": {"output_tokens": 1}},
    }
    payload.update(overrides)
    return payload


class QoderCommandTests(unittest.TestCase):
    def test_safe_defaults_and_optional_flags(self):
        adapter = QoderAdapter(
            _config(
                model="Qwen3.8-Max-Preview",
                qoder_reasoning_effort="high",
            )
        )
        cmd = adapter._build_cmd()
        self.assertEqual(cmd[:4], ["qodercli", "-p", "--output-format", "json"])
        self.assertIn("dont_ask", cmd)
        self.assertIn("Qwen3.8-Max-Preview", cmd)
        self.assertIn("high", cmd)

    def test_resume_and_explicit_permission_mode(self):
        cmd = QoderAdapter(
            _config(qoder_permission_mode="bypass_permissions")
        )._build_cmd(resume_session_id="q-1")
        self.assertIn("bypass_permissions", cmd)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "q-1")


class QoderConfigFactoryTests(unittest.TestCase):
    def test_toml_fields_and_factory(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "agents.toml"
            binary = Path(td) / "qodercli"
            binary.write_text("", encoding="utf-8")
            path.write_text(
                '[[agents]]\n'
                'id = "q"\n'
                'token = "x"\n'
                'adapter = "qoder"\n'
                f'qoder_bin = "{binary}"\n'
                'qoder_reasoning_effort = "max"\n'
                'qoder_permission_mode = "accept_edits"\n',
                encoding="utf-8",
            )
            config = _load_toml_agent(path, "q")
        self.assertEqual(config.qoder_bin, str(binary))
        self.assertEqual(config.qoder_reasoning_effort, "max")
        self.assertEqual(config.qoder_permission_mode, "accept_edits")
        self.assertIsInstance(make_adapter(config), QoderAdapter)


class QoderCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_session_metadata_progress_and_system_prompt(self):
        proc = _FakeProcess(_success())
        captured = {}
        progress = []

        async def fake_create(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return proc

        adapter = QoderAdapter(_config(system_prompt="SYSTEM"))
        with (
            patch(
                "multinexus.adapters.qoder.asyncio.create_subprocess_exec",
                new=fake_create,
            ),
            patch(
                "multinexus.adapters.qoder.async_subprocess_kwargs",
                return_value={"start_new_session": True},
            ),
            patch(
                "multinexus.adapters.qoder.filtered_env",
                return_value={"SAFE": "1"},
            ),
        ):
            result = await adapter.call("ping", work_dir="/tmp", on_progress=progress.append)

        self.assertEqual(result.text, "hello")
        self.assertEqual(result.session_id, "q-session")
        self.assertFalse(result.resumed)
        self.assertEqual(
            result.metadata["provider_evidence"]["observed_models"],
            ["qmodel_preview"],
        )
        self.assertEqual(progress[0]["stage"], "complete")
        self.assertEqual(proc.input, b"SYSTEM\n\nUSER: ping")
        self.assertEqual(captured["kwargs"]["cwd"], "/tmp")
        self.assertEqual(captured["kwargs"]["env"], {"SAFE": "1"})
        self.assertTrue(captured["kwargs"]["start_new_session"])
        self.assertIs(captured["kwargs"]["stderr"], asyncio.subprocess.DEVNULL)

    async def test_resume_requires_same_session(self):
        proc = _FakeProcess(_success(session_id="q-old"))

        async def fake_create(*args, **kwargs):
            return proc

        with patch(
            "multinexus.adapters.qoder.asyncio.create_subprocess_exec",
            new=fake_create,
        ):
            result = await QoderAdapter(_config()).resume("q-old", "continue")
        self.assertTrue(result.resumed)
        self.assertEqual(result.session_id, "q-old")

    async def test_resume_mismatch_fails_closed(self):
        proc = _FakeProcess(_success(session_id="q-other"))

        async def fake_create(*args, **kwargs):
            return proc

        with patch(
            "multinexus.adapters.qoder.asyncio.create_subprocess_exec",
            new=fake_create,
        ):
            result = await QoderAdapter(_config()).resume("q-old", "continue")
        self.assertFalse(result.resumed)
        self.assertEqual(result.text, "Qoder resume failed: session mismatch")
        self.assertEqual(result.session_id, "q-old")
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.error_category, "protocol_error")

    async def test_unrecognized_fields_are_not_forwarded(self):
        secret = "PRIVATE-THOUGHT-SENTINEL"
        proc = _FakeProcess(_success(thought=secret, raw_event={"secret": secret}))
        progress = []

        async def fake_create(*args, **kwargs):
            return proc

        with patch(
            "multinexus.adapters.qoder.asyncio.create_subprocess_exec",
            new=fake_create,
        ):
            result = await QoderAdapter(_config()).call(
                "ping", on_progress=progress.append
            )
        self.assertNotIn(secret, result.text)
        self.assertNotIn(secret, repr(result.metadata))
        self.assertNotIn(secret, repr(progress))

        cases = [
            (
                _FakeProcess(_success(result="  ")),
                "(no response)",
                OUTCOME_FAILED,
                "no_response",
            ),
            (
                _FakeProcess(stdout=b"not-json"),
                "Qoder error: invalid JSON response",
                OUTCOME_FAILED,
                "protocol_error",
            ),
            (
                _FakeProcess(_success(is_error=True)),
                "Qoder error: provider returned an error",
                OUTCOME_FAILED,
                "provider_error",
            ),
        ]
        for proc, expected_text, expected_outcome, expected_category in cases:
            async def fake_create(*args, _proc=proc, **kwargs):
                return _proc

            with patch(
                "multinexus.adapters.qoder.asyncio.create_subprocess_exec",
                new=fake_create,
            ):
                result = await QoderAdapter(_config()).call("ping")
            self.assertEqual(result.text, expected_text)
            self.assertEqual(result.outcome, expected_outcome)
            self.assertEqual(result.error_category, expected_category)

    async def test_nonzero_and_missing_binary_are_stable(self):
        proc = _FakeProcess(stdout=b"secret diagnostic", returncode=7)

        async def fake_create(*args, **kwargs):
            return proc

        with patch(
            "multinexus.adapters.qoder.asyncio.create_subprocess_exec",
            new=fake_create,
        ):
            result = await QoderAdapter(_config()).call("ping")
        self.assertEqual(result.text, "Qoder CLI failed (7)")
        self.assertNotIn("secret", result.text)
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.error_category, "process_error")

        async def missing(*args, **kwargs):
            raise FileNotFoundError

        with patch(
            "multinexus.adapters.qoder.asyncio.create_subprocess_exec",
            new=missing,
        ):
            result = await QoderAdapter(_config(qoder_bin="/missing/qoder")).call(
                "ping"
            )
        self.assertEqual(result.text, "Qoder CLI not found: /missing/qoder")
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.error_category, "unavailable")


class QoderLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_cleans_process_group_once(self):
        proc = _FakeProcess(hang=True)
        cleanup_calls = []

        async def fake_create(*args, **kwargs):
            return proc

        async def fake_cleanup(target):
            cleanup_calls.append(target)
            target.kill()

        with (
            patch(
                "multinexus.adapters.qoder.asyncio.create_subprocess_exec",
                new=fake_create,
            ),
            patch(
                "multinexus.adapters.qoder.terminate_owned_process_group",
                new=fake_cleanup,
            ),
        ):
            result = await QoderAdapter(_config(timeout=0.01)).call("ping")
        self.assertIn("timeout", result.text.lower())
        self.assertEqual(cleanup_calls, [proc])
        self.assertEqual(result.outcome, OUTCOME_TIMED_OUT)
        self.assertEqual(result.error_category, "timeout")

    async def test_cancellation_cleans_process_group_once(self):
        proc = _FakeProcess(hang=True)
        cleanup_calls = []

        async def fake_create(*args, **kwargs):
            return proc

        async def fake_cleanup(target):
            cleanup_calls.append(target)
            target.kill()

        with (
            patch(
                "multinexus.adapters.qoder.asyncio.create_subprocess_exec",
                new=fake_create,
            ),
            patch(
                "multinexus.adapters.qoder.terminate_owned_process_group",
                new=fake_cleanup,
            ),
        ):
            task = asyncio.create_task(QoderAdapter(_config()).call("ping"))
            await proc.started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(cleanup_calls, [proc])


class QoderHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_uses_path_only(self):
        with patch(
            "multinexus.adapters.qoder.shutil.which",
            return_value="/usr/local/bin/qodercli",
        ):
            status = await QoderAdapter(_config()).health_check()
        self.assertTrue(status["available"])
        self.assertEqual(status["path"], "/usr/local/bin/qodercli")


if __name__ == "__main__":
    unittest.main()
