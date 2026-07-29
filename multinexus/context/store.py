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
                SELECT message_id, author_id, author_name, author_is_bot, content, created_at_ms
                FROM messages
                WHERE channel_id = ?
                  AND message_id != ?
                  AND created_at_ms >= ?
                ORDER BY created_at_ms DESC
                LIMIT ?
                """,
                (channel_id, exclude_message_id, cutoff_ms, fetch_limit),
            ).fetchall()

        selected: list[dict[str, Any]] = []
        used = 0
        for message_id, author_id, author_name, author_is_bot, content, created_at_ms in rows:
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
                }
            )
            used += line_len
        return list(reversed(selected))

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
