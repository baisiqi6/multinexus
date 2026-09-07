import time

import pytest

from multinexus.context.store import ChatContextStore


@pytest.fixture
def store(tmp_path):
    return ChatContextStore(str(tmp_path / "context.db"))


def _record(store, *, author_is_bot, content):
    store.record_message(
        message_id=f"msg-{content}-{author_is_bot}",
        channel_id="chan-1",
        author_id="author-1",
        author_name="TestAuthor",
        author_is_bot=author_is_bot,
        content=content,
        created_at_ms=int(time.time() * 1000),
        source="kook",
        ttl_seconds=3600,
    )


def test_empty_prefixes_deletes_nothing(store):
    _record(store, author_is_bot=True, content="[bot] hello")
    assert store.purge_bot_messages_by_prefixes(()) == 0
    assert _contents(store) == ["[bot] hello"]


def test_matching_bot_message_is_deleted(store):
    _record(store, author_is_bot=True, content="[bot] hello")
    assert store.purge_bot_messages_by_prefixes(("[bot]",)) == 1
    assert _contents(store) == []


def test_human_message_with_same_prefix_is_preserved(store):
    _record(store, author_is_bot=False, content="[bot] human copy")
    assert store.purge_bot_messages_by_prefixes(("[bot]",)) == 0
    assert _contents(store) == ["[bot] human copy"]


def test_non_matching_bot_message_is_preserved(store):
    _record(store, author_is_bot=True, content="[other] bot msg")
    assert store.purge_bot_messages_by_prefixes(("[bot]",)) == 0
    assert _contents(store) == ["[other] bot msg"]


def test_multiple_prefixes_returns_combined_count(store):
    _record(store, author_is_bot=True, content="[bot] one")
    _record(store, author_is_bot=True, content="[bot] two")
    _record(store, author_is_bot=True, content="[sys] three")
    _record(store, author_is_bot=False, content="[bot] human")
    _record(store, author_is_bot=True, content="[other] four")
    assert store.purge_bot_messages_by_prefixes(("[bot]", "[sys]")) == 3
    assert sorted(_contents(store)) == ["[bot] human", "[other] four"]


def _contents(store):
    with store._connect() as conn:
        return [row[0] for row in conn.execute("SELECT content FROM messages")]


def test_runtime_reply_outbox_replays_until_sent(store):
    store.record_runtime_reply(
        job_id="job-1",
        workspace_id="workspace-1",
        platform="discord",
        destination_id="channel-1",
        quote_message_id="message-1",
        created_at_ms=123,
    )
    assert store.pending_runtime_replies(platform="discord") == [
        {
            "job_id": "job-1",
            "workspace_id": "workspace-1",
            "platform": "discord",
            "destination_id": "channel-1",
            "quote_message_id": "message-1",
            "created_at_ms": 123,
            "attempts": 0,
            "last_error_code": "",
        }
    ]
    store.mark_runtime_reply_attempt(job_id="job-1", error_code="recovery_failed")
    assert store.pending_runtime_replies(platform="discord")[0]["attempts"] == 1
    store.mark_runtime_reply_sent(job_id="job-1", sent_at_ms=456)
    assert store.pending_runtime_replies(platform="discord") == []
