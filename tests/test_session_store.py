import os
import sqlite3
import tempfile
import threading
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
        self.assertIsNone(result["context_generation"])
        self.assertIsNone(result["context_cursor_order_token"])
        self.assertIsNone(result["context_cursor_message_id"])

    def test_existing_database_is_migrated_with_nullable_cursor_columns(self):
        db_path = os.path.join(self.tmpdir, "legacy.sqlite3")
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE sessions (
                    scope_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    adapter TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    work_dir TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    turn_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (scope_id, agent_id)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO sessions
                    (scope_id, agent_id, adapter, session_id,
                     status, turn_count, created_at, updated_at)
                VALUES ('legacy', 'claude', 'claude', 'sess-legacy',
                        'active', 1, 1, 1)
                """
            )

        migrated = SessionStore(db_path)
        result = migrated.get(scope_id="legacy", agent_id="claude")
        self.assertEqual(result["session_id"], "sess-legacy")
        self.assertIsNone(result["context_generation"])
        self.assertIsNone(result["context_cursor_order_token"])
        self.assertIsNone(result["context_cursor_message_id"])
        with sqlite3.connect(db_path) as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(sessions)")
            }
        self.assertTrue(
            {
                "context_generation",
                "context_cursor_order_token",
                "context_cursor_message_id",
            }.issubset(columns)
        )
        with sqlite3.connect(db_path) as conn:
            column_types = {
                row[1]: row[2]
                for row in conn.execute("PRAGMA table_info(sessions)")
            }
        self.assertEqual(column_types["context_generation"], "TEXT")

    def test_concurrent_initialization_serializes_schema_migration(self):
        db_path = os.path.join(self.tmpdir, "concurrent.sqlite3")
        barrier = threading.Barrier(6)
        errors = []

        def initialize():
            try:
                barrier.wait(timeout=5)
                SessionStore(db_path)
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=initialize) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(errors, errors)
        store = SessionStore(db_path)
        store.upsert(
            scope_id="concurrent", agent_id="claude",
            adapter="claude", session_id="sess-1", context_generation="gen-1",
        )
        self.assertEqual(
            store.get(scope_id="concurrent", agent_id="claude")["session_id"],
            "sess-1",
        )

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

    def test_context_cursor_can_be_read_and_advanced_once(self):
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-1", context_generation="gen-3",
        )

        before = self.store.get_context_cursor(scope_id="ch1", agent_id="claude")
        self.assertEqual(before["context_generation"], "gen-3")
        self.assertIsNone(before["context_cursor_order_token"])
        advanced = self.store.advance_context_cursor(
            scope_id="ch1", agent_id="claude", session_id="sess-1",
            context_generation="gen-3",
            expected_cursor_order_token=None,
            expected_cursor_message_id=None,
            cursor_order_token="1",
            cursor_message_id="msg-1",
        )
        self.assertTrue(advanced)
        self.assertEqual(
            self.store.get_context_cursor(
                scope_id="ch1", agent_id="claude"
            )["context_cursor_message_id"],
            "msg-1",
        )

    def test_context_cursor_compare_and_set_rejects_stale_or_backward_updates(self):
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-1", context_generation="gen-1",
        )
        self.assertTrue(
            self.store.advance_context_cursor(
                scope_id="ch1", agent_id="claude", session_id="sess-1",
                context_generation="gen-1",
                expected_cursor_order_token=None,
                expected_cursor_message_id=None,
                cursor_order_token="9", cursor_message_id="msg-9",
            )
        )
        common = dict(
            scope_id="ch1", agent_id="claude", session_id="sess-1",
            context_generation="gen-1",
        )
        self.assertTrue(
            self.store.advance_context_cursor(
                **common,
                expected_cursor_order_token="9",
                expected_cursor_message_id="msg-9",
                cursor_order_token="10", cursor_message_id="msg-10",
            )
        )
        self.assertFalse(
            self.store.advance_context_cursor(
                **common,
                expected_cursor_order_token="9",
                expected_cursor_message_id="msg-9",
                cursor_order_token="8", cursor_message_id="msg-8",
            )
        )
        self.assertTrue(
            self.store.advance_context_cursor(
                **common,
                expected_cursor_order_token="10",
                expected_cursor_message_id="msg-10",
                cursor_order_token="11", cursor_message_id="msg-11",
            )
        )
        self.assertEqual(
            self.store.get_context_cursor(
                scope_id="ch1", agent_id="claude"
            )["context_cursor_order_token"],
            "11",
        )

    def test_context_cursor_rejects_invalid_tokens_and_old_session(self):
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-1", context_generation="gen-1",
        )
        with self.assertRaises(ValueError):
            self.store.advance_context_cursor(
                scope_id="ch1", agent_id="claude", session_id="sess-1",
                context_generation="gen-1",
                expected_cursor_order_token=None,
                expected_cursor_message_id=None,
                cursor_order_token="0", cursor_message_id="msg-0",
            )
        with self.assertRaises(ValueError):
            self.store.advance_context_cursor(
                scope_id="ch1", agent_id="claude", session_id="sess-1",
                context_generation="gen-1",
                expected_cursor_order_token="not-a-rowid",
                expected_cursor_message_id=None,
                cursor_order_token="1", cursor_message_id="msg-1",
            )
        self.assertFalse(
            self.store.advance_context_cursor(
                scope_id="ch1", agent_id="claude", session_id="old-session",
                context_generation="gen-1",
                expected_cursor_order_token=None,
                expected_cursor_message_id=None,
                cursor_order_token="1", cursor_message_id="msg-1",
            )
        )

    def test_upsert_new_session_and_status_changes_clear_cursor(self):
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-1", context_generation="gen-1",
        )
        self.assertTrue(
            self.store.advance_context_cursor(
                scope_id="ch1", agent_id="claude", session_id="sess-1",
                context_generation="gen-1",
                expected_cursor_order_token=None,
                expected_cursor_message_id=None,
                cursor_order_token="1", cursor_message_id="msg-1",
            )
        )
        # Progress/session upserts for the same provider session retain its
        # checkpoint; replacing the provider session starts from a full sync.
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-1", context_generation="gen-1",
        )
        self.assertEqual(
            self.store.get_context_cursor(
                scope_id="ch1", agent_id="claude"
            )["context_cursor_message_id"],
            "msg-1",
        )
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-2", context_generation="gen-2",
        )
        cursor = self.store.get_context_cursor(scope_id="ch1", agent_id="claude")
        self.assertEqual(cursor["session_id"], "sess-2")
        self.assertEqual(cursor["context_generation"], "gen-2")
        self.assertIsNone(cursor["context_cursor_order_token"])
        self.store.advance_context_cursor(
            scope_id="ch1", agent_id="claude", session_id="sess-2",
            context_generation="gen-2",
            expected_cursor_order_token=None,
            expected_cursor_message_id=None,
            cursor_order_token="4", cursor_message_id="msg-4",
        )
        self.store.mark_stale(scope_id="ch1", agent_id="claude")
        self.assertIsNone(
            self.store.get_context_cursor(scope_id="ch1", agent_id="claude")
        )
        rows = self.store.list_by_agent(agent_id="claude", include_stale=True)
        self.assertIsNone(rows[0]["context_cursor_order_token"])

    def test_upsert_work_dir_or_adapter_change_clears_cursor(self):
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-1", work_dir="/one",
            context_generation="gen-1",
        )
        self.assertTrue(
            self.store.advance_context_cursor(
                scope_id="ch1", agent_id="claude", session_id="sess-1",
                context_generation="gen-1",
                expected_cursor_order_token=None,
                expected_cursor_message_id=None,
                cursor_order_token="1", cursor_message_id="msg-1",
            )
        )
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-1", work_dir="/two",
            context_generation="gen-1",
        )
        cursor = self.store.get_context_cursor(scope_id="ch1", agent_id="claude")
        self.assertEqual(cursor["context_cursor_order_token"], None)
        self.assertEqual(cursor["context_cursor_message_id"], None)
        self.store.advance_context_cursor(
            scope_id="ch1", agent_id="claude", session_id="sess-1",
            context_generation="gen-1",
            expected_cursor_order_token=None,
            expected_cursor_message_id=None,
            cursor_order_token="2", cursor_message_id="msg-2",
        )
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="other-adapter", session_id="sess-1", work_dir="/two",
            context_generation="gen-1",
        )
        cursor = self.store.get_context_cursor(scope_id="ch1", agent_id="claude")
        self.assertIsNone(cursor["context_cursor_order_token"])
        self.assertIsNone(cursor["context_cursor_message_id"])

    def test_guarded_upsert_rejects_stale_session_replacement(self):
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="old-session", work_dir="/one",
            context_generation="gen-1",
        )
        old_snapshot = self.store.get(scope_id="ch1", agent_id="claude")
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="new-session", work_dir="/one",
            context_generation="gen-2",
        )

        result = self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="old-session", work_dir="/one",
            context_generation="gen-1",
            expected_session=old_snapshot,
        )

        self.assertIsNone(result)
        current = self.store.get(scope_id="ch1", agent_id="claude")
        self.assertEqual(current["session_id"], "new-session")
        self.assertEqual(current["context_generation"], "gen-2")

    def test_guarded_upsert_reactivates_only_the_observed_stale_row(self):
        self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="old-session", work_dir="/one",
            context_generation="gen-1",
        )
        self.store.mark_stale(scope_id="ch1", agent_id="claude")

        observed = self.store.get(scope_id="ch1", agent_id="claude", include_inactive=True)
        rejected = self.store.upsert(
            scope_id="ch1", agent_id="claude", adapter="claude", session_id="new-session",
            work_dir="/one", context_generation="gen-2", expected_session=None,
        )
        self.assertIsNone(rejected)
        result = self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="new-session", work_dir="/one",
            context_generation="gen-2", expected_session=observed,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["session_id"], "new-session")

    def test_guarded_progress_and_final_upserts_share_session_snapshot(self):
        initial = self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-1", work_dir="/one",
            context_generation="gen-1",
        )
        progress = self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-1", work_dir="/one",
            context_generation="gen-1", expected_session=initial,
        )
        final = self.store.upsert(
            scope_id="ch1", agent_id="claude",
            adapter="claude", session_id="sess-1", work_dir="/one",
            context_generation="gen-1", expected_session=progress,
        )

        self.assertIsNotNone(progress)
        self.assertIsNotNone(final)
        self.assertEqual(final["session_id"], "sess-1")
        self.assertEqual(final["turn_count"], 3)

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
