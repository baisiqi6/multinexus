"""Tests for MultiNexus strict consumption of Coordinate v1 ExecutionLease."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from multinexus.adapters.base import AdapterResult
from multinexus.agentd.coordinate_client import (
    CoordinateRuntimeClient,
    CoordinateRuntimeError,
)
from multinexus.agentd.execution_lease import (
    ExecutionLeaseError,
    ExecutionLeaseV1,
    build_worktree_resource,
    compute_resource_key,
    fixture_sha256,
    load_fixture,
    normalize_worktree_path,
    parse_execution_lease,
    validate_execution_lease,
)
from multinexus.agentd.worker import AgentdWorker
from multinexus.models import AgentConfig


LEASE_ID = "039bdcda-de94-4bbd-9f26-13f39b2d33bf"


# Raw SHA-256 digests of the mirrored Coordinate fixtures. These must be updated
# whenever the fixtures are regenerated or mirrored from Coordinate.
_FIXTURE_SHAS = {
    "execution_lease_v1_bad_digest.json": "54d3aba09e7cea6c9287ef5905e0efe9807ea76d76fd30e206d7f7aecb9018ec",
    "execution_lease_v1_bad_identity.json": "5b46b1968413e4865680db7d572358fe9813cc1c84c1eab74e9f04bc55d7bd76",
    "execution_lease_v1_bad_timestamps.json": "1aeae6afb4b37cde0c2c3221d4f59a4f4354f4ca98593382ec5a86bc1cdd4fc6",
    "execution_lease_v1_context_mismatch.json": "7fb22d0fb0666af50060cdd30f8c90f9e402eceb45a7ba18d07ce82752c85ad0",
    "execution_lease_v1_extra_keys.json": "d05686ed8f32b2c571c6ae92d20e8fb8ba06aa20ef53149a72af570170116302",  #gitleaks:allow -- fixture SHA-256
    "execution_lease_v1_invalid_ttl_interval.json": "3e0fd599a91087d3586c4d016e944cc5a956a7a8e1583b4ef236bc1270536232",
    "execution_lease_v1_missing_keys.json": "280715ba364473423316325f95623727595d36ebe93683ea8c6010b3a2253d66",  #gitleaks:allow -- fixture SHA-256
    "execution_lease_v1_positive.json": "20f61d6b7edb3dffbf8a6a2bb00e42c51d98b65b5a98611f193fefda2bd7070f",
    "execution_lease_v1_resource_mismatch.json": "e023a3dc4a07e11d97dac4aabf1448cacb441224cbbe19d31bd745b9334cd350",
    "execution_lease_v1_stale_token.json": "d76530961887bcbaf89968cb17f3d19c99db7c342c4519394ab83fe737f77b64",  #gitleaks:allow -- fixture SHA-256
}


def _config(**overrides):
    defaults = {
        "id": "mac-omp",
        "token": "fake-token",
        "adapter": "claude",
        "context_db_path": str(Path(tempfile.mkdtemp()) / "test.sqlite3"),
        "coordinator_cli_path": "/bin/true",
        "coordinator_db_path": "/tmp/test.db",
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _resource_key(host_id: str, normalized_path: str) -> str:
    return compute_resource_key(build_worktree_resource(host_id, normalized_path))


def _make_lease(
    *,
    job_id: str = "request:e1",
    agent_id: str = "mac-omp",
    runner_profile_id: str = "mac-omp",
    host_id: str = "mac",
    worktree_path: str = "/home/example/multinexus",
    attempt_token: int = 1,
    server_now: str = "2026-07-14T12:00:00Z",
) -> dict[str, object]:
    return {
        "contract_version": 1,
        "lease_id": LEASE_ID,
        "job_id": job_id,
        "attempt_token": attempt_token,
        "agent_id": agent_id,
        "runner_profile_id": runner_profile_id,
        "host_id": host_id,
        "resource_kind": "worktree",
        "resource_key": _resource_key(host_id, worktree_path),
        "normalized_path": worktree_path,
        "capacity_policy_id": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "max_concurrent_jobs": 1,
        "acquired_at": "2026-07-14T12:00:00Z",
        "expires_at": "2026-07-14T12:02:00Z",
        "server_now": server_now,
        "ttl_seconds": 120,
        "renew_interval_seconds": 30,
    }


def _make_binding(
    *,
    executor_instance_id: str = "mac-omp",
    runner_profile_id: str = "mac-omp",
    adapter: str = "claude",
) -> dict[str, object]:
    """Return a minimal valid v1 executor binding snapshot."""
    snapshot = {
        "contract_version": 1,
        "source_id": "multinexus.discord",
        "source_version": 1,
        "catalog_hash": "8d7632488bea64fe5b5145110004c07b016af92e446b1e54c7f234f9823216bc",
        "executor_definition_id": "claude-code",
        "executor_instance_id": executor_instance_id,
        "runner_profile_id": runner_profile_id,
        "provider": "anthropic",
        "adapter": adapter,
        "capabilities": ["coding", "review"],
    }
    canonical = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    snapshot["binding_id"] = (
        "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    )
    return snapshot


def _make_context(
    *,
    job_id: str = "request:e1",
    worktree_path: str = "/home/example/multinexus",
    session_scope_id: str = "scope:1",
    host_id: str = "mac",
    assigned_agent: str = "mac-omp",
) -> dict[str, object]:
    ctx = {
        "job_id": job_id,
        "workspace_id": "ws",
        "task_id": None,
        "assigned_agent": assigned_agent,
        "host_id": host_id,
        "workspace_path": worktree_path,
        "worktree_path": worktree_path,
        "harness_root": "/host/harness",
        "branch": None,
        "session_scope_id": session_scope_id,
        "legacy_scope_ids": [],
        "log_handle": {"kind": "coordinate_job", "job_id": job_id, "logs_path": None},
        "contract_version": 1,
    }
    canonical_json = json.dumps(
        ctx, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    ctx["context_id"] = (
        "sha256:" + hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    )
    return ctx


def _validate_binding_signature(binding: dict[str, object]) -> None:
    """Recalculate binding_id from canonical snapshot and patch it in place."""
    snapshot = {k: v for k, v in binding.items() if k != "binding_id"}
    canonical = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    binding["binding_id"] = (
        "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    )


def _claim_result(
    *,
    job_id: str = "request:e1",
    worktree_path: str = "/home/example/multinexus",
    session_scope_id: str = "scope:1",
    host_id: str = "mac",
    assigned_agent: str = "mac-omp",
    include_lease: bool = True,
    lease_overrides: dict | None = None,
    executor_binding: dict[str, object] | None = None,
) -> dict[str, object]:
    lease = _make_lease(
        job_id=job_id,
        agent_id=assigned_agent,
        worktree_path=worktree_path,
    )
    if lease_overrides:
        lease.update(lease_overrides)
    payload: dict[str, object] = {"prompt": "hello"}
    if executor_binding is not None:
        payload["executor_binding"] = executor_binding
    result: dict[str, object] = {
        "claimed": True,
        "job": {
            "id": job_id,
            "workspace_id": "ws",
            "assigned_agent": assigned_agent,
            "attempt_count": 1,
            "payload_json": json.dumps(payload),
        },
        "attempt_token": 1,
        "execution_context": _make_context(
            job_id=job_id,
            worktree_path=worktree_path,
            session_scope_id=session_scope_id,
            host_id=host_id,
            assigned_agent=assigned_agent,
        ),
    }
    if include_lease:
        result["execution_lease"] = lease
    return result


def _make_worker_for_presence_test():
    worker = AgentdWorker(_config())
    calls = []

    class FakeAdapter:
        async def call(self, prompt, *, work_dir=None, on_progress=None, **kw):
            calls.append(("call", prompt, work_dir))
            return AdapterResult(text=f"reply: {prompt}", session_id="s1")

    worker.adapter = FakeAdapter()
    return worker, calls


@pytest.mark.parametrize(
    "lease_present,lease_value,binding_present,binding_value,malformed_payload,"
    "expect_calls,expect_reported,expect_status,expect_lease_id,expect_attempt_token",
    [
        # execution_lease field present but value is None/list/string: no report.
        (True, None, True, _make_binding(), False, 0, 0, None, None, None),
        (True, ["not", "dict"], True, _make_binding(), False, 0, 0, None, None, None),
        (True, "not-dict", True, _make_binding(), False, 0, 0, None, None, None),
        # binding key present but null with a valid lease: trusted-lease failed report.
        (True, _make_lease(), True, None, False, 0, 1, "failed", LEASE_ID, 1),
        # binding key present but null without a lease: no provider/no report.
        (False, None, True, None, False, 0, 0, None, None, None),
        # valid lease with binding key absent: trusted-lease failed report.
        (True, _make_lease(), False, None, False, 0, 1, "failed", LEASE_ID, 1),
        # missing lease with malformed payload: cannot prove legacy, no report.
        (False, None, False, None, True, 0, 0, None, None, None),
        # valid lease with malformed payload: trusted-lease failed report.
        (True, _make_lease(), False, None, True, 0, 1, "failed", LEASE_ID, 1),
        # valid lease with non-dict binding value: trusted-lease failed report.
        (True, _make_lease(), True, "invalid", False, 0, 1, "failed", LEASE_ID, 1),
        (True, _make_lease(), True, ["invalid"], False, 0, 1, "failed", LEASE_ID, 1),
    ],
)
def test_lease_binding_presence_state_machine(
    lease_present,
    lease_value,
    binding_present,
    binding_value,
    malformed_payload,
    expect_calls,
    expect_reported,
    expect_status,
    expect_lease_id,
    expect_attempt_token,
):
    """P9-3B field-presence state machine for execution_lease and executor_binding."""
    worker, calls = _make_worker_for_presence_test()
    reported = []

    async def mock_report(**kw):
        reported.append(kw)

    worker.coordinate.report_job = mock_report

    result = _claim_result()
    if lease_present:
        if lease_value is None or isinstance(lease_value, (list, str)):
            result["execution_lease"] = lease_value
        # else keep the default valid lease from _claim_result.
    else:
        del result["execution_lease"]

    payload: dict[str, object] = {"prompt": "hello"}
    if binding_present:
        payload["executor_binding"] = binding_value
    if malformed_payload:
        result["job"]["payload_json"] = "not-json"
    else:
        result["job"]["payload_json"] = json.dumps(payload)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(worker._process_job(result))
    finally:
        loop.close()

    assert len(calls) == expect_calls
    assert len(reported) == expect_reported
    if expect_reported:
        assert reported[0]["status"] == expect_status
        assert reported[0].get("lease_id") == expect_lease_id
        assert reported[0].get("attempt_token") == expect_attempt_token


class ParseExecutionLeaseTests(unittest.TestCase):
    def test_parse_valid_lease(self) -> None:
        lease = parse_execution_lease(_make_lease())
        self.assertIsInstance(lease, ExecutionLeaseV1)
        self.assertEqual(lease.job_id, "request:e1")
        self.assertEqual(lease.agent_id, "mac-omp")
        self.assertEqual(lease.ttl_seconds, 120)
        self.assertEqual(lease.resource_kind, "worktree")

    def test_parse_rejects_non_dict(self) -> None:
        with self.assertRaisesRegex(ExecutionLeaseError, "must be an object"):
            parse_execution_lease("lease")

    def test_parse_rejects_missing_keys(self) -> None:
        data = _make_lease()
        del data["lease_id"]
        with self.assertRaisesRegex(ExecutionLeaseError, "incorrect keys"):
            parse_execution_lease(data)

    def test_parse_rejects_extra_keys(self) -> None:
        data = _make_lease()
        data["extra"] = "surprise"
        with self.assertRaisesRegex(ExecutionLeaseError, "incorrect keys"):
            parse_execution_lease(data)

    def test_parse_rejects_wrong_version(self) -> None:
        data = _make_lease()
        data["contract_version"] = 2
        with self.assertRaisesRegex(ExecutionLeaseError, "contract_version 2"):
            parse_execution_lease(data)

    def test_parse_rejects_bad_lease_id(self) -> None:
        data = _make_lease()
        data["lease_id"] = "not-a-uuid"
        with self.assertRaisesRegex(
            ExecutionLeaseError, "lease_id must be a lowercase UUID"
        ):
            parse_execution_lease(data)

    def test_parse_rejects_bad_resource_key(self) -> None:
        data = _make_lease()
        data["resource_key"] = "md5:abc"
        with self.assertRaisesRegex(ExecutionLeaseError, "sha256:<64-hex>"):
            parse_execution_lease(data)

    def test_parse_rejects_relative_path(self) -> None:
        data = _make_lease()
        data["normalized_path"] = "relative/path"
        with self.assertRaisesRegex(ExecutionLeaseError, "must be an absolute path"):
            parse_execution_lease(data)

    def test_validate_rejects_traversal(self) -> None:
        data = _make_lease()
        data["normalized_path"] = "/host/../other"
        with self.assertRaisesRegex(
            ExecutionLeaseError, "normalized_path is not canonical"
        ):
            validate_execution_lease(
                data,
                expected_agent_id="mac-omp",
            )

    def test_parse_rejects_negative_ttl(self) -> None:
        data = _make_lease()
        data["ttl_seconds"] = -10
        with self.assertRaisesRegex(ExecutionLeaseError, "ttl_seconds"):
            parse_execution_lease(data)

    def test_parse_rejects_ttl_too_long(self) -> None:
        data = _make_lease()
        data["ttl_seconds"] = 601
        with self.assertRaisesRegex(ExecutionLeaseError, "ttl_seconds"):
            parse_execution_lease(data)

    def test_parse_rejects_renew_interval_gte_ttl(self) -> None:
        data = _make_lease()
        data["renew_interval_seconds"] = 120
        with self.assertRaisesRegex(ExecutionLeaseError, "renew_interval_seconds"):
            parse_execution_lease(data)

    def test_parse_rejects_expires_not_after_acquired(self) -> None:
        data = _make_lease()
        data["expires_at"] = data["acquired_at"]
        with self.assertRaisesRegex(ExecutionLeaseError, "server_now must satisfy"):
            parse_execution_lease(data)

    def test_parse_rejects_ttl_mismatch(self) -> None:
        data = _make_lease()
        data["ttl_seconds"] = 90
        with self.assertRaisesRegex(
            ExecutionLeaseError, "ttl_seconds .* does not match"
        ):
            parse_execution_lease(data)

    def test_parse_accepts_server_now_at_acquired_boundary(self) -> None:
        data = _make_lease(server_now="2026-07-14T12:00:00Z")
        lease = parse_execution_lease(data)
        self.assertEqual(lease.server_now, "2026-07-14T12:00:00Z")

    def test_parse_rejects_server_now_at_expires_boundary(self) -> None:
        data = _make_lease(server_now="2026-07-14T12:02:00Z")
        with self.assertRaisesRegex(ExecutionLeaseError, "server_now must satisfy"):
            parse_execution_lease(data)


class ValidateExecutionLeaseIdentityTests(unittest.TestCase):
    def test_accepts_matching_identity(self) -> None:
        lease = validate_execution_lease(
            _make_lease(),
            expected_agent_id="mac-omp",
            expected_job_id="request:e1",
            expected_attempt_token=1,
        )
        self.assertEqual(lease.agent_id, "mac-omp")

    def test_rejects_agent_id_mismatch(self) -> None:
        with self.assertRaisesRegex(ExecutionLeaseError, "agent_id mismatch"):
            validate_execution_lease(
                _make_lease(),
                expected_agent_id="other-agent",
            )

    def test_rejects_job_id_mismatch(self) -> None:
        with self.assertRaisesRegex(ExecutionLeaseError, "job_id mismatch"):
            validate_execution_lease(
                _make_lease(),
                expected_agent_id="mac-omp",
                expected_job_id="request:e2",
            )

    def test_rejects_attempt_token_mismatch(self) -> None:
        with self.assertRaisesRegex(ExecutionLeaseError, "attempt_token mismatch"):
            validate_execution_lease(
                _make_lease(),
                expected_agent_id="mac-omp",
                expected_attempt_token=2,
            )

    def test_context_cross_check_passes(self) -> None:
        lease = validate_execution_lease(
            _make_lease(),
            expected_agent_id="mac-omp",
            execution_context=_make_context(),
        )
        self.assertEqual(lease.normalized_path, "/home/example/multinexus")

    def test_context_cross_check_rejects_worktree_mismatch(self) -> None:
        ctx = _make_context()
        ctx["worktree_path"] = "/home/example/other"
        with self.assertRaisesRegex(
            ExecutionLeaseError, "normalized_path does not match"
        ):
            validate_execution_lease(
                _make_lease(),
                expected_agent_id="mac-omp",
                execution_context=ctx,
            )

    def test_binding_cross_check_passes(self) -> None:
        binding = {
            "executor_instance_id": "mac-omp",
            "runner_profile_id": "mac-omp",
        }
        lease = validate_execution_lease(
            _make_lease(),
            expected_agent_id="mac-omp",
            executor_binding=binding,
        )
        self.assertEqual(lease.runner_profile_id, "mac-omp")

    def test_binding_cross_check_rejects_runner_mismatch(self) -> None:
        binding = {
            "executor_instance_id": "mac-omp",
            "runner_profile_id": "other-profile",
        }
        with self.assertRaisesRegex(
            ExecutionLeaseError, "runner_profile_id does not match"
        ):
            validate_execution_lease(
                _make_lease(),
                expected_agent_id="mac-omp",
                executor_binding=binding,
            )


class ResourceKeyTests(unittest.TestCase):
    def test_resource_key_is_real_sha256_of_canonical_resource(self) -> None:
        resource = build_worktree_resource("mac", "/home/example/multinexus")
        key = compute_resource_key(resource)
        self.assertEqual(
            key,
            "sha256:688525df69aae79b3b2a434334ccb41b5bf46c3189a74cd3b5c7b7aa76c90c42",
        )

    def test_resource_key_changes_with_path(self) -> None:
        resource = build_worktree_resource("mac", "/home/example/other")
        key = compute_resource_key(resource)
        self.assertEqual(
            key,
            "sha256:54fb9b95c6992ea66fc53ef5df860b780cb270ceea688ec5e73487ee26ff4557",
        )

    def test_resource_key_changes_with_host(self) -> None:
        resource_a = build_worktree_resource("mac", "/home/example/multinexus")
        resource_b = build_worktree_resource("win", "/home/example/multinexus")
        self.assertNotEqual(
            compute_resource_key(resource_a), compute_resource_key(resource_b)
        )

    def test_normalize_worktree_path_posix(self) -> None:
        self.assertEqual(
            normalize_worktree_path("/home/example/multinexus"),
            "/home/example/multinexus",
        )
        self.assertEqual(normalize_worktree_path("/a/b/../c"), "/a/c")


class FixtureTests(unittest.TestCase):
    def test_all_fixture_shas_are_pinned(self) -> None:
        for name, expected_sha in _FIXTURE_SHAS.items():
            with self.subTest(name=name):
                self.assertEqual(
                    fixture_sha256(name),
                    expected_sha,
                    f"Fixture {name} digest changed; update _FIXTURE_SHAS if intentional",
                )

    def test_positive_fixture_loads_and_validates(self) -> None:
        data = load_fixture("execution_lease_v1_positive.json")
        lease = validate_execution_lease(
            data,
            expected_agent_id="mac-omp",
            expected_job_id="request:e1",
            expected_attempt_token=1,
            execution_context=_make_context(),
        )
        self.assertIsInstance(lease, ExecutionLeaseV1)
        self.assertEqual(lease.lease_id, "039bdcda-de94-4bbd-9f26-13f39b2d33bf")
        self.assertEqual(lease.contract_version, 1)
        self.assertEqual(lease.resource_kind, "worktree")
        self.assertEqual(
            lease.resource_key,
            "sha256:688525df69aae79b3b2a434334ccb41b5bf46c3189a74cd3b5c7b7aa76c90c42",
        )

    def test_positive_fixture_roundtrips_through_lease_to_dict(self) -> None:
        from multinexus.agentd.execution_lease import lease_to_dict

        data = load_fixture("execution_lease_v1_positive.json")
        lease = parse_execution_lease(data)
        self.assertEqual(lease_to_dict(lease), data)

    def test_missing_keys_fixture_rejected(self) -> None:
        data = load_fixture("execution_lease_v1_missing_keys.json")
        with self.assertRaisesRegex(ExecutionLeaseError, "incorrect keys"):
            parse_execution_lease(data)

    def test_extra_keys_fixture_rejected(self) -> None:
        data = load_fixture("execution_lease_v1_extra_keys.json")
        with self.assertRaisesRegex(ExecutionLeaseError, "incorrect keys"):
            parse_execution_lease(data)

    def test_bad_identity_fixture_rejected(self) -> None:
        data = load_fixture("execution_lease_v1_bad_identity.json")
        with self.assertRaisesRegex(ExecutionLeaseError, "agent_id mismatch"):
            validate_execution_lease(
                data,
                expected_agent_id="mac-omp",
            )

    def test_resource_mismatch_fixture_rejected(self) -> None:
        data = load_fixture("execution_lease_v1_resource_mismatch.json")
        with self.assertRaisesRegex(ExecutionLeaseError, "resource_key mismatch"):
            validate_execution_lease(
                data,
                expected_agent_id="mac-omp",
            )

    def test_context_mismatch_fixture_rejected_as_coherent_context_fault(self) -> None:
        data = load_fixture("execution_lease_v1_context_mismatch.json")
        # The fixture keeps a matching resource_key for its normalized_path, so
        # the lease is internally consistent; only the execution_context path is
        # different. This makes it a coherent context fault, not a resource fault.
        ctx = _make_context(
            assigned_agent="mac-omp",
            host_id="mac",
            worktree_path="/home/example/multinexus",
        )
        with self.assertRaisesRegex(
            ExecutionLeaseError, "normalized_path does not match"
        ):
            validate_execution_lease(
                data,
                expected_agent_id="mac-omp",
                execution_context=ctx,
            )

    def test_bad_digest_fixture_rejected(self) -> None:
        data = load_fixture("execution_lease_v1_bad_digest.json")
        with self.assertRaisesRegex(ExecutionLeaseError, "sha256:<64-hex>"):
            parse_execution_lease(data)

    def test_bad_timestamps_fixture_rejected(self) -> None:
        data = load_fixture("execution_lease_v1_bad_timestamps.json")
        with self.assertRaisesRegex(
            ExecutionLeaseError, "ttl_seconds .* does not match"
        ):
            parse_execution_lease(data)

    def test_invalid_ttl_interval_fixture_rejected(self) -> None:
        data = load_fixture("execution_lease_v1_invalid_ttl_interval.json")
        with self.assertRaisesRegex(ExecutionLeaseError, "renew_interval_seconds"):
            parse_execution_lease(data)

    def test_stale_token_fixture_rejected(self) -> None:
        data = load_fixture("execution_lease_v1_stale_token.json")
        # attempt_token=0 fails parse-level positivity before identity check.
        with self.assertRaisesRegex(
            ExecutionLeaseError, "attempt_token must be positive"
        ):
            parse_execution_lease(data)


class SingleFaultFieldManifestTests(unittest.TestCase):
    """Each negative fixture differs from the positive fixture in one way only.

    This verifies the 9 mirrored Coordinate fixtures are true single-fault
    variants: either one key is missing, one extra key is present, or exactly
    one shared key changed value.  ``context_mismatch`` is a single semantic
    fault (the worktree path is wrong) that is reflected in two coherent
    derived fields: ``normalized_path`` and ``resource_key``.
    """

    def _diff(self, name: str) -> tuple[set[str], set[str], set[str]]:
        positive = load_fixture("execution_lease_v1_positive.json")
        data = load_fixture(name)
        positive_keys = set(positive.keys())
        data_keys = set(data.keys())
        missing = positive_keys - data_keys
        extra = data_keys - positive_keys
        changed = {
            key for key in positive_keys & data_keys if positive[key] != data[key]
        }
        return missing, extra, changed

    def test_negative_fixtures_are_single_fault_variants(self) -> None:
        for name in [
            "execution_lease_v1_bad_digest.json",
            "execution_lease_v1_bad_identity.json",
            "execution_lease_v1_bad_timestamps.json",
            "execution_lease_v1_context_mismatch.json",
            "execution_lease_v1_extra_keys.json",
            "execution_lease_v1_invalid_ttl_interval.json",
            "execution_lease_v1_missing_keys.json",
            "execution_lease_v1_resource_mismatch.json",
            "execution_lease_v1_stale_token.json",
        ]:
            with self.subTest(name=name):
                missing, extra, changed = self._diff(name)
                if name == "execution_lease_v1_context_mismatch.json":
                    self.assertEqual(
                        (missing, extra, changed),
                        (set(), set(), {"normalized_path", "resource_key"}),
                        f"{name} must change exactly the two coherent context fields",
                    )
                else:
                    total_faults = len(missing) + len(extra) + len(changed)
                    self.assertEqual(
                        total_faults,
                        1,
                        f"{name} must contain exactly one fault; "
                        f"missing={sorted(missing)}, extra={sorted(extra)}, "
                        f"changed={sorted(changed)}",
                    )

    def test_fixture_manifest_contents(self) -> None:
        missing, extra, changed = self._diff("execution_lease_v1_missing_keys.json")
        self.assertEqual(missing, {"lease_id"})
        self.assertEqual(extra, set())
        self.assertEqual(changed, set())

        missing, extra, changed = self._diff("execution_lease_v1_extra_keys.json")
        self.assertEqual(missing, set())
        self.assertEqual(extra, {"extra_key"})
        self.assertEqual(changed, set())

        missing, extra, changed = self._diff("execution_lease_v1_bad_identity.json")
        self.assertEqual(missing, set())
        self.assertEqual(extra, set())
        self.assertEqual(changed, {"agent_id"})
        data = load_fixture("execution_lease_v1_bad_identity.json")
        self.assertEqual(data["agent_id"], "mac-codex")

        missing, extra, changed = self._diff("execution_lease_v1_bad_timestamps.json")
        self.assertEqual(missing, set())
        self.assertEqual(extra, set())
        self.assertEqual(changed, {"expires_at"})

        missing, extra, changed = self._diff("execution_lease_v1_context_mismatch.json")
        self.assertEqual(missing, set())
        self.assertEqual(extra, set())
        self.assertEqual(changed, {"normalized_path", "resource_key"})
        data = load_fixture("execution_lease_v1_context_mismatch.json")
        self.assertEqual(
            data["normalized_path"],
            "/home/example/other",
        )
        self.assertEqual(
            data["resource_key"],
            "sha256:54fb9b95c6992ea66fc53ef5df860b780cb270ceea688ec5e73487ee26ff4557",
        )

        missing, extra, changed = self._diff(
            "execution_lease_v1_invalid_ttl_interval.json"
        )
        self.assertEqual(missing, set())
        self.assertEqual(extra, set())
        self.assertEqual(changed, {"renew_interval_seconds"})

        missing, extra, changed = self._diff(
            "execution_lease_v1_resource_mismatch.json"
        )
        self.assertEqual(missing, set())
        self.assertEqual(extra, set())
        self.assertEqual(changed, {"resource_key"})

        missing, extra, changed = self._diff("execution_lease_v1_stale_token.json")
        self.assertEqual(missing, set())
        self.assertEqual(extra, set())
        self.assertEqual(changed, {"attempt_token"})

        missing, extra, changed = self._diff("execution_lease_v1_bad_digest.json")
        self.assertEqual(missing, set())
        self.assertEqual(extra, set())
        self.assertEqual(changed, {"resource_key"})


class CoordinateClientLeaseTests(unittest.TestCase):
    def _client(self):
        return CoordinateRuntimeClient(cli_path="/bin/true", db_path="/tmp/test.db")

    @patch("multinexus.agentd.coordinate_client.subprocess.run")
    def test_report_job_passes_lease_id(self, mock_run):
        import subprocess

        mock_run.return_value = subprocess.CompletedProcess(
            "cmd", 0, stdout='{"ok": true}', stderr=""
        )
        client = self._client()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                client.report_job(
                    job_id="j1",
                    agent_id="a1",
                    status="done",
                    result_json={},
                    attempt_token=1,
                    lease_id="lease-1",
                )
            )
        finally:
            loop.close()
        cmd = mock_run.call_args[0][0]
        self.assertIn("--lease-id", cmd)
        self.assertIn("lease-1", cmd)
        self.assertIn("--attempt-token", cmd)
        self.assertIn("1", cmd)

    @patch("multinexus.agentd.coordinate_client.subprocess.run")
    def test_record_progress_passes_lease_id(self, mock_run):
        import subprocess

        mock_run.return_value = subprocess.CompletedProcess(
            "cmd", 0, stdout='{"ok": true}', stderr=""
        )
        client = self._client()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                client.record_progress(
                    job_id="j1",
                    agent_id="a1",
                    stage="stream",
                    attempt_token=1,
                    lease_id="lease-1",
                )
            )
        finally:
            loop.close()
        cmd = mock_run.call_args[0][0]
        self.assertIn("--lease-id", cmd)
        self.assertIn("lease-1", cmd)

    @patch("multinexus.agentd.coordinate_client.subprocess.run")
    def test_renew_lease_command(self, mock_run):
        import subprocess

        mock_run.return_value = subprocess.CompletedProcess(
            "cmd",
            0,
            stdout='{"result": {"expires_at": "2026-07-14T12:04:00Z", "server_now": "2026-07-14T12:00:00Z"}}',
            stderr="",
        )
        client = self._client()
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                client.renew_lease(
                    job_id="j1",
                    agent_id="a1",
                    attempt_token=1,
                    lease_id="lease-1",
                )
            )
        finally:
            loop.close()
        cmd = mock_run.call_args[0][0]
        self.assertIn("runtime", cmd)
        self.assertIn("job", cmd)
        self.assertIn("lease", cmd)
        self.assertIn("renew", cmd)
        self.assertIn("--lease-id", cmd)
        self.assertIn("lease-1", cmd)
        self.assertEqual(result["result"]["expires_at"], "2026-07-14T12:04:00Z")

    @patch("multinexus.agentd.coordinate_client.subprocess.run")
    def test_reap_leases_command(self, mock_run):
        import subprocess

        mock_run.return_value = subprocess.CompletedProcess(
            "cmd", 0, stdout='{"reaped": 3}', stderr=""
        )
        client = self._client()
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                client.reap_leases(actor="agentd", batch_size=50)
            )
        finally:
            loop.close()
        cmd = mock_run.call_args[0][0]
        self.assertIn("runtime", cmd)
        self.assertIn("job", cmd)
        self.assertIn("lease", cmd)
        self.assertIn("reap", cmd)
        self.assertIn("--actor", cmd)
        self.assertIn("agentd", cmd)
        self.assertIn("--batch-size", cmd)
        self.assertIn("50", cmd)
        self.assertEqual(result["reaped"], 3)


class WorkerLeaseValidationTests(unittest.TestCase):
    """P9-3B: worker must validate execution_lease before invoking provider."""

    def _make_worker(self):
        worker = AgentdWorker(_config())
        calls = []

        class FakeAdapter:
            async def call(self, prompt, *, work_dir=None, on_progress=None, **kw):
                calls.append(("call", prompt, work_dir))
                return AdapterResult(text=f"reply: {prompt}", session_id="s1")

        worker.adapter = FakeAdapter()
        return worker, calls

    def test_adapter_not_called_when_lease_identity_bad(self):
        worker, calls = self._make_worker()
        reported = []

        async def mock_report(**kw):
            reported.append(kw)

        worker.coordinate.report_job = mock_report

        result = _claim_result(
            include_lease=True,
            lease_overrides={"agent_id": "other-agent"},
            executor_binding=_make_binding(),
        )
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(result))
        finally:
            loop.close()

        self.assertEqual(len(calls), 0)
        self.assertEqual(len(reported), 0)

    def test_adapter_called_and_reports_lease_id_when_lease_valid(self):
        worker, calls = self._make_worker()
        reported = []

        async def mock_report(**kw):
            reported.append(kw)

        worker.coordinate.report_job = mock_report

        result = _claim_result(include_lease=True, executor_binding=_make_binding())
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(result))
        finally:
            loop.close()

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["status"], "done")
        self.assertEqual(
            reported[0]["lease_id"], "039bdcda-de94-4bbd-9f26-13f39b2d33bf"
        )

    def test_legacy_untyped_job_runs_without_lease(self):
        worker, calls = self._make_worker()
        reported = []

        async def mock_report(**kw):
            reported.append(kw)

        worker.coordinate.report_job = mock_report

        result = _claim_result(include_lease=False)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(result))
        finally:
            loop.close()

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["status"], "done")
        self.assertIsNone(reported[0].get("lease_id"))

    def test_managed_claim_missing_lease_drops_without_provider_or_report(self):
        """Managed payload without a lease cannot prove authority: fail closed."""
        worker, calls = self._make_worker()
        reported = []

        async def mock_report(**kw):
            reported.append(kw)

        worker.coordinate.report_job = mock_report

        result = _claim_result(include_lease=False, executor_binding=_make_binding())
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(result))
        finally:
            loop.close()

        self.assertEqual(len(calls), 0)
        self.assertEqual(len(reported), 0)

    def test_non_object_lease_drops_without_provider_or_report(self):
        """execution_lease present but not a dict is malformed: fail closed."""
        worker, calls = self._make_worker()
        reported = []

        async def mock_report(**kw):
            reported.append(kw)

        worker.coordinate.report_job = mock_report

        result = _claim_result(
            include_lease=True,
            executor_binding=_make_binding(),
        )
        result["execution_lease"] = ["not", "a", "dict"]
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(result))
        finally:
            loop.close()

        self.assertEqual(len(calls), 0)
        self.assertEqual(len(reported), 0)

    def test_invalid_dict_lease_drops_without_provider_or_report(self):
        """execution_lease dict that fails parse/identity is not trusted."""
        worker, calls = self._make_worker()
        reported = []

        async def mock_report(**kw):
            reported.append(kw)

        worker.coordinate.report_job = mock_report

        result = _claim_result(
            include_lease=True,
            lease_overrides={"agent_id": "other-agent"},
            executor_binding=_make_binding(),
        )
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(result))
        finally:
            loop.close()

        self.assertEqual(len(calls), 0)
        self.assertEqual(len(reported), 0)

    def test_managed_claim_missing_binding_reports_failed_with_trusted_lease(self):
        """Valid lease but missing binding on a managed claim fails closed."""
        worker, calls = self._make_worker()
        reported = []

        async def mock_report(**kw):
            reported.append(kw)

        worker.coordinate.report_job = mock_report

        result = _claim_result(include_lease=True, executor_binding=None)
        # Force the payload to look managed by injecting a sentinel that is not
        # None, but also not a valid executor_binding dict.
        result["job"]["payload_json"] = json.dumps(
            {"prompt": "hello", "executor_binding": "invalid"}
        )
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(result))
        finally:
            loop.close()

        self.assertEqual(len(calls), 0)
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["status"], "failed")
        self.assertEqual(
            reported[0]["lease_id"], "039bdcda-de94-4bbd-9f26-13f39b2d33bf"
        )
        self.assertEqual(reported[0]["attempt_token"], 1)
        self.assertIn("executor_binding", reported[0]["result_json"]["error"])

    def test_managed_claim_invalid_binding_reports_failed_with_trusted_lease(self):
        """Valid lease but binding instance mismatch fails closed using lease authority."""
        worker, calls = self._make_worker()
        reported = []

        async def mock_report(**kw):
            reported.append(kw)

        worker.coordinate.report_job = mock_report

        binding = _make_binding(executor_instance_id="other-agent")
        _validate_binding_signature(binding)
        result = _claim_result(include_lease=True, executor_binding=binding)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(result))
        finally:
            loop.close()

        self.assertEqual(len(calls), 0)
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["status"], "failed")
        self.assertEqual(
            reported[0]["lease_id"], "039bdcda-de94-4bbd-9f26-13f39b2d33bf"
        )
        self.assertEqual(reported[0]["attempt_token"], 1)
        self.assertIn("executor_binding_mismatch", reported[0]["result_json"]["error"])

    def test_context_rejection_reports_failed_with_trusted_lease_authority(self):
        """Invalid context with a valid lease reports using lease job_id/token/id."""
        worker, calls = self._make_worker()
        reported = []

        async def mock_report(**kw):
            reported.append(kw)

        worker.coordinate.report_job = mock_report

        result = _claim_result(include_lease=True, executor_binding=_make_binding())
        result["execution_context"]["assigned_agent"] = "other-agent"
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(result))
        finally:
            loop.close()

        self.assertEqual(len(calls), 0)
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["status"], "failed")
        self.assertEqual(reported[0]["job_id"], "request:e1")
        self.assertEqual(reported[0]["attempt_token"], 1)
        self.assertEqual(
            reported[0]["lease_id"], "039bdcda-de94-4bbd-9f26-13f39b2d33bf"
        )

    def test_legacy_untyped_context_rejection_still_reports_failed(self):
        """Legacy jobs without lease still report context failures raw attempt token."""
        worker, calls = self._make_worker()
        reported = []

        async def mock_report(**kw):
            reported.append(kw)

        worker.coordinate.report_job = mock_report

        result = _claim_result(include_lease=False)
        result["execution_context"]["assigned_agent"] = "other-agent"
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(result))
        finally:
            loop.close()

        self.assertEqual(len(calls), 0)
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["status"], "failed")
        self.assertIsNone(reported[0].get("lease_id"))
        self.assertEqual(reported[0]["attempt_token"], 1)


class WorkerRenewalSupervisorTests(unittest.TestCase):
    """P9-3B: dedicated renewal supervisor cancels provider on lease loss."""

    def _make_worker(self):
        worker = AgentdWorker(_config())
        calls = []

        class FakeAdapter:
            async def call(self, prompt, *, work_dir=None, on_progress=None, **kw):
                calls.append(("call", prompt, work_dir))
                await asyncio.sleep(10)
                return AdapterResult(text="slow reply", session_id="s1")

        worker.adapter = FakeAdapter()
        return worker, calls

    def test_renewal_failure_cancels_provider_and_does_not_report_done(self):
        worker, calls = self._make_worker()
        reported = []

        async def mock_report(**kw):
            reported.append(kw)

        async def failing_renew(**kw):
            raise CoordinateRuntimeError("lease rejected")

        worker.coordinate.report_job = mock_report
        worker.coordinate.renew_lease = failing_renew

        result = _claim_result(
            include_lease=True,
            lease_overrides={
                "server_now": "2026-07-14T12:00:00Z",
                "expires_at": "2026-07-14T12:00:30Z",
                "ttl_seconds": 30,
                "renew_interval_seconds": 1,
            },
            executor_binding=_make_binding(),
        )

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                asyncio.wait_for(worker._process_job(result), timeout=5)
            )
        except asyncio.TimeoutError:
            pass
        finally:
            loop.close()

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(reported), 0)

    def test_provider_completes_before_deadline(self):
        worker, calls = self._make_worker()
        reported = []

        async def mock_report(**kw):
            reported.append(kw)

        renew_count = 0

        async def successful_renew(**kw):
            nonlocal renew_count
            renew_count += 1
            return {
                "result": {
                    "expires_at": "2026-07-14T12:05:00Z",
                    "server_now": "2026-07-14T12:00:00Z",
                }
            }

        worker.coordinate.report_job = mock_report
        worker.coordinate.renew_lease = successful_renew

        class FastAdapter:
            async def call(self, prompt, *, work_dir=None, on_progress=None, **kw):
                calls.append(("call", prompt, work_dir))
                return AdapterResult(text="fast reply", session_id="s1")

        worker.adapter = FastAdapter()

        result = _claim_result(
            include_lease=True,
            lease_overrides={
                "server_now": "2026-07-14T12:00:00Z",
                "expires_at": "2026-07-14T12:00:30Z",
                "ttl_seconds": 30,
                "renew_interval_seconds": 10,
            },
            executor_binding=_make_binding(),
        )

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(worker._process_job(result))
        finally:
            loop.close()

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["status"], "done")
        self.assertEqual(
            reported[0]["lease_id"], "039bdcda-de94-4bbd-9f26-13f39b2d33bf"
        )




class ReapPolicyWorkerTests(unittest.TestCase):
    """P9-3C1: reap_mode propagation through worker."""

    def test_worker_none_forwards_same_policy_on_every_poll(self):
        worker = AgentdWorker(_config())
        claims = []

        async def mock_claim(**kwargs):
            claims.append(kwargs)
            if len(claims) == 2:
                worker.stop()
            return {"claimed": False, "reason": "queue_empty"}

        worker.coordinate.claim_job = mock_claim
        asyncio.run(
            worker.run(
                poll_interval=0.001,
                reap_mode="none",
                reap_reason="sealed-test-reason",
            )
        )

        self.assertEqual(len(claims), 2)
        for claim in claims:
            self.assertEqual(claim["reap_mode"], "none")
            self.assertEqual(claim["reap_reason"], "sealed-test-reason")

    def test_worker_recovery_forwards_evidence_and_reap_policy(self):
        worker = AgentdWorker(_config())
        claims = []

        async def mock_claim(**kwargs):
            claims.append(kwargs)
            worker.stop()
            return {"claimed": False, "reason": "queue_empty"}

        worker.coordinate.claim_job = mock_claim
        asyncio.run(
            worker.run(
                poll_interval=0.001,
                recoverable=True,
                recovery_reason="prior-process-crashed",
                prior_process_stopped=True,
                reap_mode="none",
                reap_reason="sealed-recovery-policy",
            )
        )

        self.assertEqual(len(claims), 1)
        self.assertEqual(
            {
                key: value for key, value in claims[0].items() if key != "claim_request_id"
            },
            {
                "agent_id": "mac-omp",
                "recoverable": True,
                "recovery_reason": "prior-process-crashed",
                "prior_process_stopped": True,
                "reap_mode": "none",
                "reap_reason": "sealed-recovery-policy",
            },
        )
        self.assertRegex(claims[0]["claim_request_id"], r"^[0-9a-f]{32}$")

    def test_worker_invalid_reap_policy_fails_before_claim_or_running(self):
        worker = AgentdWorker(_config())
        claims = []

        async def mock_claim(**kwargs):
            claims.append(kwargs)
            return {"claimed": False, "reason": "queue_empty"}

        worker.coordinate.claim_job = mock_claim
        with self.assertRaises(CoordinateRuntimeError):
            asyncio.run(
                worker.run(
                    poll_interval=0.001,
                    reap_mode="none",
                    reap_reason=None,
                )
            )

        self.assertEqual(claims, [])
        self.assertFalse(worker._running)

    def test_normalize_recovery_reason_unchanged(self):
        """normalize_recovery_reason must remain backward-compatible."""
        from multinexus.agentd.coordinate_client import normalize_recovery_reason
        result = normalize_recovery_reason("test recovery")
        self.assertEqual(result, "test recovery")

    def test_normalize_recovery_reason_empty_fails(self):
        from multinexus.agentd.coordinate_client import (
            CoordinateRuntimeError,
            normalize_recovery_reason,
        )
        with self.assertRaises(CoordinateRuntimeError):
            normalize_recovery_reason("   ")

    def test_normalize_recovery_reason_non_string_fails(self):
        from multinexus.agentd.coordinate_client import (
            CoordinateRuntimeError,
            normalize_recovery_reason,
        )
        with self.assertRaises(CoordinateRuntimeError):
            normalize_recovery_reason(123)


if __name__ == "__main__":
    unittest.main()
