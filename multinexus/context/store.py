import sqlite3
import time
from pathlib import Path
from typing import Any


class ChatContextStore:
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
        last_exc: Exception | None = None
        for attempt in range(8):
            try:
                with self._connect() as conn:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS messages (
                            message_id TEXT PRIMARY KEY,
                            channel_id TEXT NOT NULL,
                            author_id TEXT NOT NULL,
                            author_name TEXT NOT NULL,
                            author_is_bot INTEGER NOT NULL,
                            content TEXT NOT NULL,
                            created_at_ms INTEGER NOT NULL,
                            source TEXT NOT NULL
                        )
                        """
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_messages_channel_time "
                        "ON messages(channel_id, created_at_ms)"
                    )
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS runtime_reply_outbox (
                            job_id TEXT PRIMARY KEY,
                            workspace_id TEXT NOT NULL,
                            platform TEXT NOT NULL,
                            destination_id TEXT NOT NULL,
                            quote_message_id TEXT NOT NULL DEFAULT '',
                            created_at_ms INTEGER NOT NULL,
                            status TEXT NOT NULL DEFAULT 'pending',
                            attempts INTEGER NOT NULL DEFAULT 0,
                            last_error_code TEXT NOT NULL DEFAULT '',
                            sent_at_ms INTEGER
                        )
                        """
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_runtime_reply_status "
                        "ON runtime_reply_outbox(platform, status, created_at_ms)"
                    )
                    return
            except sqlite3.OperationalError as exc:
                last_exc = exc
                if "locked" not in str(exc).lower():
                    raise
                time.sleep(0.2 * (attempt + 1))
        raise RuntimeError(f"Could not initialize context DB {self.path}: {last_exc}")

    def record_message(
        self,
        *,
        message_id: str,
        channel_id: str,
        author_id: str,
        author_name: str,
        author_is_bot: bool,
        content: str,
        created_at_ms: int,
        source: str,
        ttl_seconds: int,
    ) -> None:
        if not content.strip() or content.strip() in ("\U0001f504 thinking...", "thinking..."):
            return
        cutoff_ms = created_at_ms - ttl_seconds * 1000
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO messages (
                    message_id, channel_id, author_id, author_name, author_is_bot,
                    content, created_at_ms, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    channel_id,
                    author_id,
                    author_name,
                    1 if author_is_bot else 0,
                    content.strip(),
                    created_at_ms,
                    source,
                ),
            )
            conn.execute("DELETE FROM messages WHERE created_at_ms < ?", (cutoff_ms,))

    def recent_messages(
        self,
        *,
        channel_id: str,
        exclude_message_id: str,
        limit: int,
        budget_chars: int,
        ttl_seconds: int,
    ) -> list[dict[str, Any]]:
        if limit <= 0 or budget_chars <= 0 or ttl_seconds <= 0:
            return []
        cutoff_ms = int(time.time() * 1000) - ttl_seconds * 1000
        fetch_limit = max(limit * 3, limit, 1)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT rowid AS context_order, message_id, author_id, author_name,
                       author_is_bot, content, created_at_ms
                FROM messages
                WHERE channel_id = ?
                  AND message_id != ?
                  AND created_at_ms >= ?
                ORDER BY created_at_ms DESC, rowid DESC
                LIMIT ?
                """,
                (channel_id, exclude_message_id, cutoff_ms, fetch_limit),
            ).fetchall()

        selected: list[dict[str, Any]] = []
        used = 0
        for context_order, message_id, author_id, author_name, author_is_bot, content, created_at_ms in rows:
            line_len = len(author_name) + len(content) + 16
            if selected and (used + line_len > budget_chars or len(selected) >= limit):
                break
            selected.append(
                {
                    "message_id": message_id,
                    "author_id": author_id,
                    "author_name": author_name,
                    "author_is_bot": bool(author_is_bot),
                    "content": content,
                    "created_at_ms": created_at_ms,
                    # SQLite rowid is the insertion order tie-breaker and is
                    # deliberately exposed as the context cursor.  It is
                    # not a second piece of mutable state.
                    "context_order": int(context_order),
                }
            )
            used += line_len
        return list(reversed(selected))

    def message_order(self, *, channel_id: str, message_id: str) -> int | None:
        """Return the SQLite insertion order for one stored message."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT rowid FROM messages WHERE channel_id = ? AND message_id = ?",
                (channel_id, message_id),
            ).fetchone()
        return int(row[0]) if row else None

    def message_by_id(
        self, *, channel_id: str, message_id: str
    ) -> dict[str, Any] | None:
        """Return one message and its SQLite rowid for envelope anchoring.

        This intentionally performs a point read instead of changing the
        bounded-history query.  A missing current row means callers must keep
        their legacy full prompt and omit the optimization envelope.
        """
        if not isinstance(channel_id, str) or not channel_id.strip():
            return None
        if not isinstance(message_id, str) or not message_id.strip():
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT rowid AS context_order, message_id, author_id, author_name,
                       author_is_bot, content, created_at_ms
                FROM messages
                WHERE channel_id = ? AND message_id = ?
                LIMIT 1
                """,
                (channel_id, message_id),
            ).fetchone()
        if row is None:
            return None
        context_order, stored_id, author_id, author_name, author_is_bot, content, created_at_ms = row
        return {
            "context_order": int(context_order),
            "message_id": str(stored_id),
            "author_id": str(author_id),
            "author_name": str(author_name),
            "author_is_bot": bool(author_is_bot),
            "content": str(content),
            "created_at_ms": int(created_at_ms),
        }

    # Explicit alias for callers that use the conventional getter spelling.
    get_message = message_by_id

    def purge_bot_messages_by_prefixes(self, prefixes: tuple[str, ...]) -> int:
        if not prefixes:
            return 0
        deleted = 0
        with self._connect() as conn:
            for prefix in prefixes:
                cursor = conn.execute(
                    "DELETE FROM messages WHERE author_is_bot = 1 AND content LIKE ?",
                    (f"{prefix}%",),
                )
                deleted += cursor.rowcount
        return deleted

    def has_recent_message(
        self,
        *,
        channel_id: str,
        author_id: str,
        content: str,
        exclude_message_id: str,
        within_seconds: int,
    ) -> bool:
        if within_seconds <= 0 or not content.strip():
            return False
        cutoff_ms = int(time.time() * 1000) - within_seconds * 1000
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM messages
                WHERE channel_id = ?
                  AND author_id = ?
                  AND message_id != ?
                  AND content = ?
                  AND created_at_ms >= ?
                LIMIT 1
                """,
                (
                    channel_id,
                    author_id,
                    exclude_message_id,
                    content.strip(),
                    cutoff_ms,
                ),
            ).fetchone()
        return row is not None

    def record_runtime_reply(
        self,
        *,
        job_id: str,
        workspace_id: str,
        platform: str,
        destination_id: str,
        quote_message_id: str = "",
        created_at_ms: int | None = None,
    ) -> None:
        """Journal a Coordinate job whose visible reply is bridge-owned."""
        if not all(
            isinstance(value, str) and value.strip()
            for value in (job_id, workspace_id, platform, destination_id)
        ):
            raise ValueError("runtime reply identity fields must be non-empty strings")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO runtime_reply_outbox (
                    job_id, workspace_id, platform, destination_id,
                    quote_message_id, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id.strip(),
                    workspace_id.strip(),
                    platform.strip().lower(),
                    destination_id.strip(),
                    quote_message_id.strip() if isinstance(quote_message_id, str) else "",
                    created_at_ms if created_at_ms is not None else int(time.time() * 1000),
                ),
            )

    def pending_runtime_replies(
        self, *, platform: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Return bridge-owned replies awaiting a successful platform send."""
        if not isinstance(platform, str) or not platform.strip():
            return []
        bounded_limit = max(1, min(int(limit), 100))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT job_id, workspace_id, platform, destination_id,
                       quote_message_id, created_at_ms, attempts, last_error_code
                FROM runtime_reply_outbox
                WHERE platform = ? AND status = 'pending'
                ORDER BY created_at_ms ASC
                LIMIT ?
                """,
                (platform.strip().lower(), bounded_limit),
            ).fetchall()
        return [
            {
                "job_id": row[0],
                "workspace_id": row[1],
                "platform": row[2],
                "destination_id": row[3],
                "quote_message_id": row[4],
                "created_at_ms": row[5],
                "attempts": row[6],
                "last_error_code": row[7],
            }
            for row in rows
        ]

    def mark_runtime_reply_attempt(self, *, job_id: str, error_code: str = "") -> None:
        """Record a bounded recovery attempt without storing exception text."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runtime_reply_outbox
                SET attempts = attempts + 1, last_error_code = ?
                WHERE job_id = ? AND status = 'pending'
                """,
                (error_code[:64] if isinstance(error_code, str) else "", job_id),
            )

    def mark_runtime_reply_sent(self, *, job_id: str, sent_at_ms: int | None = None) -> None:
        """Acknowledge a visible send after the platform call returns."""
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runtime_reply_outbox
                SET status = 'sent', sent_at_ms = ?
                WHERE job_id = ? AND status = 'pending'
                """,
                (sent_at_ms if sent_at_ms is not None else int(time.time() * 1000), job_id),
            )
