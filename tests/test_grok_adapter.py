import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from multinexus.adapters.factory import make_adapter
from multinexus.adapters.grok import GrokAdapter
from multinexus.config import _load_toml_agent
from multinexus.models import AgentConfig
from multinexus.adapters.base import OUTCOME_FAILED, OUTCOME_TIMED_OUT


def _config(**overrides):
    values = {
        "id": "mac-grok",
        "token": "",
        "adapter": "grok",
        "grok_bin": "grok",
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
        self.pid = 4201
        self.hang = hang
        self.started = asyncio.Event()
        self.killed = False

    async def communicate(self):
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
        "text": "hello",
        "sessionId": "g-session",
        "stopReason": "EndTurn",
        "modelUsage": {"deepseek-v4-pro": {"output_tokens": 1}},
        "thought": "must never be retained",
    }
    payload.update(overrides)
    return payload


class GrokCommandTests(unittest.TestCase):
    def test_safe_defaults_and_optional_flags(self):
        adapter = GrokAdapter(
            _config(model="deepseek", grok_reasoning_effort="high")
        )
        cmd = adapter._build_cmd("ping")
        self.assertEqual(cmd[:3], ["grok", "--single", "ping"])
        self.assertIn("json", cmd)
        self.assertIn("dontAsk", cmd)
        self.assertIn("--no-memory", cmd)
        self.assertNotIn("--no-subagents", cmd)
        self.assertIn("deepseek", cmd)
        self.assertIn("high", cmd)

    def test_resume_and_explicit_permission_mode(self):
        cmd = GrokAdapter(
            _config(grok_permission_mode="bypassPermissions")
        )._build_cmd("ping", resume_session_id="g-1")
        self.assertIn("bypassPermissions", cmd)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "g-1")


class GrokConfigFactoryTests(unittest.TestCase):
    def test_toml_fields_and_factory(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "agents.toml"
            binary = Path(td) / "grok"
            binary.write_text("", encoding="utf-8")
            path.write_text(
                '[[agents]]\n'
                'id = "g"\n'
                'token = "x"\n'
                'adapter = "grok"\n'
                f'grok_bin = "{binary}"\n'
                'grok_reasoning_effort = "max"\n'
                'grok_permission_mode = "acceptEdits"\n',
                encoding="utf-8",
            )
            config = _load_toml_agent(path, "g")
        self.assertEqual(config.grok_bin, str(binary))
        self.assertEqual(config.grok_reasoning_effort, "max")
        self.assertEqual(config.grok_permission_mode, "acceptEdits")
        self.assertIsInstance(make_adapter(config), GrokAdapter)


class GrokCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_session_metadata_progress_and_system_prompt(self):
        proc = _FakeProcess(_success())
        captured = {}
        progress = []

        async def fake_create(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return proc

        adapter = GrokAdapter(_config(system_prompt="SYSTEM", model="deepseek"))
        with (
            patch(
                "multinexus.adapters.grok.asyncio.create_subprocess_exec",
                new=fake_create,
            ),
            patch(
                "multinexus.adapters.grok.async_subprocess_kwargs",
                return_value={"start_new_session": True},
            ),
            patch(
                "multinexus.adapters.grok.filtered_env",
                return_value={"SAFE": "1"},
            ),
        ):
            result = await adapter.call("ping", work_dir="/tmp", on_progress=progress.append)

        self.assertEqual(result.text, "hello")
        self.assertEqual(result.session_id, "g-session")
        self.assertFalse(result.resumed)
        self.assertEqual(
            result.metadata["provider_evidence"]["observed_models"],
            ["deepseek-v4-pro"],
        )
        self.assertEqual(progress[0]["stage"], "complete")
        self.assertIn("SYSTEM\n\nUSER: ping", captured["args"])
        self.assertEqual(captured["kwargs"]["cwd"], "/tmp")
        self.assertEqual(captured["kwargs"]["env"], {"SAFE": "1"})
        self.assertTrue(captured["kwargs"]["start_new_session"])
        self.assertIs(captured["kwargs"]["stderr"], asyncio.subprocess.DEVNULL)

    async def test_resume_requires_same_session(self):
        proc = _FakeProcess(_success(sessionId="g-old"))

        async def fake_create(*args, **kwargs):
            return proc

        with patch(
            "multinexus.adapters.grok.asyncio.create_subprocess_exec",
            new=fake_create,
        ):
            result = await GrokAdapter(_config()).resume("g-old", "continue")
        self.assertTrue(result.resumed)
        self.assertEqual(result.session_id, "g-old")

    async def test_resume_mismatch_fails_closed(self):
        proc = _FakeProcess(_success(sessionId="g-other"))

        async def fake_create(*args, **kwargs):
            return proc

        with patch(
            "multinexus.adapters.grok.asyncio.create_subprocess_exec",
            new=fake_create,
        ):
            result = await GrokAdapter(_config()).resume("g-old", "continue")
        self.assertFalse(result.resumed)
        self.assertEqual(result.text, "Grok resume failed: session mismatch")
        self.assertEqual(result.session_id, "g-old")
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.error_category, "protocol_error")

    async def test_private_thought_is_not_forwarded(self):
        secret = "PRIVATE-THOUGHT-SENTINEL"
        proc = _FakeProcess(_success(thought=secret, raw_event={"secret": secret}))
        progress = []

        async def fake_create(*args, **kwargs):
            return proc

        with patch(
            "multinexus.adapters.grok.asyncio.create_subprocess_exec",
            new=fake_create,
        ):
            result = await GrokAdapter(_config()).call(
                "ping", on_progress=progress.append
            )
        self.assertNotIn(secret, result.text)
        self.assertNotIn(secret, repr(result.metadata))
        self.assertNotIn(secret, repr(progress))

        cases = [
            (
                _FakeProcess(_success(text="  ")),
                "(no response)",
                OUTCOME_FAILED,
                "no_response",
            ),
            (
                _FakeProcess(stdout=b"not-json"),
                "Grok error: invalid JSON response",
                OUTCOME_FAILED,
                "protocol_error",
            ),
            (
                _FakeProcess({"type": "error", "message": "private detail"}),
                "Grok error: provider returned an error",
                OUTCOME_FAILED,
                "provider_error",
            ),
        ]
        for proc, expected_text, expected_outcome, expected_category in cases:
            async def fake_create(*args, _proc=proc, **kwargs):
                return _proc

            with patch(
                "multinexus.adapters.grok.asyncio.create_subprocess_exec",
                new=fake_create,
            ):
                result = await GrokAdapter(_config()).call("ping")
            self.assertEqual(result.text, expected_text)
            self.assertEqual(result.outcome, expected_outcome)
            self.assertEqual(result.error_category, expected_category)

    async def test_nonzero_and_missing_binary_are_stable(self):
        proc = _FakeProcess(stdout=b"secret diagnostic", returncode=7)

        async def fake_create(*args, **kwargs):
            return proc

        with patch(
            "multinexus.adapters.grok.asyncio.create_subprocess_exec",
            new=fake_create,
        ):
            result = await GrokAdapter(_config()).call("ping")
        self.assertEqual(result.text, "Grok CLI failed (7)")
        self.assertNotIn("secret", result.text)
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.error_category, "process_error")

        async def missing(*args, **kwargs):
            raise FileNotFoundError

        with patch(
            "multinexus.adapters.grok.asyncio.create_subprocess_exec",
            new=missing,
        ):
            result = await GrokAdapter(_config(grok_bin="/missing/grok")).call(
                "ping"
            )
        self.assertEqual(result.text, "Grok CLI not found: /missing/grok")
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.error_category, "unavailable")


class GrokLifecycleTests(unittest.IsolatedAsyncioTestCase):
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
                "multinexus.adapters.grok.asyncio.create_subprocess_exec",
                new=fake_create,
            ),
            patch(
                "multinexus.adapters.grok.terminate_owned_process_group",
                new=fake_cleanup,
            ),
        ):
            result = await GrokAdapter(_config(timeout=0.01)).call("ping")
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
                "multinexus.adapters.grok.asyncio.create_subprocess_exec",
                new=fake_create,
            ),
            patch(
                "multinexus.adapters.grok.terminate_owned_process_group",
                new=fake_cleanup,
            ),
        ):
            task = asyncio.create_task(GrokAdapter(_config()).call("ping"))
            await proc.started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(cleanup_calls, [proc])


class GrokHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_uses_path_only(self):
        with patch(
            "multinexus.adapters.grok.shutil.which",
            return_value="/usr/local/bin/grok",
        ):
            status = await GrokAdapter(_config()).health_check()
        self.assertTrue(status["available"])
        self.assertEqual(status["path"], "/usr/local/bin/grok")


if __name__ == "__main__":
    unittest.main()
