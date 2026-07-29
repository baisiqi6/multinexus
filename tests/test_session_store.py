import os
import tempfile
import unittest

from multinexus.sessions.store import SessionStore


class TestSessionStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(self.tmpdir, "test_sessions.sqlite3")
        self.store = SessionStore(db_path)

    def test_get_returns_none_when_empty(self):
        result = self.store.get(scope_id="ch1", agent_id="claude")
        self.assertIsNone(result)

    def test_upsert_and_get(self):
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-abc",
            work_dir="/tmp/project",
        )
        result = self.store.get(scope_id="ch1", agent_id="claude")
        self.assertIsNotNone(result)
        self.assertEqual(result["session_id"], "sess-abc")
        self.assertEqual(result["adapter"], "claude")
        self.assertEqual(result["work_dir"], "/tmp/project")
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["turn_count"], 1)

    def test_upsert_increments_turn_count_for_same_session(self):
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-1",
        )
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-1",
        )
        result = self.store.get(scope_id="ch1", agent_id="claude")
        self.assertEqual(result["session_id"], "sess-1")
        self.assertEqual(result["turn_count"], 2)

    def test_upsert_resets_turn_count_for_new_session(self):
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-1",
        )
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-2",
        )
        result = self.store.get(scope_id="ch1", agent_id="claude")
        self.assertEqual(result["session_id"], "sess-2")
        self.assertEqual(result["turn_count"], 1)

    def test_scope_isolation(self):
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-ch1",
        )
        self.store.upsert(
            scope_id="ch2", agent_id="claude",
            adapter="claude", session_id="sess-ch2",
        )
        r1 = self.store.get(scope_id="ch1", agent_id="claude")
        r2 = self.store.get(scope_id="ch2", agent_id="claude")
        self.assertEqual(r1["session_id"], "sess-ch1")
        self.assertEqual(r2["session_id"], "sess-ch2")

    def test_agent_isolation(self):
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-claude",
        )
        self.store.upsert(
            scope_id="ch1", agent_id="codex",
            adapter="codex", session_id="sess-codex",
        )
        r1 = self.store.get(scope_id="ch1", agent_id="claude")
        r2 = self.store.get(scope_id="ch1", agent_id="codex")
        self.assertEqual(r1["session_id"], "sess-claude")
        self.assertEqual(r2["session_id"], "sess-codex")

    def test_mark_stale_then_get_returns_none(self):
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-old",
        )
        self.store.mark_stale(scope_id="ch1", agent_id="claude")
        result = self.store.get(scope_id="ch1", agent_id="claude")
        self.assertIsNone(result)

    def test_upsert_reactivates_stale(self):
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-1",
        )
        self.store.mark_stale(scope_id="ch1", agent_id="claude")
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-2",
        )
        result = self.store.get(scope_id="ch1", agent_id="claude")
        self.assertIsNotNone(result)
        self.assertEqual(result["session_id"], "sess-2")
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["turn_count"], 1)

    def test_upsert_same_session_after_stale_resets_turn_count(self):
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-1",
        )
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-1",
        )
        self.store.mark_stale(scope_id="ch1", agent_id="claude")
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-1",
        )
        result = self.store.get(scope_id="ch1", agent_id="claude")
        self.assertEqual(result["session_id"], "sess-1")
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["turn_count"], 1)

    def test_get_first_active_prefers_canonical_scope(self):
        self.store.upsert(
            scope_id="999", agent_id="claude",
            adapter="claude", session_id="sess-legacy",
        )
        self.store.upsert(
            scope_id="channel:999", agent_id="claude",
            adapter="claude", session_id="sess-canonical",
        )
        result = self.store.get_first_active(
            scope_ids=("channel:999", "999"),
            agent_id="claude",
        )
        self.assertEqual(result["session_id"], "sess-canonical")

    def test_get_first_active_falls_back_to_legacy_scope(self):
        self.store.upsert(
            scope_id="999", agent_id="claude",
            adapter="claude", session_id="sess-legacy",
        )
        result = self.store.get_first_active(
            scope_ids=("channel:999", "999"),
            agent_id="claude",
        )
        self.assertEqual(result["session_id"], "sess-legacy")

    def test_list_by_scope_prefix_can_include_stale(self):
        self.store.upsert(
            scope_id="task:multinexus:phase-a", agent_id="claude",
            adapter="claude", session_id="sess-active",
        )
        self.store.upsert(
            scope_id="task:multinexus:phase-b", agent_id="claude",
            adapter="claude", session_id="sess-stale",
        )
        self.store.mark_stale(
            scope_id="task:multinexus:phase-b", agent_id="claude",
        )

        active = self.store.list_by_scope_prefix(
            scope_prefix="task:multinexus:",
            agent_id="claude",
        )
        all_rows = self.store.list_by_scope_prefix(
            scope_prefix="task:multinexus:",
            agent_id="claude",
            include_stale=True,
        )

        self.assertEqual(len(active), 1)
        self.assertEqual(len(all_rows), 2)

    def test_list_by_scope_prefix_treats_underscore_literally(self):
        self.store.upsert(
            scope_id="task:multinexus:phase_1", agent_id="claude",
            adapter="claude", session_id="sess-underscore",
        )
        self.store.upsert(
            scope_id="task:multinexus:phaseA1", agent_id="claude",
            adapter="claude", session_id="sess-other",
        )

        rows = self.store.list_by_scope_prefix(
            scope_prefix="task:multinexus:phase_",
            agent_id="claude",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], "sess-underscore")

    def test_mark_task_archived_makes_task_session_inactive(self):
        self.store.upsert(
            scope_id="task:multinexus:phase-a", agent_id="claude",
            adapter="claude", session_id="sess-task",
        )

        changed = self.store.mark_task_archived(
            workspace_id="multinexus",
            task_id="phase-a",
            agent_id="claude",
        )

        self.assertEqual(changed, 1)
        self.assertIsNone(
            self.store.get(
                scope_id="task:multinexus:phase-a",
                agent_id="claude",
            )
        )
        rows = self.store.list_task_scope(
            workspace_id="multinexus",
            task_id="phase-a",
            agent_id="claude",
            include_stale=True,
        )
        self.assertEqual(rows[0]["status"], "archived")


if __name__ == "__main__":
    unittest.main()
