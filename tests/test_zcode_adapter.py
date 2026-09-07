import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from multinexus.adapters.base import (
    OUTCOME_FAILED,
    OUTCOME_SUCCESS,
    OUTCOME_TIMED_OUT,
)
from multinexus.adapters.factory import make_adapter
from multinexus.adapters.zcode import ZCodeAdapter
from multinexus.config import _load_toml_agent
from multinexus.models import AgentConfig


def _config(**overrides):
    values = {
        "id": "mac-zcode",
        "token": "",
        "adapter": "zcode",
        "zcode_bin": "zcode",
        "work_dir": ".",
        "timeout": 5,
    }
    values.update(overrides)
    return AgentConfig(**values)


class _FakeProcess:
    def __init__(self, payload=None, *, stdout=None, returncode=0, hang=False):
        self._stdout = stdout if stdout is not None else json.dumps(payload or {}).encode()
        self.returncode = returncode
        self.pid = 5101
        self.hang = hang
        self.started = asyncio.Event()

    async def communicate(self, input=None):
        self.started.set()
        if self.hang:
            await asyncio.Event().wait()
        return self._stdout, b""

    async def wait(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


def _success(**overrides):
    payload = {
        "sessionId": "sess_zcode_1",
        "traceId": "trace-private",
        "turnId": "turn-private",
        "response": "hello",
        "eventCount": 11,
        "projection": {
            "status": "idle",
            "turnCount": 1,
            "totalTokenCount": 0,
            "contextUsed": 0,
            "contextWindow": 200000,
        },
    }
    payload.update(overrides)
    return payload


class ZCodeCommandTests(unittest.TestCase):
    def test_safe_mode_and_resume_are_explicit(self):
        adapter = ZCodeAdapter(_config())
        cmd = adapter._build_cmd("hello", cwd="/tmp", resume_session_id="sess_1")
        self.assertEqual(cmd[0], "zcode")
        self.assertEqual(cmd[cmd.index("--mode") + 1], "build")
        self.assertEqual(cmd[cmd.index("--cwd") + 1], "/tmp")
        self.assertEqual(cmd[cmd.index("--resume") + 1], "sess_1")
        self.assertNotIn("--continue", cmd)

    def test_windows_node_and_metachar_prompt_are_explicit_argv(self):
        adapter = ZCodeAdapter(
            _config(
                zcode_bin=r"C:\Program Files\ZCode\resources\glm\zcode.cjs",
                zcode_node_bin=r"C:\Program Files\nodejs\node.exe",
            )
        )
        prompt = "literal & | ^ % text"
        cmd = adapter._build_cmd(prompt, cwd=r"C:\work")

        self.assertEqual(
            cmd[:2],
            [
                r"C:\Program Files\nodejs\node.exe",
                r"C:\Program Files\ZCode\resources\glm\zcode.cjs",
            ],
        )
        self.assertEqual(cmd[cmd.index("--prompt") + 1], prompt)

    def test_resume_rejects_invalid_session_id_without_spawn(self):
        result = asyncio.run(ZCodeAdapter(_config()).resume("not a session", "hi"))
        self.assertEqual(result.text, "ZCode resume failed: invalid session id")
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.error_category, "protocol_error")


class ZCodeConfigFactoryTests(unittest.TestCase):
    def test_toml_fields_and_factory(self):
        with TemporaryDirectory() as td:
            path = Path(td) / "agents.toml"
            binary = Path(td) / "zcode"
            home = Path(td) / "worker-home"
            binary.write_text("", encoding="utf-8")
            home.mkdir()
            path.write_text(
                '[[agents]]\n'
                'id = "z"\n'
                'token = "x"\n'
                'adapter = "zcode"\n'
                f'zcode_bin = "{binary}"\n'
                f'zcode_node_bin = "{binary}"\n'
                f'zcode_home_dir = "{home}"\n'
                'zcode_permission_mode = "plan"\n',
                encoding="utf-8",
            )
            config = _load_toml_agent(path, "z")
        self.assertEqual(config.zcode_bin, str(binary))
        self.assertEqual(config.zcode_node_bin, str(binary))
        self.assertEqual(config.zcode_home_dir, str(home))
        self.assertEqual(config.zcode_permission_mode, "plan")
        self.assertIsInstance(make_adapter(config), ZCodeAdapter)


class ZCodeCallTests(unittest.IsolatedAsyncioTestCase):
    async def _call_with(self, proc, *, resume=None, progress=None):
        async def fake_create(*args, **kwargs):
            return proc

        adapter = ZCodeAdapter(_config(system_prompt="SYSTEM"))
        with patch(
            "multinexus.adapters.zcode.asyncio.create_subprocess_exec",
            new=fake_create,
        ):
            if resume:
                return await adapter.resume(resume, "ping", on_progress=progress)
            return await adapter.call("ping", on_progress=progress)

    async def test_real_0_16_3_shape_and_bounded_metadata(self):
        secret = "PRIVATE-TRACE-SENTINEL"
        progress = []
        result = await self._call_with(
            _FakeProcess(_success(traceId=secret, reasoning=secret)), progress=progress.append
        )
        self.assertEqual(result.text, "hello")
        self.assertEqual(result.session_id, "sess_zcode_1")
        self.assertEqual(result.metadata["provider_evidence"]["session_status"], "idle")
        self.assertEqual(result.effective_outcome(), OUTCOME_SUCCESS)
        self.assertNotIn(secret, repr(result.metadata))
        self.assertNotIn(secret, repr(progress))
        # ZCode 无 usage contract：显式 unknown 单条，五 numeric 全 null。
        usage = result.metadata["usage_evidence"]
        self.assertEqual(usage["contract_version"], 1)
        record = usage["records"][0]
        self.assertEqual(record["provider"], "zcode")
        self.assertIsNone(record["model"])
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "provider_cost_microusd",
        ):
            self.assertIsNone(record[field])
        self.assertEqual(record["source"], "unknown")
        self.assertEqual(record["completeness"], "unknown")
        json.dumps(result.metadata)

    async def test_resume_requires_exact_session_identity(self):
        result = await self._call_with(
            _FakeProcess(_success(sessionId="sess_other")), resume="sess_expected"
        )
        self.assertEqual(result.text, "ZCode resume failed: session mismatch")
        self.assertEqual(result.session_id, "sess_expected")
        self.assertFalse(result.resumed)
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.error_category, "protocol_error")

    async def test_unknown_json_shape_fails_loud(self):
        for payload in (
            {"session_id": "sess_wrong", "response": "hello", "projection": {"status": "idle"}},
            {"sessionId": "sess_ok", "text": "hello", "projection": {"status": "idle"}},
            {"sessionId": "sess_ok", "response": "hello"},
        ):
            with self.subTest(payload=payload):
                result = await self._call_with(_FakeProcess(payload))
                self.assertEqual(result.text, "ZCode error: unexpected JSON contract")
                self.assertEqual(result.outcome, OUTCOME_FAILED)
                self.assertEqual(result.error_category, "protocol_error")
                # error 路径不携带 usage_evidence（V1 只在成功 AdapterResult 上添加）。
                self.assertNotIn("usage_evidence", result.metadata)

    async def test_empty_invalid_nonzero_and_missing_are_classified(self):
        empty = await self._call_with(_FakeProcess(_success(response="  ")))
        self.assertEqual(empty.text, "(no response)")
        self.assertEqual(empty.outcome, OUTCOME_FAILED)
        self.assertEqual(empty.error_category, "no_response")
        invalid = await self._call_with(_FakeProcess(stdout=b"not-json"))
        self.assertEqual(invalid.text, "ZCode error: invalid JSON response")
        self.assertEqual(invalid.outcome, OUTCOME_FAILED)
        self.assertEqual(invalid.error_category, "protocol_error")
        failed = await self._call_with(_FakeProcess(stdout=b"secret", returncode=7))
        self.assertEqual(failed.text, "ZCode CLI failed (7)")
        self.assertEqual(failed.outcome, OUTCOME_FAILED)
        self.assertEqual(failed.error_category, "process_error")

        async def missing(*args, **kwargs):
            raise FileNotFoundError

        with patch(
            "multinexus.adapters.zcode.asyncio.create_subprocess_exec", new=missing
        ):
            missing_result = await ZCodeAdapter(
                _config(zcode_bin="/missing/zcode")
            ).call("ping")
        self.assertEqual(missing_result.text, "ZCode CLI not found: /missing/zcode")
        self.assertEqual(missing_result.outcome, OUTCOME_FAILED)
        self.assertEqual(missing_result.error_category, "unavailable")

    async def test_missing_node_or_worker_home_fail_before_spawn(self):
        with patch("multinexus.adapters.zcode.shutil.which", return_value=None):
            missing_node = await ZCodeAdapter(
                _config(zcode_node_bin="/missing/node")
            ).call("ping")
        self.assertEqual(
            missing_node.text, "ZCode Node runtime not found: /missing/node"
        )
        self.assertEqual(missing_node.outcome, OUTCOME_FAILED)
        self.assertEqual(missing_node.error_category, "unavailable")

        with TemporaryDirectory() as td:
            relative_home = await ZCodeAdapter(
                _config(zcode_home_dir="worker-home")
            ).call("ping", work_dir=td)
        self.assertEqual(relative_home.text, "ZCode worker home unavailable")
        self.assertEqual(relative_home.outcome, OUTCOME_FAILED)
        self.assertEqual(relative_home.error_category, "unavailable")

    async def test_worker_home_isolated_environment_is_passed_to_child(self):
        with TemporaryDirectory() as td:
            home = Path(td) / "worker-home"
            home.mkdir()
            proc = _FakeProcess(_success())
            captured = {}

            async def fake_create(*args, **kwargs):
                captured.update(kwargs)
                return proc

            with (
                patch(
                    "multinexus.adapters.zcode.asyncio.create_subprocess_exec",
                    new=fake_create,
                ),
                patch.dict(
                    "multinexus.adapters.zcode.os.environ",
                    {
                        "HOME": "/operator-home",
                        "USERPROFILE": "/operator-profile",
                        "COORDINATE_REMOTE_MCP_TOKEN": "must-not-leak",
                        "coordinate_remote_mcp_token_file": "must-not-leak-either",
                    },
                    clear=True,
                ),
            ):
                result = await ZCodeAdapter(
                    _config(zcode_home_dir=str(home))
                ).call("ping", work_dir=td)

        self.assertEqual(result.text, "hello")
        self.assertEqual(captured["env"]["HOME"], str(home))
        self.assertEqual(captured["env"]["USERPROFILE"], str(home))
        self.assertEqual(captured["env"]["APPDATA"], str(home / "AppData" / "Roaming"))
        self.assertEqual(captured["env"]["LOCALAPPDATA"], str(home / "AppData" / "Local"))
        self.assertEqual(captured["env"]["ZCODE_STORAGE_DIR"], str(home / ".zcode"))
        self.assertEqual(captured["env"]["ZCODE_DATA_BASE_DIR"], str(home))
        self.assertNotIn("COORDINATE_REMOTE_MCP_TOKEN", captured["env"])
        self.assertNotIn("coordinate_remote_mcp_token_file", captured["env"])

    async def test_node_runtime_requires_isolated_worker_home(self):
        with patch(
            "multinexus.adapters.zcode.shutil.which",
            return_value="/usr/local/bin/node",
        ):
            result = await ZCodeAdapter(
                _config(zcode_node_bin="node")
            ).call("ping")
        self.assertEqual(result.text, "ZCode worker home required with Node runtime")
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.error_category, "unavailable")

    async def test_timeout_and_cancel_cleanup_owned_process(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                proc = _FakeProcess(hang=True)
                cleanup_calls = []

                async def fake_create(*args, **kwargs):
                    return proc

                async def fake_cleanup(target):
                    cleanup_calls.append(target)
                    target.kill()

                with (
                    patch(
                        "multinexus.adapters.zcode.asyncio.create_subprocess_exec",
                        new=fake_create,
                    ),
                    patch(
                        "multinexus.adapters.zcode.terminate_owned_process_group",
                        new=fake_cleanup,
                    ),
                ):
                    task = asyncio.create_task(
                        ZCodeAdapter(_config(timeout=0.01 if not cancel else 5)).call(
                            "ping"
                        )
                    )
                    await proc.started.wait()
                    if cancel:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        result = await task
                        self.assertIn("timeout", result.text.lower())
                        self.assertEqual(result.metadata["timeout"]["kind"], "total")
                        self.assertEqual(result.outcome, OUTCOME_TIMED_OUT)
                        self.assertEqual(result.error_category, "timeout")
                self.assertEqual(cleanup_calls, [proc])


class ZCodeHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_accepts_absolute_executable(self):
        with patch(
            "multinexus.adapters.zcode.shutil.which", return_value="/Applications/ZCode.app/zcode"
        ):
            status = await ZCodeAdapter(_config()).health_check()
        self.assertTrue(status["available"])
        self.assertEqual(status["contract"], "zcode-cli-json-0.16.3")

    async def test_health_requires_node_script_and_absolute_worker_home(self):
        with TemporaryDirectory() as td:
            script = Path(td) / "zcode.cjs"
            home = Path(td) / "worker-home"
            script.write_text("", encoding="utf-8")
            home.mkdir()
            with patch(
                "multinexus.adapters.zcode.shutil.which",
                return_value="/usr/local/bin/node",
            ):
                status = await ZCodeAdapter(
                    _config(
                        zcode_bin=str(script),
                        zcode_node_bin="node",
                        zcode_home_dir=str(home),
                    )
                ).health_check()
        self.assertTrue(status["available"])
        self.assertTrue(status["home_available"])
        self.assertEqual(status["node_path"], "/usr/local/bin/node")


if __name__ == "__main__":
    unittest.main()
