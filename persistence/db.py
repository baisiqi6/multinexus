"""SQLite persistence layer with aiosqlite for multinexus.

Stores conversation history, job tracking, agent workspaces, audit logs,
session/plan state, and memory. Shared DB is in data/nexus.db by default.
Private DB (for sensitive memories) is stored outside the repo at PRIVATE_DB_PATH.
"""

import datetime
import glob
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SHARED_DB_PATH = _REPO_ROOT / "data" / "nexus.db"


def get_repo_root() -> Path:
    return _REPO_ROOT


def get_shared_db_path() -> Path:
    return _SHARED_DB_PATH


def resolve_private_db_path() -> Path:
    """Resolve PRIVATE_DB_PATH from environment.

    The path must point to a file inside a directory named "multinexus"
    and must not be inside the repo root. This ensures private data is stored
    separately from git-tracked files.

    Raises RuntimeError if the env var is not set or the path is invalid.
    """
    try:
        raw = os.environ["PRIVATE_DB_PATH"]
    except KeyError as exc:
        raise RuntimeError("PRIVATE_DB_PATH not set") from exc
    path = Path(os.path.expandvars(raw)).resolve()
    try:
        path.relative_to(_REPO_ROOT)
        inside_repo = True
    except ValueError:
        inside_repo = False
    if inside_repo or path.parent.name != "multinexus":
        raise RuntimeError(
            f"PRIVATE_DB_PATH invariant violated: {path}\n"
            "Path must be outside the repo root and inside a 'multinexus' directory.\n"
            "Example: /home/youruser/.private/multinexus/private.db"
        )
    return path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    thread_id   TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    timestamp   REAL NOT NULL,
    PRIMARY KEY (thread_id, timestamp)
);

CREATE TABLE IF NOT EXISTS conversations_archive (
    thread_id   TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    timestamp   REAL NOT NULL,
    PRIMARY KEY (thread_id, timestamp)
);

CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id           TEXT NOT NULL,
    agent               TEXT NOT NULL,
    prompt              TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    discord_message_id  TEXT,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event       TEXT NOT NULL,
    detail      TEXT,
    timestamp   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conv_thread ON conversations(thread_id);
CREATE INDEX IF NOT EXISTS idx_conv_ts ON conversations(timestamp);
CREATE INDEX IF NOT EXISTS idx_archive_thread ON conversations_archive(thread_id);
CREATE INDEX IF NOT EXISTS idx_archive_ts ON conversations_archive(timestamp);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    thread_id      TEXT PRIMARY KEY,
    content        TEXT NOT NULL,
    covered_until  REAL NOT NULL,
    updated_at     REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conv_summary_updated ON conversation_summaries(updated_at);

CREATE TABLE IF NOT EXISTS agent_workspace (
    thread_id   TEXT NOT NULL,
    agent       TEXT NOT NULL,
    content     TEXT NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (thread_id, agent)
);

CREATE INDEX IF NOT EXISTS idx_workspace_updated ON agent_workspace(updated_at);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    task TEXT NOT NULL,
    channel_id INTEGER,
    thread_id INTEGER,
    embed_msg_id INTEGER,
    origin_channel_id INTEGER,
    config JSON,
    state JSON,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(id),
    status TEXT NOT NULL,
    task TEXT NOT NULL,
    plan_text TEXT,
    concerns JSON,
    user_notes JSON,
    project TEXT,
    embed_msg_id INTEGER,
    origin_channel_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES sessions(id),
    agent TEXT NOT NULL,
    model TEXT,
    action TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    duration_ms INTEGER,
    exit_code INTEGER,
    error TEXT,
    caller_type TEXT NOT NULL DEFAULT 'user',
    caller_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intent_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_hash TEXT NOT NULL,
    channel_id INTEGER,
    intent TEXT,
    params JSON,
    confidence REAL,
    action_taken TEXT,
    user_confirmed INTEGER,
    latency_ms INTEGER,
    caller_type TEXT NOT NULL DEFAULT 'user',
    caller_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    description TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_type ON sessions(type);
CREATE INDEX IF NOT EXISTS idx_plans_session ON plans(session_id);
CREATE INDEX IF NOT EXISTS idx_plans_status ON plans(status);
CREATE INDEX IF NOT EXISTS idx_agent_runs_session ON agent_runs(session_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent ON agent_runs(agent);
CREATE INDEX IF NOT EXISTS idx_intent_log_intent ON intent_log(intent);
CREATE INDEX IF NOT EXISTS idx_intent_log_created ON intent_log(created_at);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL CHECK(type IN ('fact','preference','context')),
    content TEXT NOT NULL,
    importance INTEGER NOT NULL DEFAULT 3,
    source_thread_id TEXT,
    source_message_id TEXT,
    extracted_by TEXT NOT NULL DEFAULT 'local-llm',
    created_at REAL NOT NULL,
    last_accessed REAL,
    access_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memories_hash ON memories(content_hash);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type);
CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance DESC);

CREATE TABLE IF NOT EXISTS memory_promotions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('fact','preference','context')),
    importance INTEGER NOT NULL DEFAULT 3,
    route_reason TEXT,
    source_thread_id TEXT,
    source_message_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending_review'
        CHECK(status IN ('pending_review','approved','rejected')),
    created_at REAL NOT NULL,
    reviewed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_promotions_status ON memory_promotions(status);

CREATE TABLE IF NOT EXISTS memory_watermark (
    source TEXT PRIMARY KEY,
    last_row_id INTEGER NOT NULL DEFAULT 0,
    last_updated REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    memory_hash TEXT NOT NULL,
    db_source TEXT NOT NULL,
    caller_type TEXT NOT NULL DEFAULT 'user',
    caller_id TEXT NOT NULL,
    detail TEXT,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_audit_hash ON memory_audit(memory_hash);
CREATE INDEX IF NOT EXISTS idx_memory_audit_ts ON memory_audit(ts);

CREATE TABLE IF NOT EXISTS wiki_references (
    content_hash TEXT PRIMARY KEY,
    page_slug TEXT NOT NULL,
    marker_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cron_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    schedule TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    agent_name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run REAL,
    next_run REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cron_next_run ON cron_jobs(next_run);
"""

_PRIVATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories_private (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL CHECK(type IN ('fact','preference','context')),
    content TEXT NOT NULL,
    importance INTEGER NOT NULL DEFAULT 3,
    source_thread_id TEXT,
    source_message_id TEXT,
    extracted_by TEXT NOT NULL DEFAULT 'local-llm',
    created_at REAL NOT NULL,
    last_accessed REAL,
    access_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL,
    type TEXT NOT NULL,
    importance INTEGER NOT NULL DEFAULT 3,
    route_reason TEXT,
    source_thread_id TEXT,
    source_message_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewed_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    memory_hash TEXT NOT NULL,
    db_source TEXT NOT NULL,
    caller_type TEXT NOT NULL DEFAULT 'user',
    caller_id TEXT NOT NULL,
    detail TEXT,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_private_audit_hash ON memory_audit(memory_hash);
"""


def _iso_now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value)


def _json_loads(value: str | None) -> Any:
    if not value:
        return None
    return json.loads(value)


def _row_to_dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _caller_value(value: Any) -> str:
    return getattr(value, "value", value)


class Database:
    """Async SQLite database wrapper for multinexus.

    Parameters:
        db_path:              Path to the SQLite database file.
        schema_sql:           SQL string to create tables (defaults to _SCHEMA).
        run_shared_migrations: Whether to run JSON-to-SQLite migration on connect.
    """

    def __init__(
        self,
        db_path: Path | None = None,
        schema_sql: str | None = None,
        run_shared_migrations: bool = True,
    ):
        self.db_path = db_path if db_path is not None else get_shared_db_path()
        self._schema_sql = schema_sql if schema_sql is not None else _SCHEMA
        self._run_shared_migrations = run_shared_migrations
        self._db: aiosqlite.Connection | None = None

    async def connect(self):
        """Open the database, create tables, run migrations."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(self._schema_sql)

        # Additive column migrations (safe to run on existing DBs)
        for col_sql in [
            "ALTER TABLE jobs ADD COLUMN tokens_input INTEGER DEFAULT NULL",
            "ALTER TABLE jobs ADD COLUMN tokens_output INTEGER DEFAULT NULL",
            "ALTER TABLE jobs ADD COLUMN tokens_cache_read INTEGER DEFAULT NULL",
            "ALTER TABLE jobs ADD COLUMN cost_usd REAL DEFAULT NULL",
        ]:
            try:
                await self._db.execute(col_sql)
            except Exception:
                pass
        for col_sql in [
            "ALTER TABLE conversations ADD COLUMN author_id TEXT",
            "ALTER TABLE conversations ADD COLUMN message_id TEXT",
            "ALTER TABLE conversations_archive ADD COLUMN author_id TEXT",
            "ALTER TABLE conversations_archive ADD COLUMN message_id TEXT",
        ]:
            try:
                await self._db.execute(col_sql)
            except Exception:
                pass
        await self._db.commit()

        if self._run_shared_migrations:
            await migrate_json_to_sqlite(self)
        log.info("Database connected: %s", self.db_path)

    async def close(self):
        if self._db:
            await self._db.close()
            log.info("Database closed")

    # --- Conversations ---

    async def save_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        *,
        author_id: str | None = None,
        message_id: str | None = None,
    ):
        await self._db.execute(
            "INSERT INTO conversations "
            "(thread_id, role, content, timestamp, author_id, message_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (thread_id, role, content, time.time(), author_id, message_id),
        )
        await self._db.commit()

    async def get_last_assistant_message(self, thread_id: str) -> str | None:
        """Return the content of the most recent assistant message, or None."""
        cursor = await self._db.execute(
            "SELECT content FROM conversations "
            "WHERE thread_id = ? AND role = 'assistant' AND content != '' "
            "ORDER BY timestamp DESC LIMIT 1",
            (thread_id,),
        )
        row = await cursor.fetchone()
        return row["content"] if row else None

    async def get_history(self, thread_id: str, budget_chars: int) -> list[dict]:
        """Get recent conversation history within char budget."""
        cursor = await self._db.execute(
            "SELECT role, content FROM conversations WHERE thread_id = ? ORDER BY timestamp DESC",
            (thread_id,),
        )
        rows = await cursor.fetchall()

        history = []
        total_chars = 0
        for row in rows:
            msg_chars = len(row["content"])
            if total_chars + msg_chars > budget_chars:
                break
            history.append({"role": row["role"], "content": row["content"]})
            total_chars += msg_chars

        history.reverse()
        return history

    async def get_history_since(
        self,
        thread_id: str,
        since_ts: float,
    ) -> list[dict]:
        """Get conversation history newer than since_ts, including timestamps."""
        cursor = await self._db.execute(
            "SELECT role, content, timestamp FROM conversations "
            "WHERE thread_id = ? AND timestamp >= ? ORDER BY timestamp ASC",
            (thread_id, since_ts),
        )
        rows = await cursor.fetchall()
        return [
            {
                "role": row["role"],
                "content": row["content"],
                "timestamp": row["timestamp"],
            }
            for row in rows
        ]

    async def get_conversation_summary(self, thread_id: str) -> dict | None:
        """Return the compacted summary for a thread, if present."""
        cursor = await self._db.execute(
            "SELECT content, covered_until, updated_at FROM conversation_summaries "
            "WHERE thread_id = ?",
            (thread_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def upsert_conversation_summary(
        self,
        thread_id: str,
        content: str,
        covered_until: float,
    ) -> None:
        """Store or replace the compacted summary for a thread."""
        now = time.time()
        await self._db.execute(
            "INSERT INTO conversation_summaries "
            "(thread_id, content, covered_until, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(thread_id) DO UPDATE SET "
            "content = excluded.content, "
            "covered_until = excluded.covered_until, "
            "updated_at = excluded.updated_at",
            (thread_id, content, covered_until, now),
        )
        await self._db.commit()

    async def clear_history(self, thread_id: str) -> int:
        """Delete all conversation history for a thread. Returns number of deleted rows."""
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM conversations WHERE thread_id = ?",
            (thread_id,),
        )
        row = await cursor.fetchone()
        count = row[0] if row else 0
        await self._db.execute(
            "DELETE FROM conversations WHERE thread_id = ?",
            (thread_id,),
        )
        await self._db.execute(
            "DELETE FROM conversation_summaries WHERE thread_id = ?",
            (thread_id,),
        )
        await self._db.commit()
        return count

    async def search_history(self, thread_id: str, query: str, limit: int = 20) -> list[dict]:
        """Search archived + active conversations for a thread."""
        results = []
        for table in ("conversations", "conversations_archive"):
            cursor = await self._db.execute(
                f"SELECT role, content, timestamp FROM {table} "
                "WHERE thread_id = ? AND content LIKE ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (thread_id, f"%{query}%", limit),
            )
            rows = await cursor.fetchall()
            for row in rows:
                results.append({
                    "role": row["role"],
                    "content": row["content"],
                    "timestamp": row["timestamp"],
                })
        results.sort(key=lambda r: r["timestamp"], reverse=True)
        return results[:limit]

    # --- Archival ---

    async def archive_old_conversations(self, retention_days: int = 7):
        """Move conversations older than retention_days to archive table."""
        cutoff = time.time() - (retention_days * 86400)
        cursor = await self._db.execute(
            "INSERT OR IGNORE INTO conversations_archive "
            "SELECT * FROM conversations WHERE timestamp < ?",
            (cutoff,),
        )
        archived = cursor.rowcount
        await self._db.execute(
            "DELETE FROM conversations WHERE timestamp < ?",
            (cutoff,),
        )
        await self._db.commit()
        if archived > 0:
            log.info("Archived %d conversation messages older than %d days", archived, retention_days)

    # --- Jobs ---

    async def create_job(self, thread_id: str, agent: str, prompt: str) -> int:
        now = time.time()
        cursor = await self._db.execute(
            "INSERT INTO jobs (thread_id, agent, prompt, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?)",
            (thread_id, agent, prompt, now, now),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def update_job(
        self,
        job_id: int,
        status: str,
        discord_message_id: str | None = None,
        tokens_input: int | None = None,
        tokens_output: int | None = None,
        tokens_cache_read: int | None = None,
        cost_usd: float | None = None,
    ):
        fields = ["status = ?", "updated_at = ?"]
        params = [status, time.time()]
        if discord_message_id is not None:
            fields.append("discord_message_id = ?")
            params.append(discord_message_id)
        if tokens_input is not None:
            fields.append("tokens_input = ?")
            params.append(tokens_input)
        if tokens_output is not None:
            fields.append("tokens_output = ?")
            params.append(tokens_output)
        if tokens_cache_read is not None:
            fields.append("tokens_cache_read = ?")
            params.append(tokens_cache_read)
        if cost_usd is not None:
            fields.append("cost_usd = ?")
            params.append(cost_usd)
        params.append(job_id)
        await self._db.execute(
            f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        await self._db.commit()

    async def recover_stale_jobs(self):
        """Mark stale 'running' jobs as 'failed' on startup."""
        cursor = await self._db.execute(
            "UPDATE jobs SET status = 'failed', updated_at = ? WHERE status = 'running'",
            (time.time(),),
        )
        await self._db.commit()
        if cursor.rowcount > 0:
            log.warning("Recovered %d stale running jobs → failed", cursor.rowcount)

    # --- Audit ---

    async def audit(self, event: str, detail: str | None = None):
        await self._db.execute(
            "INSERT INTO audit_log (event, detail, timestamp) VALUES (?, ?, ?)",
            (event, detail, time.time()),
        )
        await self._db.commit()

    async def cleanup_audit(self, retention_days: int = 30):
        cutoff = time.time() - (retention_days * 86400)
        cursor = await self._db.execute(
            "DELETE FROM audit_log WHERE timestamp < ?", (cutoff,)
        )
        await self._db.commit()
        if cursor.rowcount > 0:
            log.info("Cleaned up %d old audit entries", cursor.rowcount)

    async def get_token_totals_24h(self, agent: str) -> dict:
        """Return summed token/cost stats for an agent in the last 24 hours."""
        cutoff = time.time() - 86400
        cursor = await self._db.execute(
            "SELECT SUM(tokens_input), SUM(tokens_output), SUM(tokens_cache_read), SUM(cost_usd) "
            "FROM jobs WHERE agent = ? AND created_at > ? AND status = 'completed'",
            (agent, cutoff),
        )
        row = await cursor.fetchone()
        return {
            "tokens_input": row[0] or 0,
            "tokens_output": row[1] or 0,
            "tokens_cache_read": row[2] or 0,
            "cost_usd": row[3] or 0.0,
        }

    async def get_last_local_prompt_tokens(self, agent: str = "mac-openclaw") -> int | None:
        """Return the most recent prompt token count for a local agent."""
        cursor = await self._db.execute(
            "SELECT tokens_input FROM jobs WHERE agent = ? AND tokens_input IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (agent,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    # --- Agent Workspace ---

    async def upsert_workspace(self, thread_id: str, agent: str, content: str):
        """Store or update the scratch workspace for an agent in a thread."""
        await self._db.execute(
            "INSERT INTO agent_workspace (thread_id, agent, content, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (thread_id, agent) DO UPDATE SET "
            "content = excluded.content, updated_at = excluded.updated_at",
            (thread_id, agent, content, time.time()),
        )
        await self._db.commit()

    async def get_workspace(self, thread_id: str, agent: str) -> str:
        """Retrieve the scratch workspace for an agent in a thread. Returns '' if none."""
        cursor = await self._db.execute(
            "SELECT content FROM agent_workspace WHERE thread_id = ? AND agent = ?",
            (thread_id, agent),
        )
        row = await cursor.fetchone()
        return row[0] if row else ""

    async def cleanup_old_workspaces(self, cutoff_days: int = 30):
        """Delete workspace entries not updated within cutoff_days."""
        cutoff = time.time() - (cutoff_days * 86400)
        cursor = await self._db.execute(
            "DELETE FROM agent_workspace WHERE updated_at < ?",
            (cutoff,),
        )
        await self._db.commit()
        if cursor.rowcount > 0:
            log.info("Cleaned up %d old workspace entries", cursor.rowcount)

    # --- Shared Memory ---

    async def save_memory(
        self,
        content_hash: str,
        type: str,
        content: str,
        *,
        importance: int = 3,
        source_thread_id: str | None = None,
        source_message_id: str | None = None,
        extracted_by: str = "local-llm",
    ) -> None:
        now = time.time()
        await self._db.execute(
            """
            INSERT INTO memories
                (content_hash, type, content, importance, source_thread_id,
                 source_message_id, extracted_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(content_hash) DO UPDATE SET
                content = excluded.content,
                importance = excluded.importance,
                last_accessed = excluded.created_at
            """,
            (content_hash, type, content, importance,
             source_thread_id, source_message_id, extracted_by, now),
        )
        await self._db.commit()

    async def memory_exists(self, content_hash: str) -> bool:
        cursor = await self._db.execute(
            "SELECT 1 FROM memories WHERE content_hash = ?", (content_hash,)
        )
        return await cursor.fetchone() is not None

    async def get_memories(self, type: str | None = None, limit: int = 50) -> list[dict]:
        if type is not None:
            cursor = await self._db.execute(
                "SELECT * FROM memories WHERE type = ? ORDER BY importance DESC, created_at DESC LIMIT ?",
                (type, limit),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM memories ORDER BY importance DESC, created_at DESC LIMIT ?",
                (limit,),
            )
        return [dict(row) for row in await cursor.fetchall()]

    async def delete_memory(self, content_hash: str) -> None:
        await self._db.execute(
            "DELETE FROM memories WHERE content_hash = ?", (content_hash,)
        )
        await self._db.commit()

    # --- Memory watermark ---

    async def get_watermark(self, source: str) -> int:
        cursor = await self._db.execute(
            "SELECT last_row_id FROM memory_watermark WHERE source = ?", (source,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def set_watermark(self, source: str, row_id: int) -> None:
        await self._db.execute(
            """
            INSERT INTO memory_watermark (source, last_row_id, last_updated)
            VALUES (?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                last_row_id = excluded.last_row_id,
                last_updated = excluded.last_updated
            """,
            (source, row_id, time.time()),
        )
        await self._db.commit()

    # --- Memory promotions (shared DB) ---

    async def save_promotion(
        self,
        content_hash: str,
        content: str,
        type: str,
        *,
        importance: int = 3,
        route_reason: str | None = None,
        source_thread_id: str | None = None,
        source_message_id: str | None = None,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO memory_promotions
                (content_hash, content, type, importance, route_reason,
                 source_thread_id, source_message_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(content_hash) DO NOTHING
            """,
            (content_hash, content, type, importance, route_reason,
             source_thread_id, source_message_id, time.time()),
        )
        await self._db.commit()

    # --- Review queue (private DB) ---

    async def save_review_queue(
        self,
        content_hash: str,
        content: str,
        type: str,
        *,
        importance: int = 3,
        route_reason: str | None = None,
        source_thread_id: str | None = None,
        source_message_id: str | None = None,
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO review_queue
                (content_hash, content, type, importance, route_reason,
                 source_thread_id, source_message_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(content_hash) DO NOTHING
            """,
            (content_hash, content, type, importance, route_reason,
             source_thread_id, source_message_id, time.time()),
        )
        await self._db.commit()

    async def get_review_queue(
        self, status: str = "pending", limit: int = 50
    ) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM review_queue WHERE status = ? ORDER BY created_at ASC LIMIT ?",
            (status, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def get_review_queue_item(
        self, content_hash: str, status: str = "pending"
    ) -> dict | None:
        cursor = await self._db.execute(
            "SELECT * FROM review_queue WHERE content_hash = ? AND status = ?",
            (content_hash, status),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_review_status(self, content_hash: str, status: str) -> None:
        await self._db.execute(
            "UPDATE review_queue SET status = ?, reviewed_at = ? WHERE content_hash = ?",
            (status, time.time(), content_hash),
        )
        await self._db.commit()

    async def log_memory_audit(
        self,
        action: str,
        memory_hash: str,
        db_source: str,
        caller_type: str = "system",
        caller_id: str = "washer",
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO memory_audit (action, memory_hash, db_source, caller_type, caller_id, ts)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (action, memory_hash, db_source, caller_type, caller_id, time.time()),
        )
        await self._db.commit()

    async def private_hash_exists(self, content_hash: str) -> bool:
        cursor = await self._db.execute(
            """
            SELECT 1 FROM memories_private WHERE content_hash = ?
            UNION SELECT 1 FROM review_queue WHERE content_hash = ?
            """,
            (content_hash, content_hash),
        )
        return await cursor.fetchone() is not None

    # --- Sessions ---

    async def save_session(
        self,
        session_id,
        type,
        status,
        task,
        *,
        channel_id=None,
        thread_id=None,
        embed_msg_id=None,
        origin_channel_id=None,
        config=None,
        state=None,
        created_at: str | None = None,
        updated_at: str | None = None,
    ):
        existing = await self.get_session(session_id)
        created = created_at or (existing["created_at"] if existing else _iso_now())
        updated = updated_at or _iso_now()
        await self._db.execute(
            """
            INSERT OR REPLACE INTO sessions (
                id, type, status, task, channel_id, thread_id, embed_msg_id,
                origin_channel_id, config, state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id, type, status, task, channel_id, thread_id, embed_msg_id,
                origin_channel_id, _json_dumps(config), _json_dumps(state), created, updated,
            ),
        )
        await self._db.commit()

    async def get_session(self, session_id) -> dict | None:
        cursor = await self._db.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        row = _row_to_dict(await cursor.fetchone())
        if row is None:
            return None
        row["config"] = _json_loads(row["config"])
        row["state"] = _json_loads(row["state"])
        return row

    async def update_session(self, session_id, **kwargs):
        fields = []
        params = []
        for key, value in kwargs.items():
            if key in {"config", "state"}:
                value = _json_dumps(value)
            fields.append(f"{key} = ?")
            params.append(value)
        fields.append("updated_at = ?")
        params.append(_iso_now())
        params.append(session_id)
        await self._db.execute(
            f"UPDATE sessions SET {', '.join(fields)} WHERE id = ?", params
        )
        await self._db.commit()

    async def list_sessions(self, type=None, status=None) -> list[dict]:
        clauses = []
        params = []
        if type is not None:
            clauses.append("type = ?")
            params.append(type)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM sessions"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC"
        cursor = await self._db.execute(sql, params)
        rows = []
        for row in await cursor.fetchall():
            item = dict(row)
            item["config"] = _json_loads(item["config"])
            item["state"] = _json_loads(item["state"])
            rows.append(item)
        return rows

    async def delete_session(self, session_id):
        await self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await self._db.commit()

    # --- Plans ---

    async def save_plan(
        self,
        plan_id,
        session_id,
        status,
        task,
        *,
        plan_text=None,
        concerns=None,
        user_notes=None,
        project=None,
        embed_msg_id=None,
        origin_channel_id=None,
        created_at: str | None = None,
        updated_at: str | None = None,
    ):
        existing = await self.get_plan(plan_id)
        created = created_at or (existing["created_at"] if existing else _iso_now())
        updated = updated_at or _iso_now()
        await self._db.execute(
            """
            INSERT OR REPLACE INTO plans (
                id, session_id, status, task, plan_text, concerns, user_notes,
                project, embed_msg_id, origin_channel_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id, session_id, status, task, plan_text,
                _json_dumps(concerns), _json_dumps(user_notes),
                project, embed_msg_id, origin_channel_id, created, updated,
            ),
        )
        await self._db.commit()

    async def get_plan(self, plan_id) -> dict | None:
        cursor = await self._db.execute("SELECT * FROM plans WHERE id = ?", (plan_id,))
        row = _row_to_dict(await cursor.fetchone())
        if row is None:
            return None
        row["concerns"] = _json_loads(row["concerns"])
        row["user_notes"] = _json_loads(row["user_notes"])
        return row

    async def update_plan(self, plan_id, **kwargs):
        fields = []
        params = []
        for key, value in kwargs.items():
            if key in {"concerns", "user_notes"}:
                value = _json_dumps(value)
            fields.append(f"{key} = ?")
            params.append(value)
        fields.append("updated_at = ?")
        params.append(_iso_now())
        params.append(plan_id)
        await self._db.execute(
            f"UPDATE plans SET {', '.join(fields)} WHERE id = ?", params
        )
        await self._db.commit()

    async def list_plans(self, status=None, session_id=None) -> list[dict]:
        clauses = []
        params = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        sql = "SELECT * FROM plans"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC"
        cursor = await self._db.execute(sql, params)
        rows = []
        for row in await cursor.fetchall():
            item = dict(row)
            item["concerns"] = _json_loads(item["concerns"])
            item["user_notes"] = _json_loads(item["user_notes"])
            rows.append(item)
        return rows

    async def annotate_plan(self, plan_id, note: str) -> bool:
        plan = await self.get_plan(plan_id)
        if plan is None:
            return False
        notes = plan.get("user_notes")
        if not isinstance(notes, list):
            notes = []
        notes.append(note)
        await self.update_plan(plan_id, user_notes=notes)
        return True

    async def delete_plan(self, plan_id):
        await self._db.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
        await self._db.commit()

    # --- Agent Runs ---

    async def log_agent_run(
        self,
        session_id,
        agent,
        action,
        caller_type,
        caller_id,
        *,
        model=None,
        input_tokens=None,
        output_tokens=None,
        duration_ms=None,
        exit_code=None,
        error=None,
    ) -> int:
        cursor = await self._db.execute(
            """
            INSERT INTO agent_runs (
                session_id, agent, model, action, input_tokens, output_tokens,
                duration_ms, exit_code, error, caller_type, caller_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id, agent, model, action, input_tokens, output_tokens,
                duration_ms, exit_code, error,
                _caller_value(caller_type), caller_id, _iso_now(),
            ),
        )
        await self._db.commit()
        return cursor.lastrowid

    # --- Intent Log ---

    async def log_intent(
        self,
        message_hash,
        caller_type,
        caller_id,
        *,
        channel_id=None,
        intent=None,
        params=None,
        confidence=None,
        action_taken=None,
        user_confirmed=None,
        latency_ms=None,
    ) -> int:
        cursor = await self._db.execute(
            """
            INSERT INTO intent_log (
                message_hash, channel_id, intent, params, confidence, action_taken,
                user_confirmed, latency_ms, caller_type, caller_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_hash, channel_id, intent, _json_dumps(params),
                confidence, action_taken, user_confirmed, latency_ms,
                _caller_value(caller_type), caller_id, _iso_now(),
            ),
        )
        await self._db.commit()
        return cursor.lastrowid

    # --- Schema Version ---

    async def get_schema_version(self) -> int:
        cursor = await self._db.execute(
            "SELECT MAX(version) AS version FROM schema_version"
        )
        row = await cursor.fetchone()
        return row["version"] or 0

    async def set_schema_version(self, version: int, description: str):
        await self._db.execute(
            "INSERT INTO schema_version (version, applied_at, description) VALUES (?, ?, ?)",
            (version, _iso_now(), description),
        )
        await self._db.commit()

    # --- Private Memory (requires private DB) ---

    async def get_memories_for_injection(self, limit: int = 10) -> list[dict]:
        """Return private memories ordered by importance (requires private DB schema)."""
        cursor = await self._db.execute(
            "SELECT * FROM memories_private ORDER BY importance DESC, created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    # --- Wiki References ---

    async def save_wiki_reference(
        self, content_hash: str, page_slug: str, marker_hash: str
    ) -> None:
        await self._db.execute(
            """
            INSERT INTO wiki_references (content_hash, page_slug, marker_hash, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(content_hash) DO UPDATE SET
                page_slug = excluded.page_slug,
                marker_hash = excluded.marker_hash
            """,
            (content_hash, page_slug, marker_hash, datetime.datetime.utcnow().isoformat()),
        )
        await self._db.commit()

    async def get_wiki_reference(self, content_hash: str) -> dict | None:
        cursor = await self._db.execute(
            "SELECT * FROM wiki_references WHERE content_hash = ?", (content_hash,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def delete_wiki_reference(self, content_hash: str) -> bool:
        cursor = await self._db.execute(
            "DELETE FROM wiki_references WHERE content_hash = ?", (content_hash,)
        )
        await self._db.commit()
        return cursor.rowcount > 0

    # --- Cron Jobs ---

    async def create_cron_job(
        self,
        name: str,
        schedule: str,
        channel_id: int,
        agent_name: str,
        prompt: str,
        created_by: int,
        next_run: float,
    ) -> int:
        now = time.time()
        cursor = await self._db.execute(
            "INSERT INTO cron_jobs (name, schedule, channel_id, agent_name, prompt, created_by, next_run, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, schedule, channel_id, agent_name, prompt, created_by, next_run, now),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def list_cron_jobs(self) -> list[dict]:
        cursor = await self._db.execute("SELECT * FROM cron_jobs ORDER BY name")
        return [dict(row) for row in await cursor.fetchall()]

    async def get_due_cron_jobs(self, now: float) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM cron_jobs WHERE enabled = 1 AND next_run <= ?",
            (now,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def update_cron_job_run(self, job_id: int, last_run: float, next_run: float):
        await self._db.execute(
            "UPDATE cron_jobs SET last_run = ?, next_run = ? WHERE id = ?",
            (last_run, next_run, job_id),
        )
        await self._db.commit()

    async def delete_cron_job(self, name: str) -> bool:
        cursor = await self._db.execute(
            "DELETE FROM cron_jobs WHERE name = ?", (name,)
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def set_cron_job_enabled(self, name: str, enabled: bool) -> bool:
        cursor = await self._db.execute(
            "UPDATE cron_jobs SET enabled = ? WHERE name = ?",
            (1 if enabled else 0, name),
        )
        await self._db.commit()
        return cursor.rowcount > 0


async def migrate_json_to_sqlite(db: Database):
    """One-time migration from legacy JSON state files to SQLite."""
    if await db.get_schema_version() >= 1:
        log.info("Skipping JSON migration; schema version already applied")
        return

    data_dir = db.db_path.parent
    discuss_path = data_dir / "discuss_state.json"
    task_path = data_dir / "task_state.json"
    plan_paths = sorted(glob.glob(str(data_dir / "plans" / "*.json")))

    migrated_sessions = 0
    migrated_plans = 0
    renamed_paths: list[Path] = []

    def load_json(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    discuss_data = load_json(discuss_path)
    if discuss_data is not None:
        renamed_paths.append(discuss_path)
    if discuss_data and discuss_data.get("status") != "idle" and discuss_data.get("session_id"):
        state = {
            k: v for k, v in discuss_data.items()
            if k not in {
                "session_id", "status", "task", "channel_id",
                "thread_id", "embed_msg_id", "origin_channel_id", "started_at",
            }
        }
        created_at = discuss_data.get("started_at") or _iso_now()
        await db.save_session(
            session_id=discuss_data["session_id"],
            type="discuss",
            status=discuss_data["status"],
            task=discuss_data.get("task", ""),
            channel_id=discuss_data.get("channel_id"),
            thread_id=discuss_data.get("thread_id"),
            embed_msg_id=discuss_data.get("embed_msg_id"),
            origin_channel_id=discuss_data.get("origin_channel_id"),
            state=state,
            created_at=created_at,
            updated_at=created_at,
        )
        migrated_sessions += 1

    task_data = load_json(task_path)
    if task_data is not None:
        renamed_paths.append(task_path)
    if task_data and task_data.get("status") != "idle" and task_data.get("task_id"):
        state = {
            k: v for k, v in task_data.items()
            if k not in {"task_id", "status", "task", "channel_id", "started_at"}
        }
        created_at = task_data.get("started_at") or _iso_now()
        await db.save_session(
            session_id=task_data["task_id"],
            type="start",
            status=task_data["status"],
            task=task_data.get("task", ""),
            channel_id=task_data.get("channel_id"),
            state=state,
            created_at=created_at,
            updated_at=created_at,
        )
        migrated_sessions += 1

    for plan_path_str in plan_paths:
        plan_path = Path(plan_path_str)
        plan_data = load_json(plan_path)
        if not plan_data or not plan_data.get("plan_id"):
            continue
        created_at = plan_data.get("created_at") or _iso_now()
        await db.save_plan(
            plan_id=plan_data["plan_id"],
            session_id=plan_data.get("session_id"),
            status=plan_data.get("status", "ready"),
            task=plan_data.get("task", ""),
            plan_text=plan_data.get("plan"),
            concerns=plan_data.get("concerns"),
            user_notes=plan_data.get("user_notes"),
            project=plan_data.get("project"),
            embed_msg_id=plan_data.get("embed_msg_id"),
            origin_channel_id=plan_data.get("origin_channel_id"),
            created_at=created_at,
            updated_at=created_at,
        )
        migrated_plans += 1
        renamed_paths.append(plan_path)

    for path in renamed_paths:
        if path.exists():
            os.rename(path, f"{path}.bak")

    await db.set_schema_version(1, "Initial migration from JSON state files")
    log.info(
        "Migrated JSON state to SQLite: %d sessions, %d plans",
        migrated_sessions, migrated_plans,
    )
