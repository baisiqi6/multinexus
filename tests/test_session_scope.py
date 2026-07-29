import unittest

from multinexus.sessions.scope import (
    channel_scope,
    describe_scope,
    legacy_scope_for_channel_id,
    scope_for_channel_id,
    task_scope,
    thread_scope,
    workspace_channel_context_scope,
    workspace_channel_session_scope,
    workspace_discord_thread_scope,
)


class TestSessionScopeHelpers(unittest.TestCase):
    def test_channel_scope_key(self):
        self.assertEqual(channel_scope(123), "channel:123")
        self.assertEqual(scope_for_channel_id(123), "channel:123")

    def test_thread_scope_key(self):
        self.assertEqual(thread_scope(456), "thread:456")
        self.assertEqual(scope_for_channel_id(456, is_thread=True), "thread:456")

    def test_task_scope_key(self):
        self.assertEqual(
            task_scope("multinexus", "phase-5.2"),
            "task:multinexus:phase-5.2",
        )

    def test_legacy_scope_key(self):
        self.assertEqual(legacy_scope_for_channel_id(123), "123")

    def test_describes_scope_type(self):
        self.assertEqual(describe_scope("channel:123").label, "channel scope")
        self.assertEqual(describe_scope("thread:456").label, "thread scope")
        self.assertEqual(
            describe_scope("task:multinexus:phase-5.2").label,
            "task scope",
        )
        self.assertEqual(describe_scope("123").label, "legacy channel scope")


class TestWorkspaceQualifiedScopeHelpers(unittest.TestCase):
    def test_workspace_channel_context_scope(self):
        self.assertEqual(
            workspace_channel_context_scope("multinexus", "discord", 500),
            "workspace:multinexus:discord:channel:500",
        )

    def test_workspace_channel_session_scope(self):
        self.assertEqual(
            workspace_channel_session_scope("multinexus", "kook", "900"),
            "workspace:multinexus:kook:channel:900",
        )

    def test_workspace_discord_thread_scope(self):
        self.assertEqual(
            workspace_discord_thread_scope("multinexus", 1001),
            "workspace:multinexus:discord:thread:1001",
        )

    def test_workspace_scope_normalizes_platform_case(self):
        self.assertEqual(
            workspace_channel_context_scope("w", "DISCORD", 1),
            "workspace:w:discord:channel:1",
        )

    def test_workspace_scope_describe(self):
        description = describe_scope(
            "workspace:multinexus:discord:channel:500"
        )
        self.assertEqual(description.kind, "workspace")
        self.assertEqual(description.label, "workspace-qualified scope")
        self.assertEqual(description.detail, "multinexus:discord:channel:500")

    def test_workspace_scope_rejects_empty_workspace(self):
        with self.assertRaises(ValueError):
            workspace_channel_context_scope("", "discord", 1)

    def test_workspace_scope_rejects_unsafe_characters(self):
        with self.assertRaises(ValueError):
            workspace_channel_context_scope("a", "discord", "1;2")

    def test_workspace_scope_rejects_too_long(self):
        long_id = "x" * 300
        with self.assertRaises(ValueError):
            workspace_channel_context_scope("a", "discord", long_id)

    def test_workspace_scope_rejects_assembled_over_limit(self):
        """Each component ≤ 256 but assembled final scope exceeds 256 must fail."""
        # workspace:100chars:discord:channel:100chars = ~129 chars, OK
        # workspace:150chars:discord:channel:150chars = ~139 chars (too long for component)
        # Use components that each fit but the assembly exceeds MAX_SCOPE_LEN.
        long_workspace = "w" * 200   # 200 chars, under 256
        long_channel = "c" * 200     # 200 chars, under 256
        # assembled: workspace:200chars:discord:channel:200chars = 200+1+200+1+8+1+8+1+200 = > 256
        with self.assertRaises(ValueError):
            workspace_channel_context_scope(long_workspace, "discord", long_channel)


if __name__ == "__main__":
    unittest.main()
