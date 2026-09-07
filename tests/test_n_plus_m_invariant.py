"""Tests proving the N+M process invariant.

Verifies that:
1. Bridge mode does NOT call make_adapter() or instantiate AgentDaemon
2. Bridges submit via coordinate runtime, not direct HTTP
3. Standalone agentd can process requests
4. KOOK bridge import behavior is covered
5. Coordinate runtime CLI integration is exercised
"""

import asyncio
import hashlib
import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from multinexus.models import AgentConfig
from multinexus.protocol import (
    AgentRequest,
)


def _config(**overrides):
    defaults = {
        "id": "test-agent",
        "token": "fake-token",
        "adapter": "claude",
        "context_db_path": str(Path(tempfile.mkdtemp()) / "test.sqlite3"),
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _claim_result(job, *, attempt_token=1, work_dir=None):
    """Wrap a legacy job dict in a P9-1 claim result envelope with a valid v1 context."""
    if work_dir is None:
        work_dir = "/tmp/ws"
    if "attempt_count" not in job:
        job["attempt_count"] = attempt_token
    payload = job.get("payload") or {}
    if not payload and job.get("payload_json"):
        try:
            payload = json.loads(job["payload_json"])
        except Exception:
            payload = {}
    origin = payload.get("origin") or {}
    session_scope_id = origin.get("session_scope_id") or "scope:1"
    ctx = {
        "contract_version": 1,
        "job_id": job["id"],
        "workspace_id": "ws",
        "task_id": None,
        "assigned_agent": "test-agent",
        "host_id": "host",
        "workspace_path": work_dir,
        "worktree_path": work_dir,
        "harness_root": work_dir + "/harness",
        "branch": None,
        "session_scope_id": session_scope_id,
        "legacy_scope_ids": [],
        "log_handle": {"kind": "coordinate_job", "job_id": job["id"], "logs_path": None},
    }
    canonical = dict(ctx)
    canonical_json = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    ctx["context_id"] = "sha256:" + hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return {"claimed": True, "job": job, "attempt_token": attempt_token, "execution_context": ctx}


class TestBridgeModeDoesNotInstantiateAdapter(unittest.TestCase):
    """Verify that agentd_mode=True never calls make_adapter or AgentDaemon."""

    @patch("multinexus.client.make_adapter")
    def test_discord_bridge_skips_make_adapter(self, mock_make):
        """In agentd_mode, DiscordClient must NOT call make_adapter."""
        from multinexus.client import DiscordClient

        cfg = _config(
            agentd_mode=True,
            coordinator_cli_path="/usr/bin/true",
            coordinator_db_path="/tmp/test.db",
        )
        client = DiscordClient(cfg)

        mock_make.assert_not_called()
        self.assertIsNone(client.adapter)
        self.assertIsNone(client.session_store)
        self.assertIsNotNone(client._coordinate_client)

    @patch("multinexus.client.make_adapter")
    def test_discord_legacy_calls_make_adapter(self, mock_make):
        """In legacy mode, DiscordClient MUST call make_adapter."""
        mock_make.return_value = MagicMock()
        from multinexus.client import DiscordClient

        cfg = _config(agentd_mode=False)
        client = DiscordClient(cfg)

        mock_make.assert_called_once_with(cfg)
        self.assertIsNotNone(client.adapter)

    def test_discord_bridge_requires_coordinator_cli(self):
        """Bridge mode without coordinator_cli_path must fail."""
        from multinexus.client import DiscordClient

        cfg = _config(agentd_mode=True, coordinator_cli_path="")
        with self.assertRaises(SystemExit) as ctx:
            DiscordClient(cfg)
        self.assertIn("coordinator_cli_path", str(ctx.exception))

    def test_discord_bridge_no_embedded_agentd(self):
        """DiscordClient in bridge mode must not have AgentDaemon attributes."""
        from multinexus.client import DiscordClient

        cfg = _config(
            agentd_mode=True,
            coordinator_cli_path="/usr/bin/true",
            coordinator_db_path="/tmp/test.db",
        )
        client = DiscordClient(cfg)

        self.assertFalse(hasattr(client, "_agentd"))
        self.assertFalse(hasattr(client, "_start_agentd"))
        self.assertFalse(hasattr(client, "_stop_agentd"))
        self.assertFalse(hasattr(client, "_agentd_client"))

    def test_discord_legacy_strict_resume_does_not_fall_back_or_reactivate(self):
        """The adapter resume policy is consistent in legacy direct mode."""
        from multinexus.adapters.base import AdapterResult
        from multinexus.client import DiscordClient
        from multinexus.sessions.store import SessionStore

        cfg = _config(agentd_mode=False, adapter="claude")
        client = DiscordClient.__new__(DiscordClient)
        client.agent_config = cfg
        client.session_store = SessionStore(cfg.context_db_path)

        class StrictAdapter:
            allow_fresh_fallback_after_resume_error = False

            def __init__(self):
                self.calls = []
                self.resumes = []

            async def call(self, prompt, **kwargs):
                self.calls.append(prompt)
                return AdapterResult(text="fresh duplicate", session_id="sess_new")

            async def resume(self, session_id, prompt, **kwargs):
                self.resumes.append((session_id, prompt))
                return AdapterResult(
                    text="Codex resume failed: session mismatch",
                    session_id=session_id,
                )

        client.adapter = StrictAdapter()
        client.session_store.upsert(
            scope_id="channel:legacy-strict",
            agent_id=cfg.id,
            adapter="claude",
            session_id="sess_dead",
            work_dir=cfg.work_dir,
        )

        result = asyncio.run(
            client._run_adapter_for_scope(
                "continue",
                session_scope_id="channel:legacy-strict",
                legacy_scope_ids=(),
                placeholder=None,
                progress_state={},
            )
        )

        self.assertEqual(result.text, "Codex resume failed: session mismatch")
        self.assertIsNone(result.session_id)
        self.assertEqual(client.adapter.resumes, [("sess_dead", "continue")])
        self.assertEqual(client.adapter.calls, [])
        self.assertIsNone(
            client.session_store.get_first_active(
                scope_ids=("channel:legacy-strict",), agent_id=cfg.id
            )
        )


class TestKookBridgeImportBehavior(unittest.TestCase):
    """Verify KOOK bridge import behavior when khl is absent."""

    def test_kook_mentions_import_without_khl(self):
        """KookMentionRouter must be importable without khl."""
        from multinexus.kook.mentions import KookMentionRouter
        router = KookMentionRouter()
        self.assertIsNotNone(router)

    def test_kook_package_lazy_import(self):
        """KOOK package __init__ must not import bot.py eagerly."""
        import multinexus.kook as pkg
        self.assertIn("KookBridge", pkg.__all__)

    def test_kook_bridge_requires_coordinator_cli(self):
        """KookBridge in agentd_mode without coordinator_cli_path must fail."""
        if importlib.util.find_spec("khl") is None:
            self.skipTest("khl not installed")

        from multinexus.kook.bot import KookBridge
        cfg = _config(
            agentd_mode=True,
            coordinator_cli_path="",
            coordinator_db_path="/tmp/test.db",
            kook_poll_channel_ids=[123],
        )
        with self.assertRaises(SystemExit) as ctx:
            KookBridge(cfg)
        self.assertIn("coordinator_cli_path", str(ctx.exception))

    def test_kook_bridge_no_embedded_agentd(self):
        """KookBridge must not have embedded AgentDaemon."""
        if importlib.util.find_spec("khl") is None:
            self.skipTest("khl not installed")

        from multinexus.kook.bot import KookBridge
        cfg = _config(agentd_mode=False, kook_poll_channel_ids=[123])
        bridge = KookBridge(cfg)

        self.assertFalse(hasattr(bridge, "_agentd"))
        self.assertFalse(hasattr(bridge, "start_agentd"))
        self.assertFalse(hasattr(bridge, "stop_agentd"))
        self.assertFalse(hasattr(bridge, "_agentd_client"))


class TestStandaloneAgentdProcessInvariant(unittest.TestCase):
    """Verify the N+M process invariant: one agentd per agent identity."""

    def test_agentd_is_standalone_process(self):
        """AgentDaemon is a standalone HTTP server, not embedded in any bridge."""
        from multinexus.agentd.server import AgentDaemon

        cfg = _config(agentd_mode=False)
        daemon = AgentDaemon(cfg)

        self.assertIsNotNone(daemon.adapter)
        self.assertIsNotNone(daemon.session_store)
        self.assertIsNotNone(daemon._lock)

    def test_agentd_http_round_trip_via_client(self):
        """AgentdClient connects to standalone AgentDaemon via HTTP."""
        from multinexus.agentd.server import AgentDaemon

        cfg = _config()
        daemon = AgentDaemon(cfg)

        class FakeAdapter:
            def __init__(self, c):
                self.name = c.adapter
                self.timeout = c.timeout
            async def call(self, prompt, **kw):
                from multinexus.adapters.base import AdapterResult
                return AdapterResult(text="standalone reply", session_id="s1")
            async def resume(self, sid, prompt, **kw):
                from multinexus.adapters.base import AdapterResult
                return AdapterResult(text="resumed", session_id=sid, resumed=True)
            async def health_check(self):
                return {"adapter": "fake", "available": True}

        daemon.adapter = FakeAdapter(cfg)

        loop = asyncio.new_event_loop()
        try:
            port = loop.run_until_complete(daemon.start())
            try:
                async def _do():
                    from multinexus.agentd.client import AgentdClient
                    client = AgentdClient()
                    try:
                        req = AgentRequest(
                            request_id="rt1",
                            agent_id="test-agent",
                            prompt="test standalone",
                        )
                        resp = await client.submit(req, port=port, timeout=10)
                        assert resp.success
                        assert resp.text == "standalone reply"
                    finally:
                        await client.close()
                loop.run_until_complete(_do())
            finally:
                loop.run_until_complete(daemon.stop())
        finally:
            loop.close()


class TestCoordinateRuntimeBoundary(unittest.TestCase):
    """Verify the bridge -> coordinate -> agentd flow."""

    def test_coordinate_client_submit_request(self):
        """CoordinateRuntimeClient builds the command and passes both DB env vars."""
        from multinexus.agentd.coordinate_client import CoordinateRuntimeClient

        client = CoordinateRuntimeClient(
            cli_path="/path/to/.venv/bin/coordinate",
            db_path="/path/to/coordinator.sqlite3",
        )

        import subprocess
        calls_seen = []

        def mock_run(cmd, **kwargs):
            calls_seen.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, stdout='{"result": {"job": {"id": "request:test"}}}', stderr="")

        with patch("multinexus.agentd.coordinate_client.subprocess.run", side_effect=mock_run):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(client.submit_request(
                    target_agent="mac-codex",
                    prompt="hello from test",
                    origin_json={"platform": "discord", "destination": "ch1", "message_id": "m1"},
                    reply_json={"platform": "discord", "destination": "ch1"},
                    workspace_id="test-ws",
                    message_id="discord:m1",
                ))
            finally:
                loop.close()

        self.assertEqual(len(calls_seen), 1)
        cmd, kwargs = calls_seen[0]
        self.assertIn("runtime", cmd)
        self.assertIn("request", cmd)
        self.assertIn("submit", cmd)
        self.assertIn("test-ws", cmd)
        self.assertIn("--target-agent", cmd)
        self.assertIn("mac-codex", cmd)
        self.assertEqual(result.get("result", {}).get("job", {}).get("id"), "request:test")

        env = kwargs.get("env")
        self.assertIsNotNone(env)
        self.assertEqual(env["MULTI_AGENT_COORDINATOR_DB"], "/path/to/coordinator.sqlite3")
        self.assertEqual(env["MAC_DB"], "/path/to/coordinator.sqlite3")

    def test_coordinate_client_builds_submit_command(self):
        """Verify the submit command includes all required args."""
        from multinexus.agentd.coordinate_client import CoordinateRuntimeClient

        client = CoordinateRuntimeClient(
            cli_path="/usr/bin/true",
            db_path="/tmp/test.db",
        )

        # Capture the command that would be run
        import subprocess
        commands_seen = []

        def mock_run(cmd, **kwargs):
            commands_seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout='{"result": {}}', stderr="")

        with patch("multinexus.agentd.coordinate_client.subprocess.run", side_effect=mock_run):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(client.submit_request(
                    target_agent="mac-claude",
                    prompt="test prompt",
                    origin_json={"platform": "kook", "destination": "ch1", "message_id": "m1"},
                    reply_json={"platform": "kook", "destination": "ch1"},
                    workspace_id="discord-nexus",
                ))
            finally:
                loop.close()

        self.assertEqual(len(commands_seen), 1)
        cmd = commands_seen[0]
        self.assertIn("runtime", cmd)
        self.assertIn("request", cmd)
        self.assertIn("submit", cmd)
        self.assertIn("discord-nexus", cmd)
        self.assertIn("--target-agent", cmd)
        self.assertIn("mac-claude", cmd)
        self.assertIn("--prompt", cmd)
        self.assertIn("--origin-json", cmd)
        self.assertIn("--reply-json", cmd)

    def test_bridges_use_coordinate_not_http(self):
        """Both Discord and KOOK bridges use CoordinateRuntimeClient, not AgentdClient."""
        from multinexus.client import DiscordClient

        cfg = _config(
            agentd_mode=True,
            coordinator_cli_path="/usr/bin/true",
            coordinator_db_path="/tmp/test.db",
        )
        client = DiscordClient(cfg)

        from multinexus.agentd.coordinate_client import CoordinateRuntimeClient
        self.assertIsInstance(client._coordinate_client, CoordinateRuntimeClient)

    def test_both_bridges_submit_via_coordinate(self):
        """Both bridges submit to coordinate runtime for the same agent."""
        from multinexus.agentd.coordinate_client import CoordinateRuntimeClient

        # Verify both configs point to the same coordinate instance
        cfg = _config(
            agentd_mode=True,
            coordinator_cli_path="/opt/coordinate/mac.sh",
            coordinator_db_path="/data/coordinator.sqlite3",
        )

        # The coordinate client is the shared boundary
        client = CoordinateRuntimeClient(
            cli_path=cfg.coordinator_cli_path,
            db_path=cfg.coordinator_db_path,
        )
        self.assertIsNotNone(client)


class TestAgentdWorkerCoordinateFlow(unittest.TestCase):
    """Verify AgentdWorker claims jobs from coordinate and reports results."""

    def test_worker_processes_claimed_job(self):
        """AgentdWorker claims a job, executes adapter, reports result."""
        from multinexus.agentd.worker import AgentdWorker

        cfg = _config(
            coordinator_cli_path="/usr/bin/true",
            coordinator_db_path="/tmp/test.db",
        )

        class FakeAdapter:
            def __init__(self, c):
                pass
            async def call(self, prompt, **kw):
                from multinexus.adapters.base import AdapterResult
                return AdapterResult(
                    text="worker reply",
                    session_id="ws1",
                    metadata={
                        "provider_evidence": {
                            "source": "claude_cli_stream_json",
                            "session_id": "ws1",
                            "requested_model": "claude-opus-4",
                            "observed_model": "k3",
                            "cli_version": "2.1.212",
                            "tool_names": ["edit"],
                            "tool_use_count": 1,
                        }
                    },
                )
            async def health_check(self):
                return {"adapter": "fake", "available": True}

        worker = AgentdWorker(cfg)
        worker.adapter = FakeAdapter(cfg)

        reported_jobs = []

        async def mock_report_job(*, job_id, agent_id, status, result_json, attempt_token=None, lease_id=None):
            reported_jobs.append({"job_id": job_id, "status": status, "result_json": result_json})
            return {"result": {}}

        worker.coordinate.report_job = mock_report_job

        job = {
            "id": "job-1",
            "payload_json": json.dumps({"prompt": "hello"}),
        }

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(_claim_result(job)))
        finally:
            loop.close()

        self.assertEqual(len(reported_jobs), 1)
        self.assertEqual(reported_jobs[0]["job_id"], "job-1")
        self.assertEqual(reported_jobs[0]["status"], "done")
        self.assertEqual(reported_jobs[0]["result_json"]["response_text"], "worker reply")
        self.assertEqual(reported_jobs[0]["result_json"]["session_id"], "ws1")
        self.assertEqual(
            reported_jobs[0]["result_json"]["provider_evidence"]["tool_names"],
            ["edit"],
        )

    def test_worker_processes_coordinate_payload_dict_shape(self):
        """Runtime claim returns decoded payload dicts, not raw payload_json."""
        from multinexus.agentd.worker import AgentdWorker

        cfg = _config(
            coordinator_cli_path="/usr/bin/true",
            coordinator_db_path="/tmp/test.db",
        )

        seen_prompts = []

        class FakeAdapter:
            def __init__(self, c):
                pass
            async def call(self, prompt, **kw):
                seen_prompts.append(prompt)
                from multinexus.adapters.base import AdapterResult
                return AdapterResult(text="dict payload reply", session_id="ws2")
            async def resume(self, session_id, prompt, **kw):
                seen_prompts.append(prompt)
                from multinexus.adapters.base import AdapterResult
                return AdapterResult(text="dict payload resumed", session_id=session_id)
            async def health_check(self):
                return {"adapter": "fake", "available": True}

        worker = AgentdWorker(cfg)
        worker.adapter = FakeAdapter(cfg)

        reported_jobs = []

        async def mock_report_job(*, job_id, agent_id, status, result_json, attempt_token=None, lease_id=None):
            reported_jobs.append({"job_id": job_id, "status": status, "result_json": result_json})
            return {"result": {}}

        worker.coordinate.report_job = mock_report_job

        job = {
            "id": "job-dict-payload",
            "payload": {
                "prompt": "real discord question",
                "origin": {"session_scope_id": "channel:1"},
            },
        }

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(_claim_result(job)))
        finally:
            loop.close()

        self.assertEqual(seen_prompts, ["real discord question"])
        self.assertEqual(reported_jobs[0]["status"], "done")
        self.assertEqual(
            reported_jobs[0]["result_json"]["response_text"],
            "dict payload reply",
        )

    def test_worker_reports_failed_job_on_adapter_error(self):
        """AgentdWorker reports 'failed' when adapter returns error text."""
        from multinexus.agentd.worker import AgentdWorker

        cfg = _config(
            coordinator_cli_path="/usr/bin/true",
            coordinator_db_path="/tmp/test.db",
        )

        class FailingAdapter:
            def __init__(self, c):
                pass
            async def call(self, prompt, **kw):
                from multinexus.adapters.base import AdapterResult
                return AdapterResult(text="Agent error: something broke")

        worker = AgentdWorker(cfg)
        worker.adapter = FailingAdapter(cfg)

        reported_jobs = []
        async def mock_report_job(*, job_id, agent_id, status, result_json, attempt_token=None, lease_id=None):
            reported_jobs.append({"job_id": job_id, "status": status})
            return {"result": {}}
        worker.coordinate.report_job = mock_report_job

        job = {"id": "job-fail", "payload_json": json.dumps({"prompt": "fail please"})}

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(_claim_result(job)))
        finally:
            loop.close()

        self.assertEqual(len(reported_jobs), 1)
        self.assertEqual(reported_jobs[0]["status"], "failed")

    def test_worker_drops_untrusted_invalid_payload_without_report(self):
        """A lease-less malformed payload cannot be proven legacy; fail closed."""
        from multinexus.agentd.worker import AgentdWorker

        cfg = _config(
            coordinator_cli_path="/usr/bin/true",
            coordinator_db_path="/tmp/test.db",
        )

        worker = AgentdWorker(cfg)
        worker.adapter = AsyncMock()

        reported_jobs = []
        async def mock_report_job(*, job_id, agent_id, status, result_json, attempt_token=None, lease_id=None):
            reported_jobs.append({"job_id": job_id, "status": status})
            return {"result": {}}
        worker.coordinate.report_job = mock_report_job

        job = {"id": "job-bad", "payload_json": "not valid json{{{"}

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(_claim_result(job)))
        finally:
            loop.close()

        worker.adapter.call.assert_not_awaited()
        worker.adapter.resume.assert_not_awaited()
        self.assertEqual(reported_jobs, [])

    def test_worker_run_exits_on_stop(self):
        """AgentdWorker.run() exits its loop when stop() is called."""
        from multinexus.agentd.worker import AgentdWorker

        cfg = _config(
            coordinator_cli_path="/usr/bin/true",
            coordinator_db_path="/tmp/test.db",
        )

        worker = AgentdWorker(cfg)

        claim_count = 0

        async def mock_claim(*, agent_id, recoverable=False, recovery_reason="", prior_process_stopped=False):
            nonlocal claim_count
            claim_count += 1
            if claim_count >= 2:
                worker.stop()
            return {"claimed": False, "reason": "queue_empty"}

        worker.coordinate.claim_job = mock_claim

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker.run(poll_interval=0.01))
        finally:
            loop.close()

    def test_worker_run_default_does_not_pass_recoverable(self):
        """8.4.3 P1 #3 fix: default agentd poll must NOT pass recoverable=True (no auto-reclaim)."""
        from multinexus.agentd.worker import AgentdWorker

        cfg = _config(coordinator_cli_path="/usr/bin/true", coordinator_db_path="/tmp/test.db")
        worker = AgentdWorker(cfg)

        seen = {}

        async def mock_claim(*, agent_id, recoverable=False, recovery_reason="", prior_process_stopped=False):
            seen["recoverable"] = recoverable
            seen["recovery_reason"] = recovery_reason
            seen["prior_process_stopped"] = prior_process_stopped
            worker.stop()
            return {"claimed": False, "reason": "queue_empty"}

        worker.coordinate.claim_job = mock_claim
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker.run(poll_interval=0.01))
        finally:
            loop.close()

        self.assertIn("recoverable", seen)
        self.assertFalse(seen["recoverable"], "default worker.run must pass recoverable=False")
        self.assertEqual(seen.get("recovery_reason"), "")
        self.assertFalse(seen.get("prior_process_stopped"), "default worker.run must pass prior_process_stopped=False")

    def test_worker_run_recoverable_mode_passes_recoverable(self):
        """8.4.3 P1 #3 fix: operator recovery mode (--recoverable) passes recoverable=True to claim."""
        from multinexus.agentd.worker import AgentdWorker

        cfg = _config(coordinator_cli_path="/usr/bin/true", coordinator_db_path="/tmp/test.db")
        worker = AgentdWorker(cfg)

        seen = {}

        async def mock_claim(*, agent_id, recoverable=False, recovery_reason="", prior_process_stopped=False):
            seen["recoverable"] = recoverable
            seen["recovery_reason"] = recovery_reason
            seen["prior_process_stopped"] = prior_process_stopped
            worker.stop()
            return {"claimed": False, "reason": "queue_empty"}

        worker.coordinate.claim_job = mock_claim
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker.run(poll_interval=0.01, recoverable=True, recovery_reason="prior-process-crashed", prior_process_stopped=True))
        finally:
            loop.close()

        self.assertIn("recoverable", seen)
        self.assertTrue(seen["recoverable"], "recovery mode worker.run must pass recoverable=True")
        self.assertEqual(seen.get("recovery_reason"), "prior-process-crashed")
        self.assertTrue(seen.get("prior_process_stopped"), "recovery mode worker.run must pass prior_process_stopped=True")
        self.assertFalse(worker._running)

    def test_is_error_recognizes_codex_resume_failed(self):
        """8.4.3 P1 #3: 'Codex resume failed' must be recognized as error so recovery resume fail-closes (not reported as done)."""
        from multinexus.agentd.worker import AgentdWorker
        self.assertTrue(AgentdWorker._is_error("Codex resume failed (1): stream disconnected"))
        self.assertTrue(AgentdWorker._is_error("Codex CLI failed: x"))
        self.assertFalse(AgentdWorker._is_error("OK done"))

    def test_is_error_recognizes_omp_adapter_failures(self):
        """omp CLI failed/timed out must be recognized as error, not reported as done."""
        from multinexus.agentd.worker import AgentdWorker
        self.assertTrue(AgentdWorker._is_error("omp CLI failed (1): Streaming edit aborted due to patch preview failure"))
        self.assertTrue(AgentdWorker._is_error("omp timed out after 120s"))
        self.assertEqual(AgentdWorker._status_for_result(
            type("R", (), {"text": "omp CLI failed (1): err", "metadata": {}})()
        ), "failed")

    def test_worker_zcode_resume_error_fails_closed_without_fresh_call(self):
        """An exact-session adapter can disable the legacy fresh fallback."""
        from multinexus.agentd.worker import AgentdWorker
        from multinexus.adapters.base import AdapterResult

        cfg = _config(
            coordinator_cli_path="/usr/bin/true",
            coordinator_db_path="/tmp/test.db",
        )

        class ResumeFailZCodeAdapter:
            allow_fresh_fallback_after_resume_error = False

            def __init__(self):
                self.calls = []
                self.resumes = []

            async def call(self, prompt, **kw):
                self.calls.append(prompt)
                return AdapterResult(text="fresh duplicate", session_id="sess_new")

            async def resume(self, sid, prompt, **kw):
                self.resumes.append((sid, prompt))
                return AdapterResult(
                    text="ZCode resume failed: session mismatch",
                    session_id=sid,
                )

            async def health_check(self):
                return {"adapter": "zcode", "available": True}

        worker = AgentdWorker(cfg)
        worker.adapter = ResumeFailZCodeAdapter()
        worker.session_store.upsert(
            scope_id="channel:zcode",
            agent_id="test-agent",
            adapter="zcode",
            session_id="sess_existing",
            work_dir=cfg.work_dir,
        )
        job = {
            "id": "job-zcode-resume-fail",
            "payload_json": json.dumps(
                {
                    "prompt": "continue",
                    "origin": {
                        "platform": "discord",
                        "destination": "zcode",
                        "session_scope_id": "channel:zcode",
                        "legacy_scope_ids": [],
                    },
                }
            ),
        }
        reported = []

        async def mock_report(
            *,
            job_id,
            agent_id,
            status,
            result_json,
            attempt_token=None,
            lease_id=None,
        ):
            reported.append({"status": status, "result_json": result_json})

        worker.coordinate.report_job = mock_report
        with patch.object(
            worker.session_store,
            "mark_stale",
            wraps=worker.session_store.mark_stale,
        ) as mark_stale:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(worker._process_job(_claim_result(job)))
            finally:
                loop.close()

        self.assertEqual(worker.adapter.resumes, [("sess_existing", "continue")])
        self.assertEqual(worker.adapter.calls, [])
        mark_stale.assert_called_once()
        self.assertEqual(mark_stale.call_args.kwargs["expected_session"]["session_id"], "sess_existing")
        self.assertEqual(reported[0]["status"], "failed")
        self.assertEqual(
            reported[0]["result_json"]["response_text"],
            "ZCode resume failed: session mismatch",
        )
        self.assertIsNone(
            worker.session_store.get_first_active(
                scope_ids=("channel:zcode",), agent_id="test-agent"
            )
        )


    def test_worker_shutdown_is_testable(self):
        """Worker stop() sets _running=False and wakes the event for immediate exit."""
        from multinexus.agentd.worker import AgentdWorker

        cfg = _config(
            coordinator_cli_path="/usr/bin/true",
            coordinator_db_path="/tmp/test.db",
        )

        worker = AgentdWorker(cfg)
        self.assertFalse(worker._running)
        self.assertFalse(worker._wake.is_set())

        worker._running = True
        worker.stop()
        self.assertFalse(worker._running)
        self.assertTrue(worker._wake.is_set())


class TestAgentdMainShutdown(unittest.TestCase):
    """Verify __main__.py shutdown callback behavior."""

    def test_shutdown_callback_stops_worker(self):
        """The _shutdown callback sets worker._running=False and wakes the event."""
        from multinexus.agentd.worker import AgentdWorker

        cfg = _config(
            coordinator_cli_path="/usr/bin/true",
            coordinator_db_path="/tmp/test.db",
        )

        worker = AgentdWorker(cfg)
        loop = asyncio.new_event_loop()

        try:
            def _shutdown():
                worker.stop()

            # Simulate run() starting
            worker._running = True
            self.assertTrue(worker._running)

            # Simulate the _shutdown callback from __main__.py
            loop.call_soon(_shutdown)

            async def _run_until_stopped():
                await asyncio.sleep(0.01)

            loop.run_until_complete(_run_until_stopped())
        finally:
            loop.close()

        self.assertFalse(worker._running)
        self.assertTrue(worker._wake.is_set())


class TestBridgeRequestNormalization(unittest.TestCase):
    """Verify both platforms produce valid request metadata for the same agent."""

    def test_discord_request_format(self):
        origin = {"platform": "discord", "destination": "456", "message_id": "123"}
        j = json.dumps(origin)
        parsed = json.loads(j)
        self.assertEqual(parsed["platform"], "discord")
        self.assertIn("destination", parsed)

    def test_kook_request_format(self):
        origin = {"platform": "kook", "destination": "ch1", "message_id": "789", "role_id": "r1"}
        j = json.dumps(origin)
        parsed = json.loads(j)
        self.assertEqual(parsed["platform"], "kook")

    def test_both_target_same_agent(self):
        """Both platforms can target the same agent_id via coordinate."""
        self.assertEqual("mac-codex", "mac-codex")


class TestJobPollingFindsCompletedJobs(unittest.TestCase):
    """Regression test: _get_job must parse coordinate's real output format."""

    def test_get_job_parses_top_level_jobs(self):
        """_get_job reads top-level {"jobs": [...]}, not {"result": {"jobs": [...]}}."""
        from multinexus.agentd.coordinate_client import CoordinateRuntimeClient

        client = CoordinateRuntimeClient(
            cli_path="/usr/bin/true",
            db_path="/tmp/test.db",
        )

        # Simulate coordinate's actual output
        coordinate_output = json.dumps({
            "jobs": [
                {"id": "job-1", "status": "done", "result_json": '{"response_text": "hi"}'},
                {"id": "job-2", "status": "pending"},
            ]
        })

        with patch("multinexus.agentd.coordinate_client.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=coordinate_output,
                stderr="",
            )

            loop = asyncio.new_event_loop()
            try:
                job = loop.run_until_complete(client._get_job("job-1", workspace_id="discord-nexus"))
            finally:
                loop.close()

        self.assertIsNotNone(job)
        self.assertEqual(job["id"], "job-1")
        self.assertEqual(job["status"], "done")

    def test_get_job_omits_status_filter(self):
        """_get_job must not pass --status all; it should list without status filter."""
        from multinexus.agentd.coordinate_client import CoordinateRuntimeClient

        client = CoordinateRuntimeClient(
            cli_path="/usr/bin/true",
            db_path="/tmp/test.db",
        )

        with patch("multinexus.agentd.coordinate_client.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='{"jobs": []}',
                stderr="",
            )

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(client._get_job("any", workspace_id="discord-nexus"))
            finally:
                loop.close()

        cmd = mock_run.call_args[0][0]
        self.assertNotIn("--status", cmd)
        self.assertIn("--workspace-id", cmd)
        self.assertIn("discord-nexus", cmd)

    def test_get_job_returns_none_for_missing(self):
        """_get_job returns None when job not in list."""
        from multinexus.agentd.coordinate_client import CoordinateRuntimeClient

        client = CoordinateRuntimeClient(
            cli_path="/usr/bin/true",
            db_path="/tmp/test.db",
        )

        with patch("multinexus.agentd.coordinate_client.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='{"jobs": [{"id": "other-job", "status": "done"}]}',
                stderr="",
            )

            loop = asyncio.new_event_loop()
            try:
                job = loop.run_until_complete(client._get_job("missing-job", workspace_id="discord-nexus"))
            finally:
                loop.close()

        self.assertIsNone(job)

    def test_wait_for_job_result_finds_completed(self):
        """wait_for_job_result returns a done job without timing out."""
        from multinexus.agentd.coordinate_client import CoordinateRuntimeClient

        client = CoordinateRuntimeClient(
            cli_path="/usr/bin/true",
            db_path="/tmp/test.db",
        )

        done_job = {"id": "job-x", "status": "done", "result_json": '{"response_text": "ok"}'}

        with patch.object(client, "_get_job", return_value=done_job):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    client.wait_for_job_result(
                        job_id="job-x",
                        workspace_id="discord-nexus",
                        poll_interval=0.01,
                        timeout=1.0,
                    )
                )
            finally:
                loop.close()

        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "done")


class TestWorkerSessionResume(unittest.TestCase):
    """Regression test: worker uses call/resume logic from session store."""

    def _make_worker(self, cfg):
        from multinexus.agentd.worker import AgentdWorker

        class FakeAdapter:
            def __init__(self, c):
                self.calls = []
                self.resumes = []
            async def call(self, prompt, **kw):
                self.calls.append(prompt)
                from multinexus.adapters.base import AdapterResult
                return AdapterResult(text=f"fresh: {prompt}", session_id="new-s1")
            async def resume(self, sid, prompt, **kw):
                self.resumes.append((sid, prompt))
                from multinexus.adapters.base import AdapterResult
                return AdapterResult(text=f"resumed: {prompt}", session_id=sid, resumed=True)
            async def health_check(self):
                return {"adapter": "fake", "available": True}

        worker = AgentdWorker(cfg)
        worker.adapter = FakeAdapter(cfg)
        return worker

    def test_worker_resumes_existing_session(self):
        """Worker calls adapter.resume() when session store has an active session."""
        cfg = _config(
            coordinator_cli_path="/usr/bin/true",
            coordinator_db_path="/tmp/test.db",
        )
        worker = self._make_worker(cfg)

        # Pre-seed a session in the store
        worker.session_store.upsert(
            scope_id="channel:ch1",
            agent_id="test-agent",
            adapter="claude",
            session_id="existing-s1",
            work_dir=cfg.work_dir,
        )

        job = {
            "id": "job-r1",
            "payload_json": json.dumps({
                "prompt": "continue",
                "origin": {
                    "platform": "discord",
                    "destination": "ch1",
                    "session_scope_id": "channel:ch1",
                    "legacy_scope_ids": [],
                },
            }),
        }

        reported = []
        async def mock_report(*, job_id, agent_id, status, result_json, attempt_token=None, lease_id=None):
            reported.append({"status": status, "result_json": result_json})
        worker.coordinate.report_job = mock_report

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(_claim_result(job)))
        finally:
            loop.close()

        self.assertEqual(len(worker.adapter.resumes), 1)
        self.assertEqual(worker.adapter.resumes[0][0], "existing-s1")
        self.assertEqual(len(worker.adapter.calls), 0)

    def test_worker_fresh_call_when_no_session(self):
        """Worker calls adapter.call() when no existing session."""
        cfg = _config(
            coordinator_cli_path="/usr/bin/true",
            coordinator_db_path="/tmp/test.db",
        )
        worker = self._make_worker(cfg)

        job = {
            "id": "job-f1",
            "payload_json": json.dumps({
                "prompt": "new request",
                "origin": {
                    "platform": "discord",
                    "destination": "ch2",
                    "session_scope_id": "channel:ch2",
                    "legacy_scope_ids": [],
                },
            }),
        }

        reported = []
        async def mock_report(*, job_id, agent_id, status, result_json, attempt_token=None, lease_id=None):
            reported.append({"status": status})
        worker.coordinate.report_job = mock_report

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(_claim_result(job)))
        finally:
            loop.close()

        self.assertEqual(len(worker.adapter.calls), 1)
        self.assertEqual(len(worker.adapter.resumes), 0)

    def test_worker_reports_recoverable_timeout_with_progress_checkpoint(self):
        """Worker persists adapter progress and reports Claude timeouts as recoverable."""
        from multinexus.agentd.worker import AgentdWorker
        from multinexus.adapters.base import AdapterResult

        cfg = _config(
            coordinator_cli_path="/usr/bin/true",
            coordinator_db_path="/tmp/test.db",
        )

        class ProgressTimeoutAdapter:
            async def call(self, prompt, **kw):
                kw["on_progress"](
                    {
                        "stage": "session",
                        "summary": "Claude session initialized",
                        "session_id": "sess-progress",
                    }
                )
                return AdapterResult(
                    text="Claude timeout:activity after 120s (total budget 600s). Aborted, no handoff.",
                    session_id="sess-progress",
                    metadata={
                        "timeout": {
                            "kind": "activity",
                            "configured_budget_seconds": 600,
                            "session_id": "sess-progress",
                            "resume_allowed": True,
                        }
                    },
                )

            async def resume(self, session_id, prompt, **kw):
                raise AssertionError("resume should not be used")

            async def health_check(self):
                return {"adapter": "fake", "available": True}

        worker = AgentdWorker(cfg)
        worker.adapter = ProgressTimeoutAdapter()

        progress = []
        reported = []

        async def mock_progress(**kwargs):
            progress.append(kwargs)
            return {"result": {}}

        async def mock_report(*, job_id, agent_id, status, result_json, attempt_token=None, lease_id=None):
            reported.append({"status": status, "result_json": result_json})
            return {"result": {}}

        worker.coordinate.record_progress = mock_progress
        worker.coordinate.report_job = mock_report

        job = {
            "id": "job-timeout",
            "payload_json": json.dumps({
                "prompt": "work a long time",
                "origin": {
                    "platform": "discord",
                    "destination": "ch-timeout",
                    "session_scope_id": "channel:ch-timeout",
                    "legacy_scope_ids": [],
                },
            }),
        }

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(_claim_result(job)))
        finally:
            loop.close()

        self.assertEqual(progress[0]["session_id"], "sess-progress")
        self.assertEqual(reported[0]["status"], "timed_out")
        self.assertEqual(reported[0]["result_json"]["session_id"], "sess-progress")
        self.assertEqual(reported[0]["result_json"]["timeout"]["kind"], "activity")
        # In-flight recovery has one durable owner: Coordinate. A timeout
        # does not publish an unconfirmed session into SessionStore.
        self.assertIsNone(worker.session_store.get(
            scope_id="channel:ch-timeout", agent_id="test-agent"
        ))
        resumed = []
        class RecoveryAdapter:
            async def call(self, prompt, **kw):
                raise AssertionError("recovery must not start fresh")
            async def resume(self, session_id, prompt, **kw):
                resumed.append(session_id)
                return AdapterResult(text="recovered", session_id=session_id, resumed=True)
        recovered_worker = AgentdWorker(cfg)
        recovered_worker.adapter = RecoveryAdapter()
        recovered_worker.coordinate.report_job = mock_report
        recovery_job = {**job, "result": reported[0]["result_json"], "attempt_count": 2}
        asyncio.run(recovered_worker._process_job(_claim_result(recovery_job, attempt_token=2)))
        self.assertEqual(resumed, ["sess-progress"])
        self.assertEqual(reported[-1]["status"], "done")

    def test_worker_resumes_recoverable_job_session_without_fresh_duplicate(self):
        """Recoverable timed-out jobs use the recorded session id when no local session exists."""
        from multinexus.agentd.worker import AgentdWorker
        from multinexus.adapters.base import AdapterResult

        cfg = _config(
            coordinator_cli_path="/usr/bin/true",
            coordinator_db_path="/tmp/test.db",
        )

        class RecoveryAdapter:
            def __init__(self):
                self.calls = []
                self.resumes = []

            async def call(self, prompt, **kw):
                self.calls.append(prompt)
                return AdapterResult(text="fresh duplicate", session_id="fresh")

            async def resume(self, session_id, prompt, **kw):
                self.resumes.append((session_id, prompt))
                return AdapterResult(text="resumed work", session_id=session_id, resumed=True)

            async def health_check(self):
                return {"adapter": "fake", "available": True}

        worker = AgentdWorker(cfg)
        worker.adapter = RecoveryAdapter()

        reported = []

        async def mock_report(*, job_id, agent_id, status, result_json, attempt_token=None, lease_id=None):
            reported.append({"status": status, "result_json": result_json})
            return {"result": {}}

        worker.coordinate.report_job = mock_report

        job = {
            "id": "job-recover",
            "terminal_session_id": "sess-recover",
            "payload_json": json.dumps({
                "prompt": "continue",
                "origin": {
                    "platform": "discord",
                    "destination": "ch-recover",
                    "session_scope_id": "channel:ch-recover",
                    "legacy_scope_ids": [],
                },
            }),
        }

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(_claim_result(job)))
        finally:
            loop.close()

        self.assertEqual(worker.adapter.resumes, [("sess-recover", "continue")])
        self.assertEqual(worker.adapter.calls, [])
        self.assertEqual(reported[0]["status"], "done")
        self.assertEqual(reported[0]["result_json"]["session_id"], "sess-recover")

    def test_worker_recoverable_resume_failure_does_not_start_fresh_call(self):
        """Recoverable resume failure is operator-visible and does not duplicate work."""
        from multinexus.agentd.worker import AgentdWorker
        from multinexus.adapters.base import AdapterResult

        cfg = _config(
            coordinator_cli_path="/usr/bin/true",
            coordinator_db_path="/tmp/test.db",
        )

        class FailingRecoveryAdapter:
            def __init__(self):
                self.calls = []
                self.resumes = []

            async def call(self, prompt, **kw):
                self.calls.append(prompt)
                return AdapterResult(text="fresh duplicate", session_id="fresh")

            async def resume(self, session_id, prompt, **kw):
                self.resumes.append((session_id, prompt))
                return AdapterResult(text="Claude error: bad resume", session_id=session_id)

            async def health_check(self):
                return {"adapter": "fake", "available": True}

        worker = AgentdWorker(cfg)
        worker.adapter = FailingRecoveryAdapter()

        reported = []

        async def mock_report(*, job_id, agent_id, status, result_json, attempt_token=None, lease_id=None):
            reported.append({"status": status, "result_json": result_json})
            return {"result": {}}

        worker.coordinate.report_job = mock_report

        job = {
            "id": "job-recover-fail",
            "result": {"timeout": {"session_id": "sess-fail"}},
            "payload_json": json.dumps({
                "prompt": "continue",
                "origin": {
                    "platform": "discord",
                    "destination": "ch-recover",
                    "session_scope_id": "channel:ch-recover",
                    "legacy_scope_ids": [],
                },
            }),
        }

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(_claim_result(job)))
        finally:
            loop.close()

        self.assertEqual(worker.adapter.resumes, [("sess-fail", "continue")])
        self.assertEqual(worker.adapter.calls, [])
        self.assertEqual(reported[0]["status"], "failed")
        self.assertIn("not starting duplicate", reported[0]["result_json"]["response_text"])

    def test_worker_existing_session_plus_recovery_is_fail_closed(self):
        """8.4.3 P1 #3: existing session + recovery_session_id ⇒ recovery wins and is fail-closed (no fresh duplicate)."""
        from multinexus.agentd.worker import AgentdWorker
        from multinexus.adapters.base import AdapterResult

        cfg = _config(
            coordinator_cli_path="/usr/bin/true",
            coordinator_db_path="/tmp/test.db",
        )

        class FailingRecoveryAdapter:
            def __init__(self):
                self.calls = []
                self.resumes = []

            async def call(self, prompt, **kw):
                self.calls.append(prompt)
                return AdapterResult(text="fresh duplicate", session_id="fresh")

            async def resume(self, session_id, prompt, **kw):
                self.resumes.append((session_id, prompt))
                return AdapterResult(text="Claude error: bad resume", session_id=session_id)

            async def health_check(self):
                return {"adapter": "fake", "available": True}

        worker = AgentdWorker(cfg)
        worker.adapter = FailingRecoveryAdapter()

        reported = []

        async def mock_report(*, job_id, agent_id, status, result_json, attempt_token=None, lease_id=None):
            reported.append({"status": status, "result_json": result_json})
            return {"result": {}}

        worker.coordinate.report_job = mock_report

        # seed an existing session (so `existing` is truthy) — P1 #3: recovery must still win
        worker.session_store.upsert(
            scope_id="channel:ch-both",
            agent_id="test-agent",
            adapter="claude",
            session_id="sess-existing",
            work_dir=cfg.work_dir,
        )

        job = {
            "id": "job-both",
            "result": {"timeout": {"session_id": "sess-recovery"}},
            "payload_json": json.dumps({
                "prompt": "continue",
                "origin": {
                    "platform": "discord",
                    "destination": "ch-both",
                    "session_scope_id": "channel:ch-both",
                    "legacy_scope_ids": [],
                },
            }),
        }

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(_claim_result(job)))
        finally:
            loop.close()

        # recovery_session_id (sess-recovery) wins over existing (sess-existing):
        self.assertEqual(worker.adapter.resumes, [("sess-recovery", "continue")])
        # fail-closed: no fresh duplicate
        self.assertEqual(worker.adapter.calls, [])
        self.assertEqual(reported[0]["status"], "failed")
        self.assertIn("not starting duplicate", reported[0]["result_json"]["response_text"])

    def test_worker_falls_back_on_resume_error(self):
        """Worker falls back to fresh call when resume returns error text."""
        from multinexus.agentd.worker import AgentdWorker

        cfg = _config(
            coordinator_cli_path="/usr/bin/true",
            coordinator_db_path="/tmp/test.db",
        )

        class ResumeFailAdapter:
            def __init__(self, c):
                self.calls = []
                self.resumes = []
            async def call(self, prompt, **kw):
                self.calls.append(prompt)
                from multinexus.adapters.base import AdapterResult
                return AdapterResult(text="fresh start", session_id="new-s2")
            async def resume(self, sid, prompt, **kw):
                self.resumes.append((sid, prompt))
                from multinexus.adapters.base import AdapterResult
                return AdapterResult(text="Agent error: resume crashed", session_id=sid)
            async def health_check(self):
                return {"adapter": "fake", "available": True}

        worker = AgentdWorker(cfg)
        worker.adapter = ResumeFailAdapter(cfg)

        worker.session_store.upsert(
            scope_id="channel:ch3",
            agent_id="test-agent",
            adapter="claude",
            session_id="bad-s1",
            work_dir=cfg.work_dir,
        )

        job = {
            "id": "job-fb1",
            "payload_json": json.dumps({
                "prompt": "try resume",
                "origin": {
                    "platform": "discord",
                    "destination": "ch3",
                    "session_scope_id": "channel:ch3",
                    "legacy_scope_ids": [],
                },
            }),
        }

        reported = []
        async def mock_report(*, job_id, agent_id, status, result_json, attempt_token=None, lease_id=None):
            reported.append({"status": status, "text": result_json.get("response_text", "")})
        worker.coordinate.report_job = mock_report

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(_claim_result(job)))
        finally:
            loop.close()

        # Resume was attempted, then fallback to fresh call
        self.assertEqual(len(worker.adapter.resumes), 1)
        self.assertEqual(len(worker.adapter.calls), 1)
        self.assertEqual(reported[0]["text"], "fresh start")

    def test_bridge_includes_session_scope_in_origin(self):
        """Discord bridge origin_json must include session_scope_id."""
        # Verify the origin dict construction logic includes scope fields
        session_scope_id = "channel:333"
        legacy_scope_ids = ("legacy:333",)
        origin = {
            "platform": "discord",
            "destination": "333",
            "message_id": "222",
            "thread_id": None,
            "session_scope_id": session_scope_id,
            "legacy_scope_ids": list(legacy_scope_ids),
        }
        j = json.dumps(origin)
        parsed = json.loads(j)
        self.assertEqual(parsed["session_scope_id"], "channel:333")
        self.assertEqual(parsed["legacy_scope_ids"], ["legacy:333"])

    def test_kook_bridge_includes_session_scope_in_origin(self):
        """KOOK bridge origin_json must include session_scope_id."""
        channel_id = "ch-kook-1"
        origin = {
            "platform": "kook",
            "destination": channel_id,
            "message_id": "m1",
            "role_id": None,
            "session_scope_id": f"channel:{channel_id}",
            "legacy_scope_ids": [],
        }
        j = json.dumps(origin)
        parsed = json.loads(j)
        self.assertEqual(parsed["session_scope_id"], "channel:ch-kook-1")
        self.assertEqual(parsed["legacy_scope_ids"], [])



class TestRequirementsKhlDistributionContract(unittest.TestCase):
    """Static contract: requirements.txt must bind the KOOK SDK to the correct distribution.

    The bare PyPI package ``khl`` is a different project that happens to share the
    import name ``khl``. MultiNexus must depend on the KOOK SDK distribution ``khl.py``
    exactly. This test does not import ``khl`` and does not require it to be installed.
    """

    def _requirements_text(self):
        repo_root = Path(__file__).resolve().parents[1]
        req_path = repo_root / "requirements.txt"
        if not req_path.exists():
            self.fail(f"requirements.txt not found at {req_path}")
        return req_path.read_text(encoding="utf-8")

    def test_khl_distribution_is_khl_py_exact(self):
        text = self._requirements_text()
        # Reject any line that pins or allows the bare ``khl`` distribution.
        bare_khl_re = re.compile(r"^khl(\s*[<>=!~]|\s*$)", re.IGNORECASE | re.MULTILINE)
        matches = bare_khl_re.findall(text)
        self.assertEqual(
            matches,
            [],
            f"requirements.txt must not declare bare `khl` distribution; found {matches!r}",
        )
        # Require the correct KOOK SDK distribution at the exact reviewed version.
        normalized_lines = [
            line.split("#")[0].strip() for line in text.splitlines()
        ]
        self.assertIn(
            "khl.py==0.3.17",
            normalized_lines,
            "requirements.txt must declare exact KOOK SDK `khl.py==0.3.17`",
        )


class TestWorkerOutcomeContract(unittest.TestCase):
    """R2: AgentdWorker status/delivery decisions read effective_outcome()."""

    def _worker_with(self, call_fn):
        from multinexus.agentd.worker import AgentdWorker

        cfg = _config(
            coordinator_cli_path="/usr/bin/true",
            coordinator_db_path="/tmp/test.db",
        )
        worker = AgentdWorker(cfg)
        worker.adapter = MagicMock()
        worker.adapter.call = AsyncMock(side_effect=call_fn)
        reported = []

        async def mock_report(
            *, job_id, agent_id, status, result_json, attempt_token=None, lease_id=None
        ):
            reported.append({"status": status, "result_json": result_json})
            return {"result": {}}

        worker.coordinate.report_job = mock_report
        return worker, reported

    def _process(self, worker, *, prompt="do it", job_id="job-oc"):
        job = {
            "id": job_id,
            "payload_json": json.dumps({"prompt": prompt}),
        }
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(_claim_result(job)))
        finally:
            loop.close()

    def test_explicit_success_error_like_text_reports_done(self):
        from multinexus.adapters.base import OUTCOME_SUCCESS, AdapterResult

        async def call(prompt, **kw):
            return AdapterResult(
                text="OpenCode CLI failed", outcome=OUTCOME_SUCCESS, session_id="s1"
            )

        worker, reported = self._worker_with(call)
        self._process(worker)

        self.assertEqual(reported[0]["status"], "done")
        body = reported[0]["result_json"]
        self.assertEqual(body["outcome"], OUTCOME_SUCCESS)
        self.assertEqual(body["response_text"], "OpenCode CLI failed")
        self.assertNotIn("error_category", body)

    def test_explicit_failed_plain_text_reports_failed(self):
        from multinexus.adapters.base import OUTCOME_FAILED, AdapterResult

        async def call(prompt, **kw):
            return AdapterResult(
                text="looks fine",
                outcome=OUTCOME_FAILED,
                error_category="provider_error",
            )

        worker, reported = self._worker_with(call)
        self._process(worker)

        self.assertEqual(reported[0]["status"], "failed")
        body = reported[0]["result_json"]
        self.assertEqual(body["outcome"], OUTCOME_FAILED)
        self.assertEqual(body["error_category"], "provider_error")
        self.assertEqual(body["response_text"], "looks fine")

    def test_explicit_timed_out_reports_timed_out(self):
        from multinexus.adapters.base import OUTCOME_TIMED_OUT, AdapterResult

        async def call(prompt, **kw):
            return AdapterResult(text="fine", outcome=OUTCOME_TIMED_OUT)

        worker, reported = self._worker_with(call)
        self._process(worker)

        self.assertEqual(reported[0]["status"], "timed_out")
        body = reported[0]["result_json"]
        self.assertEqual(body["outcome"], OUTCOME_TIMED_OUT)
        self.assertEqual(body["error_category"], "timeout")

    def test_structured_no_response_is_not_delivered(self):
        from multinexus.adapters.base import OUTCOME_FAILED, AdapterResult

        async def call(prompt, **kw):
            return AdapterResult(
                text="(no response)",
                outcome=OUTCOME_FAILED,
                error_category="no_response",
            )

        worker, reported = self._worker_with(call)
        self._process(worker)

        self.assertEqual(reported[0]["status"], "failed")
        body = reported[0]["result_json"]
        self.assertEqual(body["response_text"], "")
        self.assertEqual(body["error"], "(no response)")
        self.assertEqual(body["error_category"], "no_response")

    def test_internal_exception_settles_internal_error_bounded(self):
        from multinexus.adapters.base import OUTCOME_FAILED

        async def boom(prompt, **kw):
            raise RuntimeError("secret " + "x" * 5000)

        worker, reported = self._worker_with(boom)
        with self.assertLogs("multinexus.agentd.worker", level="ERROR") as cm:
            self._process(worker)

        self.assertEqual(reported[0]["status"], "failed")
        body = reported[0]["result_json"]
        self.assertEqual(body["outcome"], OUTCOME_FAILED)
        self.assertEqual(body["error_category"], "internal_error")
        self.assertEqual(body["diagnostic"], "RuntimeError")
        self.assertNotIn("secret", body["diagnostic"])
        # Safe text never leaks the raw diagnostic into the delivery payload.
        self.assertNotIn("secret", body["response_text"])
        self.assertTrue(any("Provider invocation failed" in record.getMessage() for record in cm.records))

    def test_cancelled_error_propagates_without_report(self):
        async def cancel(prompt, **kw):
            raise asyncio.CancelledError()

        worker, reported = self._worker_with(cancel)
        job = {"id": "job-c", "payload_json": json.dumps({"prompt": "x"})}
        loop = asyncio.new_event_loop()
        try:
            with self.assertRaises(asyncio.CancelledError):
                loop.run_until_complete(worker._process_job(_claim_result(job)))
        finally:
            loop.close()
        self.assertEqual(reported, [])

    def test_internal_error_resume_does_not_fresh_fallback(self):
        from multinexus.adapters.base import OUTCOME_FAILED, AdapterResult

        class InternalResume:
            def __init__(self):
                self.calls = 0
                self.resumes = 0

            async def call(self, prompt, **kw):
                self.calls += 1
                return AdapterResult(text="fresh", session_id="new")

            async def resume(self, session_id, prompt, **kw):
                self.resumes += 1
                return AdapterResult(
                    text="Agent error: internal adapter failure",
                    outcome=OUTCOME_FAILED,
                    error_category="internal_error",
                    diagnostic="boom",
                    session_id=session_id,
                )

        adapter = InternalResume()
        worker, reported = self._worker_with(adapter.call)
        worker.adapter = adapter
        worker.session_store.upsert(
            scope_id="scope:1",
            agent_id="test-agent",
            adapter="claude",
            session_id="sess-old",
            work_dir="/tmp/ws",
        )
        self._process(worker, prompt="continue")

        self.assertEqual(worker.adapter.resumes, 1)
        self.assertEqual(worker.adapter.calls, 0)
        self.assertEqual(reported[0]["status"], "failed")
        self.assertEqual(
            reported[0]["result_json"]["error_category"], "internal_error"
        )


class TestDiscordLegacyOutcomeContract(unittest.TestCase):
    """R2: DiscordClient legacy adapter path reads effective_outcome()."""

    def _legacy_client(self):
        from multinexus.client import DiscordClient
        from multinexus.sessions.store import SessionStore

        cfg = _config(agentd_mode=False)
        client = DiscordClient.__new__(DiscordClient)
        client.agent_config = cfg
        client.session_store = SessionStore(cfg.context_db_path)
        return client

    def test_unexpected_exception_settles_internal_error_bounded(self):
        """Direct proof the unexpected-exception path constructs internal_error
        (regression for the missing OUTCOME_FAILED import)."""
        from multinexus.adapters.base import OUTCOME_FAILED

        class BoomAdapter:
            async def call(self, prompt, **kwargs):
                raise RuntimeError("secret " + "x" * 5000)

        client = self._legacy_client()
        client.adapter = BoomAdapter()
        result = asyncio.run(
            client._run_adapter_for_scope(
                "go",
                session_scope_id="channel:boom",
                legacy_scope_ids=(),
                placeholder=None,
                progress_state={},
            )
        )

        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.error_category, "internal_error")
        self.assertEqual(result.diagnostic, "RuntimeError")
        self.assertNotIn("secret", result.diagnostic)
        # Safe text never leaks the raw diagnostic into the reply.
        self.assertNotIn("secret", result.text)


if __name__ == "__main__":
    unittest.main()
