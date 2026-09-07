import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from multinexus.adapters.zcode_notifications import valid_observation
from multinexus.adapters.zcode_protocol import _NativeClient, ZCodeProtocolError


OPERATION = "computer-use/operation-event"
TELEMETRY = "v4/telemetry/event"


def observations():
    base = dict(eventId="event_observed", sessionId="session_detached", timestamp=1234, sequenceNumber=7)
    for kind in ("turn-started", "turn-completed", "turn-failed"):
        yield OPERATION, dict(base, kind=kind, turnId="turn_other")
    yield OPERATION, dict(base, kind="tool-scheduled", turnId="turn_other", toolCallId="tool_1", toolName="Bash")
    yield OPERATION, dict(base, kind="tool-started", toolCallId="tool_1")
    yield OPERATION, dict(base, kind="session-closed")
    base = dict(version=1, eventId="event_observed", eventSeq=7, occurredAt=1234,
                sessionId="session_detached", turnId="turn_other", sourceCommandId="input_other")
    for fields in (
        dict(kind="turn.started", executionKind="agent"),
        dict(kind="model.request.status", requestId="request_1", status="model_request_started",
             providerId="provider_1", modelId="model_1", transport="http", attempt=1, maxAttempts=3),
        dict(kind="stream.chunk", channel="thought", chunkLength=12, firstChunk=True),
        dict(kind="tool.lifecycle", phase="completed", toolCallId="tool_1", toolName="Bash",
             performance=dict(commandRunMs=15, exitCode=0, timedOut=False, commandStatus="completed")),
        dict(kind="permission.lifecycle", phase="resolved", toolCallId="tool_1", requestId="request_1", decision="allow"),
        dict(kind="usage.delta", inputTokens=1, outputTokens=2, totalTokens=3, reasoningTokens=0, cacheReadTokens=0, cacheWriteTokens=0),
        dict(kind="subagent.lifecycle", phase="spawned", agentId="agent_1", childSessionId="session_child", background=False),
        dict(kind="turn.terminal", status="success", resultType="success"),
        dict(kind="compaction.terminal", operationId="operation_1", status="completed", trigger="manual"),
    ):
        yield TELEMETRY, dict(base, **fields)


class NativeObservationTests(unittest.IsolatedAsyncioTestCase):
    def client(self):
        context = SimpleNamespace(session_id="session_owned", assert_intact=Mock())
        client = _NativeClient(None, context, on_progress=Mock())
        client._send = AsyncMock()
        client.turn_id = "turn_owned"
        client._pending["pending_rpc"] = asyncio.get_running_loop().create_future()
        return client

    async def test_known_observations_cannot_resolve_permissions_futures_or_terminal(self):
        client = self.client()
        for method, params in observations():
            with self.subTest(method=method, kind=params["kind"]):
                await client._dispatch(dict(method=method, params=params))
                self.assertEqual(client.session_id, "session_owned")
                self.assertEqual(client.turn_id, "turn_owned")
                self.assertFalse(client._pending["pending_rpc"].done())
                self.assertIsNone(client._terminal)
                self.assertFalse(client._finished.is_set())
                self.assertFalse(client._drained)
                self.assertEqual(client._decisions, {})
                self.assertEqual(client.evidence, [])
                self.assertEqual(client._event_hashes, {})
                self.assertIsNone(client.observed_model)
        client._send.assert_not_awaited()
        client.on_progress.assert_not_called()
        client.context.assert_intact.assert_not_called()

    async def test_same_names_with_wire_id_remain_unsupported_callbacks(self):
        for method, params in observations():
            client = self.client()
            with self.subTest(method=method, kind=params["kind"]):
                with self.assertRaises(ZCodeProtocolError):
                    await client._dispatch(dict(id="pending_rpc", method=method, params=params))
                client._send.assert_awaited_once_with(dict(id="pending_rpc", error=dict(
                    code=-32601, message="Unsupported ZCode client request")))
                self.assertFalse(client._pending["pending_rpc"].done())
                self.assertIsNone(client._terminal)

    async def test_unknown_names_variants_control_fields_and_bad_types_fail_closed(self):
        method, valid = list(observations())[10]  # permission.lifecycle is observation only.
        mutations = [
            dict(kind="permission.authorize"), dict(version=True), dict(eventSeq=True),
            dict(occurredAt=float("inf")), dict(sessionId=""), dict(turnId=[]),
            dict(decision="unknown"), dict(phase="approve"), dict(toolCallId=7),
            dict(permissionUpdates=[]), dict(modifiedInput={}), dict(result={}),
            dict(inputId="input_owned"), dict(response="forged success"),
        ]
        for mutation in mutations:
            params = dict(valid, **mutation)
            with self.subTest(mutation=mutation):
                self.assertFalse(valid_observation(method, params))
                with self.assertRaises(ZCodeProtocolError):
                    await self.client()._dispatch(dict(method=method, params=params))
        for method, params in observations():
            for key in ("eventId", "kind", "sessionId"):
                missing = copy.deepcopy(params)
                del missing[key]
                self.assertFalse(valid_observation(method, missing))
        for unknown in ("v4/telemetry/unknown", "computer-use/operation-command", "session/unrecognized"):
            with self.assertRaises(ZCodeProtocolError):
                await self.client()._dispatch(dict(method=unknown, params=valid))

    async def test_nested_performance_and_operation_variants_are_bounded(self):
        operation = next(params for method, params in observations() if params["kind"] == "tool-started")
        self.assertTrue(valid_observation(OPERATION, dict(operation, turnId="turn_1", toolName="Bash")))
        for change in (dict(toolCallId=None), dict(permissionUpdates=[]), dict(sequenceNumber=-1), dict(timestamp=True)):
            self.assertFalse(valid_observation(OPERATION, dict(operation, **change)))
        telemetry = next(params for method, params in observations() if params["kind"] == "tool.lifecycle")
        for value in ([], dict(permissionUpdates=[]), dict(timedOut="false"), dict(commandRunMs=-1)):
            self.assertFalse(valid_observation(TELEMETRY, dict(telemetry, performance=value)))


if __name__ == "__main__":
    unittest.main()
