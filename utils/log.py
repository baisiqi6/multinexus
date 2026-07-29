"""Structured logging — JSON formatter, rotating file handler, correlation context.

Sets up two log handlers:
  - Console: human-readable, INFO level
  - File:    JSON structured, DEBUG level, rotating at 5MB with 5 backups

Correlation context (session_id, job_id, agent, channel) is attached to every
log line via contextvars — set per-request in agents.py.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
from logging.handlers import RotatingFileHandler

# Correlation context — set per-request, appears in every log line
_ctx_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "session_id", default=None
)
_ctx_job_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "job_id", default=None
)
_ctx_agent: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent", default=None
)
_ctx_channel: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "channel", default=None
)


def set_correlation(
    *,
    session_id: str | None = None,
    job_id: str | None = None,
    agent: str | None = None,
    channel: str | None = None,
):
    """Set correlation IDs for the current async context."""
    if session_id is not None:
        _ctx_session_id.set(session_id)
    if job_id is not None:
        _ctx_job_id.set(job_id)
    if agent is not None:
        _ctx_agent.set(agent)
    if channel is not None:
        _ctx_channel.set(channel)


def clear_correlation():
    """Clear all correlation IDs."""
    _ctx_session_id.set(None)
    _ctx_job_id.set(None)
    _ctx_agent.set(None)
    _ctx_channel.set(None)


class JsonFormatter(logging.Formatter):
    """Structured JSON log formatter with correlation context."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        sid = _ctx_session_id.get()
        if sid:
            entry["session_id"] = sid
        jid = _ctx_job_id.get()
        if jid:
            entry["job_id"] = jid
        ag = _ctx_agent.get()
        if ag:
            entry["agent"] = ag
        ch = _ctx_channel.get()
        if ch:
            entry["channel"] = ch

        if record.exc_info and record.exc_info[1]:
            entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(entry, default=str)


def setup_logging(
    log_dir: str,
    *,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
    max_bytes: int = 5 * 1024 * 1024,  # 5 MB
    backup_count: int = 5,
):
    """Configure root logger with console (human) + file (JSON) handlers.

    Call once at startup, before any loggers are used.

    Args:
        log_dir:       Directory to write rotating log files to.
        console_level: Log level for console output (default: INFO).
        file_level:    Log level for file output (default: DEBUG).
        max_bytes:     Max log file size before rotation (default: 5MB).
        backup_count:  Number of rotated log files to keep (default: 5).
    """
    os.makedirs(log_dir, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    # Console handler — human-readable
    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(console)

    # File handler — JSON, rotating
    log_path = os.path.join(log_dir, "nexus.log")
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)
