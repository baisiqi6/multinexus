"""Tests for MultiNexus strict consumption of Coordinate's ExecutorBinding snapshot."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from multinexus.agentd.executor_binding import (
    ExecutorBindingError,
    parse_executor_binding,
    validate_executor_binding,
)
from multinexus.models import AgentConfig


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"


def _fixture_binding() -> dict[str, object]:
    return json.loads((FIXTURES / "executor_binding_v1.json").read_text(encoding="utf-8"))


def _make_snapshot(**overrides) -> dict[str, object]:
    snap = _fixture_binding().copy()
    snap.update(overrides)
    if "binding_id" not in overrides:
        body = {k: v for k, v in snap.items() if k != "binding_id"}
        snap["binding_id"] = "sha256:" + hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    return snap


class ParseBindingTests(unittest.TestCase):
    def test_parse_valid_fixture(self):
        snap = _fixture_binding()
        binding = parse_executor_binding(snap)
        self.assertEqual(binding.binding_id, snap["binding_id"])
        self.assertEqual(binding.executor_instance_id, "mac-omp")
        self.assertEqual(binding.adapter, "omp")

    def test_rejects_missing_field(self):
        snap = _make_snapshot()
        del snap["provider"]
        with self.assertRaisesRegex(ExecutorBindingError, "incorrect fields"):
            parse_executor_binding(snap)

    def test_rejects_extra_field(self):
        snap = _make_snapshot()
        snap["command"] = "/bin/evil"
        with self.assertRaisesRegex(ExecutorBindingError, "incorrect fields"):
            parse_executor_binding(snap)

    def test_extra_field_error_is_bounded(self):
        snap = _make_snapshot()
        for index in range(100):
            snap[f"unexpected_{index}"] = "x"
        with self.assertRaises(ExecutorBindingError) as caught:
            parse_executor_binding(snap)
        self.assertLess(len(str(caught.exception)), 512)
        self.assertIn("unexpected_count=100", str(caught.exception))

    def test_rejects_path_separator_in_provider(self):
        snap = _make_snapshot(provider="kimi/code")
        with self.assertRaisesRegex(ExecutorBindingError, "unsafe characters"):
            parse_executor_binding(snap)

    def test_rejects_shell_metacharacter_in_adapter(self):
        for bad in ('a;b', 'a|b', 'a&b', 'a`b', 'a$(b)'):
            snap = _make_snapshot(adapter=bad)
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ExecutorBindingError, "unsafe characters"):
                    parse_executor_binding(snap)

    def test_rejects_digest_mismatch(self):
        snap = _make_snapshot()
        snap["binding_id"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ExecutorBindingError, "digest mismatch"):
            parse_executor_binding(snap)

    def test_rejects_unsorted_capabilities(self):
        snap = _make_snapshot(capabilities=["review", "coding"])
        with self.assertRaisesRegex(ExecutorBindingError, "must be sorted"):
            parse_executor_binding(snap)


class ValidateAgainstConfigTests(unittest.TestCase):
    def _config(self, **overrides):
        defaults = {
            "id": "mac-omp",
            "token": "fake-token",
            "adapter": "omp",
            "context_db_path": str(Path(tempfile.mkdtemp()) / "test.sqlite3"),
        }
        defaults.update(overrides)
        return AgentConfig(**defaults)

    def test_legacy_null_binding_accepted(self):
        self.assertIsNone(validate_executor_binding(None, agent_id="mac-omp", adapter="omp"))

    def test_typed_binding_matching_config_accepted(self):
        snap = _fixture_binding()
        binding = validate_executor_binding(snap, agent_id="mac-omp", adapter="omp")
        self.assertIsNotNone(binding)
        self.assertEqual(binding.executor_instance_id, "mac-omp")

    def test_instance_id_mismatch_fails(self):
        snap = _make_snapshot()
        with self.assertRaisesRegex(ExecutorBindingError, "executor_binding_mismatch.*instance_id"):
            validate_executor_binding(snap, agent_id="other", adapter="omp")

    def test_runner_profile_id_mismatch_fails(self):
        snap = _make_snapshot(executor_instance_id="other")
        with self.assertRaisesRegex(ExecutorBindingError, "executor_binding_mismatch.*runner_profile_id"):
            validate_executor_binding(snap, agent_id="other", adapter="omp")

    def test_adapter_mismatch_fails(self):
        snap = _make_snapshot()
        with self.assertRaisesRegex(ExecutorBindingError, "executor_binding_mismatch.*adapter"):
            validate_executor_binding(snap, agent_id="mac-omp", adapter="claude")


if __name__ == "__main__":
    unittest.main()
