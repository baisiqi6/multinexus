"""Validated structured history passed from a bridge to agentd."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContextEnvelope:
    scope_id: str
    session_scope_id: str
    generation: str
    bot_id: int | str | None
    complete: bool
    order_token: str
    messages: tuple[dict[str, Any], ...]
    current_text: str
    current_message_id: str
    current_order_token: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "scope_id": self.scope_id,
            "session_scope_id": self.session_scope_id,
            "generation": self.generation,
            "bot_id": self.bot_id,
            "complete": self.complete,
            "order_token": self.order_token,
            "messages": list(self.messages),
            "current_text": self.current_text,
            "current_message_id": self.current_message_id,
            "current_order_token": self.current_order_token,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def _token(item: dict[str, Any]) -> str:
    return str(item["context_order"])


def build_context_envelope(
    *,
    scope_id: str,
    session_scope_id: str,
    generation: str,
    bot_id: int | str | None,
    messages: list[dict[str, Any]],
    current_text: str,
    current_message_id: str,
    current_order_token: str,
) -> ContextEnvelope:
    normalized = tuple(dict(item) for item in messages)
    return ContextEnvelope(
        scope_id=scope_id,
        session_scope_id=session_scope_id,
        generation=generation,
        bot_id=bot_id,
        complete=True,
        order_token=str(max(int(item["context_order"]) for item in normalized)) if normalized else "",
        messages=normalized,
        current_text=current_text,
        current_message_id=current_message_id,
        current_order_token=current_order_token,
    )


def parse_context_envelope(raw: Any) -> ContextEnvelope | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1:
        return None
    strings = ("scope_id", "session_scope_id", "generation", "current_text", "current_message_id", "current_order_token", "order_token")
    if any(not isinstance(raw.get(key), str) for key in strings):
        return None
    if not all(raw.get(key) for key in ("scope_id", "session_scope_id", "generation", "current_message_id", "current_order_token")):
        return None
    if not isinstance(raw.get("complete"), bool) or not raw["complete"]:
        return None
    bot_id = raw.get("bot_id")
    if bot_id is not None and (isinstance(bot_id, bool) or not isinstance(bot_id, (int, str))):
        return None
    messages = raw.get("messages")
    if not isinstance(messages, list) or len(messages) > 500 or len(raw["current_text"]) > 100000:
        return None
    normalized: list[dict[str, Any]] = []
    seen_orders: set[int] = set()
    for item in messages:
        if not isinstance(item, dict):
            return None
        if type(item.get("context_order")) is not int or type(item.get("created_at_ms")) is not int:
            return None
        if not all(isinstance(item.get(key), str) for key in ("message_id", "author_id", "author_name", "content")):
            return None
        if type(item.get("author_is_bot")) is not bool or len(item["content"]) > 10000:
            return None
        try:
            order = int(item["context_order"])
            message_id = str(item["message_id"])
        except (KeyError, TypeError, ValueError):
            return None
        if order <= 0 or order in seen_orders or not message_id:
            return None
        seen_orders.add(order)
        normalized.append(dict(item))
    if normalized and raw["order_token"] != str(max(seen_orders)):
        return None
    if not normalized and raw["order_token"]:
        return None
    try:
        current_order = int(raw["current_order_token"])
        if current_order <= 0 or str(current_order) != raw["current_order_token"]:
            return None
    except ValueError:
        return None
    return ContextEnvelope(
        scope_id=raw["scope_id"],
        session_scope_id=raw["session_scope_id"],
        generation=raw["generation"],
        bot_id=bot_id,
        complete=True,
        order_token=raw["order_token"],
        messages=tuple(normalized),
        current_text=raw["current_text"],
        current_message_id=raw["current_message_id"],
        current_order_token=raw["current_order_token"],
    )


def messages_after_cursor(
    envelope: ContextEnvelope, cursor_order_token: str | None, cursor_message_id: str | None
) -> tuple[dict[str, Any], ...] | None:
    if not cursor_order_token or not cursor_message_id:
        return None
    try:
        cursor = int(cursor_order_token)
    except (TypeError, ValueError):
        return None
    anchor = next(
        (item for item in envelope.messages if str(item["context_order"]) == str(cursor) and item["message_id"] == cursor_message_id),
        None,
    )
    if anchor is None:
        return None
    return tuple(item for item in envelope.messages if int(item["context_order"]) > cursor)
