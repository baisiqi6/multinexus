"""Pinned, opt-in ZCode app-server invocation and permission callbacks.

This is the small native subset used by ZCodeAdapter, not an ACP/JSON-RPC
implementation. No native transcript or reasoning is retained here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import AgentConfig
from ..usage import unknown_usage_evidence
from .base import AdapterResult, OUTCOME_SUCCESS, failed_result, timed_out_result
from .utils import async_subprocess_kwargs, terminate_owned_process_group
from .zcode_context import SESSION_ID_RE, ZCodeContext, ZCodeContextError, prepare_context
from .zcode_notifications import OBSERVATION_METHODS, valid_observation
from .zcode_permissions import absolute_path, path_key

log = logging.getLogger(__name__)
APP_SERVER_SHA256 = "e9f1868c0fdb863537ed910ee3828b9be96b8c2fd805473f63b439e1113266b8"
CONTRACT = "zcode-app-server-0.16.5"
_EVENT_TYPES = frozenset((
    "session.created", "session.resumed", "session.updated", "session.titleUpdated", "session.closed",
    "turn.started", "turn.steerQueued", "turn.steerDrained", "turn.completed", "turn.failed",
    "message.upserted", "message.removed", "part.started", "part.delta", "part.upserted", "part.removed",
    "model.streaming", "tool.updated", "permission.requested", "permission.resolved", "userInput.requested",
    "userInput.resolved", "checkpoint.created", "rewind.triggered", "streamRecovery.updated",
))
_PASSIVE_NOTIFICATIONS = frozenset(("process/mcpTelemetry", "plugins/operationProgress", "process/resourceSample"))
_RESULT_TYPES = frozenset(("success", "cancelled", "error_max_turns", "error_max_budget",
                           "error_during_execution", "error_max_tool_calls"))
_SLASH_INPUT = re.compile(r"^[\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*/")


class ZCodeProtocolError(ValueError):
    """Safe messages only: provider payloads must not become diagnostics."""


@dataclass(frozen=True)
class _SnapshotIdentity:
    session_id: str
    event_seq: int
    model_ref: dict[str, str]


@dataclass(frozen=True)
class _Terminal:
    session_id: str
    input_id: str
    turn_id: str
    event_id: str
    event_seq: int
    result_type: str
    response: str


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _integer(value: Any) -> bool:
    return type(value) is int and value >= 0


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ZCodeProtocolError("Duplicate ZCode wire field")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ZCodeProtocolError("Non-JSON numeric constant in ZCode wire message")


def _permission_options(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for option in value:
        if not isinstance(option, dict) or not {"optionId", "kind", "name", "response"} <= option.keys():
            return False
        if option.keys() - {"optionId", "kind", "name", "response", "description"}:
            return False
        if not all(_nonempty(option[key]) for key in ("optionId", "kind", "name")):
            return False
        if "description" in option and not isinstance(option["description"], str):
            return False
        response = option["response"]
        if not isinstance(response, dict) or response.keys() - {"decision", "reason", "modifiedInput", "permissionUpdates"}:
            return False
        if response.get("decision") not in ("allow", "deny", "modify", "escalate"):
            return False
    return True


def _decode(line: bytes) -> dict[str, Any]:
    try:
        value = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ZCodeProtocolError("Invalid ZCode native NDJSON") from exc
    if not isinstance(value, dict):
        raise ZCodeProtocolError("Invalid ZCode native envelope")
    if "method" in value:
        if value.keys() - {"id", "method", "params", "trace"} or not _nonempty(value["method"]):
            raise ZCodeProtocolError("Unsupported ZCode request envelope")
        if not isinstance(value.get("params"), dict):
            raise ZCodeProtocolError("Invalid ZCode request params")
        if "trace" in value and not isinstance(value["trace"], dict):
            raise ZCodeProtocolError("Invalid ZCode trace envelope")
    elif set(value) not in ({"id", "result"}, {"id", "error"}):
        raise ZCodeProtocolError("Unsupported ZCode response envelope")
    if "id" in value and not (type(value["id"]) is int or _nonempty(value["id"])):
        raise ZCodeProtocolError("Invalid ZCode wire request id")
    return value


def _snapshot(value: Any, context: ZCodeContext, expected_session: str | None, *,
              require_build: bool = True, require_idle: bool = True) -> _SnapshotIdentity:
    if not isinstance(value, dict):
        raise ZCodeProtocolError("Missing ZCode native snapshot")
    session, settings, runtime, projection = [value.get(key) for key in ("session", "settings", "runtime", "projection")]
    if not all(isinstance(item, dict) for item in (session, settings, runtime, projection)):
        raise ZCodeProtocolError("Unsupported ZCode snapshot schema")
    if isinstance(projection.get("lastError"), dict) and projection["lastError"].get("type") == "ZCODE_RUNTIME_MODEL_UNAVAILABLE":
        raise ZCodeProtocolError("ZCode native runtime model is unavailable; no fallback")
    sid = session.get("sessionId")
    if not isinstance(sid, str) or not SESSION_ID_RE.fullmatch(sid) or expected_session and sid != expected_session:
        raise ZCodeProtocolError("ZCode native session identity mismatch")
    if value.get("protocol") != {"name": "ZCode Protocol", "version": 1}:
        raise ZCodeProtocolError("Unsupported ZCode native protocol version")
    workspace = session.get("workspace")
    if not isinstance(workspace, dict) or not isinstance(workspace.get("workspacePath"), str):
        raise ZCodeProtocolError("Missing ZCode native workspace")
    if path_key(absolute_path(workspace["workspacePath"])) != path_key(context.workspace):
        raise ZCodeProtocolError("ZCode native workspace mismatch")
    if path_key(workspace.get("workspaceKey", "")) != path_key(context.workspace):
        raise ZCodeProtocolError("ZCode native workspace key mismatch")
    if require_build and (settings.get("mode", {}).get("current") != "build" or settings.get("permission", {}).get("mode") != "build"):
        raise ZCodeProtocolError("ZCode native runtime is not in build mode")
    model = settings.get("model", {}).get("current")
    if not isinstance(model, dict) or any(model.get(key) != val for key, val in context.provider.model_ref.items()):
        raise ZCodeProtocolError("ZCode native provider/model selection mismatch")
    if not _integer(runtime.get("eventSeq")):
        raise ZCodeProtocolError("Invalid ZCode snapshot event sequence")
    if require_idle and (projection.get("status") != "idle" or runtime.get("activeTurnId")):
        raise ZCodeProtocolError("ZCode native session is not idle at invocation boundary")
    observed = {key: model[key] for key in ("providerId", "modelId")}
    if isinstance(model.get("variant"), str):
        observed["variant"] = model["variant"]
    return _SnapshotIdentity(sid, runtime["eventSeq"], observed)


class _NativeClient:
    def __init__(self, process, context: ZCodeContext, on_progress=None):
        self.process = process
        self.context = context
        self.on_progress = on_progress
        self.input_id = "input_" + uuid.uuid4().hex
        self.session_id = context.session_id
        self.turn_id: str | None = None
        self.observed_model: dict[str, str] | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._request_number = 0
        self._reader: asyncio.Task | None = None
        self._finished = asyncio.Event()
        self._failure: Exception | None = None
        self._terminal: _Terminal | None = None
        self._drained = False
        self._closing = False
        self._send_issued = False
        self._event_floor = 0
        self._event_hashes: dict[str, str] = {}
        self._decisions: dict[tuple[str, str], tuple[str, dict[str, str], dict[str, Any]]] = {}
        self.evidence: list[dict[str, Any]] = []

    async def invoke(self, prompt: str, *, resume: bool) -> _Terminal:
        self._reader = asyncio.create_task(self._read_loop())
        identity = await self._open_session(resume=resume)
        self.session_id = identity.session_id
        self.observed_model = identity.model_ref
        self.context.bind_session(identity.session_id)
        self.context.assert_intact()
        self._event_floor = identity.event_seq
        subscribed = await self._rpc("session/subscribe", {
            "sessionId": self.session_id, "deliveryKind": "desktop-continuous",
            "afterSeq": self._event_floor, "includeSnapshot": False,
        })
        self._subscribe_result(subscribed)
        self.context.assert_intact()
        self._send_issued = True
        accepted = await self._rpc("session/send", {
            "sessionId": self.session_id, "inputId": self.input_id,
            "queryId": self.input_id, "content": prompt,
        })
        if (not isinstance(accepted, dict) or accepted.get("accepted") is not True
                or accepted.get("sessionId") != self.session_id or not _integer(accepted.get("stateRevision"))):
            raise ZCodeProtocolError("ZCode did not accept the exact native session input")
        await self._finished.wait()
        if self._failure:
            raise self._failure
        if self._terminal is None:
            raise ZCodeProtocolError("Missing correlated ZCode terminal")
        await self._drain_terminal()
        self.context.assert_intact(require_persisted_session=True)
        return self._terminal

    async def _drain_terminal(self) -> None:
        # sendInput returns an admission handle; prompt_completed does not await
        # its completion promise. Read runtime finalization after the causal
        # terminal instead: finishActiveTurn runs after persistence/accounting.
        terminal = self._terminal
        while True:
            value = await self._rpc("session/read", {"sessionId": self.session_id, "messageLimit": 1})
            identity = _snapshot(value, self.context, self.session_id, require_idle=False)
            runtime, projection = value["runtime"], value["projection"]
            active_turn = runtime.get("activeTurnId")
            if active_turn is not None and active_turn != self.turn_id:
                raise ZCodeProtocolError("ZCode finalization has an unrelated active turn")
            if runtime.get("pendingRequestIds") != []:
                raise ZCodeProtocolError("ZCode finalization has pending or invalid interactions")
            status = projection.get("status")
            expected_statuses = {"idle"} if terminal.result_type == "success" else {"idle", "error"}
            if identity.event_seq >= terminal.event_seq and not active_turn:
                if status not in expected_statuses:
                    raise ZCodeProtocolError("ZCode finalization projection conflicts with its terminal")
                self.context.assert_intact()
                self._drained = True
                return
            await asyncio.sleep(0.05)  # Bounded by the existing total invocation timeout.

    async def _open_session(self, *, resume: bool) -> _SnapshotIdentity:
        if resume:
            value = await self._rpc("session/resume", {"sessionId": self.session_id,
                "workspace": self._workspace_params(), "runtimeModel": self.context.provider.runtime_model()})
            identity = _snapshot(value, self.context, self.session_id, require_build=False)
            await self._rpc("session/setMode", {"sessionId": identity.session_id, "mode": "build"})
            value = await self._rpc("session/read", {"sessionId": identity.session_id, "messageLimit": 1})
        else:
            value = await self._rpc("session/create", {"workspace": self._workspace_params(), "mode": "build",
                "model": self.context.provider.model_ref, "persistence": "deferred", "titleGenerationEnabled": False})
        return _snapshot(value, self.context, self.session_id)

    def _workspace_params(self) -> dict[str, str]:
        return {"workspacePath": str(self.context.workspace), "workspaceKey": str(self.context.workspace)}

    def _subscribe_result(self, value: Any) -> None:
        if not isinstance(value, dict) or value.get("sessionId") != self.session_id or not _integer(value.get("eventSeq")):
            raise ZCodeProtocolError("Invalid ZCode subscription response")
        events = value.get("events")
        if not isinstance(events, list):
            raise ZCodeProtocolError("Invalid ZCode subscription events")
        for event in events:
            self._event(event)

    async def _send(self, value: dict[str, Any]) -> None:
        self.process.stdin.write((json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.process.stdin.drain()

    async def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        if self._failure:
            raise self._failure
        self._request_number += 1
        rid = f"client-{self.input_id}-{self._request_number}"
        future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        try:
            await self._send({"id": rid, "method": method, "params": params})
            response = await future
        finally:
            self._pending.pop(rid, None)
        if "error" in response:
            error = response["error"]
            if not isinstance(error, dict) or type(error.get("code")) is not int or not isinstance(error.get("message"), str):
                raise ZCodeProtocolError("Malformed ZCode native error response")
            raise ZCodeProtocolError(f"ZCode native {method} failed (code {error['code']})")
        return response["result"]

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    if self._closing or self._terminal is not None and self._drained:
                        return
                    raise ZCodeProtocolError("ZCode native EOF before correlated completion")
                await self._dispatch(_decode(line))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failure = exc if isinstance(exc, (ZCodeProtocolError, ZCodeContextError)) else ZCodeProtocolError("ZCode native transport failed")
            for pending in self._pending.values():
                if not pending.done():
                    pending.set_exception(self._failure)
            self._finished.set()

    async def _dispatch(self, message: dict[str, Any]) -> None:
        if "method" not in message:
            pending = self._pending.get(message["id"])
            if pending is None or pending.done():
                raise ZCodeProtocolError("Unexpected ZCode response id")
            pending.set_result(message)
        elif "id" in message:
            await self._server_request(message)
        elif message["method"] == "session/event":
            self._event(message["params"])
        elif message["method"] == "state.updated":
            self._state(message["params"])
        elif message["method"] in OBSERVATION_METHODS:
            if not valid_observation(message["method"], message["params"]):
                raise ZCodeProtocolError("Invalid ZCode observation notification schema")
        elif message["method"] not in _PASSIVE_NOTIFICATIONS:
            raise ZCodeProtocolError("Unsupported ZCode notification")

    async def _server_request(self, message: dict[str, Any]) -> None:
        self.context.assert_intact()
        if message["method"] == "session/requestRuntimePreferences":
            result = self._preferences(message["params"])
        elif message["method"] == "interaction/requestPermission":
            result = await self._permission(message)
        else:
            await self._send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported ZCode client request"}})
            raise ZCodeProtocolError("Unsupported ZCode server request; stopped")
        await self._send({"id": message["id"], "result": result})

    def _preferences(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = params.get("sessionId")
        if set(params) != {"sessionId", "scope"} or params["scope"] not in ("runtime-materialization", "user-execution"):
            raise ZCodeProtocolError("Unsupported ZCode runtime preferences schema")
        if not isinstance(sid, str) or not SESSION_ID_RE.fullmatch(sid) or self.session_id and sid != self.session_id:
            raise ZCodeProtocolError("ZCode runtime preferences session mismatch")
        self.session_id = sid  # create requests these preferences before returning its snapshot.
        return {"nativeSearchEnhancementsEnabled": False, "memoryEnabled": False,
                "askUserQuestionAutoResolutionEnabled": False, "modelContextBudgetStrategy": "preflight-v1"}

    async def _permission(self, message: dict[str, Any]) -> dict[str, str]:
        params = message["params"]
        required = {"requestId", "sessionId", "toolCallId", "toolName", "reason", "riskLevel", "input", "options"}
        shape = required <= params.keys() and not params.keys() - required - {"turnId", "origin"}
        binding = (params.get("sessionId") == self.session_id and self.turn_id is not None and self._terminal is None
                   and params.get("turnId") == self.turn_id and "origin" not in params)
        strings = all(_nonempty(params.get(key)) for key in ("requestId", "toolCallId", "toolName", "reason"))
        strings = strings and all(len(params[key]) <= 256 for key in ("requestId", "toolCallId", "toolName"))
        options = _permission_options(params.get("options"))
        if not shape or not binding or not strings or not options or params.get("riskLevel") not in ("low", "medium", "high", "critical"):
            await self._deny_then_fail(message["id"], "Invalid ZCode permission correlation/schema")
        request_hash = hashlib.sha256(json.dumps(params, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        key = (self.session_id, params["requestId"])
        cached = self._decisions.get(key)
        if cached:
            if cached[0] != request_hash:
                await self._deny_then_fail(message["id"], "Conflicting repeated ZCode permission request")
            cached[2]["delivery_count"] += 1
            self.context.record_permissions(self.input_id, self.evidence)
            return cached[1]
        if len(self._decisions) >= 256:
            await self._deny_then_fail(message["id"], "ZCode permission evidence limit exceeded")
        decision = self.context.policy.decide(params["toolName"], params["input"])
        result = {"decision": "allow" if decision.allowed else "deny", "reason": decision.reason}
        evidence = {"session_id": self.session_id, "turn_id": self.turn_id, "input_id": self.input_id,
                    "request_id": params["requestId"], "tool_call_id": params["toolCallId"],
                    "tool": params["toolName"], "decision": result["decision"],
                    "input_summary": decision.input_summary, "delivery_count": 1}
        self._decisions[key] = (request_hash, result, evidence)
        self.evidence.append(evidence)
        self.context.record_permissions(self.input_id, self.evidence)
        if self.on_progress:
            self.on_progress({"stage": "permission", "summary": f"ZCode tool permission {result['decision']}",
                              "session_id": self.session_id})
        return result

    async def _deny_then_fail(self, wire_id: str | int, reason: str) -> None:
        await self._send({"id": wire_id, "result": {"decision": "deny", "reason": reason}})
        raise ZCodeProtocolError(reason)

    def _event(self, event: Any) -> None:
        if not isinstance(event, dict) or event.get("type") not in _EVENT_TYPES:
            raise ZCodeProtocolError("Unsupported ZCode session event")
        if event.get("sessionId") != self.session_id or not _nonempty(event.get("eventId")) or not _integer(event.get("seq")):
            raise ZCodeProtocolError("Invalid ZCode event identity")
        if event["seq"] <= self._event_floor:
            return
        digest = hashlib.sha256(json.dumps(event, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        old = self._event_hashes.get(event["eventId"])
        if old:
            if old != digest:
                raise ZCodeProtocolError("Conflicting repeated ZCode event")
            return
        self._event_hashes[event["eventId"]] = digest
        kind, payload = event["type"], event.get("payload", {})
        if not isinstance(payload, dict):
            raise ZCodeProtocolError("Invalid ZCode event payload")
        if kind == "turn.started":
            self._started(event, payload)
        elif kind in ("turn.completed", "turn.failed"):
            self._completed(event, payload)
        elif kind == "session.updated" and "mode" in payload and payload["mode"] != "build":
            raise ZCodeProtocolError("ZCode runtime mode changed outside the client policy")
        elif kind == "session.closed" and self._terminal is None:
            raise ZCodeProtocolError("ZCode session closed before a terminal result")

    def _started(self, event: dict[str, Any], payload: dict[str, Any]) -> None:
        if not self._send_issued or payload.get("inputId") != self.input_id or payload.get("queryId") != self.input_id:
            raise ZCodeProtocolError("ZCode turn.started input correlation mismatch")
        turn = event.get("turnId")
        if not _nonempty(turn) or self.turn_id and self.turn_id != turn:
            raise ZCodeProtocolError("ZCode native turn identity mismatch")
        self.turn_id = turn

    def _completed(self, event: dict[str, Any], payload: dict[str, Any]) -> None:
        if not self.turn_id or event.get("turnId") != self.turn_id or payload.get("inputId") != self.input_id:
            raise ZCodeProtocolError("ZCode terminal does not match the current input/session/turn")
        if event["type"] == "turn.failed":
            result_type, response = "error_during_execution", ""
        else:
            result_type, response = payload.get("resultType"), payload.get("response")
            if result_type not in _RESULT_TYPES or not isinstance(response, str):
                raise ZCodeProtocolError("Unsupported ZCode terminal schema")
        terminal = _Terminal(self.session_id, self.input_id, self.turn_id, event["eventId"], event["seq"], result_type, response)
        if self._terminal is not None and self._terminal != terminal:
            raise ZCodeProtocolError("Multiple ZCode terminals for the same input")
        self._terminal = terminal
        self._finished.set()

    def _state(self, params: dict[str, Any]) -> None:
        if params.get("type") != "state.updated" or params.get("scope") not in ("session", "workspace", "server") or not _integer(params.get("revision")):
            raise ZCodeProtocolError("Invalid ZCode state.updated schema")
        if params["scope"] != "session":
            return
        if params.get("sessionId") != self.session_id:
            raise ZCodeProtocolError("ZCode state session mismatch")
        patch = params.get("patch")
        if isinstance(patch, dict) and isinstance(patch.get("mode"), dict) and patch["mode"].get("current") != "build":
            raise ZCodeProtocolError("ZCode native settings mode drift")
        if params.get("reason") == "prompt_failed" and self._send_issued:
            raise ZCodeProtocolError("ZCode native input admission failed")

    async def stop(self) -> None:
        if self.session_id:
            try:
                await asyncio.wait_for(self._send({"id": "client-stop", "method": "session/stop",
                                                   "params": {"sessionId": self.session_id}}), 0.5)
            except (Exception, asyncio.CancelledError):
                pass  # The process-group cleanup below is the authoritative stop.

    async def finish_reader(self) -> None:
        if self._reader:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass


def _binaries(config: AgentConfig) -> tuple[list[str], str]:
    binary = shutil.which(config.zcode_bin) if not config.zcode_node_bin else config.zcode_bin
    if not binary or not Path(binary).is_file():
        raise ZCodeContextError("ZCode native CLI unavailable")
    if hashlib.sha256(Path(binary).read_bytes()).hexdigest() != APP_SERVER_SHA256:
        raise ZCodeContextError("ZCode native binary does not match the audited 0.16.5 bundle")
    node = shutil.which(config.zcode_node_bin or "node")
    if not node:
        raise ZCodeContextError("ZCode native Node runtime unavailable")
    return ([node, binary] if config.zcode_node_bin else [binary]), node


async def _verify_node_home(node: str, context: ZCodeContext) -> None:
    script = 'process.stdout.write(JSON.stringify({home:require("node:os").homedir(),db:process.env.ZCODE_SESSION_DB_PATH}))'
    process = await asyncio.create_subprocess_exec(node, "-e", script, env=context.environment(), cwd=str(context.workspace),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        **async_subprocess_kwargs())
    try:
        stdout, _ = await process.communicate()
        result = json.loads(stdout)
        if process.returncode != 0 or result != {"home": str(context.home), "db": str(context.storage / "session.sqlite")}:
            raise ZCodeContextError("ZCode native Node home/database isolation mismatch")
    except BaseException:
        if process.returncode is None:
            await terminate_owned_process_group(process)
        raise


async def _execute(config: AgentConfig, context: ZCodeContext, prompt: str, on_progress, *, resume: bool) -> AdapterResult:
    prefix, node = _binaries(config)
    await _verify_node_home(node, context)
    context.assert_intact()
    if os.name == "nt":
        # The isolated Python launcher corrects only its own default token
        # owner before native SQLite/config creation. -I excludes cwd imports.
        prefix = [sys.executable, "-I", str(Path(__file__).with_name("zcode_windows.py")), *prefix]
    process = await asyncio.create_subprocess_exec(*prefix, "app-server", "--cwd", str(context.workspace), "--surface", "terminal",
        env=context.environment(), cwd=str(context.workspace), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=10 * 1024 * 1024,
        **async_subprocess_kwargs())
    client = _NativeClient(process, context, on_progress)
    completed = False
    try:
        terminal = await client.invoke(prompt, resume=resume)
        client._closing = True
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 3)
        if client._reader:
            await client._reader
        if client._failure:
            raise client._failure
        if process.returncode != 0:
            raise ZCodeProtocolError("ZCode native process exited unsuccessfully")
        context.assert_intact(require_persisted_session=True)
        completed = True
        return _result(context, client, terminal, resumed=resume)
    finally:
        try:
            if not completed:
                await client.stop()
                await terminate_owned_process_group(process)
        finally:
            await client.finish_reader()


def _result(context: ZCodeContext, client: _NativeClient, terminal: _Terminal, *, resumed: bool) -> AdapterResult:
    evidence = {
        "source": CONTRACT, "session_id": terminal.session_id, "input_id": terminal.input_id,
        "turn_id": terminal.turn_id, "terminal_event_id": terminal.event_id, "result_type": terminal.result_type,
        "requested_model": context.provider.model_ref, "observed_model": client.observed_model,
        "observed_model_source": "native_snapshot.settings.model.current",
        "downstream_model_verified": False, "permission_decisions": client.evidence,
        "context_locator": str(context._locator()), "policy_sha256": context.binding["policy_sha256"],
        "bash_permission_gate": context.native_rule_evidence,
    }
    metadata = {"adapter": "zcode", "provider_evidence": context.provider.redact_value(evidence)}
    response = context.provider.redact(terminal.response).strip()
    if terminal.result_type != "success":
        return failed_result(f"ZCode error: native turn ended with {terminal.result_type}", category="provider_error",
                             session_id=terminal.session_id, resumed=resumed, metadata=metadata)
    if not response:
        return failed_result("(no response)", category="no_response", session_id=terminal.session_id, resumed=resumed, metadata=metadata)
    metadata["usage_evidence"] = unknown_usage_evidence(provider="zcode")
    return AdapterResult(text=response, session_id=terminal.session_id, resumed=resumed, metadata=metadata, outcome=OUTCOME_SUCCESS)


async def run_native(config: AgentConfig, prompt: str, *, cwd: str, timeout: float, on_progress=None,
                     resume_session_id: str | None = None) -> AdapterResult:
    context = None
    try:
        # The vendor expands custom-command shell syntax before permissionBroker.
        # Match JS trimStart whitespace (including BOM) on the actual wire text.
        if _SLASH_INPUT.match(prompt):
            raise ZCodeProtocolError("ZCode app-server does not support slash/custom commands; send plain task text")
        context = prepare_context(config, cwd, resume_session_id)
        result = await asyncio.wait_for(_execute(config, context, prompt, on_progress, resume=bool(resume_session_id)), timeout)
    except asyncio.CancelledError:
        if context:
            context.close()
        raise
    except asyncio.TimeoutError:
        result = timed_out_result(f"ZCode timeout after {timeout}s. Aborted, no handoff.",
            session_id=context.session_id if context else resume_session_id,
            metadata={"timeout": {"kind": "total", "configured_budget_seconds": timeout}})
    except (ZCodeContextError, OSError) as exc:
        log.warning("zcode native context/process unavailable: %s", type(exc).__name__)
        message = str(exc) if isinstance(exc, ZCodeContextError) else "ZCode native context/process unavailable"
        result = failed_result("ZCode error: " + message, category="unavailable",
                               session_id=context.session_id if context else resume_session_id)
    except ZCodeProtocolError as exc:
        result = failed_result("ZCode error: " + str(exc), category="protocol_error",
                               session_id=context.session_id if context else resume_session_id)
    except Exception as exc:
        log.warning("zcode native invocation failed: %s", type(exc).__name__)
        result = failed_result("ZCode error: native invocation failed", category="process_error",
                               session_id=context.session_id if context else resume_session_id)
    if context:
        try:
            context.close()
        except Exception as exc:
            log.warning("zcode native credential cleanup failed: %s", type(exc).__name__)
            return failed_result("ZCode error: private credential cleanup failed", category="process_error", session_id=context.session_id)
    if on_progress and result.outcome == OUTCOME_SUCCESS:
        on_progress({"stage": "complete", "summary": "ZCode turn completed", "session_id": result.session_id})
    return result
