"""Washer — nightly memory extraction from conversation history.

Memory washing machine concept inspired by Mark Kashef:
"I Tried OpenClaw and Hermes. I Kept Claude Code." (https://youtu.be/rVzGu5OYYS0)
See timestamp 10:57 — "Gemini as a memory washing machine"

Scheduled daily (e.g. 2am) via cron, Windows Task Scheduler,
Systemd timer, or any service manager you choose.

Reads conversations + conversations_archive, extracts memories via a local LLM
(LM Studio OpenAI-compatible API), and routes to shared memories, shared
promotions, or private review queue.

Required env vars (.env):
  TARGET_USER_ID     Discord user ID whose messages to extract from
  USER_DISPLAY_NAME  Display name used in the extraction prompt (default: "the user")
  E4B_BASE_URL       LM Studio base URL (default: http://localhost:1234/v1)
  E4B_MODEL          Model ID to use for extraction (default: local-model)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import logging.handlers
import os
import sys

from dotenv import load_dotenv

from memory.content_validator import ALLOWED_TARGETS, validate_content
from persistence.db import (
    Database,
    _PRIVATE_SCHEMA,
    get_repo_root,
    get_shared_db_path,
    resolve_private_db_path,
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging() -> logging.Logger:
    log_dir = get_repo_root() / "logs"
    log_dir.mkdir(exist_ok=True)

    logger = logging.getLogger("washer")
    logger.setLevel(logging.DEBUG)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "washer.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(console_handler)

    return logger


log = _setup_logging()


# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

def _build_extraction_prompt(content: str, user_name: str) -> str:
    """Build the structured extraction prompt for the local LLM.

    Instructs the model to extract durable facts, preferences, and context
    about the user, returning a JSON array of extraction items.
    Each item: {type, content, importance, is_private}
    """
    return (
        f"You are a memory extraction assistant. Extract durable, memorable information"
        f" about {user_name} from their message.\n\n"
        "**Extractable types:**\n"
        '- "fact": A verifiable fact about the user or their world'
        " (job, tools, decisions, names of things they own or use)\n"
        '- "preference": A stated or implied preference, habit, or pattern'
        " (coding style, communication style, things they like or avoid)\n"
        '- "context": Background relevant to ongoing work'
        " (current project, goals, constraints, team dynamics)\n\n"
        "**Importance (1-5):**\n"
        "1 = trivial  2 = minor  3 = useful  4 = important  5 = foundational\n\n"
        "**is_private:** true if the item contains sensitive personal information\n"
        "(health, finances, family, legal, credentials - anything not for broad sharing)\n\n"
        "Return ONLY a valid JSON array. If nothing is extractable, return [].\n"
        "No prose, no code fences, no explanation - only the JSON array.\n\n"
        "Example output:\n"
        '[{"type":"fact","content":"Uses Python for all automation scripts",'
        '"importance":3,"is_private":false}]\n\n'
        f"**Message from {user_name}:**\n{content}"
    )


# ---------------------------------------------------------------------------
# LLM extraction call
# ---------------------------------------------------------------------------

async def _call_llm(
    content: str,
    user_name: str,
    base_url: str,
    model: str,
) -> list[dict] | None:
    """Call the local LLM (LM Studio OpenAI-compatible API) for memory extraction.

    Returns list of dicts: [{type, content, importance, is_private}, ...]
    Returns None on API/transport failure (caller should NOT advance watermark).
    Returns [] on success with no extractions (watermark may advance normally).
    """
    import json

    import aiohttp

    prompt = _build_extraction_prompt(content, user_name)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 512,
    }
    url = f"{base_url.rstrip('/')}/chat/completions"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    log.warning("LLM API returned status %d -- will retry on next run", resp.status)
                    return None  # transient failure -- do NOT advance watermark
                data = await resp.json()
    except Exception as exc:
        log.warning("LLM API call failed: %s -- will retry on next run", exc)
        return None  # transient failure -- do NOT advance watermark

    try:
        raw = data["choices"][0]["message"]["content"]
        start = raw.find("[")
        end = raw.rfind("]")
        if start == -1 or end == -1:
            log.debug("LLM response has no JSON array: %.200s", raw)
            return []  # model returned nothing extractable -- success, advance watermark
        items = json.loads(raw[start : end + 1])
        if not isinstance(items, list):
            return []
        return items
    except Exception as exc:
        log.debug("LLM response parse failed: %s -- raw=%.200s", exc, raw if "raw" in dir() else "")
        return []


# ---------------------------------------------------------------------------
# Schema verification
# ---------------------------------------------------------------------------

async def _verify_schema(shared_db: Database) -> None:
    """Verify required tables exist in shared DB."""
    required = {"memories", "memory_promotions", "memory_watermark", "conversations"}
    cursor = await shared_db._db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    existing = {row[0] for row in await cursor.fetchall()}
    missing = required - existing
    if missing:
        raise RuntimeError(f"Shared DB missing required tables: {sorted(missing)}")


# ---------------------------------------------------------------------------
# Message fetch (watermark-based)
# ---------------------------------------------------------------------------

async def _fetch_messages_since(
    db: Database,
    table: str,
    watermark: int,
    author_id_filter: str,
    limit: int = 500,
) -> list[dict]:
    """Fetch user messages from table with rowid > watermark, filtered to one author.

    NULL-aware: skips rows with NULL or empty content.
    """
    cursor = await db._db.execute(
        f"SELECT rowid, thread_id, content, message_id "
        f"FROM {table} "
        f"WHERE rowid > ? AND role = 'user' "
        f"AND author_id = ? "
        f"AND content IS NOT NULL AND content != '' "
        f"ORDER BY rowid ASC LIMIT ?",
        (watermark, author_id_filter, limit),
    )
    rows = await cursor.fetchall()
    return [
        {
            "rowid": row[0],
            "thread_id": row[1],
            "content": row[2],
            "message_id": row[3],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

async def _route_extraction(
    item: dict,
    shared_db: Database,
    private_db: Database,
    source_thread_id: str | None,
    source_message_id: str | None,
) -> str:
    """Route an extracted item to the appropriate tier.

    Privacy gate: is_private -> private_db.save_review_queue
    Non-fact gate: preference/context -> shared promotions; fact -> shared memories

    Returns: "private_queue" | "shared_memory" | "shared_promotion" | "rejected"
    """
    content = (item.get("content") or "").strip()
    item_type = (item.get("type") or "").lower()
    importance = int(item.get("importance") or 3)
    is_private = bool(item.get("is_private"))

    if not content:
        return "rejected"

    val = validate_content(content)
    if not val.valid:
        log.debug("Rejected extraction (reason=%s): %.60s", val.reason, content)
        return "rejected"

    if item_type not in ALLOWED_TARGETS:
        log.debug("Rejected extraction (unknown type=%r): %.60s", item_type, content)
        return "rejected"

    content_hash = hashlib.sha256(content.encode()).hexdigest()

    # Privacy gate -- runs first, routing is final
    if is_private:
        await private_db.save_review_queue(
            content_hash=content_hash,
            content=content,
            type=item_type,
            importance=importance,
            route_reason="private_flag",
            source_thread_id=source_thread_id,
            source_message_id=source_message_id,
        )
        log.debug("Routed to private queue: %.50s", content)
        return "private_queue"

    # Non-fact gate
    if item_type in ("preference", "context"):
        await shared_db.save_promotion(
            content_hash=content_hash,
            content=content,
            type=item_type,
            importance=importance,
            route_reason="non_fact",
            source_thread_id=source_thread_id,
            source_message_id=source_message_id,
        )
        log.debug("Routed to promotions (non-fact, type=%s): %.50s", item_type, content)
        return "shared_promotion"

    # Fact -> shared memories
    await shared_db.save_memory(
        content_hash=content_hash,
        type=item_type,
        content=content,
        importance=importance,
        source_thread_id=source_thread_id,
        source_message_id=source_message_id,
    )
    log.debug("Saved to shared memory (fact): %.50s", content)
    return "shared_memory"


# ---------------------------------------------------------------------------
# Main extraction loop
# ---------------------------------------------------------------------------

async def run_extraction(
    shared_db: Database,
    private_db: Database,
    target_user_id: str,
    user_name: str,
    llm_base_url: str,
    llm_model: str,
) -> None:
    """Main extraction pass over conversations and conversations_archive."""
    stats = {
        "processed": 0, "extracted": 0,
        "shared": 0, "promo": 0, "private": 0, "rejected": 0,
    }

    # Two independent sequential queries -- NOT a UNION or JOIN
    for source in ("conversations", "conversations_archive"):
        watermark = await shared_db.get_watermark(source)
        messages = await _fetch_messages_since(
            shared_db, source, watermark, target_user_id
        )

        if not messages:
            log.info("[%s] No new messages since watermark=%d", source, watermark)
            continue

        log.info(
            "[%s] Processing %d messages since watermark=%d",
            source, len(messages), watermark,
        )

        max_rowid = watermark
        llm_failed = False
        for msg in messages:
            rowid = msg["rowid"]
            content = msg.get("content") or ""

            if not content or not content.strip():
                max_rowid = max(max_rowid, rowid)
                continue

            val = validate_content(content)
            if not val.valid:
                stats["rejected"] += 1
                log.debug("Pre-filter rejected (reason=%s): %.60s", val.reason, content)
                max_rowid = max(max_rowid, rowid)
                continue

            stats["processed"] += 1

            extracted = await _call_llm(content, user_name, llm_base_url, llm_model)
            if extracted is None:
                log.warning(
                    "[%s] LLM failure at rowid=%d -- stopping watermark advance for this source",
                    source, rowid,
                )
                llm_failed = True
                break  # do not advance watermark past this point

            max_rowid = max(max_rowid, rowid)

            for item in extracted:
                stats["extracted"] += 1
                route = await _route_extraction(
                    item,
                    shared_db,
                    private_db,
                    source_thread_id=msg.get("thread_id"),
                    source_message_id=msg.get("message_id"),
                )
                if route == "shared_memory":
                    stats["shared"] += 1
                elif route == "shared_promotion":
                    stats["promo"] += 1
                elif route == "private_queue":
                    stats["private"] += 1
                elif route == "rejected":
                    stats["rejected"] += 1

        if max_rowid > watermark:
            await shared_db.set_watermark(source, max_rowid)
            log.info(
                "[%s] Watermark updated: %d -> %d%s",
                source, watermark, max_rowid,
                " (partial -- LLM failure halted progress)" if llm_failed else "",
            )

    log.info(
        "Extraction complete -- processed=%d extracted=%d shared=%d "
        "promo=%d private=%d rejected=%d",
        stats["processed"], stats["extracted"], stats["shared"],
        stats["promo"], stats["private"], stats["rejected"],
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> int:
    load_dotenv(get_repo_root() / ".env")

    shared_db_path = get_shared_db_path()
    log.info("Shared DB: %s", shared_db_path)

    try:
        private_db_path = resolve_private_db_path()
    except RuntimeError as exc:
        log.error("Cannot resolve private DB path: %s", exc)
        return 1
    log.info("Private DB: %s", private_db_path)

    target_user_id = os.environ.get("TARGET_USER_ID", "").strip()
    if not target_user_id:
        log.error("TARGET_USER_ID is required -- set it in .env")
        return 1

    user_name = os.environ.get("USER_DISPLAY_NAME", "the user")
    llm_base_url = os.environ.get("E4B_BASE_URL", "http://localhost:1234/v1")
    llm_model = os.environ.get("E4B_MODEL", "local-model")

    shared_db = Database(shared_db_path)
    private_db = Database(
        private_db_path,
        schema_sql=_PRIVATE_SCHEMA,
        run_shared_migrations=False,
    )

    try:
        await shared_db.connect()
        await private_db.connect()

        await _verify_schema(shared_db)

        await run_extraction(
            shared_db,
            private_db,
            target_user_id,
            user_name,
            llm_base_url,
            llm_model,
        )

    except Exception as exc:
        log.error("Washer failed: %s", exc, exc_info=True)
        return 1
    finally:
        await shared_db.close()
        await private_db.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
