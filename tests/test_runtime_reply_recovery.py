from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from multinexus.client import DiscordClient
from multinexus.context.store import ChatContextStore
from multinexus.kook.bot import KookBridge


class RuntimeReplyRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _store(self) -> ChatContextStore:
        return ChatContextStore(str(Path(self.tmp.name) / "context.sqlite3"))

    def test_discord_recovery_replays_terminal_job_and_acknowledges(self) -> None:
        store = self._store()
        store.record_runtime_reply(
            job_id="job-discord",
            workspace_id="workspace-1",
            platform="discord",
            destination_id="123",
        )
        client = DiscordClient.__new__(DiscordClient)
        client.context_store = store
        client.agent_config = SimpleNamespace(id="mac-claude", timeout=1)
        client._coordinate_client = SimpleNamespace(
            wait_for_job_result=AsyncMock(
                return_value={
                    "status": "done",
                    "result": {"response_text": "recovered reply"},
                }
            )
        )
        client.mention_router = SimpleNamespace(
            resolve_handoff_mentions=lambda text: text
        )
        channel = object()
        client.get_channel = lambda channel_id: channel
        client.fetch_channel = AsyncMock()

        async def run() -> None:
            with patch.object(
                DiscordClient, "_send_text_chunks", new=AsyncMock()
            ) as send:
                await client._recover_runtime_replies()
                send.assert_awaited_once_with(channel, None, "recovered reply", [], [])

        asyncio.run(run())
        self.assertEqual(store.pending_runtime_replies(platform="discord"), [])

    def test_kook_recovery_replays_terminal_job_and_acknowledges(self) -> None:
        store = self._store()
        store.record_runtime_reply(
            job_id="job-kook",
            workspace_id="workspace-1",
            platform="kook",
            destination_id="channel-1",
            quote_message_id="message-1",
        )
        bridge = KookBridge.__new__(KookBridge)
        bridge.context_store = store
        bridge.config = SimpleNamespace(id="kook-agent", timeout=1)
        bridge._coordinate_client = SimpleNamespace(
            wait_for_job_result=AsyncMock(
                return_value={
                    "status": "done",
                    "result": {"response_text": "recovered reply"},
                }
            )
        )
        bridge.send_channel_message = AsyncMock()

        asyncio.run(bridge._recover_runtime_replies())

        bridge.send_channel_message.assert_awaited_once_with(
            "channel-1", "recovered reply", quote="message-1"
        )
        self.assertEqual(store.pending_runtime_replies(platform="kook"), [])


if __name__ == "__main__":
    unittest.main()
