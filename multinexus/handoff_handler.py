"""Handoff protocol handler: auto-accept coordinator handoffs at the runtime layer.

Intercepts [handoff] messages from the coordinator bot, executes assignment accept
with fixed parameters, reads the bootstrap file, and injects it into the agent prompt.
Only assignment.accept is automated — mark-done, blocker, merge etc. must go through
normal adapter/LLM flow.
"""

from __future__ import annotations

import logging
import json
import os
import re
import shlex
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CoordinatorHandoff:
    workspace_id: str
    task_id: str
    bootstrap_path: str
    action: str
    # v1 execution context metadata from the handoff body. These are advisory
    # bootstrap-location fields only; adapter cwd/session authority comes from
    # the Coordinate runtime job claim response.
    context_version: int | None = None
    workspace_path: str = ""
    harness_root: str = ""
    branch: str | None = None


@dataclass(frozen=True)
class CoordinatorLifecycleEvent:
    workspace_id: str
    task_id: str
    action: str


_HANDOFF_PREFIX_RE = re.compile(r"\[handoff\]\s*<@!?(\d+)>", re.IGNORECASE)
_LIFECYCLE_PREFIX_RE = re.compile(
    r"\[(?:lifecycle|handoff)\]\s*<@!?(\d+)>",
    re.IGNORECASE,
)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_ALLOWED_ACTIONS = frozenset({"assignment.accept", "review.begin"})
# Host-absolute path forms: POSIX ``/...`` or Windows ``C:\...`` / UNC ``\\...``.
_HOST_ABS_RE = re.compile(r"^([A-Za-z]:[\\/]|\\\\|/)")


def _is_host_absolute_path(path: str) -> bool:
    return bool(_HOST_ABS_RE.match(path))
_LIFECYCLE_ACTIONS = frozenset(
    {"assignment.closeout", "assignment.mark-done", "task.done"}
)
_REPORT_ACTIONS = frozenset({"accept", "blocker", "done", "progress"})
_AGENT_REPORT_LINE_RE = re.compile(
    r"^\s*\[(agent-report|accept|accepted|handoff-received|done|blocker|progress)\](?=\s|$)",
    re.IGNORECASE | re.MULTILINE,
)
_STRICT_AGENT_REPORT_LINE_RE = re.compile(
    r"^\s*\[agent-report\](?=\s|$)",
    re.IGNORECASE,
)
_AGENT_REPORT_FIELD_LINE_RE = re.compile(
    r"^\s*[A-Za-z_][A-Za-z0-9_-]*=",
)


def parse_coordinator_handoff(
    content: str,
    *,
    my_discord_user_id: int,
    diagnostic: bool = False,
) -> CoordinatorHandoff | None:
    """Parse a structured [handoff] message from the coordinator bot.

    Returns None if the message doesn't match the expected format or
    the action isn't in the allowed whitelist.

    When ``diagnostic`` is True, the parser returns a candidate handoff for
    messages that identify this agent with a supported action, even if the
    optional v1 execution context block is malformed. The candidate always has
    ``context_version=None`` when v1 authority is missing or invalid, so the
    managed runtime's existing authority gate emits exactly one blocker. This
    keeps malformed context from becoming authority while preserving a visible
    fail-closed path.
    """
    prefix = _HANDOFF_PREFIX_RE.search(content)
    if prefix is None:
        return None
    if int(prefix.group(1)) != my_discord_user_id:
        return None

    fields = _parse_key_values(content[prefix.end():])
    workspace_id = fields.get("workspace_id")
    task_id = fields.get("task_id")
    action = (fields.get("action") or "").lower()
    bootstrap_path = fields.get("bootstrap") or ""
    if not workspace_id or not task_id or not action:
        return None
    if not _SAFE_ID_RE.match(workspace_id) or not _SAFE_ID_RE.match(task_id):
        return None
    if action not in _ALLOWED_ACTIONS:
        log.info("Handoff action %r not in allowed list, skipping", action)
        return None

    # Parse optional v1 execution context advisory fields.
    context_version_raw = fields.get("context_version")
    workspace_path = fields.get("workspace_path") or ""
    harness_root = fields.get("harness_root") or ""
    branch = fields.get("branch") or None
    context_version: int | None = None
    if context_version_raw is not None:
        try:
            parsed_version = int(context_version_raw)
        except ValueError:
            log.info("Handoff context_version is not an integer, skipping")
            if not diagnostic:
                return None
            parsed_version = None
        if parsed_version is not None and parsed_version != 1:
            log.info("Handoff context_version %r not supported, skipping", parsed_version)
            if not diagnostic:
                return None
            parsed_version = None
        if parsed_version == 1:
            # A v1 block requires both a host-absolute workspace_path and a
            # host-absolute harness_root; partial blocks are rejected.
            if not workspace_path or not _is_host_absolute_path(workspace_path):
                log.info("Handoff v1 block missing or invalid workspace_path, skipping")
                if not diagnostic:
                    return None
                parsed_version = None
            elif not harness_root or not _is_host_absolute_path(harness_root):
                log.info("Handoff v1 block missing or invalid harness_root, skipping")
                if not diagnostic:
                    return None
                parsed_version = None
        if parsed_version is None and diagnostic:
            # Malformed v1 must not leak partial path authority. Clear the
            # advisory fields so the managed authority gate sees a legacy
            # candidate and emits one visible blocker.
            workspace_path = ""
            harness_root = ""
            branch = None
        context_version = parsed_version

    return CoordinatorHandoff(
        workspace_id=workspace_id,
        task_id=task_id,
        bootstrap_path=bootstrap_path or "",
        action=action,
        context_version=context_version,
        workspace_path=workspace_path,
        harness_root=harness_root,
        branch=branch,
    )


def parse_coordinator_lifecycle(
    content: str,
    *,
    my_discord_user_id: int,
) -> CoordinatorLifecycleEvent | None:
    """Parse a coordinator lifecycle notice that closes a local task session.

    This does not execute coordinator lifecycle mutations from Discord text. It only
    lets the runtime archive its own task-scoped CLI session after the coordinator
    has already emitted a closeout/done event.
    """
    prefix = _LIFECYCLE_PREFIX_RE.search(content)
    if prefix is None:
        return None
    if int(prefix.group(1)) != my_discord_user_id:
        return None

    fields = _parse_key_values(content[prefix.end():])
    workspace_id = fields.get("workspace_id")
    task_id = fields.get("task_id")
    action = (fields.get("action") or "").lower()
    if not workspace_id or not task_id or not action:
        return None
    if not _SAFE_ID_RE.match(workspace_id) or not _SAFE_ID_RE.match(task_id):
        return None
    if action not in _LIFECYCLE_ACTIONS:
        return None

    return CoordinatorLifecycleEvent(
        workspace_id=workspace_id,
        task_id=task_id,
        action=action,
    )


def _parse_key_values(text: str) -> dict[str, str]:
    try:
        parts = shlex.split(text)
    except ValueError:
        return {}
    fields: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        normalized = key.strip().lower().replace("-", "_")
        if normalized:
            fields[normalized] = value.strip()
    return fields


def execute_assignment_accept(
    *,
    cli_path: str,
    db_path: str,
    workspace_id: str,
    task_id: str,
    agent_name: str,
) -> tuple[bool, str]:
    """Run coordinator assignment accept with fixed parameters.

    Returns (success, output_or_error).
    """
    session_id = f"auto-{agent_name}-{int(time.time())}"

    cmd = [
        cli_path,
        "assignment", "accept", workspace_id,
        "--task-id", task_id,
        "--owner", agent_name,
        "--session", session_id,
    ]

    if not cli_path:
        return False, "coordinator_cli_path is not configured"
    if not db_path:
        return False, "coordinator_db_path is not configured"

    env = os.environ.copy()
    env["MAC_DB"] = db_path
    coordinator_repo = _infer_coordinator_repo(cli_path)
    if coordinator_repo:
        env["MAC_REPO"] = str(coordinator_repo)

    log.info("Executing: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        output = result.stdout.strip() or result.stderr.strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "assignment accept timed out (30s)"
    except Exception as exc:
        return False, str(exc)


def bootstrap_text_from_accept_output(output: str) -> str | None:
    """Extract bootstrap_text returned by newer coordinate assignment accept."""
    if not output:
        return None
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    bootstrap_text = result.get("bootstrap_text")
    if isinstance(bootstrap_text, str) and bootstrap_text.strip():
        return bootstrap_text
    return None


def _infer_coordinator_repo(cli_path: str) -> Path | None:
    """Find the coordinator repo root that owns the configured CLI wrapper."""
    try:
        path = Path(cli_path).expanduser().resolve()
    except OSError:
        return None
    candidates = [path.parent, *path.parents]
    for candidate in candidates:
        if (
            (candidate / "pyproject.toml").is_file()
            and (
                (candidate / "src" / "coordinate").is_dir()
                or (candidate / "src" / "multi_agent_coordinator").is_dir()
            )
        ):
            return candidate
    return None


def _is_allowed_bootstrap_path(relative_path: Path) -> bool:
    _allowed_names = frozenset({"worker-bootstrap.md", "reviewer-bootstrap.md"})
    parts = relative_path.parts
    if len(parts) >= 5 and parts[:3] == ("docs", "project-harness", "tasks"):
        return relative_path.name in _allowed_names
    if len(parts) >= 4 and parts[:2] == ("docs", "tasks"):
        return relative_path.name in _allowed_names
    return parts in (
        ("docs", "project-harness", "current", "worker-bootstrap.md"),
        ("docs", "project-harness", "current", "reviewer-bootstrap.md"),
    )


def read_bootstrap(workspace_path: str, bootstrap_relative_path: str) -> str | None:
    """Read the bootstrap file from the workspace."""
    if not bootstrap_relative_path:
        return None
    if os.path.isabs(bootstrap_relative_path):
        log.warning("Rejecting absolute bootstrap path: %s", bootstrap_relative_path)
        return None

    try:
        workspace_root = Path(workspace_path).expanduser().resolve()
        full_path = (workspace_root / bootstrap_relative_path).resolve()
        relative_path = full_path.relative_to(workspace_root)
    except (OSError, ValueError) as exc:
        log.warning("Rejecting bootstrap path %s: %s", bootstrap_relative_path, exc)
        return None

    if not _is_allowed_bootstrap_path(relative_path):
        log.warning("Rejecting non-bootstrap path: %s", relative_path)
        return None

    try:
        with open(full_path, "r", encoding="utf-8") as f:
            return f.read()
    except (FileNotFoundError, PermissionError) as exc:
        log.warning("Cannot read bootstrap %s: %s", full_path, exc)
        return None


def resolve_workspace_path(
    *,
    db_path: str,
    workspace_id: str,
    fallback_workspace_path: str,
) -> str:
    """Resolve a coordinator workspace path from the DB, falling back to config."""
    if not db_path or not workspace_id:
        return fallback_workspace_path
    if not Path(db_path).expanduser().exists():
        return fallback_workspace_path
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT path FROM workspaces WHERE id = ?",
                (workspace_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        log.warning("Cannot resolve workspace path for %s: %s", workspace_id, exc)
        return fallback_workspace_path
    if row and row[0]:
        return str(row[0])
    return fallback_workspace_path


def build_agent_report(
    action: str,
    handoff: CoordinatorHandoff,
    *,
    summary: str | None = None,
    reason: str | None = None,
) -> str:
    """Build a structured report that coordinator daemon can ingest."""
    normalized = action.lower()
    if normalized not in _REPORT_ACTIONS:
        raise ValueError(f"unsupported agent report action: {action}")
    fields = [
        "[agent-report]",
        f"action={normalized}",
        f"workspace_id={shlex.quote(handoff.workspace_id)}",
        f"task_id={shlex.quote(handoff.task_id)}",
    ]
    if summary:
        fields.append(f"summary={shlex.quote(summary)}")
    if reason:
        fields.append(f"reason={shlex.quote(reason)}")
    return " ".join(fields)


def contains_agent_report(text: str) -> bool:
    """Return True when text contains a machine-readable agent report line."""
    return bool(_AGENT_REPORT_LINE_RE.search(text or ""))


def contains_execution_agent_report(text: str) -> bool:
    """Return True when text contains a non-accept execution report line.

    Runtime auto-accept emits ``action=accept`` before the adapter runs. That
    report proves the handoff was accepted, but it is not an execution update
    and must not suppress the missing-report fallback after the adapter returns.
    """
    report_lines, _ = split_agent_report_lines(text)
    for report in report_lines:
        fields = _parse_key_values(report)
        action = (fields.get("action") or "").lower()
        if action in {"blocker", "done", "progress"}:
            return True
        if (fields.get("decision") or "").lower() in {"approve", "reject"}:
            return True
    return False


def split_agent_report_lines(text: str) -> tuple[list[str], str]:
    """Extract parseable action or review-decision report blocks.

    Coordinator watches message-create events. Runtime responses may edit a
    placeholder message, so direct-adapter reports must be sent separately as
    new messages to be ingested reliably. Both single-line reports and the
    readable multiline review form are supported.
    """
    report_lines: list[str] = []
    display_lines: list[str] = []
    lines = (text or "").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not _STRICT_AGENT_REPORT_LINE_RE.match(line):
            display_lines.append(line)
            index += 1
            continue

        block = [line.strip()]
        next_index = index + 1
        while (
            next_index < len(lines)
            and _AGENT_REPORT_FIELD_LINE_RE.match(lines[next_index])
        ):
            block.append(lines[next_index].strip())
            next_index += 1

        candidate = "\n".join(block)
        fields = _parse_key_values(candidate)
        action = (fields.get("action") or "").lower()
        decision = (fields.get("decision") or "").lower()
        if action in _REPORT_ACTIONS or decision in {"approve", "reject"}:
            report_lines.append(candidate)
        else:
            display_lines.extend(lines[index:next_index])
        index = next_index
    return report_lines, "\n".join(display_lines).strip()


def build_handoff_prompt(
    handoff: CoordinatorHandoff,
    bootstrap_content: str | None,
    *,
    agent_name: str | None = None,
    accept_output: str | None = None,
) -> str:
    """Build the agent prompt for a coordinator handoff."""
    parts = [
        "[Coordinator Handoff Accepted]\n",
        f"任务: {handoff.task_id}",
        f"Workspace: {handoff.workspace_id}",
    ]

    if agent_name:
        parts.extend(
            [
                "\n任务接收状态:",
                (
                    "- multinexus runtime 已经以 "
                    f"`{agent_name}` 身份完成本任务的 `assignment accept`。"
                ),
                (
                    "- 不要再次运行 `assignment accept`。当前 active lease "
                    "已经由该 agent 持有。"
                ),
                (
                    "- 后续只在需要更新生命周期状态时使用 coordinator CLI，"
                    "例如 branch、PR、CI、blocker、closeout 或 mark-done。"
                ),
            ]
        )
        if accept_output:
            sanitized_output = " ".join(accept_output.split())
            parts.append(f"- 接收结果: {sanitized_output[:500]}")

    parts.extend(
        [
            "\nDiscord 可见协作规则:",
            "- 你是承接任务的 agent；执行进展应由你在群里直接说明，不要只依赖 coordinator 复述事件。",
            "- 开始时先发一句人类可读说明：已接收任务、准备先做哪 2-3 件事。",
            "- 完成一个有意义的小阶段时，发一句进度说明，并在同一条消息最后单独一行附上 `[agent-report] action=progress ...`。",
            "- 如果卡住或需要决策，明确 @Coordinator、@Codex 或可见的 reviewer/operator，并在最后单独一行附上 `[agent-report] action=blocker ...`。",
            "- 认为实现完成时，明确 @Coordinator 和 @Codex 请求 review，说明改动文件、测试结果、剩余风险，并在最后单独一行附上 `[agent-report] action=done ...`。",
            "- `[agent-report]` 行必须从新的一行行首开始；不要把它埋在普通句子中。",
        ]
    )

    if bootstrap_content:
        parts.append("\n你的任务启动说明:\n")
        parts.append(bootstrap_content)
    else:
        parts.append("\n未找到 bootstrap 文件，请联系 operator。")

    return "\n".join(parts)

def build_review_handoff_prompt(
    handoff: CoordinatorHandoff,
    bootstrap_content: str | None,
    *,
    agent_name: str | None = None,
) -> str:
    """Build the reviewer agent prompt for a coordinator review handoff."""
    parts = [
        "[Reviewer Handoff Accepted]\n",
        f"任务: {handoff.task_id}",
        f"Workspace: {handoff.workspace_id}",
    ]

    if agent_name:
        parts.extend(
            [
                "\n任务接收状态:",
                (
                    "- 你以 reviewer 角色接收此 handoff。你 **不持有** task ownership。"
                ),
                (
                    "- 不要运行 `assignment accept`。reviewer 不声索任务所有权。"
                ),
                (
                    "- 你的职责是评审（review），不是实现。不要修改代码、commit 或 push。"
                ),
            ]
        )

    parts.extend(
        [
            "\nReview 协作规则:",
            "- 开始评审前，先在群里发一句说明：已接收 review 请求，准备先检查哪几个方面。",
            "- 完成评审时，在消息中附带一个 parseable block：",
            "  `[agent-report] decision=approve workspace_id=... task_id=... summary=\"...\"`",
            "  或 `[agent-report] decision=reject workspace_id=... task_id=... reason=\"...\"`",
            "- `[agent-report]` 行必须从新的一行行首开始；不要把它埋在普通句子中。",
            "- 如果 approve，明确 @Coordinator 和 @Codex 告知 plan/code 已通过。",
            "- 如果 reject，明确说明原因和需要修改的地方。",
        ]
    )

    if bootstrap_content:
        parts.append("\n你的 review 启动说明:\n")
        parts.append(bootstrap_content)
    else:
        parts.append("\n未找到 bootstrap 文件，请联系 operator。")

    return "\n".join(parts)
