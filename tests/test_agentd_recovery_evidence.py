"""Tests for P9-3B recovery evidence wiring in agentd coordinate client and worker."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import unittest
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import AsyncMock, MagicMock, patch

from multinexus.agentd.coordinate_client import (
    CoordinateRuntimeClient,
    CoordinateRuntimeError,
)
from multinexus.agentd.worker import AgentdWorker
from multinexus.models import AgentConfig


def _config(**overrides):
    defaults = {
        "id": "test-agent",
        "token": "fake-token",
        "adapter": "claude",
        "context_db_path": str(Path(mkdtemp()) / "test.sqlite3"),
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


class ClaimJobRecoveryEvidenceTests(unittest.TestCase):
    """A) Recovery evidence validation must fail closed before any subprocess."""

    def _client(self):
        return CoordinateRuntimeClient(cli_path="/bin/true", db_path="/tmp/test.db")

    def _capture_run(self, commands):
        def mock_run(cmd, **kwargs):
            commands.append(cmd)
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout='{"result": {"claimed": false, "reason": "queue_empty"}}',
                stderr="",
            )

        return mock_run

    def _run_claim(self, *, commands=None, **kwargs):
        client = self._client()
        commands = commands if commands is not None else []
        with patch(
            "multinexus.agentd.coordinate_client.subprocess.run",
            side_effect=self._capture_run(commands),
        ):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(
                    client.claim_job(agent_id="test-agent", **kwargs)
                ), commands
            finally:
                loop.close()

    def test_recoverable_with_full_evidence_appends_flags(self):
        result, commands = self._run_claim(
            recoverable=True,
            recovery_reason="prior-process-crashed",
            prior_process_stopped=True,
        )
        self.assertEqual(len(commands), 1)
        cmd = commands[0]
        self.assertIn("--recoverable", cmd)
        self.assertIn("--recovery-reason", cmd)
        self.assertIn("prior-process-crashed", cmd)
        self.assertIn("--prior-process-stopped", cmd)
        self.assertEqual(result.get("reason"), "queue_empty")

    def test_recoverable_without_reason_fails_closed_before_subprocess(self):
        with self.assertRaisesRegex(CoordinateRuntimeError, "recovery_reason"):
            self._run_claim(
                recoverable=True,
                recovery_reason="",
                prior_process_stopped=True,
            )

    def test_recoverable_without_prior_stopped_fails_closed_before_subprocess(self):
        with self.assertRaisesRegex(
            CoordinateRuntimeError, "prior_process_stopped"
        ):
            self._run_claim(
                recoverable=True,
                recovery_reason="prior-process-crashed",
                prior_process_stopped=False,
            )

    def test_non_recoverable_with_reason_fails_closed_before_subprocess(self):
        with self.assertRaisesRegex(
            CoordinateRuntimeError,
            "non-recoverable claim must not carry recovery_reason",
        ):
            self._run_claim(
                recoverable=False,
                recovery_reason="oops",
            )

    def test_non_recoverable_with_prior_stopped_fails_closed_before_subprocess(self):
        with self.assertRaisesRegex(
            CoordinateRuntimeError,
            "non-recoverable claim must not carry prior_process_stopped",
        ):
            self._run_claim(
                recoverable=False,
                prior_process_stopped=True,
            )

    def test_recoverable_whitespace_reason_fails_closed_before_subprocess(self):
        with self.assertRaisesRegex(CoordinateRuntimeError, "recovery_reason"):
            self._run_claim(
                recoverable=True,
                recovery_reason="   ",
                prior_process_stopped=True,
            )

    def test_recoverable_oversized_reason_fails_closed_before_subprocess(self):
        with self.assertRaisesRegex(CoordinateRuntimeError, "recovery_reason"):
            self._run_claim(
                recoverable=True,
                recovery_reason="x" * 513,
                prior_process_stopped=True,
            )

    def test_recoverable_control_character_reason_fails_closed_before_subprocess(self):
        with self.assertRaisesRegex(CoordinateRuntimeError, "control"):
            self._run_claim(
                recoverable=True,
                recovery_reason="bad\x00reason",
                prior_process_stopped=True,
            )

    def test_non_recoverable_reason_none_fails_closed_before_subprocess(self):
        with self.assertRaisesRegex(CoordinateRuntimeError, "recovery_reason"):
            self._run_claim(
                recoverable=False,
                recovery_reason=None,  # type: ignore[arg-type]
            )

    def test_non_recoverable_reason_zero_fails_closed_before_subprocess(self):
        with self.assertRaisesRegex(CoordinateRuntimeError, "recovery_reason"):
            self._run_claim(
                recoverable=False,
                recovery_reason=0,  # type: ignore[arg-type]
            )

    def test_recoverable_prior_int_one_rejected_before_subprocess(self):
        with self.assertRaisesRegex(
            CoordinateRuntimeError, "prior_process_stopped"
        ):
            self._run_claim(
                recoverable=True,
                recovery_reason="prior-process-crashed",
                prior_process_stopped=1,  # type: ignore[arg-type]
            )

    def test_normalized_reason_passed_exactly_to_cli(self):
        result, commands = self._run_claim(
            recoverable=True,
            recovery_reason="  prior-process-crashed  ",
            prior_process_stopped=True,
        )
        self.assertEqual(len(commands), 1)
        cmd = commands[0]
        reason_index = cmd.index("--recovery-reason") + 1
        self.assertEqual(cmd[reason_index], "prior-process-crashed")
        self.assertNotIn("  prior-process-crashed  ", cmd)
        self.assertEqual(result.get("reason"), "queue_empty")


class ClaimedFalseDiagnosticsTests(unittest.TestCase):
    """B) claim_job must return the full inner dict when claimed=False."""

    def test_claimed_false_preserves_reason_and_blocker_fields(self):
        client = CoordinateRuntimeClient(cli_path="/bin/true", db_path="/tmp/test.db")
        envelope = {
            "result": {
                "claimed": False,
                "reason": "resource_blocked",
                "oldest_blocked_job_id": "job:old",
                "oldest_blocked_resource_key": "resource:gpu",
            }
        }

        def mock_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps(envelope), stderr=""
            )

        with patch(
            "multinexus.agentd.coordinate_client.subprocess.run", side_effect=mock_run
        ):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    client.claim_job(agent_id="test-agent")
                )
            finally:
                loop.close()

        self.assertEqual(result["claimed"], False)
        self.assertEqual(result["reason"], "resource_blocked")
        self.assertEqual(result["oldest_blocked_job_id"], "job:old")
        self.assertEqual(result["oldest_blocked_resource_key"], "resource:gpu")


class WorkerRunEvidencePassthroughTests(unittest.TestCase):
    """C) Worker.run must validate and pass recovery evidence to claim_job."""

    def test_worker_run_passes_evidence_in_recovery_mode(self):
        worker = AgentdWorker(_config())
        seen = {}

        async def mock_claim(
            *,
            agent_id,
            recoverable=False,
            recovery_reason="",
            prior_process_stopped=False,
        ):
            seen.update(
                {
                    "agent_id": agent_id,
                    "recoverable": recoverable,
                    "recovery_reason": recovery_reason,
                    "prior_process_stopped": prior_process_stopped,
                }
            )
            worker.stop()
            return {"claimed": False, "reason": "queue_empty"}

        worker.coordinate.claim_job = mock_claim

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                worker.run(
                    poll_interval=0.01,
                    recoverable=True,
                    recovery_reason="prior-process-crashed",
                    prior_process_stopped=True,
                )
            )
        finally:
            loop.close()

        self.assertEqual(seen["agent_id"], "test-agent")
        self.assertTrue(seen["recoverable"])
        self.assertEqual(seen["recovery_reason"], "prior-process-crashed")
        self.assertTrue(seen["prior_process_stopped"])

    def test_worker_run_fails_closed_without_recovery_reason(self):
        worker = AgentdWorker(_config())
        loop = asyncio.new_event_loop()
        try:
            with self.assertRaisesRegex(CoordinateRuntimeError, "recovery_reason"):
                loop.run_until_complete(
                    worker.run(
                        poll_interval=0.01,
                        recoverable=True,
                        recovery_reason="",
                        prior_process_stopped=True,
                    )
                )
        finally:
            loop.close()
        self.assertFalse(worker._running)

    def test_worker_run_fails_closed_without_prior_process_stopped(self):
        worker = AgentdWorker(_config())
        loop = asyncio.new_event_loop()
        try:
            with self.assertRaisesRegex(
                CoordinateRuntimeError, "prior_process_stopped"
            ):
                loop.run_until_complete(
                    worker.run(
                        poll_interval=0.01,
                        recoverable=True,
                        recovery_reason="prior-process-crashed",
                        prior_process_stopped=False,
                    )
                )
        finally:
            loop.close()
        self.assertFalse(worker._running)

    def test_worker_run_rejects_evidence_in_non_recoverable_mode(self):
        worker = AgentdWorker(_config())
        loop = asyncio.new_event_loop()
        try:
            with self.assertRaisesRegex(
                CoordinateRuntimeError,
                "non-recoverable worker run must not carry recovery_reason",
            ):
                loop.run_until_complete(
                    worker.run(
                        poll_interval=0.01,
                        recoverable=False,
                        recovery_reason="oops",
                    )
                )
        finally:
            loop.close()
        self.assertFalse(worker._running)

    def test_worker_run_prior_int_one_rejected_before_claim(self):
        worker = AgentdWorker(_config())
        seen = {"called": False}

        async def mock_claim(*, agent_id, **kwargs):
            seen["called"] = True
            return {"claimed": False, "reason": "queue_empty"}

        worker.coordinate.claim_job = mock_claim
        loop = asyncio.new_event_loop()
        try:
            with self.assertRaisesRegex(
                CoordinateRuntimeError, "prior_process_stopped"
            ):
                loop.run_until_complete(
                    worker.run(
                        poll_interval=0.01,
                        recoverable=True,
                        recovery_reason="prior-process-crashed",
                        prior_process_stopped=1,  # type: ignore[arg-type]
                    )
                )
        finally:
            loop.close()
        self.assertFalse(seen["called"])
        self.assertFalse(worker._running)

    def test_worker_run_whitespace_reason_rejected_before_claim(self):
        worker = AgentdWorker(_config())
        seen = {"called": False}

        async def mock_claim(*, agent_id, **kwargs):
            seen["called"] = True
            return {"claimed": False, "reason": "queue_empty"}

        worker.coordinate.claim_job = mock_claim
        loop = asyncio.new_event_loop()
        try:
            with self.assertRaisesRegex(CoordinateRuntimeError, "recovery_reason"):
                loop.run_until_complete(
                    worker.run(
                        poll_interval=0.01,
                        recoverable=True,
                        recovery_reason="   ",
                        prior_process_stopped=True,
                    )
                )
        finally:
            loop.close()
        self.assertFalse(seen["called"])
        self.assertFalse(worker._running)

    def test_worker_run_passes_normalized_reason_to_claim(self):
        worker = AgentdWorker(_config())
        seen = {}

        async def mock_claim(
            *,
            agent_id,
            recoverable=False,
            recovery_reason="",
            prior_process_stopped=False,
        ):
            seen.update(
                {
                    "agent_id": agent_id,
                    "recoverable": recoverable,
                    "recovery_reason": recovery_reason,
                    "prior_process_stopped": prior_process_stopped,
                }
            )
            worker.stop()
            return {"claimed": False, "reason": "queue_empty"}

        worker.coordinate.claim_job = mock_claim
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                worker.run(
                    poll_interval=0.01,
                    recoverable=True,
                    recovery_reason="  normalized-reason  ",
                    prior_process_stopped=True,
                )
            )
        finally:
            loop.close()

        self.assertEqual(seen["recovery_reason"], "normalized-reason")


class BoundedBlockerLogTests(unittest.TestCase):
    """C) Blocker diagnostics are bounded, allowlisted, and never leak payload."""

    def test_queue_empty_logged_at_debug(self):
        with self.assertLogs("multinexus.agentd.worker", level="DEBUG") as cm:
            AgentdWorker._log_claim_blocker(
                {
                    "claimed": False,
                    "reason": "queue_empty",
                }
            )
        self.assertTrue(
            any("queue_empty" in record.getMessage() for record in cm.records)
        )
        self.assertFalse(
            any(record.levelno >= 20 for record in cm.records)
        )  # not INFO/WARNING

    def test_resource_blocked_logged_at_warning_with_truncated_ids(self):
        long_id = "job:" + "x" * 200
        long_resource = "resource:" + "y" * 200
        with self.assertLogs("multinexus.agentd.worker", level="WARNING") as cm:
            AgentdWorker._log_claim_blocker(
                {
                    "claimed": False,
                    "reason": "resource_blocked",
                    "oldest_blocked_job_id": long_id,
                    "oldest_blocked_resource_key": long_resource,
                }
            )
        message = cm.records[0].getMessage()
        self.assertIn("resource_blocked", message)
        self.assertIn(long_id[:64], message)
        self.assertNotIn(long_id, message)
        self.assertIn(long_resource[:64], message)
        self.assertNotIn(long_resource, message)

    def test_capacity_exhausted_logged(self):
        with self.assertLogs("multinexus.agentd.worker", level="INFO") as cm:
            AgentdWorker._log_claim_blocker(
                {
                    "claimed": False,
                    "reason": "capacity_exhausted",
                }
            )
        self.assertTrue(
            any("capacity_exhausted" in record.getMessage() for record in cm.records)
        )

    def test_scan_limit_reached_logged(self):
        with self.assertLogs("multinexus.agentd.worker", level="INFO") as cm:
            AgentdWorker._log_claim_blocker(
                {
                    "claimed": False,
                    "reason": "scan_limit_reached",
                }
            )
        self.assertTrue(
            any("scan_limit_reached" in record.getMessage() for record in cm.records)
        )

    def test_non_allowlist_reason_is_silent(self):
        with self.assertNoLogs("multinexus.agentd.worker", level="DEBUG"):
            AgentdWorker._log_claim_blocker(
                {
                    "claimed": False,
                    "reason": "malicious_payload_leak",
                    "prompt": "secret",
                    "payload": {"key": "value"},
                }
            )

    def test_payload_and_prompt_never_logged(self):
        with self.assertLogs("multinexus.agentd.worker", level="DEBUG") as cm:
            AgentdWorker._log_claim_blocker(
                {
                    "claimed": False,
                    "reason": "queue_empty",
                    "prompt": "do not log me",
                    "payload": {"secret": "data"},
                }
            )
        output = "\n".join(cm.output)
        self.assertNotIn("do not log me", output)
        self.assertNotIn("secret", output)
        self.assertNotIn("payload", output)

    def test_control_characters_in_blocked_ids_are_sanitized(self):
        # Include newline, tab, NUL, DEL plus a long suffix.
        long_suffix = "x" * 100
        raw_job_id = f"job:line\x00one\x7ftwo\nthree\tfour{long_suffix}"
        raw_resource = f"res:\x00\x7f\n\t{long_suffix}"
        with self.assertLogs("multinexus.agentd.worker", level="WARNING") as cm:
            AgentdWorker._log_claim_blocker(
                {
                    "claimed": False,
                    "reason": "resource_blocked",
                    "oldest_blocked_job_id": raw_job_id,
                    "oldest_blocked_resource_key": raw_resource,
                }
            )
        output = "\n".join(cm.output)
        # Raw control characters must not appear in the log message.
        for char in "\x00\x7f\n\t":
            self.assertNotIn(char, output)
        # A sanitized replacement must be present and length-bounded.
        self.assertIn("oldest_job_id=", output)
        self.assertIn("oldest_resource_key=", output)
        self.assertIn("?", output)
        for record in cm.records:
            message = record.getMessage()
            for prefix in ("oldest_job_id=", "oldest_resource_key="):
                if prefix in message:
                    value_part = message.split(prefix, 1)[1].split()[0]
                    self.assertLessEqual(len(value_part), 64)

    def test_sanitized_value_replaces_each_control_character(self):
        raw_job_id = "\x00\x01\n\r\t\x7f"
        sanitized = AgentdWorker._sanitize_blocked_field(raw_job_id)
        self.assertEqual(sanitized, "??????")
        self.assertEqual(len(sanitized), 6)

    def test_prompt_payload_with_control_chars_still_not_logged(self):
        with self.assertLogs("multinexus.agentd.worker", level="WARNING") as cm:
            AgentdWorker._log_claim_blocker(
                {
                    "claimed": False,
                    "reason": "resource_blocked",
                    "oldest_blocked_job_id": "job:normal",
                    "oldest_blocked_resource_key": "res:normal",
                    "prompt": "secret\x00with\ncontrols",
                    "payload": {"key": "val\x7fue"},
                }
            )
        output = "\n".join(cm.output)
        self.assertNotIn("secret", output)
        self.assertNotIn("with", output)
        self.assertNotIn("payload", output)
        self.assertNotIn("value", output)
        self.assertIn("oldest_job_id=job:normal", output)
        self.assertIn("oldest_resource_key=res:normal", output)


class CliRecoveryArgumentTests(unittest.TestCase):
    """D) CLI must enforce --recoverable grouping via parser.error fail closed."""

    def test_reject_recoverable_without_reason(self):
        from multinexus.agentd.__main__ import main

        with patch("multinexus.agentd.__main__.load_config") as mock_load:
            with self.assertRaises(SystemExit):
                main(["--agent", "test-agent", "--recoverable"])
        mock_load.assert_not_called()

    def test_reject_recoverable_without_prior_stopped(self):
        from multinexus.agentd.__main__ import main

        with patch("multinexus.agentd.__main__.load_config") as mock_load:
            with self.assertRaises(SystemExit):
                main(
                    ["--agent", "test-agent", "--recoverable", "--recovery-reason", "x"]
                )
        mock_load.assert_not_called()

    def test_reject_reason_without_recoverable(self):
        from multinexus.agentd.__main__ import main

        with patch("multinexus.agentd.__main__.load_config") as mock_load:
            with self.assertRaises(SystemExit):
                main(["--agent", "test-agent", "--recovery-reason", "x"])
        mock_load.assert_not_called()

    def test_reject_prior_stopped_without_recoverable(self):
        from multinexus.agentd.__main__ import main

        with patch("multinexus.agentd.__main__.load_config") as mock_load:
            with self.assertRaises(SystemExit):
                main(["--agent", "test-agent", "--prior-process-stopped"])
        mock_load.assert_not_called()

    def test_accept_recoverable_with_full_evidence(self):
        from multinexus.agentd.__main__ import main

        mock_config = MagicMock()
        mock_config.id = "test-agent"
        mock_worker = MagicMock()
        mock_worker.run = AsyncMock()
        mock_worker.stop = MagicMock()

        with (
            patch(
                "multinexus.agentd.__main__.load_config", return_value=mock_config
            ) as mock_load,
            patch("multinexus.agentd.__main__.AgentdWorker", return_value=mock_worker),
        ):
            main(
                [
                    "--agent",
                    "test-agent",
                    "--recoverable",
                    "--recovery-reason",
                    "prior-process-crashed",
                    "--prior-process-stopped",
                ]
            )

        mock_load.assert_called_once()
        mock_worker.run.assert_awaited_once_with(
            poll_interval=2.0,
            recoverable=True,
            recovery_reason="prior-process-crashed",
            prior_process_stopped=True,
        )


class CliLogLevelArgumentTests(unittest.TestCase):
    """E) CLI --log-level must be parsed before basicConfig; default INFO."""

    def test_default_log_level_is_info(self):
        from multinexus.agentd.__main__ import main

        mock_config = MagicMock()
        mock_config.id = "test-agent"
        mock_worker = MagicMock()
        mock_worker.run = AsyncMock()
        mock_worker.stop = MagicMock()

        with (
            patch("multinexus.agentd.__main__.load_config", return_value=mock_config),
            patch("multinexus.agentd.__main__.AgentdWorker", return_value=mock_worker),
            patch("multinexus.agentd.__main__.logging.basicConfig") as mock_config_fn,
        ):
            main(["--agent", "test-agent"])

        mock_config_fn.assert_called_once()
        self.assertEqual(mock_config_fn.call_args.kwargs["level"], logging.INFO)

    def test_debug_log_level_accepted(self):
        from multinexus.agentd.__main__ import main

        mock_config = MagicMock()
        mock_config.id = "test-agent"
        mock_worker = MagicMock()
        mock_worker.run = AsyncMock()
        mock_worker.stop = MagicMock()

        with (
            patch("multinexus.agentd.__main__.load_config", return_value=mock_config),
            patch("multinexus.agentd.__main__.AgentdWorker", return_value=mock_worker),
            patch("multinexus.agentd.__main__.logging.basicConfig") as mock_config_fn,
        ):
            main(["--agent", "test-agent", "--log-level", "DEBUG"])

        self.assertEqual(mock_config_fn.call_args.kwargs["level"], logging.DEBUG)

    def test_invalid_log_level_rejected_before_worker_creation(self):
        from multinexus.agentd.__main__ import main

        with patch("multinexus.agentd.__main__.load_config") as mock_load:
            with self.assertRaises(SystemExit):
                main(["--agent", "test-agent", "--log-level", "TRACE"])
        mock_load.assert_not_called()

    def test_launchd_argv_without_log_level_defaults_info(self):
        from multinexus.agentd.__main__ import main

        mock_config = MagicMock()
        mock_config.id = "mac-claude"
        mock_worker = MagicMock()
        mock_worker.run = AsyncMock()
        mock_worker.stop = MagicMock()

        launchd_argv = [
            "/home/example/multinexus/.venv/bin/python",
            "-m",
            "multinexus.agentd",
            "--config",
            "/home/example/multinexus/agents.toml",
            "--agent",
            "mac-claude",
            "--poll-interval",
            "2",
        ]

        with (
            patch("multinexus.agentd.__main__.load_config", return_value=mock_config),
            patch("multinexus.agentd.__main__.AgentdWorker", return_value=mock_worker),
            patch("multinexus.agentd.__main__.logging.basicConfig") as mock_config_fn,
        ):
            main(launchd_argv[3:])

        self.assertEqual(mock_config_fn.call_args.kwargs["level"], logging.INFO)
        mock_worker.run.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
