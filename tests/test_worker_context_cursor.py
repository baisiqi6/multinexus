"""Cross-layer tests for the lounge context envelope and session cursor.

These tests deliberately exercise ``AgentdWorker._process_job`` with a small
fake adapter.  The context store builds the same envelope a bridge would put
on a Coordinate job; the worker must either consume its delta or conservatively
fall back to the already-rendered full prompt.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from multinexus.adapters.base import (
    OUTCOME_FAILED,
    AdapterResult,
)
from multinexus.agentd.worker import AgentdWorker
from multinexus.context.prompt import build_agent_prompt_with_context, render_envelope
from multinexus.context.store import ChatContextStore
from multinexus.models import AgentConfig
from multinexus.sessions.scope import workspace_channel_context_scope


class RecordingAdapter:
    """Small deterministic adapter used to observe worker prompt selection."""

    allow_fresh_fallback_after_resume_error = True

    def __init__(self, *, session_id: str = "session-1", resume_result=None):
        self.session_id = session_id
        self.resume_result = resume_result
        self.calls: list[tuple[str, str | None]] = []
        self.resumes: list[tuple[str, str, str | None]] = []

    async def call(self, prompt, *, work_dir=None, on_progress=None, **_kwargs):
        self.calls.append((prompt, work_dir))
        return AdapterResult(text="fresh reply", session_id=self.session_id)

    async def resume(self, session_id, prompt, *, work_dir=None, on_progress=None, **_kwargs):
        self.resumes.append((session_id, prompt, work_dir))
        if self.resume_result is not None:
            return self.resume_result
        return AdapterResult(text="resumed reply", session_id=session_id, resumed=True)

    async def health_check(self):
        return {"adapter": "fake", "available": True}


class WorkerContextCursorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _config(self) -> AgentConfig:
        return AgentConfig(
            id="test-agent",
            token="fake-token",
            adapter="claude",
            display_name="Test Agent",
            context_db_path=str(self.root / "sessions.sqlite3"),
            coordinator_cli_path="/bin/true",
            coordinator_db_path=str(self.root / "coordinate.sqlite3"),
            work_dir=str(self.root / "workspace"),
        )

    def _context(
        self,
        *,
        history_ids: tuple[str, ...] = ("m1", "m2"),
        current_id: str = "m3",
    ) -> tuple[str, dict, dict, ChatContextStore]:
        """Return rendered full prompt, envelope, origin, and context store."""
        context_db = ChatContextStore(str(self.root / "context.sqlite3"))
        context_scope = workspace_channel_context_scope(
            workspace_id="ws", platform="discord", channel_id="channel-1"
        )
        now = int(time.time() * 1000)
        contents = {
            "m1": "old-message-m1",
            "m2": "new-message-m2",
            "m3": "current-message-m3",
        }
        for index, message_id in enumerate((*history_ids, current_id)):
            context_db.record_message(
                message_id=message_id,
                channel_id=context_scope,
                author_id="human-1",
                author_name="Human",
                author_is_bot=False,
                content=contents[message_id],
                created_at_ms=now + index,
                source="test",
                ttl_seconds=3600,
            )

        config = self._config()
        scope_id = context_scope
        full_prompt, envelope = build_agent_prompt_with_context(
            context_store=context_db,
            config=config,
            bot_id=42,
            channel_id=context_scope,
            message_id=current_id,
            current_text=contents[current_id],
            scope_id=scope_id,
            session_scope_id=scope_id,
            recipient={"id": config.id, "name": config.display_name},
        )
        self.assertIsNotNone(envelope)
        origin = {
            "platform": "discord",
            "destination": "channel-1",
            "message_id": current_id,
            "session_scope_id": scope_id,
            "legacy_scope_ids": [],
            "context": envelope,
        }
        return full_prompt, envelope, origin, context_db

    def _claim(self, *, job_id: str, prompt: str, origin: dict, result=None) -> dict:
        work_dir = str(self.root / "workspace")
        session_scope_id = origin.get("session_scope_id", "scope:1")
        ctx = {
            "contract_version": 1,
            "job_id": job_id,
            "workspace_id": "ws",
            "task_id": None,
            "assigned_agent": "test-agent",
            "host_id": "test-host",
            "workspace_path": work_dir,
            "worktree_path": work_dir,
            "harness_root": work_dir + "/harness",
            "branch": None,
            "session_scope_id": session_scope_id,
            "legacy_scope_ids": [],
            "log_handle": {"kind": "coordinate_job", "job_id": job_id, "logs_path": None},
        }
        canonical = json.dumps(ctx, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        ctx["context_id"] = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        job = {
            "id": job_id,
            "workspace_id": "ws",
            "assigned_agent": "test-agent",
            "attempt_count": 1,
            "payload": {"prompt": prompt, "origin": origin},
        }
        if result is not None:
            job["result"] = result
        return {
            "claimed": True,
            "job": job,
            "attempt_token": 1,
            "execution_context": ctx,
        }

    def _worker_with_report(self, adapter: RecordingAdapter):
        worker = AgentdWorker(self._config())
        worker.adapter = adapter
        reports: list[dict] = []

        async def report_job(**kwargs):
            reports.append(kwargs)
            return {"result": {"job": {
                "id": kwargs["job_id"], "assigned_agent": kwargs["agent_id"],
                "status": kwargs["status"], "attempt_count": kwargs["attempt_token"],
                "result": dict(kwargs["result_json"]),
            }}}

        worker.coordinate.report_job = report_job
        return worker, reports

    def _seed_session(self, worker: AgentdWorker, envelope: dict, *, session_id="session-1") -> dict:
        scope_id = envelope["session_scope_id"]
        stored = worker.session_store.upsert(
            scope_id=scope_id,
            agent_id="test-agent",
            adapter="claude",
            session_id=session_id,
            work_dir=str(self.root / "workspace"),
            context_generation=envelope["generation"],
        )
        self.assertIsNotNone(stored)
        return stored

    def _advance_to_first_history(self, worker: AgentdWorker, envelope: dict) -> str:
        first = envelope["messages"][0]
        advanced = worker.session_store.advance_context_cursor(
            scope_id=envelope["session_scope_id"],
            agent_id="test-agent",
            session_id="session-1",
            context_generation=envelope["generation"],
            expected_cursor_order_token=None,
            expected_cursor_message_id=None,
            cursor_order_token=str(first["context_order"]),
            cursor_message_id=first["message_id"],
        )
        self.assertTrue(advanced)
        return first["message_id"]

    def _warm_session(self, worker, envelope):
        """Confirm a real full-history turn in this worker before testing delta."""
        first = envelope["messages"][0]
        prior = {
            **envelope, "messages": [], "order_token": "",
            "current_message_id": first["message_id"],
            "current_order_token": str(first["context_order"]),
            "current_text": first["content"],
        }
        origin = {
            "platform": "discord", "destination": "channel-1",
            "message_id": first["message_id"], "session_scope_id": prior["session_scope_id"],
            "context": prior,
        }
        adapter = worker.adapter
        worker.adapter = RecordingAdapter()
        try:
            asyncio.run(worker._process_job(self._claim(
                job_id="job-warm", prompt=render_envelope(prior, worker.config), origin=origin,
            )))
        finally:
            worker.adapter = adapter

    def test_success_resumes_with_delta_and_advances_cursor(self):
        full_prompt, envelope, origin, _ = self._context()
        adapter = RecordingAdapter()
        worker, reports = self._worker_with_report(adapter)
        self._seed_session(worker, envelope)
        self._warm_session(worker, envelope)
        reports.clear()

        asyncio.run(
            worker._process_job(
                self._claim(job_id="job-delta", prompt=full_prompt, origin=origin)
            )
        )

        self.assertEqual(len(adapter.calls), 0)
        self.assertEqual(len(adapter.resumes), 1)
        resume_prompt = adapter.resumes[0][1]
        self.assertNotIn("old-message-m1", resume_prompt)
        self.assertIn("new-message-m2", resume_prompt)
        self.assertIn("current-message-m3", resume_prompt)
        self.assertEqual(reports[0]["status"], "done")
        cursor = worker.session_store.get_context_cursor(
            scope_id=envelope["session_scope_id"], agent_id="test-agent"
        )
        self.assertEqual(cursor["context_cursor_message_id"], "m3")
        self.assertEqual(
            cursor["context_cursor_order_token"], envelope["current_order_token"]
        )

    def test_missing_cursor_anchor_falls_back_to_full_prompt(self):
        full_prompt, envelope, origin, _ = self._context(history_ids=("m2",))
        adapter = RecordingAdapter()
        worker, reports = self._worker_with_report(adapter)
        self._seed_session(worker, envelope)
        # This cursor belongs to a message evicted from the bounded context.
        self.assertTrue(
            worker.session_store.advance_context_cursor(
                scope_id=envelope["session_scope_id"],
                agent_id="test-agent",
                session_id="session-1",
                context_generation=envelope["generation"],
                expected_cursor_order_token=None,
                expected_cursor_message_id=None,
                cursor_order_token="1",
                cursor_message_id="evicted-m1",
            )
        )

        asyncio.run(
            worker._process_job(
                self._claim(job_id="job-full-fallback", prompt=full_prompt, origin=origin)
            )
        )

        self.assertEqual(len(adapter.calls), 0)
        self.assertEqual(len(adapter.resumes), 1)
        self.assertEqual(adapter.resumes[0][1], full_prompt)
        self.assertIn("new-message-m2", adapter.resumes[0][1])
        self.assertIn("current-message-m3", adapter.resumes[0][1])
        self.assertEqual(reports[0]["status"], "done")
        cursor = worker.session_store.get_context_cursor(
            scope_id=envelope["session_scope_id"], agent_id="test-agent"
        )
        self.assertEqual(cursor["context_cursor_message_id"], "m3")

    def test_session_store_error_discards_optimization_and_uses_full_prompt(self):
        full_prompt, envelope, origin, _ = self._context()
        adapter = RecordingAdapter()
        worker, reports = self._worker_with_report(adapter)
        original_get = worker.session_store.get
        count = 0

        def fail_once(**kwargs):
            nonlocal count
            count += 1
            if count == 1:
                raise sqlite3.OperationalError("temporary session store outage")
            return original_get(**kwargs)

        worker.session_store.get = fail_once
        asyncio.run(
            worker._process_job(
                self._claim(job_id="job-store-fallback", prompt=full_prompt, origin=origin)
            )
        )

        self.assertEqual(len(adapter.resumes), 0)
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(adapter.calls[0][0], full_prompt)
        self.assertEqual(reports[0]["status"], "done")

    def test_failed_result_does_not_advance_cursor(self):
        full_prompt, envelope, origin, _ = self._context()
        failed = AdapterResult(
            text="provider failed",
            session_id="session-1",
            outcome=OUTCOME_FAILED,
            error_category="provider_error",
        )
        adapter = RecordingAdapter(resume_result=failed)
        adapter.allow_fresh_fallback_after_resume_error = False
        worker, reports = self._worker_with_report(adapter)
        self._seed_session(worker, envelope)
        self._advance_to_first_history(worker, envelope)

        asyncio.run(
            worker._process_job(
                self._claim(job_id="job-failed", prompt=full_prompt, origin=origin)
            )
        )

        self.assertEqual(reports[0]["status"], "failed")
        # A failed resume retires the provider session (and clears its
        # checkpoint); importantly, the failed turn never advances it to m3.
        rows = worker.session_store.list_by_agent(
            agent_id="test-agent", include_stale=True
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "stale")
        self.assertIsNone(rows[0]["context_cursor_message_id"])

    def test_late_result_cannot_replace_newer_provider_session(self):
        config = self._config()
        worker = AgentdWorker(config)
        scope_id = "channel:late-result"
        reports: list[dict] = []

        class LateAdapter(RecordingAdapter):
            async def call(self, prompt, *, work_dir=None, on_progress=None, **kwargs):
                worker.session_store.upsert(
                    scope_id=scope_id,
                    agent_id="test-agent",
                    adapter="claude",
                    session_id="newer-session",
                    work_dir=str(self_outer.root / "workspace"),
                )
                self.calls.append((prompt, work_dir))
                return AdapterResult(text="late reply", session_id="old-session")

        self_outer = self
        adapter = LateAdapter()
        worker.adapter = adapter

        async def report_job(**kwargs):
            reports.append(kwargs)
            return {"result": {"job": {
                "id": kwargs["job_id"], "assigned_agent": kwargs["agent_id"],
                "status": kwargs["status"], "attempt_count": kwargs["attempt_token"],
                "result": dict(kwargs["result_json"]),
            }}}

        worker.coordinate.report_job = report_job
        origin = {
            "platform": "discord",
            "destination": "late-result",
            "message_id": "late-m1",
            "session_scope_id": scope_id,
            "legacy_scope_ids": [],
        }
        asyncio.run(
            worker._process_job(
                self._claim(job_id="job-late", prompt="late prompt", origin=origin)
            )
        )

        self.assertEqual(reports[0]["status"], "done")
        stored = worker.session_store.get(scope_id=scope_id, agent_id="test-agent")
        self.assertEqual(stored["session_id"], "newer-session")

    def test_resume_failure_falls_back_to_full_prompt(self):
        full_prompt, envelope, origin, _ = self._context()
        failed = AdapterResult(
            text="Claude error: resume unavailable",
            session_id="session-1",
            outcome=OUTCOME_FAILED,
            error_category="provider_error",
        )
        adapter = RecordingAdapter(session_id="session-2", resume_result=failed)
        worker, reports = self._worker_with_report(adapter)
        self._seed_session(worker, envelope)
        self._warm_session(worker, envelope)
        reports.clear()

        asyncio.run(
            worker._process_job(
                self._claim(job_id="job-resume-fallback", prompt=full_prompt, origin=origin)
            )
        )

        self.assertEqual(len(adapter.resumes), 1)
        self.assertNotIn("old-message-m1", adapter.resumes[0][1])
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(adapter.calls[0][0], full_prompt)
        self.assertEqual(reports[0]["status"], "done")

    def test_recoverable_resume_never_starts_fresh_call(self):
        full_prompt, envelope, origin, _ = self._context()
        failed = AdapterResult(
            text="Claude error: recoverable resume failed",
            session_id="recover-session",
            outcome=OUTCOME_FAILED,
            error_category="provider_error",
        )
        adapter = RecordingAdapter(resume_result=failed)
        worker, reports = self._worker_with_report(adapter)
        claim = self._claim(
            job_id="job-recoverable",
            prompt=full_prompt,
            origin=origin,
            result={"timeout": {"session_id": "recover-session"}},
        )

        asyncio.run(worker._process_job(claim))

        self.assertEqual(adapter.resumes, [("recover-session", full_prompt, str(self.root / "workspace"))])
        self.assertEqual(adapter.calls, [])
        self.assertEqual(reports[0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
