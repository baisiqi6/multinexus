"""Tests for agentd worker integration of the executor binding validator."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from multinexus.adapters.base import AdapterResult
from multinexus.agentd.worker import AgentdWorker
from multinexus.models import AgentConfig


def _config(**overrides):
    defaults = {
        "id": "mac-omp",
        "token": "fake-token",
        "adapter": "omp",
        "context_db_path": str(Path(tempfile.mkdtemp()) / "test.sqlite3"),
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _make_binding(**overrides) -> dict[str, object]:
    snapshot = {
        "contract_version": 1,
        "source_id": "multinexus.discord",
        "source_version": 1,
        "catalog_hash": "8d7632488bea64fe5b5145110004c07b016af92e446b1e54c7f234f9823216bc",
        "executor_definition_id": "omp-code",
        "executor_instance_id": "mac-omp",
        "runner_profile_id": "mac-omp",
        "provider": "kimi-code",
        "adapter": "omp",
        "capabilities": ["coding", "review"],
    }
    snapshot.update(overrides)
    canonical = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    snapshot["binding_id"] = (
        "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    )
    return snapshot


def _make_context(job_id: str = "request:e1") -> dict[str, object]:
    ctx = {
        "job_id": job_id,
        "workspace_id": "ws",
        "task_id": None,
        "assigned_agent": "mac-omp",
        "host_id": "host1",
        "workspace_path": "/host/ws",
        "worktree_path": "/host/ws",
        "harness_root": "/host/harness",
        "branch": None,
        "session_scope_id": "scope:1",
        "legacy_scope_ids": [],
        "log_handle": {"kind": "coordinate_job", "job_id": job_id, "logs_path": None},
        "contract_version": 1,
    }
    canonical = json.dumps(
        ctx, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    ctx["context_id"] = (
        "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    )
    return ctx


def _make_lease(
    *,
    job_id: str = "request:e1",
    agent_id: str = "mac-omp",
    runner_profile_id: str = "mac-omp",
    host_id: str = "host1",
    worktree_path: str = "/host/ws",
    attempt_token: int = 1,
) -> dict[str, object]:
    import hashlib
    import json

    resource = {
        "contract_version": 1,
        "resource_kind": "worktree",
        "host_id": host_id,
        "normalized_path": worktree_path,
    }
    canonical = json.dumps(
        resource, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    resource_key = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "contract_version": 1,
        "lease_id": "039bdcda-de94-4bbd-9f26-13f39b2d33bf",
        "job_id": job_id,
        "attempt_token": attempt_token,
        "agent_id": agent_id,
        "runner_profile_id": runner_profile_id,
        "host_id": host_id,
        "resource_kind": "worktree",
        "resource_key": resource_key,
        "normalized_path": worktree_path,
        "capacity_policy_id": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "max_concurrent_jobs": 1,
        "acquired_at": "2026-07-14T12:00:00Z",
        "expires_at": "2026-07-14T12:02:00Z",
        "server_now": "2026-07-14T12:00:00Z",
        "ttl_seconds": 120,
        "renew_interval_seconds": 30,
    }


def _claim_result(
    binding: dict[str, object] | None = None,
    include_lease: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {"prompt": "hello"}
    if binding is not None:
        payload["executor_binding"] = binding
    result: dict[str, object] = {
        "claimed": True,
        "job": {
            "id": "request:e1",
            "workspace_id": "ws",
            "assigned_agent": "mac-omp",
            "attempt_count": 1,
            "payload_json": json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
        "attempt_token": 1,
        "execution_context": _make_context(),
    }
    if include_lease:
        result["execution_lease"] = _make_lease()
    return result


class WorkerBindingIntegrationTests(unittest.TestCase):
    def _make_worker(self, config=None):
        worker = AgentdWorker(config or _config())
        worker.adapter = AsyncMock()
        worker.adapter.call = AsyncMock(
            return_value=AdapterResult(text="ok", session_id="s1")
        )
        return worker

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_typed_binding_matching_config_executes_adapter(self):
        worker = self._make_worker()
        reported = []

        async def mock_report(
            *, job_id, agent_id, status, result_json, attempt_token=None, lease_id=None
        ):
            reported.append({"status": status, "result_json": result_json})

        worker.coordinate.report_job = mock_report
        self._run(worker._process_job(_claim_result(_make_binding())))

        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["status"], "done")
        self.assertIn("executor_binding_id", reported[0]["result_json"])
        self.assertIn("executor_definition_id", reported[0]["result_json"])
        worker.adapter.call.assert_awaited_once()

    def test_legacy_null_binding_executes_adapter(self):
        worker = self._make_worker()
        reported = []

        async def mock_report(
            *, job_id, agent_id, status, result_json, attempt_token=None, lease_id=None
        ):
            reported.append({"status": status, "result_json": result_json})

        worker.coordinate.report_job = mock_report
        self._run(worker._process_job(_claim_result(None, include_lease=False)))

        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["status"], "done")
        self.assertNotIn("executor_binding_id", reported[0]["result_json"])
        worker.adapter.call.assert_awaited_once()

    def test_adapter_mismatch_fails_closed_before_adapter(self):
        worker = self._make_worker(_config(adapter="claude"))
        reported = []

        async def mock_report(
            *, job_id, agent_id, status, result_json, attempt_token=None, lease_id=None
        ):
            reported.append(
                {
                    "status": status,
                    "result_json": result_json,
                    "attempt_token": attempt_token,
                }
            )

        worker.coordinate.report_job = mock_report
        self._run(worker._process_job(_claim_result(_make_binding())))

        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["status"], "failed")
        self.assertIn("executor_binding_mismatch", reported[0]["result_json"]["error"])
        self.assertEqual(reported[0]["attempt_token"], 1)
        worker.adapter.call.assert_not_awaited()

    def test_instance_id_mismatch_fails_closed_before_adapter(self):
        worker = self._make_worker()
        reported = []

        async def mock_report(
            *, job_id, agent_id, status, result_json, attempt_token=None, lease_id=None
        ):
            reported.append({"status": status, "result_json": result_json})

        worker.coordinate.report_job = mock_report
        binding = _make_binding()
        binding["executor_instance_id"] = "other"
        binding["runner_profile_id"] = "other"
        canonical = json.dumps(
            {k: v for k, v in binding.items() if k != "binding_id"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        binding["binding_id"] = (
            "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        )
        self._run(worker._process_job(_claim_result(binding)))

        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["status"], "failed")
        self.assertIn("executor_binding_mismatch", reported[0]["result_json"]["error"])
        worker.adapter.call.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
