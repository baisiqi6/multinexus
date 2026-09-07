"""Validate disposable observations from the pinned native notification stream.

These notifications carry no client authority, including permission.lifecycle
and turn.terminal telemetry. Never use them to resolve a request or a turn.
"""

import math
from typing import Any


OBSERVATION_METHODS = frozenset(("computer-use/operation-event", "v4/telemetry/event"))
_OPERATION_FIELDS = {
    "turn-started": ("turnId", ""),
    "turn-completed": ("turnId", ""),
    "turn-failed": ("turnId", ""),
    "tool-scheduled": ("turnId toolCallId toolName", ""),
    "tool-started": ("toolCallId", "turnId toolName"),
    "session-closed": ("", ""),
}
_TELEMETRY_FIELDS = {
    "turn.started": ("", "executionKind automationId offPeakTaskId offPeakRunType taskTrigger scheduledAt"),
    "model.request.status": (
        "requestId status providerId modelId transport attempt maxAttempts",
        "providerKind providerHostname querySource queryId durationMs reason retryable statusCode delayMs nextAttempt idleMs timeoutMs"),
    "stream.chunk": ("channel chunkLength firstChunk", "assistantMessageId partId parentToolCallId"),
    "tool.lifecycle": ("phase toolCallId", "toolName durationMs errorCode errorMessage parentToolCallId childToolCallId agentId agentType childSessionId skillQualifiedName skillPluginId skillSource performance automationId"),
    "permission.lifecycle": ("phase toolCallId", "requestId toolName decision"),
    "usage.delta": (
        "inputTokens outputTokens totalTokens reasoningTokens cacheReadTokens cacheWriteTokens",
        "requestId providerId modelId providerKind providerHostname"),
    "subagent.lifecycle": ("phase agentId childSessionId background", "agentType parentToolCallId status"),
    "turn.terminal": ("status", "resultType durationMs tokenCount toolCallCount errorCode errorMessage errorRetryable turnPhase"),
    "compaction.terminal": (
        "operationId status trigger",
        "messageId summaryMessageId compactReason reason attempt maxAttempts startedAt endedAt preCompactTokenCount postCompactTokenCount truePostCompactTokenCount modelName modelProvider"),
}
_ENUMS = {
    "executionKind": ("agent", "controlOnly"), "offPeakRunType": ("init", "resume"),
    "taskTrigger": ("schedule", "manual"), "channel": ("thought", "text"),
    "decision": ("allow", "deny", "escalate", "modify"),
    "skillSource": ("agents", "zcode", "bundled", "plugin", "remote"),
    "trigger": ("manual", "auto", "partial", "reactive", "session_memory"),
    "commandStatus": ("completed", "failed", "timed_out", "cancelled", "spawn_error", "backgrounded"),
    "workspaceKind": ("local", "remote", "unknown"),
}
_KINDED_ENUMS = {
    ("model.request.status", "status"): ("model_request_started", "model_request_completed", "model_request_failed", "model_retry_scheduled", "model_stream_stalled"),
    ("tool.lifecycle", "phase"): ("scheduled", "started", "progress", "completed", "failed"),
    ("permission.lifecycle", "phase"): ("requested", "resolved", "denied"),
    ("subagent.lifecycle", "phase"): ("spawned", "stopped"),
    ("turn.terminal", "status"): ("success", "interrupted", "failed"),
    ("compaction.terminal", "status"): ("completed", "failed", "interrupted"),
}
_INTEGER_FIELDS = frozenset("version eventSeq sequenceNumber attempt maxAttempts nextAttempt chunkLength toolCallCount preCompactTokenCount postCompactTokenCount truePostCompactTokenCount totalMs permissionWaitMs commandRunMs firstOutputMs noOutputMs outputBytes commandCount fsReadMs fsWriteMs patchMatchMs fileCount totalBytes maxFileBytes hunkCount matchAttempts".split())
_NUMBER_FIELDS = frozenset("timestamp occurredAt scheduledAt durationMs delayMs idleMs timeoutMs inputTokens outputTokens totalTokens reasoningTokens cacheReadTokens cacheWriteTokens tokenCount startedAt endedAt".split())
_BOOL_FIELDS = frozenset(("firstChunk", "background", "retryable", "errorRetryable", "timedOut"))
_PERFORMANCE_FIELDS = frozenset("totalMs permissionWaitMs commandRunMs firstOutputMs noOutputMs exitCode timedOut outputBytes commandCategory commandName commandCount commandStatus commandHash fsReadMs fsWriteMs patchMatchMs fileCount totalBytes maxFileBytes hunkCount matchAttempts workspaceKind".split())


def _fields_valid(values: dict[str, Any], kind: str) -> bool:
    for key, value in values.items():
        if key in _INTEGER_FIELDS:
            valid = type(value) is int and value >= 0
        elif key in ("exitCode", "statusCode"):
            valid = type(value) is int
        elif key in _NUMBER_FIELDS:
            valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
        elif key in _BOOL_FIELDS:
            valid = type(value) is bool
        elif key == "performance":
            valid = isinstance(value, dict) and not value.keys() - _PERFORMANCE_FIELDS and _fields_valid(value, kind)
        else:
            valid = isinstance(value, str)
        if not valid:
            return False
        choices = _KINDED_ENUMS.get((kind, key), _ENUMS.get(key))
        if choices is not None and value not in choices:
            return False
        if (key.endswith("Id") or key in ("eventId", "sessionId")) and not value.strip():
            return False
    return True


def valid_observation(method: str, params: dict[str, Any]) -> bool:
    """Check the known envelope/variant fields without retaining their contents."""
    kind = params.get("kind")
    if not isinstance(kind, str):
        return False
    if method == "computer-use/operation-event":
        variants = _OPERATION_FIELDS
        base = set("eventId sequenceNumber sessionId timestamp kind".split())
        optional = set()
    elif method == "v4/telemetry/event":
        variants = _TELEMETRY_FIELDS
        base = set("version eventId eventSeq occurredAt sessionId kind".split())
        optional = {"sourceCommandId", "turnId"}
        if type(params.get("version")) is not int or params["version"] != 1:
            return False
    else:
        return False
    if kind not in variants:
        return False
    required_fields, optional_fields = variants[kind]
    required = base | set(required_fields.split())
    allowed = required | optional | set(optional_fields.split())
    if not required <= params.keys() or params.keys() - allowed or not _fields_valid(params, kind):
        return False
    if kind == "turn.started":
        if "automationId" in params and "offPeakTaskId" in params:
            return False
        if "offPeakRunType" in params and "offPeakTaskId" not in params:
            return False
    return True
