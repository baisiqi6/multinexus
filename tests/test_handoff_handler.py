import tempfile
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from multinexus.handoff_handler import (
    CoordinatorHandoff,
    bootstrap_text_from_accept_output,
    build_agent_report,
    build_handoff_prompt,
    contains_execution_agent_report,
    execute_assignment_accept,
    parse_coordinator_handoff,
    parse_coordinator_lifecycle,
    read_bootstrap,
    resolve_workspace_path,
    split_agent_report_lines,
)


class TestParseCoordinatorHandoff(unittest.TestCase):
    def test_parses_assignment_accept_handoff(self):
        handoff = parse_coordinator_handoff(
            "[handoff] <@123> workspace_id=multinexus "
            "task_id=phase-4-coordinator-integration "
            "bootstrap=docs/project-harness/tasks/phase-4-coordinator-integration/worker-bootstrap.md "
            "action=assignment.accept",
            my_discord_user_id=123,
        )

        self.assertIsNotNone(handoff)
        self.assertEqual(handoff.workspace_id, "multinexus")
        self.assertEqual(handoff.task_id, "phase-4-coordinator-integration")
        self.assertEqual(handoff.action, "assignment.accept")

    def test_execution_report_detector_ignores_accept_only_report(self):
        self.assertFalse(
            contains_execution_agent_report(
                "[agent-report] action=accept workspace_id=demo task_id=t1 "
                "summary='auto accepted'\nRound 3 rework complete."
            )
        )
        self.assertTrue(
            contains_execution_agent_report(
                "Round 3 rework complete.\n"
                "[agent-report] action=done workspace_id=demo task_id=t1 summary='tests OK'"
            )
        )

    def test_parses_nickname_mention_and_unordered_fields(self):
        handoff = parse_coordinator_handoff(
            "[handoff] <@!123> action=assignment.accept "
            "task_id=phase-4 workspace_id=multinexus",
            my_discord_user_id=123,
        )

        self.assertIsNotNone(handoff)
        self.assertEqual(handoff.task_id, "phase-4")

    def test_rejects_wrong_target(self):
        handoff = parse_coordinator_handoff(
            "[handoff] <@456> workspace_id=multinexus "
            "task_id=phase-4 action=assignment.accept",
            my_discord_user_id=123,
        )

        self.assertIsNone(handoff)

    def test_rejects_unsupported_action(self):
        handoff = parse_coordinator_handoff(
            "[handoff] <@123> workspace_id=multinexus "
            "task_id=phase-4 action=assignment.mark-done",
            my_discord_user_id=123,
        )

        self.assertIsNone(handoff)

    def test_rejects_missing_required_fields(self):
        handoff = parse_coordinator_handoff(
            "[handoff] <@123> workspace_id=multinexus action=assignment.accept",
            my_discord_user_id=123,
        )

        self.assertIsNone(handoff)


class TestParseCoordinatorHandoffV1Authority(unittest.TestCase):
    def _v1_handoff(self, **overrides):
        fields = {
            "workspace_id": "multinexus",
            "task_id": "phase-5.1",
            "action": "assignment.accept",
            "context_version": "1",
            "workspace_path": "/fake/workspace",
            "harness_root": "/fake/harness",
            "branch": "main",
        }
        fields.update(overrides)
        parts = " ".join(f"{k}={v}" for k, v in fields.items())
        return parse_coordinator_handoff(
            f"[handoff] <@123> {parts}",
            my_discord_user_id=123,
        )

    def test_accepts_valid_posix_v1_block(self):
        handoff = self._v1_handoff()
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff.context_version, 1)
        self.assertEqual(handoff.workspace_path, "/fake/workspace")
        self.assertEqual(handoff.harness_root, "/fake/harness")

    def test_accepts_valid_quoted_windows_v1_block(self):
        handoff = self._v1_handoff(
            workspace_path="'C:\\Users\\My User\\demo'",
            harness_root="'C:\\Users\\My User\\demo\\harness'",
        )
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff.workspace_path, "C:\\Users\\My User\\demo")
        self.assertEqual(handoff.harness_root, "C:\\Users\\My User\\demo\\harness")

    def test_rejects_v1_missing_workspace_path(self):
        self.assertIsNone(self._v1_handoff(workspace_path=""))

    def test_rejects_v1_missing_harness_root(self):
        self.assertIsNone(self._v1_handoff(harness_root=""))

    def test_rejects_v1_relative_workspace_path(self):
        self.assertIsNone(self._v1_handoff(workspace_path="relative/path"))

    def test_rejects_v1_relative_harness_root(self):
        self.assertIsNone(self._v1_handoff(harness_root="relative/harness"))

    def test_diagnostic_mode_returns_candidate_for_partial_v1(self):
        handoff = parse_coordinator_handoff(
            "[handoff] <@123> workspace_id=multinexus "
            "task_id=phase-5.1 action=assignment.accept "
            "context_version=1 workspace_path=/fake/workspace branch=main",
            my_discord_user_id=123,
            diagnostic=True,
        )
        self.assertIsNotNone(handoff)
        self.assertIsNone(handoff.context_version)
        self.assertEqual(handoff.workspace_path, "")
        self.assertEqual(handoff.harness_root, "")
        self.assertIsNone(handoff.branch)

    def test_diagnostic_mode_returns_candidate_for_legacy_handoff(self):
        handoff = parse_coordinator_handoff(
            "[handoff] <@123> workspace_id=multinexus "
            "task_id=phase-5.1 action=assignment.accept",
            my_discord_user_id=123,
            diagnostic=True,
        )
        self.assertIsNotNone(handoff)
        self.assertIsNone(handoff.context_version)

    def test_diagnostic_mode_rejects_unidentified_handoff(self):
        # Wrong target user must not produce a candidate, even in diagnostic mode.
        handoff = parse_coordinator_handoff(
            "[handoff] <@456> workspace_id=multinexus "
            "task_id=phase-5.1 action=assignment.accept "
            "context_version=1 workspace_path=/fake/workspace",
            my_discord_user_id=123,
            diagnostic=True,
        )
        self.assertIsNone(handoff)


class TestParseCoordinatorLifecycle(unittest.TestCase):
    def test_parses_closeout_lifecycle_notice(self):
        event = parse_coordinator_lifecycle(
            "[lifecycle] <@123> workspace_id=multinexus "
            "task_id=phase-5.2 action=assignment.closeout",
            my_discord_user_id=123,
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.workspace_id, "multinexus")
        self.assertEqual(event.task_id, "phase-5.2")
        self.assertEqual(event.action, "assignment.closeout")

    def test_parses_task_done_lifecycle_notice(self):
        event = parse_coordinator_lifecycle(
            "[lifecycle] <@123> workspace_id=multinexus "
            "task_id=phase-5.2 action=task.done",
            my_discord_user_id=123,
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.action, "task.done")

    def test_rejects_assignment_accept_as_lifecycle(self):
        event = parse_coordinator_lifecycle(
            "[lifecycle] <@123> workspace_id=multinexus "
            "task_id=phase-5.2 action=assignment.accept",
            my_discord_user_id=123,
        )

        self.assertIsNone(event)

    def test_parses_legacy_handoff_lifecycle_notice(self):
        event = parse_coordinator_lifecycle(
            "[handoff] <@123> workspace_id=multinexus "
            "task_id=phase-5.2 action=task.done",
            my_discord_user_id=123,
        )

        self.assertIsNotNone(event)
        self.assertEqual(event.action, "task.done")

    def test_handoff_parser_rejects_lifecycle_prefix(self):
        handoff = parse_coordinator_handoff(
            "[lifecycle] <@123> workspace_id=multinexus "
            "task_id=phase-5.2 action=assignment.accept",
            my_discord_user_id=123,
        )

        self.assertIsNone(handoff)


class TestBootstrapRead(unittest.TestCase):
    def test_extracts_bootstrap_text_from_accept_output(self):
        output = (
            '{"result":{"bootstrap_text":"# Worker Bootstrap\\ncd /home/example/multinexus",'
            '"bootstrap_path":"docs/project-harness/tasks/t1/worker-bootstrap.md"}}'
        )

        self.assertEqual(
            bootstrap_text_from_accept_output(output),
            "# Worker Bootstrap\ncd /home/example/multinexus",
        )

    def test_extract_bootstrap_text_ignores_non_json_accept_output(self):
        self.assertIsNone(bootstrap_text_from_accept_output("accepted"))

    def test_reads_task_scoped_worker_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "docs/project-harness/tasks/phase-4/worker-bootstrap.md"
            path.parent.mkdir(parents=True)
            path.write_text("bootstrap", encoding="utf-8")

            content = read_bootstrap(
                tmp, "docs/project-harness/tasks/phase-4/worker-bootstrap.md"
            )

        self.assertEqual(content, "bootstrap")

    def test_reads_coordinate_task_scoped_worker_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "docs/tasks/phase-4/worker-bootstrap.md"
            path.parent.mkdir(parents=True)
            path.write_text("bootstrap", encoding="utf-8")

            content = read_bootstrap(
                tmp, "docs/tasks/phase-4/worker-bootstrap.md"
            )

        self.assertEqual(content, "bootstrap")

    def test_reads_legacy_current_worker_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "docs/project-harness/current/worker-bootstrap.md"
            path.parent.mkdir(parents=True)
            path.write_text("bootstrap", encoding="utf-8")

            content = read_bootstrap(
                tmp, "docs/project-harness/current/worker-bootstrap.md"
            )

        self.assertEqual(content, "bootstrap")

    def test_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp).parent / "worker-bootstrap.md"
            outside.write_text("secret", encoding="utf-8")
            try:
                with self.assertLogs("multinexus.handoff_handler", level="WARNING"):
                    content = read_bootstrap(tmp, "../worker-bootstrap.md")
            finally:
                outside.unlink(missing_ok=True)

        self.assertIsNone(content)

    def test_rejects_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "docs/project-harness/tasks/phase-4/worker-bootstrap.md"
            path.parent.mkdir(parents=True)
            path.write_text("bootstrap", encoding="utf-8")

            with self.assertLogs("multinexus.handoff_handler", level="WARNING"):
                content = read_bootstrap(tmp, str(path))

        self.assertIsNone(content)

    def test_rejects_non_bootstrap_path_inside_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "docs/project-harness/tasks/phase-4/plan.md"
            path.parent.mkdir(parents=True)
            path.write_text("plan", encoding="utf-8")

            with self.assertLogs("multinexus.handoff_handler", level="WARNING"):
                content = read_bootstrap(tmp, "docs/project-harness/tasks/phase-4/plan.md")

        self.assertIsNone(content)


class TestWorkspacePathResolution(unittest.TestCase):
    def test_resolves_workspace_path_from_coordinate_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "coordinator.sqlite3"
            workspace_path = Path(tmp) / "coordinate"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE workspaces (id TEXT PRIMARY KEY, path TEXT)")
                conn.execute(
                    "INSERT INTO workspaces (id, path) VALUES (?, ?)",
                    ("mac-smoke", str(workspace_path)),
                )

            resolved = resolve_workspace_path(
                db_path=str(db_path),
                workspace_id="mac-smoke",
                fallback_workspace_path="/fallback",
            )

        self.assertEqual(resolved, str(workspace_path))

    def test_workspace_path_resolution_falls_back_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "coordinator.sqlite3"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE workspaces (id TEXT PRIMARY KEY, path TEXT)")

            resolved = resolve_workspace_path(
                db_path=str(db_path),
                workspace_id="missing",
                fallback_workspace_path="/fallback",
            )

        self.assertEqual(resolved, "/fallback")


class TestAssignmentAccept(unittest.TestCase):
    def test_rejects_missing_cli_config(self):
        ok, output = execute_assignment_accept(
            cli_path="",
            db_path="/tmp/coordinator.sqlite3",
            workspace_id="multinexus",
            task_id="phase-4",
            agent_name="mac-codex",
        )

        self.assertFalse(ok)
        self.assertIn("coordinator_cli_path", output)

    def test_runs_fixed_argv_and_infers_repo_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "coordinate"
            (repo / "src/coordinate").mkdir(parents=True)
            (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            cli = repo / "skills/coordinate-operator/generic.sh"
            cli.parent.mkdir(parents=True)
            cli.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

            completed = SimpleNamespace(returncode=0, stdout="accepted\n", stderr="")
            with patch("multinexus.handoff_handler.subprocess.run", return_value=completed) as run:
                ok, output = execute_assignment_accept(
                    cli_path=str(cli),
                    db_path="/tmp/coordinator.sqlite3",
                    workspace_id="multinexus",
                    task_id="phase-4",
                    agent_name="mac-codex",
                )

        self.assertTrue(ok)
        self.assertEqual(output, "accepted")
        argv = run.call_args.args[0]
        self.assertEqual(
            argv[:6],
            [
                str(cli),
                "assignment",
                "accept",
                "multinexus",
                "--task-id",
                "phase-4",
            ],
        )
        self.assertIn("--owner", argv)
        self.assertIn("--session", argv)
        self.assertEqual(run.call_args.kwargs["env"]["MAC_REPO"], str(repo.resolve()))
        self.assertEqual(run.call_args.kwargs["env"]["MAC_DB"], "/tmp/coordinator.sqlite3")


class TestAgentReport(unittest.TestCase):
    def test_builds_structured_accept_report(self):
        handoff = CoordinatorHandoff(
            workspace_id="multinexus",
            task_id="phase-4",
            bootstrap_path="",
            action="assignment.accept",
        )

        report = build_agent_report("accept", handoff, summary="auto accepted by mac-codex")

        self.assertIn("[agent-report]", report)
        self.assertIn("action=accept", report)
        self.assertIn("workspace_id=multinexus", report)
        self.assertIn("task_id=phase-4", report)
        self.assertIn("summary='auto accepted by mac-codex'", report)

    def test_builds_structured_progress_report(self):
        handoff = CoordinatorHandoff(
            workspace_id="multinexus",
            task_id="phase-4",
            bootstrap_path="",
            action="assignment.accept",
        )

        report = build_agent_report(
            "progress",
            handoff,
            summary="launchd scripts done; tests OK",
        )

        self.assertIn("[agent-report]", report)
        self.assertIn("action=progress", report)
        self.assertIn("workspace_id=multinexus", report)
        self.assertIn("task_id=phase-4", report)
        self.assertIn("summary='launchd scripts done; tests OK'", report)

    def test_splits_strict_agent_report_lines_from_display_text(self):
        report_lines, display_text = split_agent_report_lines(
            "Done.\n"
            "[agent-report] action=done workspace_id=multinexus task_id=phase-5.1 summary='ok'\n"
            "See commit abc123."
        )

        self.assertEqual(
            report_lines,
            ["[agent-report] action=done workspace_id=multinexus task_id=phase-5.1 summary='ok'"],
        )
        self.assertEqual(display_text, "Done.\nSee commit abc123.")

    def test_splits_review_decision_report_from_display_text(self):
        report_lines, display_text = split_agent_report_lines(
            "Review approved.\n"
            "[agent-report] decision=approve workspace_id=multinexus "
            "task_id=phase-5.1 summary='looks good'"
        )

        self.assertEqual(
            report_lines,
            [
                "[agent-report] decision=approve workspace_id=multinexus "
                "task_id=phase-5.1 summary='looks good'"
            ],
        )
        self.assertEqual(display_text, "Review approved.")

    def test_splits_multiline_review_report_block(self):
        report_lines, display_text = split_agent_report_lines(
            "Review rejected.\n"
            "[agent-report]\n"
            "decision=reject\n"
            "workspace_id=multinexus\n"
            "task_id=phase-5.1\n"
            "reason='missing tests'\n"
            "Please revise."
        )

        self.assertEqual(
            report_lines,
            [
                "[agent-report]\n"
                "decision=reject\n"
                "workspace_id=multinexus\n"
                "task_id=phase-5.1\n"
                "reason='missing tests'"
            ],
        )
        self.assertEqual(display_text, "Review rejected.\nPlease revise.")

    def test_split_ignores_non_strict_report_examples(self):
        report_lines, display_text = split_agent_report_lines(
            "Example:\n"
            "[progress] this is not the machine-readable report format"
        )

        self.assertEqual(report_lines, [])
        self.assertIn("[progress]", display_text)

    def test_builds_handoff_prompt_with_bootstrap(self):
        handoff = CoordinatorHandoff(
            workspace_id="multinexus",
            task_id="phase-4",
            bootstrap_path="",
            action="assignment.accept",
        )

        prompt = build_handoff_prompt(handoff, "Step 1")

        self.assertIn("任务: phase-4", prompt)
        self.assertIn("Workspace: multinexus", prompt)
        self.assertIn("Step 1", prompt)

    def test_handoff_prompt_prevents_duplicate_accept(self):
        handoff = CoordinatorHandoff(
            workspace_id="multinexus",
            task_id="phase-4",
            bootstrap_path="",
            action="assignment.accept",
        )

        prompt = build_handoff_prompt(
            handoff,
            "Step 1",
            agent_name="mac-codex",
            accept_output="accepted",
        )

        self.assertIn("完成本任务的 `assignment accept`", prompt)
        self.assertIn("不要再次运行 `assignment accept`", prompt)
        self.assertIn("mac-codex", prompt)
        self.assertIn("接收结果: accepted", prompt)

    def test_handoff_prompt_requires_visible_worker_updates(self):
        handoff = CoordinatorHandoff(
            workspace_id="multinexus",
            task_id="phase-5.2",
            bootstrap_path="",
            action="assignment.accept",
        )

        prompt = build_handoff_prompt(
            handoff,
            "Step 1",
            agent_name="mac-codex",
        )

        self.assertIn("Discord 可见协作规则", prompt)
        self.assertIn("@Coordinator", prompt)
        self.assertIn("@Codex", prompt)
        self.assertIn("[agent-report] action=progress", prompt)
        self.assertIn("[agent-report] action=blocker", prompt)
        self.assertIn("[agent-report] action=done", prompt)
        self.assertIn("单独一行", prompt)


if __name__ == "__main__":
    unittest.main()
