"""Tests for MultiNexus strict consumption of Coordinate v1 ExecutionContext."""
from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from multinexus.adapters.base import AdapterResult
from multinexus.agentd.coordinate_client import (
    CoordinateRuntimeClient,
    CoordinateRuntimeError,
)
from multinexus.agentd.execution_context import (
    ExecutionContextError,
    parse_execution_context,
    validate_claim_response,
)
from multinexus.agentd.worker import AgentdWorker
from multinexus.handoff_handler import CoordinatorHandoff, parse_coordinator_handoff
from multinexus.models import AgentConfig


def _config(**overrides):
    defaults = {
        "id": "test-agent",
        "token": "fake-token",
        "adapter": "claude",
        "context_db_path": str(Path(tempfile.mkdtemp()) / "test.sqlite3"),
        "coordinator_cli_path": "/bin/true",
        "coordinator_db_path": "/tmp/test.db",
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _make_context(
    *,
    job_id: str = "request:e1",
    workspace_path: str = "/host/ws",
    worktree_path: str = "/host/ws",
    session_scope_id: str = "scope:1",
    host_id: str = "host1",
    assigned_agent: str = "test-agent",
    task_id: str | None = None,
) -> dict[str, object]:
    ctx = {
        "job_id": job_id,
        "workspace_id": "ws",
        "task_id": task_id,
        "assigned_agent": assigned_agent,
        "host_id": host_id,
        "workspace_path": workspace_path,
        "worktree_path": worktree_path,
        "harness_root": "/host/harness",
        "branch": None,
        "session_scope_id": session_scope_id,
        "legacy_scope_ids": [],
        "log_handle": {"kind": "coordinate_job", "job_id": job_id, "logs_path": None},
        "contract_version": 1,
    }
    canonical_json = json.dumps(ctx, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    ctx["context_id"] = "sha256:" + hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return ctx


class ParseExecutionContextTests(unittest.TestCase):
    def test_parse_valid_context(self) -> None:
        data = _make_context()
        ctx = parse_execution_context(data)
        self.assertEqual(ctx.context_id, data["context_id"])
        self.assertEqual(ctx.worktree_path, "/host/ws")
        self.assertEqual(ctx.session_scope_id, "scope:1")

    def test_parse_rejects_missing_field(self) -> None:
        data = _make_context()
        del data["host_id"]
        with self.assertRaisesRegex(ExecutionContextError, "incorrect keys"):
            parse_execution_context(data)

    def test_parse_rejects_extra_field(self) -> None:
        data = _make_context()
        data["extra_field"] = "surprise"
        with self.assertRaisesRegex(ExecutionContextError, "incorrect keys"):
            parse_execution_context(data)

    def test_parse_rejects_wrong_version(self) -> None:
        data = _make_context()
        data["contract_version"] = 2
        with self.assertRaisesRegex(ExecutionContextError, "contract_version must be 1"):
            parse_execution_context(data)

    def test_parse_rejects_digest_mismatch(self) -> None:
        data = _make_context()
        data["workspace_path"] = "/host/other"
        with self.assertRaisesRegex(ExecutionContextError, "digest mismatch"):
            parse_execution_context(data)

    def test_parse_rejects_identity_mismatch(self) -> None:
        data = _make_context(job_id="request:e1")
        with self.assertRaisesRegex(ExecutionContextError, "job_id mismatch"):
            parse_execution_context(data, expected_job_id="request:e2")

    def test_parse_rejects_empty_scope(self) -> None:
        data = _make_context(session_scope_id="")
        with self.assertRaisesRegex(ExecutionContextError, "session_scope_id is required"):
            parse_execution_context(data)

    def test_parse_rejects_relative_path(self) -> None:
        data = _make_context()
        data["worktree_path"] = "relative/path"
        with self.assertRaisesRegex(ExecutionContextError, "must be absolute"):
            parse_execution_context(data)

    def test_parse_rejects_traversal(self) -> None:
        data = _make_context()
        data["workspace_path"] = "/host/../other"
        with self.assertRaisesRegex(ExecutionContextError, "traversal"):
            parse_execution_context(data)

    def test_parse_rejects_invalid_context_id(self) -> None:
        data = _make_context()
        data["context_id"] = "sha256:deadbeef"
        with self.assertRaisesRegex(ExecutionContextError, "64-hex"):
            parse_execution_context(data)

    def test_parse_rejects_bad_log_handle_kind(self) -> None:
        data = _make_context()
        data["log_handle"] = {"kind": "other", "job_id": data["job_id"], "logs_path": None}
        with self.assertRaisesRegex(ExecutionContextError, "kind must be 'coordinate_job'"):
            parse_execution_context(data)

    def test_parse_rejects_log_handle_job_id_mismatch(self) -> None:
        data = _make_context()
        data["log_handle"] = {"kind": "coordinate_job", "job_id": "other", "logs_path": None}
        with self.assertRaisesRegex(ExecutionContextError, "log_handle.job_id mismatch"):
            parse_execution_context(data)

    def test_parse_rejects_too_many_legacy_scopes(self) -> None:
        data = _make_context()
        data["legacy_scope_ids"] = [f"legacy:{i}" for i in range(11)]
        with self.assertRaisesRegex(ExecutionContextError, "exceeds"):
            parse_execution_context(data)

    def test_parse_rejects_tuple_legacy_scope_ids(self) -> None:
        data = _make_context()
        data["legacy_scope_ids"] = ("legacy:a",)
        with self.assertRaisesRegex(ExecutionContextError, "must be a list"):
            parse_execution_context(data)

    def test_parse_rejects_duplicate_legacy_scope_ids(self) -> None:
        data = _make_context()
        data["legacy_scope_ids"] = ["legacy:a", "legacy:a"]
        with self.assertRaisesRegex(ExecutionContextError, "duplicate"):
            parse_execution_context(data)


class ValidateClaimResponseTests(unittest.TestCase):
    def test_valid_claim_response(self) -> None:
        job = {
            "id": "request:e1",
            "workspace_id": "ws",
            "assigned_agent": "test-agent",
            "attempt_count": 7,
        }
        ctx = _make_context()
        response = {
            "result": {
                "claimed": True,
                "job": job,
                "attempt_token": 7,
                "execution_context": ctx,
            }
        }
        parsed_job, parsed_ctx, token = validate_claim_response(
            response, agent_id="test-agent"
        )
        self.assertEqual(parsed_job, job)
        self.assertEqual(parsed_ctx.context_id, ctx["context_id"])
        self.assertEqual(token, 7)

    def test_unclaimed_response_raises(self) -> None:
        with self.assertRaisesRegex(ExecutionContextError, "did not return a claimed job"):
            validate_claim_response(
                {"result": {"claimed": False}},
                agent_id="test-agent",
            )

    def test_missing_context_raises(self) -> None:
        with self.assertRaisesRegex(ExecutionContextError, "missing execution_context"):
            validate_claim_response(
                {"result": {"claimed": True, "job": {"id": "j"}, "attempt_token": 1}},
                agent_id="test-agent",
            )

    def test_cli_error_raises(self) -> None:
        client = CoordinateRuntimeClient(cli_path="/usr/bin/true", db_path="/tmp/test.db")

        def fake_run(cmd):
            raise CoordinateRuntimeError("coordinate CLI exploded")

        client._run_cli = fake_run

        loop = asyncio.new_event_loop()
        try:
            with self.assertRaisesRegex(CoordinateRuntimeError, "coordinate CLI exploded"):
                loop.run_until_complete(client.claim_job(agent_id="test-agent"))
        finally:
            loop.close()


class WorkerContextUsageTests(unittest.TestCase):
    def _make_worker(self):
        worker = AgentdWorker(_config())
        calls = []
        resumes = []

        class FakeAdapter:
            async def call(self, prompt, *, work_dir=None, on_progress=None, **kw):
                calls.append(("call", prompt, work_dir))
                return AdapterResult(text=f"reply: {prompt}", session_id="s1")

            async def resume(self, session_id, prompt, *, work_dir=None, on_progress=None, **kw):
                resumes.append(("resume", session_id, prompt, work_dir))
                return AdapterResult(text=f"resumed: {prompt}", session_id=session_id)

        worker.adapter = FakeAdapter()
        return worker, calls, resumes

    def _claim_result(self, job_id: str, work_dir: str, scope: str) -> dict:
        return {
            "claimed": True,
            "job": {
                "id": job_id,
                "workspace_id": "ws",
                "assigned_agent": "test-agent",
                "attempt_count": 1,
                "payload_json": json.dumps({"prompt": "hello"}),
            },
            "attempt_token": 1,
            "execution_context": _make_context(
                job_id=job_id,
                workspace_path=work_dir,
                worktree_path=work_dir,
                session_scope_id=scope,
            ),
        }

    def test_worker_uses_context_cwd_not_config_work_dir(self) -> None:
        worker, calls, _ = self._make_worker()
        worker.config.work_dir = "/config/wrong"
        reported = []

        async def mock_report(*, job_id, agent_id, status, result_json, attempt_token=None, lease_id=None):
            reported.append({"status": status, "result_json": result_json})

        worker.coordinate.report_job = mock_report

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(self._claim_result("j1", "/ws/a", "scope:a")))
        finally:
            loop.close()

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], "/ws/a")
        self.assertEqual(reported[0]["status"], "done")
        self.assertEqual(reported[0]["result_json"]["execution_context_id"][:7], "sha256:")

    def test_worker_fails_closed_on_bad_context(self) -> None:
        worker, calls, _ = self._make_worker()
        reported = []

        async def mock_report(*, job_id, agent_id, status, result_json, attempt_token=None, lease_id=None):
            reported.append({"status": status, "result_json": result_json, "attempt_token": attempt_token, "lease_id": lease_id})

        worker.coordinate.report_job = mock_report

        bad = self._claim_result("j2", "/ws/a", "scope:a")
        bad["execution_context"]["workspace_path"] = "/tampered"

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(bad))
        finally:
            loop.close()

        self.assertEqual(len(calls), 0)
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["status"], "failed")
        self.assertEqual(reported[0]["attempt_token"], 1)
        self.assertIsNone(reported[0]["lease_id"])

    def test_sequential_jobs_change_cwd_and_session_scope(self) -> None:
        worker, calls, _ = self._make_worker()
        stored = []
        upsert = worker.session_store.upsert
        def record_upsert(**kw):
            stored.append(kw)
            return upsert(**kw)
        worker.session_store.upsert = record_upsert
        async def mock_report(**kw):
            return {"result": {"job": {
                "id": kw["job_id"], "assigned_agent": kw["agent_id"],
                "status": kw["status"], "attempt_count": kw["attempt_token"],
                "result": dict(kw["result_json"]),
            }}}
        worker.coordinate.report_job = mock_report

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(self._claim_result("j1", "/ws/a", "scope:a")))
            loop.run_until_complete(worker._process_job(self._claim_result("j2", "/ws/b", "scope:b")))
        finally:
            loop.close()

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][2], "/ws/a")
        self.assertEqual(calls[1][2], "/ws/b")
        self.assertEqual(stored[0]["scope_id"], "scope:a")
        self.assertEqual(stored[0]["work_dir"], "/ws/a")
        self.assertEqual(stored[1]["scope_id"], "scope:b")
        self.assertEqual(stored[1]["work_dir"], "/ws/b")


class HandoffParsingTests(unittest.TestCase):
    def test_handoff_parses_v1_fields(self) -> None:
        content = (
            "[handoff] <@123> workspace_id=ws task_id=t1 action=assignment.accept "
            "bootstrap=docs/project-harness/tasks/t1/worker-bootstrap.md "
            "context_version=1 workspace_path=/host/ws "
            "harness_root=/host/harness branch=feature/t1"
        )
        handoff = parse_coordinator_handoff(content, my_discord_user_id=123)
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff.context_version, 1)
        self.assertEqual(handoff.workspace_path, "/host/ws")
        self.assertEqual(handoff.harness_root, "/host/harness")
        self.assertEqual(handoff.branch, "feature/t1")

    def test_handoff_parses_quoted_windows_paths(self) -> None:
        content = (
            '[handoff] <@123> workspace_id=ws task_id=t1 action=assignment.accept '
            'context_version=1 workspace_path="C:\\Users\\My User\\ws" '
            'harness_root="C:\\Users\\My User\\harness"'
        )
        handoff = parse_coordinator_handoff(content, my_discord_user_id=123)
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff.workspace_path, "C:\\Users\\My User\\ws")
        self.assertEqual(handoff.harness_root, "C:\\Users\\My User\\harness")

    def test_handoff_rejects_relative_workspace_path_in_v1_block(self) -> None:
        content = (
            "[handoff] <@123> workspace_id=ws task_id=t1 action=assignment.accept "
            "context_version=1 workspace_path=relative/ws"
        )
        self.assertIsNone(parse_coordinator_handoff(content, my_discord_user_id=123))

    def test_handoff_rejects_missing_workspace_path_in_v1_block(self) -> None:
        content = (
            "[handoff] <@123> workspace_id=ws task_id=t1 action=assignment.accept "
            "context_version=1 harness_root=/host/harness"
        )
        self.assertIsNone(parse_coordinator_handoff(content, my_discord_user_id=123))

    def test_handoff_rejects_relative_harness_root_in_v1_block(self) -> None:
        content = (
            "[handoff] <@123> workspace_id=ws task_id=t1 action=assignment.accept "
            "context_version=1 workspace_path=/host/ws harness_root=relative/h"
        )
        self.assertIsNone(parse_coordinator_handoff(content, my_discord_user_id=123))

    def test_handoff_rejects_unsupported_context_version(self) -> None:
        content = (
            "[handoff] <@123> workspace_id=ws task_id=t1 action=assignment.accept "
            "context_version=2 workspace_path=/host/ws"
        )
        self.assertIsNone(parse_coordinator_handoff(content, my_discord_user_id=123))


class ManagedHandoffNoSQLiteTests(unittest.TestCase):
    def test_agentd_mode_handoff_does_not_call_resolve_workspace_path(self) -> None:
        from multinexus.coordinator_handoff import CoordinatorHandoffMixin

        # Build a minimal mixin instance in agentd_mode.
        class DummyMixin(CoordinatorHandoffMixin):
            def __init__(self):
                self._agentd_mode = True
                self.agent_config = _config(
                    coordinator_workspace_path="/fallback/ws",
                )

        mixin = DummyMixin()
        handoff = CoordinatorHandoff(
            workspace_id="ws",
            task_id="t1",
            bootstrap_path="docs/project-harness/tasks/t1/worker-bootstrap.md",
            action="assignment.accept",
            context_version=1,
            workspace_path="/handoff/ws",
            harness_root="/handoff/harness",
            branch="main",
        )

        with patch("multinexus.client.resolve_workspace_path") as mock_resolve:
            result = mixin._resolve_bootstrap_workspace_path(handoff)
            self.assertEqual(result, "/handoff/ws")
            mock_resolve.assert_not_called()


class FixtureTests(unittest.TestCase):
    def test_v1_fixture_loads_and_validates(self) -> None:
        fixture = Path(__file__).resolve().parent / "fixtures" / "execution_context_v1.json"
        data = json.loads(fixture.read_text(encoding="utf-8"))
        ctx = parse_execution_context(data)
        self.assertEqual(ctx.context_id, data["context_id"])
        self.assertEqual(ctx.session_scope_id, "task:discord-nexus:p9-1-task")

    def test_v1_fixture_sha_is_pinned(self) -> None:
        fixture = Path(__file__).resolve().parent / "fixtures" / "execution_context_v1.json"
        expected_sha = (
            "b86a79d6bd4bb16ae06d4b1cd2a309fb4015ec5e9202fff32d550b6674892166"
        )
        actual = hashlib.sha256(fixture.read_bytes()).hexdigest()
        self.assertEqual(actual, expected_sha)

    def test_v1_fixture_mutation_rejected(self) -> None:
        fixture = Path(__file__).resolve().parent / "fixtures" / "execution_context_v1.json"
        data = json.loads(fixture.read_text(encoding="utf-8"))
        data["context_id"] = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
        with self.assertRaisesRegex(ExecutionContextError, "digest mismatch"):
            parse_execution_context(data)


class EnvelopeBindingMutationTests(unittest.TestCase):
    """R1-4: claim response must bind execution_context to the full job envelope."""

    def _claim_response(self, *, job_id="request:e1", ctx_job_id="request:e1",
                        task_id=None, ctx_task_id=None, workspace_id="ws",
                        assigned_agent="test-agent", attempt_token=7,
                        attempt_count=None) -> dict[str, object]:
        ctx = _make_context(
            job_id=ctx_job_id,
            task_id=ctx_task_id,
            workspace_path="/host/ws",
            worktree_path="/host/ws",
            session_scope_id="scope:1",
            assigned_agent=assigned_agent,
        )
        return {
            "result": {
                "claimed": True,
                "job": {
                    "id": job_id,
                    "task_id": task_id,
                    "workspace_id": workspace_id,
                    "assigned_agent": assigned_agent,
                    "attempt_count": attempt_count if attempt_count is not None else attempt_token,
                },
                "attempt_token": attempt_token,
                "execution_context": ctx,
            }
        }

    def test_context_task_id_differs_from_job_rejected(self):
        response = self._claim_response(job_id="request:e1", task_id="t1", ctx_task_id="t2")
        with self.assertRaisesRegex(ExecutionContextError, "task_id mismatch"):
            validate_claim_response(response, agent_id="test-agent")

    def test_context_task_id_set_but_job_null_rejected(self):
        response = self._claim_response(job_id="request:e1", task_id=None, ctx_task_id="t1")
        with self.assertRaisesRegex(ExecutionContextError, "task_id mismatch"):
            validate_claim_response(response, agent_id="test-agent")

    def test_context_workspace_id_mismatch_rejected(self):
        response = self._claim_response(workspace_id="other")
        with self.assertRaisesRegex(ExecutionContextError, "workspace_id mismatch"):
            validate_claim_response(response, agent_id="test-agent", workspace_id="other")

    def test_context_assigned_agent_mismatch_rejected(self):
        response = self._claim_response(assigned_agent="other-agent")
        with self.assertRaisesRegex(ExecutionContextError, "assigned_agent mismatch"):
            validate_claim_response(response, agent_id="test-agent")

    def test_attempt_token_missing_rejected(self):
        response = self._claim_response()
        del response["result"]["attempt_token"]
        with self.assertRaisesRegex(ExecutionContextError, "missing attempt_token"):
            validate_claim_response(response, agent_id="test-agent")

    def test_attempt_token_not_int_rejected(self):
        response = self._claim_response(attempt_token="abc")
        with self.assertRaisesRegex(ExecutionContextError, "missing attempt_token"):
            validate_claim_response(response, agent_id="test-agent")

    def test_attempt_token_zero_rejected(self):
        response = self._claim_response(attempt_token=0)
        with self.assertRaisesRegex(ExecutionContextError, "attempt_token must be positive"):
            validate_claim_response(response, agent_id="test-agent")

    def test_attempt_token_negative_rejected(self):
        response = self._claim_response(attempt_token=-1)
        with self.assertRaisesRegex(ExecutionContextError, "attempt_token must be positive"):
            validate_claim_response(response, agent_id="test-agent")

    def test_job_id_differs_from_context_rejected(self):
        response = self._claim_response(job_id="request:e1", ctx_job_id="request:e2")
        with self.assertRaisesRegex(ExecutionContextError, "job_id mismatch"):
            validate_claim_response(response, agent_id="test-agent")

    def test_job_workspace_id_differs_from_context_rejected(self):
        response = self._claim_response(workspace_id="other")
        with self.assertRaisesRegex(ExecutionContextError, "workspace_id mismatch"):
            validate_claim_response(response, agent_id="test-agent", workspace_id="other")

    def test_job_assigned_agent_differs_from_context_rejected(self):
        response = self._claim_response(assigned_agent="other-agent")
        with self.assertRaisesRegex(ExecutionContextError, "assigned_agent mismatch"):
            validate_claim_response(response, agent_id="test-agent")

    def test_attempt_token_differs_from_job_attempt_count_rejected(self):
        response = self._claim_response(attempt_token=7, attempt_count=8)
        with self.assertRaisesRegex(ExecutionContextError, "attempt_token mismatch"):
            validate_claim_response(response, agent_id="test-agent")

    def test_job_not_dict_rejected(self):
        response = self._claim_response()
        response["result"]["job"] = "not-a-dict"
        with self.assertRaisesRegex(ExecutionContextError, "missing job"):
            validate_claim_response(response, agent_id="test-agent")

    def test_result_not_dict_rejected(self):
        response = self._claim_response()
        response["result"] = "not-a-dict"
        with self.assertRaisesRegex(ExecutionContextError, "missing result"):
            validate_claim_response(response, agent_id="test-agent")

    def test_response_none_rejected(self):
        with self.assertRaisesRegex(ExecutionContextError, "no response"):
            validate_claim_response(None, agent_id="test-agent")


class EnvelopeBindingFailClosedWorkerTests(unittest.TestCase):
    """R1-4: bad claim envelope must not invoke the adapter."""

    def _make_worker(self):
        worker = AgentdWorker(_config())
        calls = []

        class FakeAdapter:
            async def call(self, prompt, *, work_dir=None, on_progress=None, **kw):
                calls.append(("call", prompt, work_dir))
                return AdapterResult(text=f"reply: {prompt}", session_id="s1")

        worker.adapter = FakeAdapter()
        return worker, calls

    def _claim_result(self, **ctx_overrides) -> dict[str, object]:
        return {
            "claimed": True,
            "job": {
                "id": "request:e1",
                "task_id": "t1",
                "workspace_id": "ws",
                "assigned_agent": ctx_overrides.get("assigned_agent", "test-agent"),
                "attempt_count": 1,
            },
            "attempt_token": 1,
            "execution_context": _make_context(**ctx_overrides),
        }

    def test_adapter_not_called_when_assigned_agent_mismatches(self):
        worker, calls = self._make_worker()
        reported = []

        async def mock_report(**kw):
            reported.append(kw)

        worker.coordinate.report_job = mock_report

        result = self._claim_result(assigned_agent="other-agent")
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(result))
        finally:
            loop.close()

        self.assertEqual(len(calls), 0)
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["status"], "failed")

    def test_adapter_not_called_when_task_id_mismatches(self):
        worker, calls = self._make_worker()
        reported = []

        async def mock_report(**kw):
            reported.append(kw)

        worker.coordinate.report_job = mock_report

        result = self._claim_result(task_id="t1")
        result["job"] = {"id": "request:e1", "task_id": "t2"}
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(result))
        finally:
            loop.close()

        self.assertEqual(len(calls), 0)
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["status"], "failed")


class CliErrorNormalizationTests(unittest.TestCase):
    """R1-6: all CLI failure modes are normalized to CoordinateRuntimeError."""

    def _client(self):
        return CoordinateRuntimeClient(cli_path="/bin/true", db_path="/tmp/test.db")

    @patch("multinexus.agentd.coordinate_client.subprocess.run")
    def test_non_zero_exit_raises(self, mock_run):
        import subprocess
        mock_run.return_value = subprocess.CompletedProcess("cmd", 1, stdout="", stderr="boom")
        client = self._client()
        loop = asyncio.new_event_loop()
        try:
            with self.assertRaisesRegex(CoordinateRuntimeError, "coordinate CLI exit 1"):
                loop.run_until_complete(client.claim_job(agent_id="test-agent"))
        finally:
            loop.close()

    @patch("multinexus.agentd.coordinate_client.subprocess.run")
    def test_non_json_stdout_raises(self, mock_run):
        import subprocess
        mock_run.return_value = subprocess.CompletedProcess("cmd", 0, stdout="not-json", stderr="")
        client = self._client()
        loop = asyncio.new_event_loop()
        try:
            with self.assertRaisesRegex(CoordinateRuntimeError, "coordinate CLI non-JSON"):
                loop.run_until_complete(client.claim_job(agent_id="test-agent"))
        finally:
            loop.close()

    @patch("multinexus.agentd.coordinate_client.subprocess.run")
    def test_timeout_raises(self, mock_run):
        import subprocess

        def raise_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired("cmd", 30)

        mock_run.side_effect = raise_timeout
        client = self._client()
        loop = asyncio.new_event_loop()
        try:
            with self.assertRaisesRegex(CoordinateRuntimeError, "coordinate CLI timed out"):
                loop.run_until_complete(client.claim_job(agent_id="test-agent"))
        finally:
            loop.close()

    @patch("multinexus.agentd.coordinate_client.subprocess.run")
    def test_os_error_raises(self, mock_run):
        mock_run.side_effect = FileNotFoundError("No such file")
        client = self._client()
        loop = asyncio.new_event_loop()
        try:
            with self.assertRaisesRegex(CoordinateRuntimeError, "coordinate CLI execution failed"):
                loop.run_until_complete(client.claim_job(agent_id="test-agent"))
        finally:
            loop.close()


class WorkerBackoffTests(unittest.TestCase):
    """R1-6: agentd loop backs off on CLI errors and never invokes the adapter."""

    def test_loop_backs_off_without_invoking_adapter(self):
        worker = AgentdWorker(_config())
        calls = []

        class FakeAdapter:
            async def call(self, prompt, *, work_dir=None, on_progress=None, **kw):
                calls.append(("call", prompt))
                return AdapterResult(text="reply", session_id="s1")

        worker.adapter = FakeAdapter()

        claim_count = 0

        async def failing_claim(*args, **kwargs):
            nonlocal claim_count
            claim_count += 1
            raise CoordinateRuntimeError("coordinate CLI exploded")

        worker.coordinate.claim_job = failing_claim

        loop = asyncio.new_event_loop()
        try:
            task = loop.create_task(worker.run(poll_interval=0.01))
            loop.run_until_complete(asyncio.sleep(0.05))
            worker.stop()
            loop.run_until_complete(task)
        finally:
            loop.close()

        self.assertEqual(len(calls), 0)
        self.assertGreater(claim_count, 1)


class ManagedHandoffFailClosedTests(unittest.TestCase):
    """R1-5: managed handoff paths fail closed without v1 authority."""

    def _mixin(self, agentd_mode=True, coordinator_workspace_path="/fallback/ws"):
        from multinexus.coordinator_handoff import CoordinatorHandoffMixin

        class DummyMixin(CoordinatorHandoffMixin):
            def __init__(self):
                self._agentd_mode = agentd_mode
                self.agent_config = _config(
                    coordinator_workspace_path=coordinator_workspace_path,
                )

        return DummyMixin()

    def test_agentd_mode_returns_handoff_path_even_when_empty(self):

        mixin = self._mixin(agentd_mode=True)
        handoff = CoordinatorHandoff(
            workspace_id="ws",
            task_id="t1",
            bootstrap_path="docs/bootstrap.md",
            action="assignment.accept",
            context_version=1,
            workspace_path="",
            harness_root="",
            branch="",
        )

        with patch("multinexus.client.resolve_workspace_path") as mock_resolve:
            result = mixin._resolve_bootstrap_workspace_path(handoff)
            self.assertEqual(result, "")
            mock_resolve.assert_not_called()

    def test_try_coordinator_handoff_reports_blocker_without_v1_authority(self):
        from multinexus.coordinator_handoff import CoordinatorHandoffMixin
        import discord

        class DummyMixin(CoordinatorHandoffMixin):
            def __init__(self):
                self._agentd_mode = True
                self.agent_config = _config()
                self._coordinate_client = None
                self.user = MagicMock()
                self.user.id = 111
                self._resolve_channel_id = lambda msg: 500

        mixin = DummyMixin()
        handoff = CoordinatorHandoff(
            workspace_id="ws",
            task_id="t1",
            bootstrap_path="",
            action="assignment.accept",
            context_version=None,
            workspace_path="",
            harness_root="",
            branch="",
        )
        msg = MagicMock(spec=discord.Message)
        msg.content = "[handoff] <@111> workspace_id=ws task_id=t1 action=assignment.accept"
        msg.channel = MagicMock()
        msg.channel.send = AsyncMock(return_value=MagicMock())

        reports = []

        def capture_report(action, handoff_obj, **kwargs):
            reports.append((action, handoff_obj, kwargs))
            return "[agent-report] action=blocker"

        with patch("multinexus.client.parse_coordinator_handoff", return_value=handoff), \
             patch("multinexus.client.execute_assignment_accept", return_value=(True, '{"bootstrap_text": ""}')) as accept, \
             patch("multinexus.client.bootstrap_text_from_accept_output", return_value=None), \
             patch("multinexus.client.build_agent_report", side_effect=capture_report):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(mixin._try_coordinator_handoff(msg))
            finally:
                loop.close()

        self.assertTrue(result)
        accept.assert_not_called()
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0][0], "blocker")
        self.assertIn("v1 handoff authority", reports[0][2].get("reason", "").lower())

    def test_try_coordinator_handoff_reports_blocker_with_v1_but_missing_bootstrap(self):
        from multinexus.coordinator_handoff import CoordinatorHandoffMixin
        import discord

        class DummyMixin(CoordinatorHandoffMixin):
            def __init__(self):
                self._agentd_mode = True
                self.agent_config = _config()
                self._coordinate_client = None
                self.user = MagicMock()
                self.user.id = 111
                self._resolve_channel_id = lambda msg: 500

        mixin = DummyMixin()
        handoff = CoordinatorHandoff(
            workspace_id="ws",
            task_id="t1",
            bootstrap_path="docs/project-harness/tasks/t1/worker-bootstrap.md",
            action="assignment.accept",
            context_version=1,
            workspace_path="/handoff/ws",
            harness_root="/handoff/harness",
            branch="main",
        )
        msg = MagicMock(spec=discord.Message)
        msg.content = "[handoff] <@111> workspace_id=ws task_id=t1 action=assignment.accept"
        msg.channel = MagicMock()
        msg.channel.send = AsyncMock(return_value=MagicMock())

        reports = []

        def capture_report(action, handoff_obj, **kwargs):
            reports.append((action, handoff_obj, kwargs))
            return "[agent-report] action=blocker"

        with patch("multinexus.client.parse_coordinator_handoff", return_value=handoff), \
             patch("multinexus.client.execute_assignment_accept", return_value=(True, '{"bootstrap_text": ""}')), \
             patch("multinexus.client.bootstrap_text_from_accept_output", return_value=None), \
             patch("multinexus.client.read_bootstrap", return_value=None), \
             patch("multinexus.client.build_agent_report", side_effect=capture_report):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    mixin._try_coordinator_handoff(msg, resolved_workspace="ws")
                )
            finally:
                loop.close()

        self.assertTrue(result)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0][0], "blocker")
        self.assertIn("bootstrap", reports[0][2].get("reason", "").lower())

    def test_handle_review_handoff_reports_blocker_without_v1_authority(self):
        from multinexus.coordinator_handoff import CoordinatorHandoffMixin
        import discord

        class DummyMixin(CoordinatorHandoffMixin):
            def __init__(self):
                self._agentd_mode = True
                self.agent_config = _config()
                self._coordinate_client = None
                self.user = MagicMock()
                self.user.id = 111
                self._resolve_channel_id = lambda msg: 500
                self.context_store = MagicMock()

        mixin = DummyMixin()
        handoff = CoordinatorHandoff(
            workspace_id="ws",
            task_id="t1",
            bootstrap_path="",
            action="review.begin",
            context_version=None,
            workspace_path="",
            harness_root="",
            branch="",
        )
        msg = MagicMock(spec=discord.Message)
        msg.content = "[handoff] <@111> workspace_id=ws task_id=t1 action=review.begin"
        msg.channel = MagicMock()
        msg.channel.send = AsyncMock(return_value=MagicMock())

        reports = []

        def capture_report(action, handoff_obj, **kwargs):
            reports.append((action, handoff_obj, kwargs))
            return "[agent-report] action=blocker"

        with patch("multinexus.client.parse_coordinator_handoff", return_value=handoff), \
             patch("multinexus.client.build_agent_report", side_effect=capture_report):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(mixin._handle_review_handoff(
                    msg, handoff, mixin.agent_config, "500", "scope:ws:t1"
                ))
            finally:
                loop.close()

        self.assertTrue(result)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0][0], "blocker")
        self.assertIn("v1 handoff authority", reports[0][2].get("reason", "").lower())

    def test_handle_review_handoff_reports_blocker_with_v1_but_missing_bootstrap(self):
        from multinexus.coordinator_handoff import CoordinatorHandoffMixin
        import discord

        class DummyMixin(CoordinatorHandoffMixin):
            def __init__(self):
                self._agentd_mode = True
                self.agent_config = _config()
                self._coordinate_client = None
                self.user = MagicMock()
                self.user.id = 111
                self._resolve_channel_id = lambda msg: 500
                self.context_store = MagicMock()

        mixin = DummyMixin()
        handoff = CoordinatorHandoff(
            workspace_id="ws",
            task_id="t1",
            bootstrap_path="docs/project-harness/tasks/t1/reviewer-bootstrap.md",
            action="review.begin",
            context_version=1,
            workspace_path="/handoff/ws",
            harness_root="/handoff/harness",
            branch="main",
        )
        msg = MagicMock(spec=discord.Message)
        msg.content = "[handoff] <@111> workspace_id=ws task_id=t1 action=review.begin"
        msg.channel = MagicMock()
        msg.channel.send = AsyncMock(return_value=MagicMock())

        reports = []

        def capture_report(action, handoff_obj, **kwargs):
            reports.append((action, handoff_obj, kwargs))
            return "[agent-report] action=blocker"

        with patch("multinexus.client.parse_coordinator_handoff", return_value=handoff), \
             patch("multinexus.client.read_bootstrap", return_value=None), \
             patch("multinexus.client.build_agent_report", side_effect=capture_report):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(mixin._handle_review_handoff(
                    msg, handoff, mixin.agent_config, "500", "scope:ws:t1"
                ))
            finally:
                loop.close()

        self.assertTrue(result)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0][0], "blocker")
        self.assertIn("bootstrap", reports[0][2].get("reason", "").lower())


if __name__ == "__main__":
    unittest.main()
