import sqlite3
import time
import re
from pathlib import Path

from .scope import task_scope


_ACTIVE_STATUS = "active"
_STALE_STATUS = "stale"
_ARCHIVED_STATUS = "archived"
_VALID_STATUSES = frozenset({_ACTIVE_STATUS, _STALE_STATUS, _ARCHIVED_STATUS})
_POSITIVE_DECIMAL_RE = re.compile(r"^[1-9][0-9]*$")
_UNSET = object()
_SESSION_IDENTITY_FIELDS = (
    "status",
    "created_at",
    "updated_at",
    "turn_count",
    "session_id",
    "adapter",
    "work_dir",
    "context_generation",
    "context_cursor_order_token",
    "context_cursor_message_id",
)


def _row_to_dict(row: tuple) -> dict:
    return {
        "scope_id": row[0],
        "agent_id": row[1],
        "adapter": row[2],
        "session_id": row[3],
        "work_dir": row[4],
        "status": row[5],
        "turn_count": row[6],
        "created_at": row[7],
        "updated_at": row[8],
        "context_generation": row[9],
        "context_cursor_order_token": row[10],
        "context_cursor_message_id": row[11],
    }


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_order_token(token: str | None) -> int | None:
    """Parse the canonical SQLite rowid cursor token."""
    if not isinstance(token, str) or not _POSITIVE_DECIMAL_RE.fullmatch(token):
        return None
    return int(token)


def _session_matches_expected(
    session: dict | None, expected_session: object
) -> bool:
    if expected_session is _UNSET:
        return True
    if expected_session is None:
        # Absence is an exact observation, not permission to reactivate a
        # row another attempt retired after the caller started.
        return session is None
    if not isinstance(expected_session, dict) or session is None:
        return False
    return all(
        field in expected_session and expected_session[field] == session[field]
        for field in _SESSION_IDENTITY_FIELDS
    )


class SessionStore:
    """Persist CLI session IDs scoped to channel/thread/task + agent."""

    def __init__(self, db_path: str):
        self.path = Path(db_path).expanduser()
        if not self.path.is_absolute():
            self.path = Path.cwd() / self.path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_db(self) -> None:
        for attempt in range(8):
            try:
                with self._connect() as conn:
                    conn.execute("PRAGMA journal_mode=WAL")
                    # Multiple agentd processes can initialize the same DB at once.
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS sessions (
                            scope_id TEXT NOT NULL,
                            agent_id TEXT NOT NULL,
                            adapter TEXT NOT NULL,
                            session_id TEXT NOT NULL,
                            work_dir TEXT,
                            status TEXT NOT NULL DEFAULT 'active',
                            turn_count INTEGER NOT NULL DEFAULT 0,
                            created_at REAL NOT NULL,
                            updated_at REAL NOT NULL,
                            context_generation TEXT,
                            context_cursor_order_token TEXT,
                            context_cursor_message_id TEXT,
                            PRIMARY KEY (scope_id, agent_id)
                        )
                        """
                    )
                    columns = {
                        row[1]
                        for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
                    }
                    for name, column_type in (
                        ("context_generation", "TEXT"),
                        ("context_cursor_order_token", "TEXT"),
                        ("context_cursor_message_id", "TEXT"),
                    ):
                        if name not in columns:
                            conn.execute(
                                f"ALTER TABLE sessions ADD COLUMN {name} {column_type}"
                            )
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 7:
                    raise
                time.sleep(0.2 * (attempt + 1))

    def get(
        self, *, scope_id: str, agent_id: str, include_inactive: bool = False
    ) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT scope_id, agent_id, adapter, session_id, work_dir,
                       status, turn_count, created_at, updated_at,
                       context_generation, context_cursor_order_token,
                       context_cursor_message_id
                FROM sessions
                WHERE scope_id = ? AND agent_id = ? AND (? OR status = 'active')
                """,
                (scope_id, agent_id, include_inactive),
            ).fetchone()
        if not row:
            return None
        return _row_to_dict(row)

    def get_first_active(
        self, *, scope_ids: list[str] | tuple[str, ...], agent_id: str
    ) -> dict | None:
        for scope_id in scope_ids:
            session = self.get(scope_id=scope_id, agent_id=agent_id)
            if session:
                return session
        return None

    def list_by_agent(
        self, *, agent_id: str, include_stale: bool = False
    ) -> list[dict]:
        sql = (
            "SELECT scope_id, agent_id, adapter, session_id, work_dir, "
            "status, turn_count, created_at, updated_at, "
            "context_generation, context_cursor_order_token, "
            "context_cursor_message_id "
            "FROM sessions WHERE agent_id = ?"
        )
        params: list[str] = [agent_id]
        if not include_stale:
            sql += " AND status = 'active'"
        sql += " ORDER BY updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_by_scope_prefix(
        self,
        *,
        scope_prefix: str,
        agent_id: str | None = None,
        include_stale: bool = False,
    ) -> list[dict]:
        sql = (
            "SELECT scope_id, agent_id, adapter, session_id, work_dir, "
            "status, turn_count, created_at, updated_at, "
            "context_generation, context_cursor_order_token, "
            "context_cursor_message_id "
            "FROM sessions WHERE scope_id LIKE ? ESCAPE '\\'"
        )
        params: list[str] = [f"{_escape_like(scope_prefix)}%"]
        if agent_id is not None:
            sql += " AND agent_id = ?"
            params.append(agent_id)
        if not include_stale:
            sql += " AND status = 'active'"
        sql += " ORDER BY updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_task_scope(
        self,
        *,
        workspace_id: str,
        task_id: str,
        agent_id: str | None = None,
        include_stale: bool = False,
    ) -> list[dict]:
        return self.list_by_scope_prefix(
            scope_prefix=task_scope(workspace_id, task_id),
            agent_id=agent_id,
            include_stale=include_stale,
        )

    def upsert(
        self,
        *,
        scope_id: str,
        agent_id: str,
        adapter: str,
        session_id: str,
        work_dir: str | None = None,
        context_generation: str | None = None,
        expected_session: dict | None | object = _UNSET,
    ) -> dict | None:
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_row = conn.execute(
                """
                SELECT scope_id, agent_id, adapter, session_id, work_dir,
                       status, turn_count, created_at, updated_at,
                       context_generation, context_cursor_order_token,
                       context_cursor_message_id
                FROM sessions
                WHERE scope_id = ? AND agent_id = ?
                """,
                (scope_id, agent_id),
            ).fetchone()
            current = _row_to_dict(current_row) if current_row else None
            if not _session_matches_expected(current, expected_session):
                return None
            conn.execute(
                """
                INSERT INTO sessions
                    (scope_id, agent_id, adapter, session_id, work_dir,
                     status, turn_count, created_at, updated_at,
                     context_generation, context_cursor_order_token,
                     context_cursor_message_id)
                VALUES (?, ?, ?, ?, ?, 'active', 1, ?, ?, ?, NULL, NULL)
                ON CONFLICT(scope_id, agent_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    adapter = excluded.adapter,
                    work_dir = excluded.work_dir,
                    turn_count = CASE
                        WHEN sessions.status = 'active'
                         AND sessions.session_id = excluded.session_id
                        THEN sessions.turn_count + 1
                        ELSE 1
                    END,
                    updated_at = excluded.updated_at,
                    status = 'active',
                    context_generation = CASE
                        WHEN sessions.status = 'active'
                         AND sessions.session_id = excluded.session_id
                         AND sessions.adapter IS excluded.adapter
                         AND sessions.work_dir IS excluded.work_dir
                         AND excluded.context_generation IS NULL
                        THEN sessions.context_generation
                        ELSE excluded.context_generation
                    END,
                    context_cursor_order_token = CASE
                        WHEN sessions.status = 'active'
                         AND sessions.session_id = excluded.session_id
                         AND sessions.adapter IS excluded.adapter
                         AND sessions.work_dir IS excluded.work_dir
                         AND (
                             excluded.context_generation IS NULL
                             OR sessions.context_generation IS excluded.context_generation
                         )
                        THEN sessions.context_cursor_order_token
                        ELSE NULL
                    END,
                    context_cursor_message_id = CASE
                        WHEN sessions.status = 'active'
                         AND sessions.session_id = excluded.session_id
                         AND sessions.adapter IS excluded.adapter
                         AND sessions.work_dir IS excluded.work_dir
                         AND (
                             excluded.context_generation IS NULL
                             OR sessions.context_generation IS excluded.context_generation
                         )
                        THEN sessions.context_cursor_message_id
                        ELSE NULL
                    END
                """,
                (
                    scope_id,
                    agent_id,
                    adapter,
                    session_id,
                    work_dir,
                    now,
                    now,
                    context_generation,
                ),
            )
            updated_row = conn.execute(
                """
                SELECT scope_id, agent_id, adapter, session_id, work_dir,
                       status, turn_count, created_at, updated_at,
                       context_generation, context_cursor_order_token,
                       context_cursor_message_id
                FROM sessions
                WHERE scope_id = ? AND agent_id = ?
                """,
                (scope_id, agent_id),
            ).fetchone()
            return _row_to_dict(updated_row) if updated_row else None

    def get_context_cursor(
        self, *, scope_id: str, agent_id: str
    ) -> dict | None:
        """Return the active session's persisted context checkpoint."""
        session = self.get(scope_id=scope_id, agent_id=agent_id)
        if session is None:
            return None
        return {
            "scope_id": session["scope_id"],
            "agent_id": session["agent_id"],
            "session_id": session["session_id"],
            "context_generation": session["context_generation"],
            "context_cursor_order_token": session["context_cursor_order_token"],
            "context_cursor_message_id": session["context_cursor_message_id"],
        }

    def advance_context_cursor(
        self,
        *,
        scope_id: str,
        agent_id: str,
        session_id: str,
        context_generation: str,
        expected_cursor_order_token: str | None,
        expected_cursor_message_id: str | None,
        cursor_order_token: str,
        cursor_message_id: str,
        expected_session: dict | object = _UNSET,
    ) -> bool:
        """Advance a session cursor iff its identity and prior cursor match.

        The update is deliberately a single SQL compare-and-set.  A stale or
        concurrent result therefore cannot overwrite a newer checkpoint or
        move it backwards.
        """
        if _parse_order_token(cursor_order_token) is None:
            raise ValueError(
                "context cursor order token must be a positive decimal string"
            )
        if (
            cursor_message_id is None
            or not isinstance(cursor_message_id, str)
            or not cursor_message_id
        ):
            raise ValueError("new context cursor must include message id")
        if (
            expected_cursor_order_token is not None
            and _parse_order_token(expected_cursor_order_token) is None
        ):
            raise ValueError(
                "expected context cursor order token must be a positive decimal string"
            )
        with self._connect() as conn:
            # An immediate transaction makes the read/compare/update sequence
            # one serialized CAS while order tokens are compared as integers.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT scope_id, agent_id, adapter, session_id, work_dir,
                       status, turn_count, created_at, updated_at,
                       context_generation, context_cursor_order_token,
                       context_cursor_message_id
                FROM sessions
                WHERE scope_id = ? AND agent_id = ?
                """,
                (scope_id, agent_id),
            ).fetchone()
            current = _row_to_dict(row) if row else None
            if not _session_matches_expected(current, expected_session):
                return False
            if not current or current["session_id"] != session_id or current["status"] != _ACTIVE_STATUS:
                return False
            if current["context_generation"] != context_generation:
                return False
            if (
                current["context_cursor_order_token"] != expected_cursor_order_token
                or current["context_cursor_message_id"] != expected_cursor_message_id
            ):
                return False
            current_order_token = _parse_order_token(current["context_cursor_order_token"])
            if current["context_cursor_order_token"] is not None and current_order_token is None:
                return False
            if current_order_token is not None and current_order_token >= int(
                cursor_order_token
            ):
                return False
            cursor = conn.execute(
                """
                UPDATE sessions
                SET context_cursor_order_token = ?,
                    context_cursor_message_id = ?,
                    updated_at = ?
                WHERE scope_id = ? AND agent_id = ?
                  AND session_id = ? AND status = 'active'
                  AND context_generation IS ?
                  AND context_cursor_order_token IS ?
                  AND context_cursor_message_id IS ?
                """,
                (
                    cursor_order_token,
                    cursor_message_id,
                    time.time(),
                    scope_id,
                    agent_id,
                    session_id,
                    context_generation,
                    expected_cursor_order_token,
                    expected_cursor_message_id,
                ),
            )
            return cursor.rowcount == 1

    def mark_stale(
        self, *, scope_id: str, agent_id: str, expected_session: dict | object = _UNSET
    ) -> dict | None:
        return self._mark_status(
            scope_id=scope_id, agent_id=agent_id, status=_STALE_STATUS,
            expected_session=expected_session,
        )

    def mark_archived(self, *, scope_id: str, agent_id: str) -> None:
        self._mark_status(scope_id=scope_id, agent_id=agent_id, status=_ARCHIVED_STATUS)

    def mark_task_stale(
        self, *, workspace_id: str, task_id: str, agent_id: str | None = None
    ) -> int:
        return self._mark_scope_status(
            scope_id=task_scope(workspace_id, task_id),
            status=_STALE_STATUS,
            agent_id=agent_id,
        )

    def mark_task_archived(
        self, *, workspace_id: str, task_id: str, agent_id: str | None = None
    ) -> int:
        return self._mark_scope_status(
            scope_id=task_scope(workspace_id, task_id),
            status=_ARCHIVED_STATUS,
            agent_id=agent_id,
        )

    def _mark_status(
        self, *, scope_id: str, agent_id: str, status: str,
        expected_session: dict | object = _UNSET,
    ) -> dict | None:
        if status not in _VALID_STATUSES:
            raise ValueError(f"unsupported session status: {status}")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT scope_id, agent_id, adapter, session_id, work_dir,
                          status, turn_count, created_at, updated_at,
                          context_generation, context_cursor_order_token,
                          context_cursor_message_id
                   FROM sessions WHERE scope_id = ? AND agent_id = ?""",
                (scope_id, agent_id),
            ).fetchone()
            current = _row_to_dict(row) if row else None
            if current is None or not _session_matches_expected(current, expected_session):
                return None
            now = time.time()
            conn.execute(
                """
                UPDATE sessions SET status = ?, updated_at = ?,
                    context_cursor_order_token = NULL,
                    context_cursor_message_id = NULL
                WHERE scope_id = ? AND agent_id = ?
                """,
                (status, now, scope_id, agent_id),
            )
            return {
                **current, "status": status, "updated_at": now,
                "context_cursor_order_token": None, "context_cursor_message_id": None,
            }

    def _mark_scope_status(
        self, *, scope_id: str, status: str, agent_id: str | None = None
    ) -> int:
        if status not in _VALID_STATUSES:
            raise ValueError(f"unsupported session status: {status}")
        sql = (
            "UPDATE sessions SET status = ?, updated_at = ?, "
            "context_cursor_order_token = NULL, "
            "context_cursor_message_id = NULL WHERE scope_id = ?"
        )
        params: list[str | float] = [status, time.time(), scope_id]
        if agent_id is not None:
            sql += " AND agent_id = ?"
            params.append(agent_id)
        with self._connect() as conn:
            cursor = conn.execute(sql, params)
            return cursor.rowcount
