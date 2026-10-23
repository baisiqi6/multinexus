"""Immutable request identity derived from a confirmed channel delivery."""
from __future__ import annotations

import hashlib
from typing import Any

from .bindings import DesktopProfile, verified_receipt, required_string


def runtime_key(delivery_id: str) -> str:
    return "desktop-delivery-v1:" + required_string(delivery_id)


def payload_digest(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("Invalid desktop message text.")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_submission(profile: DesktopProfile, source_alias: str, target_alias: str, text: str,
                     channel_receipt: dict, reply_to_message_id: str | None = None) -> dict[str, Any]:
    if (source_alias not in profile.bindings or target_alias not in profile.bindings
            or source_alias == target_alias or not isinstance(channel_receipt, dict)
            or channel_receipt.get("status") != "sent"):
        raise ValueError("A confirmed channel delivery and distinct fixed bindings are required.")
    delivery_id = required_string(channel_receipt.get("delivery_id"))
    message_id = required_string(channel_receipt.get("message_id"))
    if reply_to_message_id is not None:
        required_string(reply_to_message_id)
    binding = profile.bindings[target_alias]
    identity = verified_receipt(profile, source_alias, channel_receipt)
    return {
        "workspace_id": profile.workspace_id, "target_agent": binding.agent_id, "prompt": text,
        "origin": {
            "platform": "none", "destination": "desktop-channel:" + profile.channel_id,
            "session_scope_id": "desktop-delivery:" + delivery_id,
            "desktop": {
                "contract_version": 2, "source_alias": source_alias, "source_client_id": identity["client_id"],
                "source_sender_identity": identity, "source_actual_author_bot_id": channel_receipt["actual_author_bot_id"],
                "source_channel_id": profile.channel_id, "source_delivery_id": delivery_id,
                "source_message_id": message_id, "source_reply_to_message_id": reply_to_message_id,
                "recipient": target_alias, "binding": binding.as_dict(), "binding_digest": binding.digest,
                "payload_digest": payload_digest(text),
            },
        },
        "reply": {"platform": "none", "destination": "desktop-receipt:" + delivery_id},
        "idempotency_key": runtime_key(delivery_id),
    }
