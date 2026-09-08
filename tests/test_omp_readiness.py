"""OMP startup is a local runtime check, distinct from binary/provider health."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from multinexus.adapters.omp import OmpAdapter
from multinexus.agentd.worker import AgentdWorker
from multinexus.models import AgentConfig


READY = {"type": "ready", "protocolVersion": 1}
STATE = {
    "type": "response", "id": "multinexus-startup",
    "command": "get_state", "success": True,
}


def config(**overrides):
    values = dict(
        id="test-omp", token="", adapter="omp", context_db_path=":memory:",
        coordinator_cli_path="/unused/coordinate", coordinator_db_path="/unused/db",
    )
    values.update(overrides)
    return AgentConfig(**values)


def output_script(*frames, exit_code=0, stderr=""):
    return (
        "import sys\nsys.stdin.read()\n"
        + "\n".join(f"print({json.dumps(json.dumps(frame))})" for frame in frames)
        + f"\nprint({stderr!r}, file=sys.stderr)\nsys.exit({exit_code})\n"
    )


class OmpStartupTests(unittest.IsolatedAsyncioTestCase):
    async def probe(self, script, **overrides):
        real_spawn = asyncio.create_subprocess_exec
        self.spawns = []

        async def spawn(*args, **kwargs):
            self.spawns.append((args, kwargs))
            return await real_spawn(sys.executable, "-c", script, **kwargs)

        adapter = OmpAdapter(config(**overrides))
        with patch("multinexus.adapters.omp.asyncio.create_subprocess_exec", new=spawn):
            return await adapter.startup_check()

    async def test_success_needs_rpc_handshake_without_prompt_or_provider_call(self):
        script = (
            "import json,sys\ncommands=sys.stdin.read().splitlines()\n"
            "assert len(commands)==1\n"
            "assert json.loads(commands[0])=="
            "{'id':'multinexus-startup','type':'get_state'}\n"
            + f"print({json.dumps(json.dumps(READY))})\n"
            + f"print({json.dumps(json.dumps(STATE))})\n"
        )
        with tempfile.TemporaryDirectory() as cwd:
            before = dict(os.environ)
            with patch.dict(os.environ, {
                "DISCORD_BOT_TOKEN": "synthetic-secret", "OMP_PROFILE": "test-profile",
            }):
                result = await self.probe(
                    script, omp_model="example/model", omp_thinking="max", work_dir=cwd,
                )
            self.assertEqual(dict(os.environ), before)
            args, kwargs = self.spawns[0]
            self.assertEqual(kwargs["cwd"], cwd)
            self.assertNotIn("DISCORD_BOT_TOKEN", kwargs["env"])
            self.assertEqual(kwargs["env"]["OMP_PROFILE"], "test-profile")
            self.assertEqual(kwargs["env"].get("HOME"), before.get("HOME"))
        self.assertTrue(result["runtime_ready"])
        self.assertFalse(result["provider_checked"])
        self.assertIn("rpc", args)
        self.assertNotIn("-p", args)
        self.assertNotIn("--auto-approve", args)
        self.assertIn("example/model", args)
        self.assertIn("max", args)
        for flag in ("--no-session", "--no-tools", "--no-extensions", "--no-skills",
                     "--no-rules", "--no-lsp", "--no-title", "--no-pty"):
            self.assertIn(flag, args)

    async def test_readonly_failure_does_not_forward_raw_stderr(self):
        result = await self.probe(output_script(
            READY, STATE, exit_code=1, stderr="SQLITE_READONLY: synthetic-secret",
        ))
        self.assertFalse(result["runtime_ready"])
        self.assertEqual(result["reason_code"], "runtime_not_writable")
        self.assertNotIn("synthetic-secret", json.dumps(result))
        self.assertIn("~/.omp", result["runtime_directory_hint"])

    async def test_incomplete_or_invalid_protocol_cannot_be_ready(self):
        cases = [
            (), (READY,), (STATE,),
            (READY, {**STATE, "id": "wrong"}),
            (READY, {**STATE, "success": False}),
            (READY, {**STATE, "success": 1}),
            ({**READY, "protocolVersion": 2}, STATE),
            (READY, {**STATE, "command": "prompt"}),
            (READY, STATE, {**STATE, "success": False}),
        ]
        for frames in cases:
            with self.subTest(frames=frames):
                result = await self.probe(output_script(*frames))
                self.assertFalse(result["runtime_ready"])
        result = await self.probe("import sys\nsys.stdin.read()\nprint('invalid json')\n")
        self.assertFalse(result["runtime_ready"])

    async def test_nonzero_exit_overrides_success_frames(self):
        result = await self.probe(output_script(READY, STATE, exit_code=3))
        self.assertFalse(result["runtime_ready"])

    async def test_missing_cwd_is_not_reported_as_missing_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = await self.probe(
                output_script(READY, STATE), work_dir=str(Path(tmp) / "missing"),
            )
        self.assertFalse(result["runtime_ready"])
        self.assertEqual(result["reason_code"], "runtime_startup_failed")

    async def test_large_stdout_and_stderr_are_bounded(self):
        for pipe in ("stdout", "stderr"):
            with self.subTest(pipe=pipe):
                with patch("multinexus.adapters.omp.STARTUP_OUTPUT_LIMIT", 1024):
                    result = await self.probe(
                        f"import sys,time\nsys.{pipe}.write('x'*4096)\n"
                        f"sys.{pipe}.flush()\ntime.sleep(30)\n"
                    )
                self.assertFalse(result["runtime_ready"])
                self.assertEqual(result["reason_code"], "runtime_output_limit")

    async def test_timeout_cleans_owned_process(self):
        real_spawn = asyncio.create_subprocess_exec
        processes = []

        async def spawn(*args, **kwargs):
            proc = await real_spawn(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
            processes.append(proc)
            return proc

        with (
            patch("multinexus.adapters.omp.asyncio.create_subprocess_exec", new=spawn),
            patch("multinexus.adapters.omp.STARTUP_TIMEOUT_SECONDS", 0.1),
        ):
            result = await OmpAdapter(config()).startup_check()
        self.assertEqual(result["reason_code"], "runtime_timeout")
        self.assertIsNotNone(processes[0].returncode)

    async def test_cancellation_cleans_owned_process_and_propagates(self):
        real_spawn = asyncio.create_subprocess_exec
        spawned = asyncio.Event()
        processes = []

        async def spawn(*args, **kwargs):
            proc = await real_spawn(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
            processes.append(proc)
            spawned.set()
            return proc

        with patch("multinexus.adapters.omp.asyncio.create_subprocess_exec", new=spawn):
            task = asyncio.create_task(OmpAdapter(config()).startup_check())
            await asyncio.wait_for(spawned.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(processes[0].returncode)

    @unittest.skipIf(sys.platform == "win32", "POSIX process-group assertion")
    async def test_timeout_cleans_descendant_holding_probe_pipes(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            script = (
                "import subprocess,sys,signal,time\n"
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\n"
                f"open({str(pid_file)!r},'w').write(str(child.pid))\n"
                "def stop(*args):\n child.wait(); sys.exit(0)\n"
                "signal.signal(signal.SIGTERM,stop)\n"
                "time.sleep(30)\n"
            )
            with patch("multinexus.adapters.omp.STARTUP_TIMEOUT_SECONDS", 0.5):
                result = await self.probe(script)
            self.assertEqual(result["reason_code"], "runtime_timeout")
            child_pid = int(pid_file.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    async def test_version_success_does_not_imply_runtime_readiness(self):
        proc = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"18.1.5", b"")))
        adapter = OmpAdapter(config())
        with (
            patch("multinexus.adapters.omp.asyncio.create_subprocess_exec", return_value=proc),
            patch.object(adapter, "startup_check", new=AsyncMock(return_value={
                "runtime_ready": False, "provider_checked": False,
                "reason_code": "runtime_not_writable",
            })),
        ):
            result = await adapter.health_check()
        self.assertTrue(result["binary_available"])
        self.assertFalse(result["runtime_ready"])
        self.assertFalse(result["available"])


class WorkerStartupGateTests(unittest.IsolatedAsyncioTestCase):
    def worker(self, check):
        worker = AgentdWorker(config(adapter="claude"))
        worker.adapter = SimpleNamespace(startup_check=check)
        worker.coordinate = SimpleNamespace(claim_job=AsyncMock())
        return worker

    async def wait_latched(self, worker):
        async def poll():
            while worker.health.state != "latched":
                await asyncio.sleep(0)
        await asyncio.wait_for(poll(), 1)

    async def test_failed_startup_stays_alive_but_never_claims_until_stopped(self):
        worker = self.worker(AsyncMock(return_value={"runtime_ready": False}))
        task = asyncio.create_task(worker.run(poll_interval=0.01))
        try:
            await self.wait_latched(worker)
            self.assertEqual(worker.health.reason_code, "adapter_not_ready")
            self.assertFalse(task.done())
            worker.coordinate.claim_job.assert_not_awaited()
        finally:
            worker.stop()
            await asyncio.wait_for(task, 1)

    async def test_probe_exception_is_sanitized_and_latched(self):
        worker = self.worker(AsyncMock(side_effect=RuntimeError("synthetic-secret")))
        with self.assertLogs("multinexus.agentd.worker", level="ERROR") as logs:
            task = asyncio.create_task(worker.run())
            try:
                await self.wait_latched(worker)
                worker.coordinate.claim_job.assert_not_awaited()
            finally:
                worker.stop()
                await asyncio.wait_for(task, 1)
        self.assertNotIn("synthetic-secret", "\n".join(logs.output))

    async def test_success_probes_once_before_claiming_and_keeps_normal_path(self):
        check = AsyncMock(return_value={"runtime_ready": True})
        worker = self.worker(check)
        claims = 0
        async def claim(**kwargs):
            nonlocal claims
            claims += 1
            self.assertEqual(check.await_count, 1)
            self.assertEqual(worker.health.state, "ready")
            if claims == 3:
                worker.stop()
            return {"claimed": False}
        worker.coordinate.claim_job.side_effect = claim
        await asyncio.wait_for(worker.run(poll_interval=0.01), 1)
        check.assert_awaited_once()
        self.assertEqual(worker.coordinate.claim_job.await_count, 3)

    async def test_cancelling_latched_worker_stops_it_without_claim(self):
        worker = self.worker(AsyncMock(return_value={"runtime_ready": False}))
        task = asyncio.create_task(worker.run())
        await self.wait_latched(worker)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(worker._running)
        self.assertEqual(worker.health.state, "stopped")
        worker.coordinate.claim_job.assert_not_awaited()

    async def test_stop_during_probe_does_not_resume_claims(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        async def check():
            entered.set()
            await release.wait()
            return {"runtime_ready": True}
        worker = self.worker(check)
        task = asyncio.create_task(worker.run())
        await asyncio.wait_for(entered.wait(), 1)
        worker.stop()
        release.set()
        await asyncio.wait_for(task, 1)
        worker.coordinate.claim_job.assert_not_awaited()
        self.assertEqual(worker.health.state, "stopped")

    async def test_contract_check_does_not_prematurely_publish_ready(self):
        worker = self.worker(AsyncMock(return_value={"runtime_ready": False}))
        worker.coordinate.get_runtime_contract = AsyncMock(return_value={"contract_version": 1})
        await worker.verify_coordinate_contract()
        self.assertEqual(worker.health.state, "starting")
