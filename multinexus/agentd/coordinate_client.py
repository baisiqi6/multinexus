"""Client for submitting bridge requests via coordinate runtime.

Uses the coordinate CLI to submit requests, which creates pending jobs
that standalone agentd processes can claim. This is the bridge -> coordinate
part of the N+M runtime boundary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
import subprocess
import sys
import uuid
from typing import Any
from urllib.parse import quote, urlsplit

log = logging.getLogger(__name__)


class CoordinateRuntimeError(RuntimeError):
    """Raised when the Coordinate CLI returns a non-zero exit or non-JSON output."""


class CoordinatePreSendError(CoordinateRuntimeError):
    """The CLI process or parser rejected the call before a runtime mutation."""


class CoordinateContractError(CoordinatePreSendError):
    """Raised when the Coordinate runtime contract is missing or incompatible."""


RUNTIME_CONTRACT_VERSION = 1
REQUIRED_RUNTIME_CAPABILITIES = frozenset(
    {"claim_fencing", "agent_reconcile", "managed_lease", "terminal_report"}
)


def _validate_runtime_contract(
    raw: Any,
    *,
    transport: str,
    recoverable: bool,
) -> dict[str, Any]:
    """Validate the small, typed contract shared by CLI and HTTP clients."""
    if not isinstance(raw, dict):
        raise CoordinateContractError("coordinate runtime contract is not an object")
    if raw.get("contract_version") != RUNTIME_CONTRACT_VERSION:
        raise CoordinateContractError(
            "coordinate runtime contract version is unsupported"
        )
    if raw.get("transport") != transport:
        raise CoordinateContractError(
            "coordinate runtime contract transport does not match client"
        )
    coordinate_version = raw.get("coordinate_version")
    if not isinstance(coordinate_version, str) or not coordinate_version.strip():
        raise CoordinateContractError(
            "coordinate runtime contract has no coordinate version"
        )
    capabilities = raw.get("capabilities")
    if not isinstance(capabilities, dict):
        raise CoordinateContractError(
            "coordinate runtime contract capabilities are invalid"
        )
    missing = {
        name for name in REQUIRED_RUNTIME_CAPABILITIES if capabilities.get(name) is not True
    }
    if recoverable and capabilities.get("recoverable_claim") is not True:
        missing.add("recoverable_claim")
    if missing:
        names = ", ".join(sorted(missing))
        raise CoordinateContractError(
            f"coordinate runtime contract missing capabilities: {names}"
        )
    return {
        "contract_version": RUNTIME_CONTRACT_VERSION,
        "coordinate_version": coordinate_version.strip(),
        "transport": transport,
        "capabilities": {name: capabilities.get(name) is True for name in capabilities},
    }


# ============================ R2B HTTP transport ============================
# Loopback runtime HTTP data-plane client (R2A wire). Errors are bounded to a
# static category plus a sanitized request id; tokens, prompts, results and DB
# paths never appear in messages, reprs or logs.

class CoordinateHttpError(CoordinateRuntimeError):
    """Base class for bounded HTTP transport errors.

    ``category`` is a static wire-level category; ``request_id`` is the
    server-issued X-Coordinate-Request-Id after allowlist sanitization
    ([A-Za-z0-9_-], 1-64 chars; anything else is dropped).
    """

    def __init__(self, category: str, *, request_id: str = "") -> None:
        self.category = category
        self.request_id = request_id
        detail = f"coordinate HTTP {category}"
        if request_id:
            detail += f" (request_id={request_id})"
        super().__init__(detail)


class CoordinateHttpConfigError(CoordinateHttpError):
    """Pre-send configuration/contract error: invalid URL, token file or
    transport selection. Safe to retry later; never latches the worker."""


class CoordinateHttpTimeoutError(CoordinateHttpError):
    """The request was sent but no response arrived in time."""


class CoordinateHttpConnectionError(CoordinateHttpError):
    """The connection could not be established (refused, unreachable, DNS for
    a rejected URL) — the request was never sent. Claim keeps the ordinary
    bounded poll for this class; it never latches the worker."""


class CoordinateHttpDisconnectedError(CoordinateHttpError):
    """The request was sent but the connection dropped before a response
    (reset, server disconnect). Claim treats this as authority-uncertain:
    the server may have acted on the request."""


class CoordinateHttpServerError(CoordinateHttpError):
    """Authoritative 5xx from the server (503 unavailable, 500 internal)."""


class CoordinateHttpMalformedError(CoordinateHttpError):
    """Response failed status/content-type/body-size/JSON/envelope checks."""


class CoordinateHttpTerminalError(CoordinateHttpError):
    """Authoritative 4xx (except 409): contract/config error, never retried."""

    def __init__(self, category: str, *, status: int, request_id: str = "") -> None:
        self.status = status
        super().__init__(category, request_id=request_id)


class CoordinateHttpConflictError(CoordinateHttpTerminalError):
    """HTTP 409 conflict: a real stale attempt/lease/mutation conflict. Never
    guessed as success and never retried with the same body."""


class CoordinateClaimAuthorityUncertainError(CoordinateHttpError):
    """A claim request was sent but no authoritative response arrived
    (timeout/reset/malformed/5xx). Without fencing the worker must stop
    claiming, stay alive and wait for an operator or stop signal. With a
    supported ``claim_request_id``, the worker may later replay the same
    operation key after an agent-scoped authority probe; it must never issue
    an unkeyed or new-key claim while the outcome is uncertain."""


class CoordinateClaimReplayConflictError(CoordinateRuntimeError):
    """Authoritative replay conflict/expiry; worker must stay fail-closed."""


class CoordinateClaimTerminalError(CoordinateRuntimeError):
    """Keyed claim was authoritatively rejected; worker must stay latched."""


# R2A wire limits mirrored from the server.
_HTTP_MAX_BODY_BYTES = 1024 * 1024
_HTTP_ATTEMPTS = 3  # 1 + 2 bounded retries
_HTTP_PER_ATTEMPT_TIMEOUT_SECONDS = 10.0
_HTTP_RETRY_BACKOFF_SECONDS = 0.25
# Renew budget: 2 attempts * 1.5s + 0.25s backoff = 3.25s, below the worker's
# RENEWAL_SAFETY_MARGIN_SECONDS = 5.
_RENEW_ATTEMPTS = 2
_RENEW_PER_ATTEMPT_TIMEOUT_SECONDS = 1.5
_RENEW_BACKOFF_SECONDS = 0.25
_CLAIM_TIMEOUT_SECONDS = 10.0

_LOOPBACK_IPV4_RE = re.compile(r"^127\.(?:[0-9]{1,3}\.){2}[0-9]{1,3}$")

# Server-issued request ids are untrusted header bytes: only an allowlist of
# [A-Za-z0-9_-] up to 64 chars may ever reach exception messages, reprs or
# logs; anything else is dropped.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _sanitize_request_id(raw: Any) -> str:
    """Bound a server-issued request id to the allowlist; invalid values
    become the empty string and never appear in error text."""
    if isinstance(raw, str) and _REQUEST_ID_RE.fullmatch(raw):
        return raw
    return ""


def _validate_envelope_coherence(
    *,
    status: int,
    envelope: dict[str, Any],
    request_id: str,
) -> None:
    """Enforce the exact R2A envelope coherence rules.

    Success: HTTP 200 with ``ok is True``, ``error is None`` and ``data`` an
    object. Error: non-200 with ``ok is False``, ``data is None`` and
    ``error`` exactly ``{"code": str, "message": str}``. Anything else is
    malformed. Conflict is decided solely by HTTP 409 inside ``_classify``;
    a body-level ``code=conflict`` on another status is never a conflict.
    """
    if status == 200:
        if (
            envelope.get("ok") is not True
            or envelope.get("error") is not None
            or not isinstance(envelope.get("data"), dict)
        ):
            raise CoordinateHttpMalformedError(
                "invalid success envelope", request_id=request_id
            )
        return
    error = envelope.get("error")
    if (
        envelope.get("ok") is not False
        or envelope.get("data") is not None
        or not isinstance(error, dict)
        or set(error) != {"code", "message"}
        or not isinstance(error.get("code"), str)
        or not isinstance(error.get("message"), str)
    ):
        raise CoordinateHttpMalformedError(
            "invalid error envelope", request_id=request_id
        )


def validate_http_base_url(value: Any) -> str:
    """Validate a coordinate HTTP base URL: ``http://`` + numeric loopback
    IPv4 (127/8) or ``::1`` + explicit port, root path only, no userinfo,
    query or fragment. DNS names and non-loopback hosts are rejected because
    the R2A listener only ever binds loopback; cross-host agentd must use an
    SSH local-forward and point this URL at the local loopback port.

    Returns the canonical form without a trailing slash, so route paths join
    as single-slash ``/v1/...`` even when the config wrote ``...:port/``.
    """
    if not isinstance(value, str) or not value.strip():
        raise CoordinateHttpConfigError(
            "coordinate_http_base_url is required for the http transport"
        )
    value = value.strip()
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise CoordinateHttpConfigError(
            "coordinate_http_base_url is not a valid http URL"
        ) from exc
    if parts.scheme != "http":
        raise CoordinateHttpConfigError(
            "coordinate_http_base_url must use http:// (loopback only)"
        )
    if parts.username is not None or parts.password is not None:
        raise CoordinateHttpConfigError(
            "coordinate_http_base_url must not contain userinfo"
        )
    if parts.query or parts.fragment:
        raise CoordinateHttpConfigError(
            "coordinate_http_base_url must not contain query or fragment"
        )
    if parts.path not in ("", "/"):
        raise CoordinateHttpConfigError(
            "coordinate_http_base_url must use the root path"
        )
    host = parts.hostname
    if host is None:
        raise CoordinateHttpConfigError(
            "coordinate_http_base_url requires a loopback host"
        )
    if port is None or isinstance(port, bool) or not 1 <= port <= 65535:
        raise CoordinateHttpConfigError(
            "coordinate_http_base_url requires an explicit port 1-65535"
        )
    canonical = value[:-1] if value.endswith("/") else value
    if host == "::1":
        return canonical
    if _LOOPBACK_IPV4_RE.match(host):
        octets = host.split(".")
        if all(int(octet) <= 255 for octet in octets):
            return canonical
    raise CoordinateHttpConfigError(
        "coordinate_http_base_url must be a numeric loopback address "
        "(127.0.0.0/8 or ::1); DNS names and non-loopback hosts are rejected"
    )


def validate_http_token_file(value: Any) -> str:
    """Validate and read a coordinate HTTP token file.

    The file must be an absolute regular non-symlink path that is not
    world-readable and not group/world-writable (``0640 root:multinexus`` is
    allowed). The check and the read happen on the same fd opened with
    ``O_NOFOLLOW`` (where supported), closing the lstat/read replacement
    window. The token is returned to the caller and kept in memory only; it
    never appears in error messages, reprs or logs.
    """
    if not isinstance(value, str) or not value.strip():
        raise CoordinateHttpConfigError(
            "coordinate_http_token_file is required for the http transport"
        )
    value = value.strip()
    if not os.path.isabs(value):
        raise CoordinateHttpConfigError(
            "coordinate_http_token_file must be an absolute path"
        )
    try:
        path_stat = os.lstat(value)
    except OSError as exc:
        raise CoordinateHttpConfigError(
            "coordinate_http_token_file is not readable"
        ) from exc
    if stat.S_ISLNK(path_stat.st_mode):
        raise CoordinateHttpConfigError(
            "coordinate_http_token_file must not be a symlink"
        )
    if sys.platform == "win32":
        # Query the path ACL before opening it.  A replacement after this
        # point is caught by the lstat/fstat identity comparison below, so
        # the ACL evidence cannot describe a different inode than the fd.
        _validate_windows_private_acl(value)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = None
    try:
        fd = os.open(value, flags)
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != (path_stat.st_dev, path_stat.st_ino):
            raise CoordinateHttpConfigError(
                "coordinate_http_token_file changed during validation"
            )
        if not stat.S_ISREG(st.st_mode):
            raise CoordinateHttpConfigError(
                "coordinate_http_token_file must be a regular file"
            )
        if sys.platform != "win32":
            mode = stat.S_IMODE(st.st_mode)
            if mode & 0o004:
                raise CoordinateHttpConfigError(
                    "coordinate_http_token_file must not be world-readable"
                )
            if mode & 0o022:
                raise CoordinateHttpConfigError(
                    "coordinate_http_token_file must not be group or world writable"
                )
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            fd = None  # fh owns the descriptor from here on
            content = fh.read()
    except CoordinateHttpConfigError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise CoordinateHttpConfigError(
            "coordinate_http_token_file is not readable"
        ) from exc
    finally:
        if fd is not None:
            os.close(fd)
    token = content.strip()
    if not token:
        raise CoordinateHttpConfigError(
            "coordinate_http_token_file must contain a non-empty token"
        )
    return token


def _validate_windows_private_acl(path: str) -> None:
    """Fail closed unless the Windows DACL is private to trusted principals.

    Python exposes synthetic POSIX mode bits (normally ``0666``) for NTFS
    files, so those bits cannot establish confidentiality.  Ask Windows for
    effective explicit/inherited access rules as SIDs instead.  Only the file
    owner, current process identity, SYSTEM and BUILTIN\\Administrators may
    have allow ACEs; at least one trusted ACE must grant ``ReadData``.
    """
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    powershell = os.path.join(
        system_root,
        "System32",
        "WindowsPowerShell",
        "v1.0",
        "powershell.exe",
    )
    script = r"""
$ErrorActionPreference = 'Stop'
$acl = Get-Acl -LiteralPath $env:COORDINATE_TOKEN_PATH
$sidType = [System.Security.Principal.SecurityIdentifier]
$owner = $acl.GetOwner($sidType).Value
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$rules = @($acl.GetAccessRules($true, $true, $sidType) | ForEach-Object {
  @{ sid = $_.IdentityReference.Value;
     type = $_.AccessControlType.ToString();
     rights = [Int64]$_.FileSystemRights }
})
@{ owner = $owner; current = $current; rules = $rules } |
  ConvertTo-Json -Compress -Depth 4
"""
    try:
        child_env = {
            key: os.environ[key]
            for key in (
                "SystemRoot",
                "WINDIR",
                "SystemDrive",
                "COMSPEC",
                "PATH",
                "PATHEXT",
                "PSModulePath",
                "TEMP",
                "TMP",
            )
            if key in os.environ
        }
        child_env["COORDINATE_TOKEN_PATH"] = path
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=child_env,
        )
        if result.returncode != 0:
            raise ValueError("ACL query failed")
        document = json.loads(result.stdout)
        owner = document.get("owner")
        current = document.get("current")
        rules = document.get("rules")
        if not isinstance(owner, str) or not isinstance(current, str):
            raise ValueError("ACL identity missing")
        if not isinstance(rules, list):
            raise ValueError("ACL rules missing")
        allowed = {owner, current, "S-1-5-18", "S-1-5-32-544"}
        trusted_read = False
        for rule in rules:
            if not isinstance(rule, dict):
                raise ValueError("ACL rule malformed")
            sid = rule.get("sid")
            rule_type = rule.get("type")
            rights = rule.get("rights")
            if not isinstance(sid, str) or not isinstance(rights, int):
                raise ValueError("ACL rule malformed")
            if rule_type != "Allow":
                continue
            if sid not in allowed:
                raise ValueError("ACL grants an untrusted principal")
            if rights & 0x1:  # FileSystemRights.ReadData
                trusted_read = True
        if not trusted_read:
            raise ValueError("ACL has no trusted read grant")
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
        raise CoordinateHttpConfigError(
            "coordinate_http_token_file Windows ACL is not private"
        ) from exc


def normalize_recovery_reason(value: Any) -> str:
    """Validate and normalize a recovery reason per the Coordinate contract.

    Returns the stripped reason. Raises ``CoordinateRuntimeError`` on any
    contract violation so callers fail closed before invoking subprocesses.
    """
    if not isinstance(value, str):
        raise CoordinateRuntimeError("recovery_reason must be a string")
    reason = value.strip()
    if not reason:
        raise CoordinateRuntimeError("recovery_reason must be non-empty after strip")
    if any(ord(ch) < 32 for ch in reason):
        raise CoordinateRuntimeError("recovery_reason must not contain control characters")
    if len(reason) > 512:
        raise CoordinateRuntimeError("recovery_reason must be at most 512 characters")
    return reason


def normalize_claim_reap_policy(
    reap_mode: Any = "global",
    reap_reason: Any = None,
) -> tuple[str, str | None]:
    """Validate and normalize a claim reap policy per the Coordinate P1 contract.

    Exact modes: ``global`` accepts only ``None`` reason and returns
    ``("global", None)``. ``none`` requires a non-blank, stripped-stable,
    C0/DEL-free string reason of at most 512 code points and 2048 UTF-8 bytes.
    Any other combination fails closed before subprocess invocation.
    """
    if reap_mode not in ("global", "none"):
        raise CoordinateRuntimeError(
            f"reap_mode must be 'global' or 'none', got {reap_mode!r}"
        )
    if reap_mode == "global":
        if reap_reason is not None:
            raise CoordinateRuntimeError(
                "global reap_mode must not carry reap_reason"
            )
        return ("global", None)
    if not isinstance(reap_reason, str):
        raise CoordinateRuntimeError(
            f"none reap_mode requires a string reap_reason, got {type(reap_reason).__name__}"
        )
    reason = reap_reason.strip()
    if not reason:
        raise CoordinateRuntimeError(
            "none reap_mode requires a non-blank reap_reason"
        )
    if reap_reason != reason:
        raise CoordinateRuntimeError(
            "reap_reason must be stripped-stable (no leading/trailing whitespace)"
        )
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in reason):
        raise CoordinateRuntimeError(
            "reap_reason must not contain C0 or DEL control characters"
        )
    if len(reason) > 512:
        raise CoordinateRuntimeError(
            "reap_reason must be at most 512 code points"
        )
    if len(reason.encode("utf-8")) > 2048:
        raise CoordinateRuntimeError(
            "reap_reason must be at most 2048 UTF-8 bytes"
        )
    return "none", reason


def _require_non_empty_workspace(workspace_id: str) -> str:
    """Fail closed before any subprocess invocation when workspace is missing."""
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise CoordinateRuntimeError("workspace_id is required")
    return workspace_id.strip()


class CoordinateRuntimeClient:
    """Submit bridge requests to coordinate runtime.

    Wraps the coordinate CLI:
      runtime request submit <workspace> --target-agent <id> --prompt <text>
        --origin-json <json> --reply-json <json>
    """

    def __init__(
        self,
        *,
        cli_path: str,
        db_path: str,
    ):
        self.cli_path = cli_path
        self.db_path = db_path

        if sys.platform == "win32" and cli_path.endswith(".py"):
            self._base_cmd = [sys.executable, cli_path]
        else:
            self._base_cmd = [cli_path]

    def _base_env(self) -> dict[str, str]:
        env = os.environ.copy()
        # Coordinate console script reads MULTI_AGENT_COORDINATOR_DB as its
        # default --db; MAC_DB is retained for legacy wrappers (mac.sh etc.).
        env["MULTI_AGENT_COORDINATOR_DB"] = self.db_path
        env["MAC_DB"] = self.db_path
        return env

    async def get_runtime_contract(self, *, recoverable: bool = False) -> dict[str, Any]:
        """Probe and validate the CLI runtime contract before claiming work.

        The CLI transport uses existing read-only help/version commands so this
        probe does not expand the public CLI parser contract.
        """
        try:
            version = await asyncio.to_thread(
                self._run_cli_text,
                [*self._base_cmd, "--version"],
            )
            claim_help = await asyncio.to_thread(
                self._run_cli_text,
                [*self._base_cmd, "runtime", "job", "claim", "--help"],
            )
            reconcile_help = await asyncio.to_thread(
                self._run_cli_text,
                [*self._base_cmd, "runtime", "agent", "reconcile", "--help"],
            )
            report_help = await asyncio.to_thread(
                self._run_cli_text,
                [*self._base_cmd, "runtime", "job", "report", "--help"],
            )
            lease_help = await asyncio.to_thread(
                self._run_cli_text,
                [*self._base_cmd, "runtime", "job", "lease", "renew", "--help"],
            )
            capabilities = {
                "claim_fencing": "--claim-request-id" in claim_help,
                "agent_reconcile": "runtime agent reconcile" in reconcile_help,
                "recoverable_claim": "--recoverable" in claim_help,
                "managed_lease": "runtime job lease renew" in lease_help,
                "terminal_report": "runtime job report" in report_help,
            }
            contract = {
                "contract_version": RUNTIME_CONTRACT_VERSION,
                "coordinate_version": version.strip()[:64] or "unknown",
                "transport": "cli",
                "capabilities": capabilities,
            }
            return _validate_runtime_contract(
                contract,
                transport="cli",
                recoverable=recoverable,
            )
        except CoordinateContractError:
            raise
        except CoordinateRuntimeError as exc:
            raise CoordinateRuntimeError(
                "coordinate runtime contract probe unavailable"
            ) from exc

    async def resolve_channel_workspace(
        self,
        *,
        platform: str,
        channel_id: str,
    ) -> str | None:
        """Resolve (platform, channel_id) -> workspace_id via Coordinate.

        Returns the workspace id when bound, None when unbound. Any Coordinate
        error or malformed envelope raises CoordinateRuntimeError; callers must
        not downgrade it to unbound.
        """
        if not isinstance(platform, str) or not platform.strip():
            raise CoordinateRuntimeError("platform is required")
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise CoordinateRuntimeError("channel_id is required")
        canonical_platform = platform.strip().lower()
        canonical_channel_id = channel_id.strip()
        cmd = [
            *self._base_cmd,
            "workspace", "channel", "resolve",
            canonical_platform,
            canonical_channel_id,
        ]
        result = await asyncio.to_thread(self._run_cli, cmd)
        if not isinstance(result, dict):
            raise CoordinateRuntimeError(
                f"channel resolve returned non-dict result: {result!r}"
            )
        if result.get("error"):
            raise CoordinateRuntimeError(
                f"channel resolve runtime error: {result['error']}"
            )
        status = result.get("status")
        binding = result.get("binding")
        if status == "unbound" and binding is None:
            return None
        if status == "bound" and isinstance(binding, dict):
            workspace_id = binding.get("workspace_id")
            if not isinstance(workspace_id, str) or not workspace_id.strip():
                raise CoordinateRuntimeError(
                    "channel resolve returned bound envelope without a non-empty workspace_id"
                )
            binding_platform = str(binding.get("platform", "")).strip().lower()
            binding_channel_id = str(binding.get("channel_id", "")).strip()
            if binding_platform != canonical_platform:
                raise CoordinateRuntimeError(
                    f"channel resolve platform mismatch: "
                    f"expected {canonical_platform!r}, got {binding_platform!r}"
                )
            if binding_channel_id != canonical_channel_id:
                raise CoordinateRuntimeError(
                    f"channel resolve channel_id mismatch: "
                    f"expected {canonical_channel_id!r}, got {binding_channel_id!r}"
                )
            return workspace_id.strip()
        raise CoordinateRuntimeError(
            f"channel resolve returned unexpected envelope: {result!r}"
        )

    async def submit_request(
        self,
        *,
        target_agent: str,
        prompt: str,
        origin_json: dict,
        reply_json: dict,
        workspace_id: str,
        task_id: str = "",
        message_id: str = "",
        idempotency_key: str = "",
    ) -> dict:
        """Submit a bridge request to coordinate. Returns the coordinate response dict."""
        workspace = _require_non_empty_workspace(workspace_id)
        cmd = [
            *self._base_cmd,
            "runtime", "request", "submit",
            workspace,
            "--target-agent", target_agent,
            "--prompt", prompt,
            "--origin-json", json.dumps(origin_json, ensure_ascii=False),
            "--reply-json", json.dumps(reply_json, ensure_ascii=False),
        ]
        if task_id:
            cmd.extend(["--task-id", task_id])
        idempotency = idempotency_key or message_id
        if idempotency:
            cmd.extend(["--idempotency-key", idempotency])

        log.info("coordinate submit: agent=%s msg=%s workspace=%s", target_agent, message_id, workspace)

        return await asyncio.to_thread(self._run_cli, cmd)

    async def claim_job(
        self,
        *,
        agent_id: str,
        claim_request_id: str = "",
        recoverable: bool = False,
        recovery_reason: str = "",
        prior_process_stopped: bool = False,
        reap_mode: str = "global",
        reap_reason: Any = None,
    ) -> dict[str, Any]:
        """Claim the next pending job for this agent. Returns the full result envelope.

        recoverable=True (operator recovery mode only) also claims timed_out+
        recoverable jobs. It requires both a non-empty ``recovery_reason`` and
        ``prior_process_stopped=True`` as evidence; missing evidence fails closed
        before the subprocess is invoked. recoverable=False rejects any supplied
        evidence. Default False = only pending jobs, so normal launchd agentd never
        auto-reclaims a stuck timed_out job (8.4.3 P1 #1).

        reap_mode="global" (default) preserves legacy argv exactly and omits both
        Coordinate reap flags. reap_mode="none" appends ``--reap-mode none
        --reap-reason <reason>``. Policy is validated inside this method before
        any subprocess invocation; callers must never bypass it.

        Even when ``claimed`` is False, the full inner result dict is returned so
        the caller can preserve ``reason`` and blocker diagnostics.

        The caller must validate ``result["execution_context"]`` before invoking
        an adapter; this client preserves the raw Coordinate response.
        """

        # Validate and normalize reap policy before any subprocess invocation.
        normalized_mode, normalized_reap_reason = normalize_claim_reap_policy(
            reap_mode=reap_mode,
            reap_reason=reap_reason,
        )

        if recoverable:
            normalized_reason = normalize_recovery_reason(recovery_reason)
            if prior_process_stopped is not True:
                raise CoordinateRuntimeError(
                    "recoverable claim requires prior_process_stopped=True"
                )
        else:
            if recovery_reason != "":
                raise CoordinateRuntimeError(
                    "non-recoverable claim must not carry recovery_reason"
                )
            if prior_process_stopped is not False:
                raise CoordinateRuntimeError(
                    "non-recoverable claim must not carry prior_process_stopped"
                )
            normalized_reason = ""

        cmd = [
            *self._base_cmd,
            "runtime", "job", "claim",
            "--agent-id", agent_id,
        ]
        if claim_request_id:
            cmd.extend(["--claim-request-id", claim_request_id])
        if recoverable:
            cmd.append("--recoverable")
            cmd.extend(["--recovery-reason", normalized_reason])
            cmd.append("--prior-process-stopped")
        if normalized_mode == "none":
            assert normalized_reap_reason is not None
            cmd.extend(
                ["--reap-mode", "none", "--reap-reason", normalized_reap_reason]
            )
        try:
            result = await asyncio.to_thread(self._run_cli, cmd)
        except CoordinateRuntimeError as exc:
            if isinstance(exc, CoordinatePreSendError):
                raise
            if claim_request_id and ("claim replay" in str(exc) or "conflict" in str(exc)):
                raise CoordinateClaimReplayConflictError(str(exc)) from exc
            if claim_request_id:
                raise CoordinateClaimAuthorityUncertainError(
                    "claim response uncertain"
                ) from exc
            raise
        if not isinstance(result, dict):
            raise CoordinateRuntimeError(
                f"coordinate claim for {agent_id} returned non-dict result"
            )
        inner = result.get("result")
        if not isinstance(inner, dict):
            raise CoordinateRuntimeError(
                f"coordinate claim for {agent_id} returned missing/invalid result envelope"
            )
        return inner

    async def reconcile_agent(self, *, agent_id: str) -> dict[str, Any]:
        """Read the agent's minimal active-lease authority snapshot.

        This is intentionally read-only and returns only the server's
        agent-scoped ``active_leases`` list.  It is used after an uncertain
        claim to prove that no lease remains before another claim is allowed.
        """
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise CoordinateRuntimeError("agent_id is required")
        result = await asyncio.to_thread(
            self._run_cli,
            [
                *self._base_cmd,
                "runtime",
                "agent",
                "reconcile",
                "--agent-id",
                agent_id.strip(),
            ],
        )
        if not isinstance(result, dict):
            raise CoordinateRuntimeError("agent reconcile returned non-dict result")
        snapshot = result.get("result", result)
        if not isinstance(snapshot, dict):
            raise CoordinateRuntimeError("agent reconcile returned invalid snapshot")
        active = snapshot.get("active_leases")
        if not isinstance(active, list):
            raise CoordinateRuntimeError("agent reconcile returned invalid active_leases")
        return snapshot

    async def report_job(
        self,
        *,
        job_id: str,
        agent_id: str,
        status: str,
        result_json: dict,
        attempt_token: int | None = None,
        lease_id: str | None = None,
    ) -> dict:
        """Report job result back to coordinate."""
        cmd = [
            *self._base_cmd,
            "runtime", "job", "report",
            job_id,
            "--agent-id", agent_id,
            "--status", status,
            "--result-json", json.dumps(result_json, ensure_ascii=False),
        ]
        if attempt_token is not None:
            cmd.extend(["--attempt-token", str(attempt_token)])
        if lease_id is not None:
            cmd.extend(["--lease-id", lease_id])
        return await asyncio.to_thread(self._run_cli, cmd)

    async def record_progress(
        self,
        *,
        job_id: str,
        agent_id: str,
        stage: str = "",
        summary: str = "",
        session_id: str = "",
        attempt_token: int | None = None,
        lease_id: str | None = None,
    ) -> dict:
        """Record a bounded progress checkpoint for a running job."""
        cmd = [
            *self._base_cmd,
            "runtime", "job", "progress",
            job_id,
            "--agent-id", agent_id,
        ]
        if stage:
            cmd.extend(["--stage", stage])
        if summary:
            cmd.extend(["--summary", summary])
        if session_id:
            cmd.extend(["--session-id", session_id])
        if attempt_token is not None:
            cmd.extend(["--attempt-token", str(attempt_token)])
        if lease_id is not None:
            cmd.extend(["--lease-id", lease_id])
        return await asyncio.to_thread(self._run_cli, cmd)

    async def renew_lease(
        self,
        *,
        job_id: str,
        agent_id: str,
        attempt_token: int,
        lease_id: str,
    ) -> dict:
        """Renew one managed lease."""
        cmd = [
            *self._base_cmd,
            "runtime", "job", "lease", "renew",
            job_id,
            "--agent-id", agent_id,
            "--attempt-token", str(attempt_token),
            "--lease-id", lease_id,
        ]
        return await asyncio.to_thread(self._run_cli, cmd)

    async def reap_leases(
        self,
        *,
        actor: str = "agentd",
        batch_size: int = 100,
    ) -> dict:
        """Expire due active leases and make their jobs recoverable."""
        cmd = [
            *self._base_cmd,
            "runtime", "job", "lease", "reap",
            "--actor", actor,
            "--batch-size", str(batch_size),
        ]
        return await asyncio.to_thread(self._run_cli, cmd)

    async def wait_for_job_result(
        self,
        *,
        job_id: str,
        workspace_id: str,
        poll_interval: float = 2.0,
        timeout: float = 1800.0,
    ) -> dict | None:
        """Poll coordinate until a job reaches a terminal state, then return the result.

        Returns the job dict with result_json populated, or None on timeout.
        """
        import time as _time
        start = _time.monotonic()
        workspace = _require_non_empty_workspace(workspace_id)
        while _time.monotonic() - start < timeout:
            job = await self._get_job(job_id, workspace_id=workspace)
            if job is None:
                await asyncio.sleep(poll_interval)
                continue
            status = job.get("status", "")
            if status in ("done", "failed", "timed_out"):
                return job
            await asyncio.sleep(poll_interval)
        return None

    async def _get_job(self, job_id: str, *, workspace_id: str) -> dict | None:
        """Fetch a single job's current state from coordinate."""
        workspace = _require_non_empty_workspace(workspace_id)
        cmd = [
            *self._base_cmd,
            "job", "list",
            "--workspace-id", workspace,
        ]
        result = await asyncio.to_thread(self._run_cli, cmd)
        if not isinstance(result, dict):
            raise CoordinateRuntimeError(
                f"coordinate job list for {job_id} returned non-dict result"
            )
        jobs = result.get("jobs", [])
        if not isinstance(jobs, list):
            raise CoordinateRuntimeError(
                f"coordinate job list for {job_id} returned non-list jobs"
            )
        for job in jobs:
            if job.get("id") == job_id:
                return job
        return None

    def _run_cli(self, cmd: list[str]) -> dict:
        """Run the Coordinate CLI and return its JSON output as a dict.

        All failure modes (non-zero exit, timeout, non-JSON output, OS errors,
        and malformed envelopes) are normalized into a bounded
        CoordinateRuntimeError so the agentd loop can back off instead of
        spinning or crashing.
        """
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                env=self._base_env(),
            )
        except subprocess.TimeoutExpired:
            raise CoordinateRuntimeError("coordinate CLI timed out")
        except OSError as exc:
            raise CoordinatePreSendError(
                f"coordinate CLI execution failed: {exc}"
            ) from exc
        except Exception as exc:
            raise CoordinateRuntimeError(f"coordinate CLI unexpected error: {exc}")

        if proc.returncode != 0:
            raw_stderr = proc.stderr or ""
            stderr = raw_stderr[:300]
            if proc.returncode == 2 and (
                "unrecognized arguments:" in raw_stderr
                or "invalid choice:" in raw_stderr
            ):
                raise CoordinateContractError(
                    "coordinate CLI rejected the runtime command contract"
                )
            raise CoordinateRuntimeError(
                f"coordinate CLI exit {proc.returncode}: {stderr}"
            )

        stdout = proc.stdout.strip()
        if not stdout:
            raise CoordinateRuntimeError("coordinate CLI returned empty stdout")

        try:
            result = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise CoordinateRuntimeError(f"coordinate CLI non-JSON: {exc}")

        if not isinstance(result, dict):
            raise CoordinateRuntimeError("coordinate CLI returned non-object JSON")

        # If Coordinate itself reported a runtime error, surface it as a bounded
        # exception rather than making every caller inspect an error dict.
        if result.get("error"):
            raise CoordinateRuntimeError(
                f"coordinate runtime error: {result['error']}"
            )

        return result

    def _run_cli_text(self, cmd: list[str]) -> str:
        """Run a read-only CLI probe and return bounded text output."""
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                env=self._base_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise CoordinateRuntimeError("coordinate CLI probe timed out") from exc
        except OSError as exc:
            raise CoordinatePreSendError(
                f"coordinate CLI probe failed: {exc}"
            ) from exc
        if proc.returncode != 0:
            raw_stderr = proc.stderr or ""
            if proc.returncode == 2 and (
                "unrecognized arguments:" in raw_stderr
                or "invalid choice:" in raw_stderr
            ):
                raise CoordinateContractError(
                    "coordinate CLI runtime contract is unsupported"
                )
            raise CoordinateRuntimeError(
                f"coordinate CLI probe exit {proc.returncode}: {raw_stderr[:300]}"
            )
        return (proc.stdout or "")[:4096]


class CoordinateHttpRuntimeClient:
    """Loopback runtime HTTP data-plane client (R2B).

    Implements the same public async method contract as
    :class:`CoordinateRuntimeClient` over the R2A wire: channel resolve,
    submit, claim, progress, terminal report, lease renew, job get/poll.
    Every response passes status/content-type/body-size/JSON-object/exact
    three-field envelope coherence checks; redirects are never followed;
    errors surface only a static category plus the sanitized request id.
    Proxy env vars are never inherited (``trust_env=False``), retries are
    bounded per method. A claim is never transport-retried with a new
    operation; a worker may replay the same fenced key only after the
    server-side contract allows it.
    """

    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        token_file: str,
    ) -> None:
        self.base_url = validate_http_base_url(base_url)
        if not isinstance(client_id, str) or not client_id.strip():
            raise CoordinateHttpConfigError(
                "coordinate_http_client_id is required for the http transport"
            )
        self.client_id = client_id.strip()
        # The token lives in memory only; __repr__/errors/logs never echo it.
        self._token = validate_http_token_file(token_file)

    def __repr__(self) -> str:
        return (
            f"CoordinateHttpRuntimeClient(base_url={self.base_url!r}, "
            f"client_id={self.client_id!r})"
        )

    # -- wire plumbing ------------------------------------------------------

    async def _request_once(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout_seconds: float = _HTTP_PER_ATTEMPT_TIMEOUT_SECONDS,
    ) -> tuple[int, dict[str, Any], str]:
        """Send one request and return (status, envelope, request_id).

        Redirects are never followed (``allow_redirects=False``): a 3xx is a
        malformed/static outcome. The envelope must satisfy the exact R2A
        coherence rules (:func:`_validate_envelope_coherence`) or the
        response is malformed. Every non-200/non-ok outcome is raised as a
        bounded ``CoordinateHttpError`` subclass, except authoritative
        4xx/409 which are raised as terminal/conflict errors by
        :meth:`_classify`.
        """
        import aiohttp

        request_id = ""
        try:
            async with aiohttp.ClientSession(
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            ) as session:
                headers = {
                    "X-Coordinate-Client-ID": self.client_id,
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                }
                kwargs: dict[str, Any] = {
                    "headers": headers,
                    "allow_redirects": False,
                }
                if payload is not None:
                    kwargs["json"] = payload
                async with session.request(
                    method, self.base_url + path, **kwargs
                ) as resp:
                    request_id = _sanitize_request_id(
                        resp.headers.get("X-Coordinate-Request-Id", "")
                    )
                    content_type = (
                        resp.headers.get("Content-Type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    raw = await resp.content.read(_HTTP_MAX_BODY_BYTES + 1)
                    if len(raw) > _HTTP_MAX_BODY_BYTES:
                        raise CoordinateHttpMalformedError(
                            "response body too large", request_id=request_id
                        )
                    if content_type != "application/json":
                        raise CoordinateHttpMalformedError(
                            "response is not application/json",
                            request_id=request_id,
                        )
                    if not raw:
                        raise CoordinateHttpMalformedError(
                            "empty response body", request_id=request_id
                        )
                    try:
                        envelope = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError) as exc:
                        raise CoordinateHttpMalformedError(
                            "response is not JSON", request_id=request_id
                        ) from exc
                    if (
                        not isinstance(envelope, dict)
                        or set(envelope) != {"ok", "data", "error"}
                    ):
                        raise CoordinateHttpMalformedError(
                            "response envelope is not ok/data/error",
                            request_id=request_id,
                        )
                    _validate_envelope_coherence(
                        status=resp.status,
                        envelope=envelope,
                        request_id=request_id,
                    )
                    return resp.status, envelope, request_id
        except CoordinateHttpError:
            raise
        except asyncio.TimeoutError as exc:
            raise CoordinateHttpTimeoutError(
                "request timed out", request_id=request_id
            ) from exc
        except aiohttp.ClientConnectorError as exc:
            raise CoordinateHttpConnectionError(
                "connection failed", request_id=request_id
            ) from exc
        except aiohttp.ClientError as exc:
            raise CoordinateHttpDisconnectedError(
                "connection lost", request_id=request_id
            ) from exc
        except OSError as exc:
            raise CoordinateHttpConnectionError(
                "connection failed", request_id=request_id
            ) from exc

    @staticmethod
    def _classify(
        status: int,
        envelope: dict[str, Any],
        request_id: str,
    ) -> CoordinateHttpError:
        """Map a non-success (status, coherent error envelope) to a bounded
        error class. Conflict is decided solely by HTTP 409 — a body-level
        ``code=conflict`` on another status is never a conflict (the R2A
        code-to-status map is not replicated here). 3xx never reaches this
        method as success and classifies as malformed."""
        if status == 409:
            return CoordinateHttpConflictError(
                "conflict", status=status, request_id=request_id
            )
        if 400 <= status < 500:
            return CoordinateHttpTerminalError(
                "request rejected", status=status, request_id=request_id
            )
        if status >= 500:
            return CoordinateHttpServerError(
                "server error", request_id=request_id
            )
        return CoordinateHttpMalformedError(
            "unexpected status", request_id=request_id
        )

    async def _request_with_retry(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        attempts: int = _HTTP_ATTEMPTS,
        per_attempt_timeout: float = _HTTP_PER_ATTEMPT_TIMEOUT_SECONDS,
        backoff: float = _HTTP_RETRY_BACKOFF_SECONDS,
    ) -> Any:
        """Bounded retry for transport-level failures (timeout, connection,
        malformed, 5xx). Authoritative 4xx/409/terminal errors are raised
        immediately and never retried. The request body/headers are byte
        identical on every attempt."""
        last: CoordinateHttpError | None = None
        for attempt in range(attempts):
            try:
                status, envelope, request_id = await self._request_once(
                    method, path, payload=payload, timeout_seconds=per_attempt_timeout
                )
                if status == 200 and envelope.get("ok") is True:
                    return envelope["data"]
                raise self._classify(status, envelope, request_id)
            except CoordinateHttpTerminalError:
                raise
            except CoordinateHttpError as exc:
                last = exc
                if attempt == attempts - 1:
                    raise
                await asyncio.sleep(backoff * (attempt + 1))
        assert last is not None
        raise last

    # -- caller contract -----------------------------------------------------

    async def resolve_channel_workspace(
        self,
        *,
        platform: str,
        channel_id: str,
    ) -> str | None:
        """Resolve (platform, channel_id) -> workspace_id via Coordinate.

        Returns the workspace id when bound, None when unbound. Any
        Coordinate error or malformed envelope raises; callers must not
        downgrade it to unbound.
        """
        if not isinstance(platform, str) or not platform.strip():
            raise CoordinateRuntimeError("platform is required")
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise CoordinateRuntimeError("channel_id is required")
        canonical_platform = platform.strip().lower()
        canonical_channel_id = channel_id.strip()
        data = await self._request_with_retry(
            "GET",
            f"/v1/channel-bindings/{quote(canonical_platform, safe='')}/"
            f"{quote(canonical_channel_id, safe='')}",
        )
        if not isinstance(data, dict):
            raise CoordinateHttpMalformedError(
                "channel resolve returned non-object data"
            )
        if data.get("bound") is not True:
            if data.get("bound") is False and data.get("binding") is None:
                return None
            raise CoordinateHttpMalformedError(
                "channel resolve returned unexpected data"
            )
        binding = data.get("binding")
        if not isinstance(binding, dict):
            raise CoordinateHttpMalformedError(
                "channel resolve bound envelope without a binding object"
            )
        workspace_id = binding.get("workspace_id")
        if not isinstance(workspace_id, str) or not workspace_id.strip():
            raise CoordinateHttpMalformedError(
                "channel resolve bound envelope without a non-empty workspace_id"
            )
        binding_platform = str(binding.get("platform", "")).strip().lower()
        binding_channel_id = str(binding.get("channel_id", "")).strip()
        if binding_platform != canonical_platform:
            raise CoordinateRuntimeError(
                f"channel resolve platform mismatch: expected {canonical_platform!r}, "
                f"got {binding_platform!r}"
            )
        if binding_channel_id != canonical_channel_id:
            raise CoordinateRuntimeError(
                f"channel resolve channel_id mismatch: expected {canonical_channel_id!r}, "
                f"got {binding_channel_id!r}"
            )
        return workspace_id.strip()

    async def get_runtime_contract(self, *, recoverable: bool = False) -> dict[str, Any]:
        """Read and validate the authenticated HTTP runtime contract."""
        try:
            data = await self._request_with_retry("GET", "/v1/runtime/contract")
            return _validate_runtime_contract(
                data,
                transport="http",
                recoverable=recoverable,
            )
        except CoordinateContractError:
            raise
        except CoordinateHttpConnectionError as exc:
            raise CoordinateRuntimeError(
                "coordinate HTTP runtime contract probe unavailable"
            ) from exc
        except CoordinateHttpError as exc:
            raise CoordinateContractError(
                f"coordinate runtime contract is unavailable: {exc.category}"
            ) from exc

    async def submit_request(
        self,
        *,
        target_agent: str,
        prompt: str,
        origin_json: dict,
        reply_json: dict,
        workspace_id: str,
        task_id: str = "",
        message_id: str = "",
        idempotency_key: str = "",
    ) -> dict:
        """Submit a bridge request to coordinate. Returns ``{"result": data}``
        exactly like the CLI client. A stable non-empty idempotency key is
        always sent; retries reuse the exact same body and key."""
        workspace = _require_non_empty_workspace(workspace_id)
        key = idempotency_key or message_id or uuid.uuid4().hex
        payload: dict[str, Any] = {
            "workspace_id": workspace,
            "prompt": prompt,
            "origin": origin_json,
            "reply": reply_json,
            "target_agent": target_agent,
            "idempotency_key": key,
        }
        if task_id:
            payload["task_id"] = task_id
        data = await self._request_with_retry("POST", "/v1/requests", payload=payload)
        return {"result": data}

    async def claim_job(
        self,
        *,
        agent_id: str,
        claim_request_id: str = "",
        recoverable: bool = False,
        recovery_reason: str = "",
        prior_process_stopped: bool = False,
        reap_mode: str = "global",
        reap_reason: Any = None,
    ) -> dict[str, Any]:
        """Claim the next pending job for this agent (R2A principal identity).

        Mirrors the CLI client's fail-closed policy checks. The HTTP wire has
        no recoverable claim, so ``recoverable=True`` fails closed and points
        at the CLI recovery path. Any non-authoritative outcome after the
        request was sent (timeout/reset/malformed/5xx) raises
        :class:`CoordinateClaimAuthorityUncertainError`; the worker may only
        replay the same fenced key after a successful authority probe. A
        pre-send connector failure (refused/unreachable, request never sent)
        keeps the ordinary bounded poll; authoritative 401/403/4xx/409 raise
        terminal/conflict errors and are fail-closed when a key is supplied.
        """
        if recoverable:
            raise CoordinateHttpConfigError(
                "HTTP transport does not support recoverable claims; "
                "use the CLI recovery path"
            )
        if recovery_reason != "":
            raise CoordinateHttpConfigError(
                "non-recoverable claim must not carry recovery_reason"
            )
        if prior_process_stopped is not False:
            raise CoordinateHttpConfigError(
                "non-recoverable claim must not carry prior_process_stopped"
            )
        normalized_mode, normalized_reap_reason = normalize_claim_reap_policy(
            reap_mode=reap_mode,
            reap_reason=reap_reason,
        )
        payload: dict[str, Any] = {}
        if claim_request_id:
            payload["claim_request_id"] = claim_request_id
        if normalized_mode == "none":
            assert normalized_reap_reason is not None
            payload.update(
                reap_mode="none",
                reap_reason=normalized_reap_reason,
            )
        try:
            status, envelope, request_id = await self._request_once(
                "POST",
                "/v1/jobs/claim",
                payload=payload,
                timeout_seconds=_CLAIM_TIMEOUT_SECONDS,
            )
            if status == 200 and envelope.get("ok") is True:
                return envelope["data"]
            raise self._classify(status, envelope, request_id)
        except CoordinateHttpConflictError as exc:
            if claim_request_id:
                raise CoordinateClaimReplayConflictError(
                    "coordinate claim replay conflict"
                ) from exc
            raise
        except CoordinateHttpTerminalError as exc:
            if claim_request_id:
                raise CoordinateClaimTerminalError("coordinate claim rejected") from exc
            raise
        except CoordinateHttpConnectionError:
            # The request was never sent (refused/unreachable): the tunnel or
            # listener is not ready yet. Keep the ordinary bounded poll; this
            # must not latch the worker.
            raise
        except CoordinateHttpError as exc:
            raise CoordinateClaimAuthorityUncertainError(
                "claim response uncertain", request_id=exc.request_id
            ) from exc

    async def reconcile_agent(self, *, agent_id: str) -> dict[str, Any]:
        """Read the authenticated agent's minimal active-lease snapshot."""
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise CoordinateHttpConfigError("agent_id is required")
        data = await self._request_with_retry(
            "GET", f"/v1/agents/{quote(agent_id.strip(), safe='')}/reconcile"
        )
        if not isinstance(data, dict) or not isinstance(data.get("active_leases"), list):
            raise CoordinateHttpMalformedError(
                "agent reconcile returned invalid active_leases"
            )
        return data

    async def report_job(
        self,
        *,
        job_id: str,
        agent_id: str,
        status: str,
        result_json: dict,
        attempt_token: int | None = None,
        lease_id: str | None = None,
    ) -> dict:
        """Report a terminal result. Retries reuse the exact same status/
        result/attempt/lease body; any 409 is a real conflict and is raised."""
        payload: dict[str, Any] = {"status": status, "result": result_json}
        if attempt_token is not None:
            payload["attempt_token"] = attempt_token
        if lease_id is not None:
            payload["lease_id"] = lease_id
        data = await self._request_with_retry(
            "POST", f"/v1/jobs/{quote(job_id, safe='')}/report", payload=payload
        )
        return {"result": data}

    async def record_progress(
        self,
        *,
        job_id: str,
        agent_id: str,
        stage: str = "",
        summary: str = "",
        session_id: str = "",
        attempt_token: int | None = None,
        lease_id: str | None = None,
    ) -> dict:
        """Record a bounded progress checkpoint. Retries reuse the same
        job/attempt/lease/body."""
        payload: dict[str, Any] = {}
        if stage:
            payload["stage"] = stage
        if summary:
            payload["summary"] = summary
        if session_id:
            payload["session_id"] = session_id
        if attempt_token is not None:
            payload["attempt_token"] = attempt_token
        if lease_id is not None:
            payload["lease_id"] = lease_id
        data = await self._request_with_retry(
            "POST",
            f"/v1/jobs/{quote(job_id, safe='')}/progress",
            payload=payload,
        )
        return {"result": data}

    async def renew_lease(
        self,
        *,
        job_id: str,
        agent_id: str,
        attempt_token: int,
        lease_id: str,
    ) -> dict:
        """Renew one managed lease with a tight total budget: 2 attempts at
        1.5s plus 0.25s backoff stays below the worker's 5s renewal safety
        margin. Retries reuse the exact same lease/job/attempt body."""
        payload: dict[str, Any] = {
            "lease_id": lease_id,
            "attempt_token": attempt_token,
        }
        data = await self._request_with_retry(
            "POST",
            f"/v1/jobs/{quote(job_id, safe='')}/lease/renew",
            payload=payload,
            attempts=_RENEW_ATTEMPTS,
            per_attempt_timeout=_RENEW_PER_ATTEMPT_TIMEOUT_SECONDS,
            backoff=_RENEW_BACKOFF_SECONDS,
        )
        return {"result": data}

    async def reap_leases(
        self,
        *,
        actor: str = "agentd",
        batch_size: int = 100,
    ) -> dict:
        """Not implemented over HTTP: lease reaping is a recovery-only
        capability. Fail closed and point at the preserved CLI/SSH path."""
        raise CoordinateHttpConfigError(
            "HTTP transport does not implement lease reaping; "
            "use the CLI recovery path (coordinate runtime job lease reap)"
        )

    async def wait_for_job_result(
        self,
        *,
        job_id: str,
        workspace_id: str,
        poll_interval: float = 2.0,
        timeout: float = 1800.0,
    ) -> dict | None:
        """Poll coordinate until a job reaches a terminal state, then return
        the job dict, or None on timeout."""
        import time as _time

        start = _time.monotonic()
        workspace = _require_non_empty_workspace(workspace_id)
        while _time.monotonic() - start < timeout:
            job = await self._get_job(job_id, workspace_id=workspace)
            if job is None:
                await asyncio.sleep(poll_interval)
                continue
            status = job.get("status", "")
            if status in ("done", "failed", "timed_out"):
                return job
            await asyncio.sleep(poll_interval)
        return None

    async def _get_job(self, job_id: str, *, workspace_id: str) -> dict | None:
        """Fetch a single job's current state. A 404 (not found) maps to None
        exactly like the CLI job-list scan; any other error is raised."""
        workspace = _require_non_empty_workspace(workspace_id)
        try:
            data = await self._request_with_retry(
                "GET",
                f"/v1/workspaces/{quote(workspace, safe='')}/jobs/"
                f"{quote(job_id, safe='')}",
            )
        except CoordinateHttpTerminalError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(data, dict):
            raise CoordinateHttpMalformedError("job get returned non-object data")
        return data


def make_coordinate_runtime_client(config: Any) -> Any:
    """Single transport factory for Coordinate-managed consumers.

    ``cli`` returns the existing :class:`CoordinateRuntimeClient`; ``http``
    returns :class:`CoordinateHttpRuntimeClient`. Fail closed on unknown
    transport or missing required fields (SystemExit keeps the legacy
    consumer startup contract). Non-agentd direct configs never reach this
    factory.
    """
    transport = str(getattr(config, "coordinate_transport", "cli") or "cli")
    if transport == "cli":
        cli_path = str(getattr(config, "coordinator_cli_path", "") or "")
        if not cli_path:
            raise SystemExit(
                "agentd_mode requires coordinator_cli_path. "
                "Set it in agents.toml or the [defaults] section."
            )
        return CoordinateRuntimeClient(
            cli_path=cli_path,
            db_path=str(getattr(config, "coordinator_db_path", "") or ""),
        )
    if transport == "http":
        return CoordinateHttpRuntimeClient(
            base_url=str(getattr(config, "coordinate_http_base_url", "") or ""),
            client_id=str(getattr(config, "coordinate_http_client_id", "") or ""),
            token_file=str(
                getattr(config, "coordinate_http_token_file", "") or ""
            ),
        )
    raise SystemExit(
        f"agentd_mode requires coordinate_transport to be 'cli' or 'http', "
        f"got {transport!r}. Set it in agents.toml or the [defaults] section."
    )
