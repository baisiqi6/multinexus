"""Private, exact-session contexts for the opt-in ZCode native transport."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..models import AgentConfig
from .zcode_permissions import ZCodePermissionPolicy, absolute_path, check_no_links, is_within, path_key
from .zcode_native_rules import RULE_SHA256, initialize_bash_restriction, verify_bash_restriction

SESSION_ID_RE = re.compile(r"^sess_[A-Za-z0-9._-]{1,160}$")
CONTEXT_ID_RE = re.compile(r"^ctx_[0-9a-f]{32}$")
POLICY_VERSION = "zcode-native-scoped-v2"
_MAX_CONFIG_BYTES = 1024 * 1024


class ZCodeContextError(ValueError):
    """A safe, local configuration or context-integrity failure."""


def _bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _private(path: Path, *, new: bool = False, directory: bool = False) -> None:
    check_no_links(path)
    if os.name == "nt":
        from .zcode_windows import protect_private, verify_private
        if new:
            protect_private(path, directory=directory)
        verify_private(path, allow_inherited_file=not directory)
        return
    if new:
        path.chmod(0o700 if directory else 0o600)
    info = path.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ZCodeContextError("ZCode context paths must be owner-only")
    if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise ZCodeContextError("ZCode context files must be private regular files")


def _mkdir(path: Path) -> None:
    check_no_links(path)
    try:
        path.mkdir(mode=0o700)
        new = True
    except FileExistsError:
        new = False
    if not path.is_dir():
        raise ZCodeContextError("ZCode context directory unavailable")
    _private(path, new=new, directory=True)


def _unique_json_object(pairs) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON object key")
        value[key] = item
    return value


def _reject_json_constant(value):
    raise ValueError("Nonstandard JSON number")


def _finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Nonfinite JSON number")
    return number


def _read_json(path: Path, *, private: bool = True) -> dict[str, Any]:
    check_no_links(path)
    if private:
        _private(path)
    if path.stat().st_size > _MAX_CONFIG_BYTES:
        raise ZCodeContextError("ZCode configuration exceeds the supported size")
    with path.open("rb") as stream:
        raw = stream.read(_MAX_CONFIG_BYTES + 1)
    if len(raw) > _MAX_CONFIG_BYTES:
        raise ZCodeContextError("ZCode configuration exceeds the supported size")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_json_object,
                           parse_constant=_reject_json_constant, parse_float=_finite_json_float)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ZCodeContextError("Invalid ZCode configuration JSON") from exc
    if not isinstance(value, dict):
        raise ZCodeContextError("ZCode configuration must be an object")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    check_no_links(path)
    if path.exists():
        _private(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _private(temporary, new=True)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
        check_no_links(path)
        os.replace(temporary, path)
        _private(path)
    finally:
        if fd != -1:
            os.close(fd)
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class ZCodeProvider:
    model: str
    provider_id: str
    model_id: str
    auth: dict[str, Any] = field(repr=False)
    secrets: tuple[str, ...] = field(repr=False)
    identity_sha256: str

    @property
    def model_ref(self) -> dict[str, str]:
        return {"providerId": self.provider_id, "modelId": self.model_id}

    def runtime_model(self) -> dict[str, Any]:
        """Reapply this same static connection to the cold process's catalog.

        This private stdin payload contains the already selected API key. Never
        persist it or include it in diagnostics, locators or result metadata.
        """
        selected = self.auth["provider"][self.provider_id]
        metadata = selected.get("models", {}).get(self.model_id, {})
        model = {"modelId": self.model_id, **_runtime_model_metadata(metadata)}
        provider = {
            "providerId": self.provider_id, "kind": selected["kind"], "source": "ephemeral",
            "baseURL": selected["options"]["baseURL"],
            "apiKey": {"source": "inline", "value": selected["options"]["apiKey"]},
            "models": [model],
        }
        if "name" in selected:
            provider["label"] = selected["name"]
        return {"revision": "multinexus-" + uuid.uuid4().hex, "generatedAt": int(time.time() * 1000),
                "model": self.model_ref, "provider": provider}

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

    def redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {self.redact(key): self.redact_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact_value(item) for item in value]
        return value


def read_provider(home: Path) -> ZCodeProvider:
    source = _read_json(home / ".zcode" / "cli" / "config.json", private=False)
    model = source.get("model")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._:-]+/[A-Za-z0-9._:/+-]+", model):
        raise ZCodeContextError("Unsupported ZCode source model; expected provider/model string")
    provider_id, model_id = model.split("/", 1)
    providers = source.get("provider")
    if not isinstance(providers, dict) or not isinstance(providers.get(provider_id), dict):
        raise ZCodeContextError("Selected ZCode provider is missing from source provider map")
    selected = providers[provider_id]
    clean = _provider_connection(selected)
    models = selected.get("models", {})
    if not isinstance(models, dict):
        raise ZCodeContextError("Unsupported ZCode provider.models schema")
    if model_id in models:
        clean["models"] = {model_id: _model_metadata(models[model_id])}
    public = json.loads(json.dumps(clean))
    secret = public["options"].pop("apiKey")
    identity = _sha(_bytes({"model": model, "provider": public}))
    return ZCodeProvider(model, provider_id, model_id, {"model": model, "provider": {provider_id: clean}}, (secret,), identity)


def _provider_connection(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("enabled", True) is not True or value.get("kind") not in ("anthropic", "openai", "openai-compatible"):
        raise ZCodeContextError("Selected ZCode provider kind/enabled is unsupported")
    options = value.get("options")
    if not isinstance(options, dict) or set(options) != {"apiKey", "baseURL"} or value.get("headers"):
        raise ZCodeContextError("Unsupported ZCode provider authentication fields")
    if any(not isinstance(options[key], str) or not options[key] for key in ("apiKey", "baseURL")):
        raise ZCodeContextError("ZCode provider apiKey/baseURL must be explicit strings")
    parsed = urlsplit(options["baseURL"])
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ZCodeContextError("Unsupported ZCode provider baseURL")
    clean: dict[str, Any] = {"kind": value["kind"], "options": dict(options)}
    if "name" in value:
        if not isinstance(value["name"], str) or not value["name"]:
            raise ZCodeContextError("Unsupported ZCode provider name")
        clean["name"] = value["name"]
    return clean


def _model_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("headers") or value.get("options"):
        raise ZCodeContextError("Unsupported selected ZCode model metadata")
    clean: dict[str, Any] = {}
    if "limit" in value:
        limits = value["limit"]
        if not isinstance(limits, dict) or any(type(v) is not int or v <= 0 for k, v in limits.items() if k in ("context", "output")):
            raise ZCodeContextError("Unsupported ZCode model limit metadata")
        clean["limit"] = {k: v for k, v in limits.items() if k in ("context", "output")}
    if "modalities" in value:
        modalities = value["modalities"]
        if not isinstance(modalities, dict):
            raise ZCodeContextError("Unsupported ZCode model modalities metadata")
        if "input" in modalities:
            items = modalities["input"]
            if not isinstance(items, list) or any(item not in ("text", "audio", "image", "video", "pdf") for item in items):
                raise ZCodeContextError("Unsupported ZCode model modalities.input")
            clean["modalities"] = {"input": list(items)}
    if "reasoning" in value:
        clean["reasoning"] = _reasoning_metadata(value["reasoning"])
    return clean


def _reasoning_metadata(value: Any) -> bool | dict[str, Any]:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict) and value.keys() & {"variants", "defaultVariant"}:
        # Desktop source configs name the same named reasoning choices variants.
        # Translate only this closed shape; mixed schemas are ambiguous.
        if value.keys() - {"enabled", "variants", "defaultVariant"}:
            raise ZCodeContextError("Unsupported mixed ZCode reasoning schemas")
        value = {({"variants": "levels", "defaultVariant": "defaultLevel"}.get(key, key)): item
                 for key, item in value.items()}
    if not isinstance(value, dict) or value.keys() - {"enabled", "levels", "defaultLevel"}:
        raise ZCodeContextError("Unsupported ZCode model reasoning structure")
    if "enabled" in value and not isinstance(value["enabled"], bool):
        raise ZCodeContextError("Unsupported ZCode reasoning.enabled")
    if "levels" in value and (not isinstance(value["levels"], list) or any(not isinstance(v, str) or not v for v in value["levels"])):
        raise ZCodeContextError("Unsupported ZCode reasoning.levels")
    if "defaultLevel" in value and (not isinstance(value["defaultLevel"], str) or not value["defaultLevel"]):
        raise ZCodeContextError("Unsupported ZCode reasoning.defaultLevel")
    if "defaultLevel" in value and "levels" in value and value["defaultLevel"] not in value["levels"]:
        raise ZCodeContextError("ZCode reasoning.defaultLevel is not in levels")
    return json.loads(json.dumps(value))


def _runtime_model_metadata(value: dict[str, Any]) -> dict[str, Any]:
    model: dict[str, Any] = {}
    for source, target in (("context", "contextWindow"), ("output", "maxOutputTokens")):
        if source in value.get("limit", {}):
            model[target] = value["limit"][source]
    inputs = value.get("modalities", {}).get("input")
    if inputs is not None:
        for source, target in (("image", "supportsImages"), ("pdf", "supportsPdf"), ("video", "supportsVideo")):
            model[target] = source in inputs
    if "reasoning" in value:
        reasoning = value["reasoning"]
        if isinstance(reasoning, bool):
            model["reasoning"] = {"enabled": reasoning, "levels": []}
        else:
            levels = reasoning.get("levels", [])
            enabled = reasoning.get("enabled", True if levels else None)
            default = reasoning.get("defaultLevel")
            if enabled is None or default is not None and default not in levels:
                raise ZCodeContextError("ZCode cold resume requires unambiguous reasoning enabled/levels/defaultLevel")
            model["reasoning"] = {"enabled": enabled, "levels": [{"value": level, "label": level} for level in levels]}
            if default is not None:
                model["reasoning"]["defaultLevel"] = default
    return model


def _workspace(value: str) -> Path:
    workspace = absolute_path(value)
    if not workspace.is_dir():
        raise ZCodeContextError("ZCode workspace is not a directory")
    for directory in (workspace, *workspace.parents):
        for name in ("zcode.json", ".zcode/config.json"):
            candidate = directory / name
            if candidate.exists() or candidate.is_symlink():
                raise ZCodeContextError("ZCode native permissions refuse discovered project configuration")
        git = directory / ".git"
        if git.exists() or git.is_symlink():
            check_no_links(git)
            break
    return workspace


def _isolated_config(provider: ZCodeProvider, storage: Path) -> dict[str, Any]:
    return {
        **provider.auth,
        "permission": {"mode": "build", "allowedTools": [], "disallowedTools": [], "autoApproveHighRisk": False},
        "storage": {"dir": str(storage), "sessionDbPath": str(storage / "session.sqlite")},
        "hooks": {"enabled": False, "events": {}},
        "plugins": {"enabled": False, "dirs": [], "enabledPlugins": {}},
        "features": {"mcp": False, "subagent": False, "memory": False, "skill": False},
        "mcp": {"servers": {}}, "skills": {"enabled": False, "includeInstructions": False, "roots": []},
    }


@dataclass
class ZCodeContext:
    root: Path
    directory: Path
    workspace: Path
    policy: ZCodePermissionPolicy
    provider: ZCodeProvider
    binding: dict[str, Any]
    config_sha256: str
    lock_token: str = field(repr=False)
    session_id: str | None = None
    native_rule_evidence: dict[str, Any] = field(default_factory=dict, repr=False)
    _native_session_pending: bool = field(default=False, repr=False)

    @property
    def home(self) -> Path:
        return self.directory / "home"

    @property
    def config_file(self) -> Path:
        return self.home / ".zcode" / "cli" / "config.json"

    @property
    def storage(self) -> Path:
        return self.directory / "storage"

    def environment(self) -> dict[str, str]:
        runtime_keys = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "LANG", "LC_ALL", "LC_CTYPE"}
        env = {key: value for key, value in os.environ.items() if key.upper() in runtime_keys}
        env.update({
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "APPDATA": str(self.home / "AppData" / "Roaming"), "LOCALAPPDATA": str(self.home / "AppData" / "Local"),
            "TMPDIR": str(self.directory / "tmp"), "TEMP": str(self.directory / "tmp"), "TMP": str(self.directory / "tmp"),
            "ZCODE_STORAGE_DIR": str(self.storage), "ZCODE_SESSION_DB_PATH": str(self.storage / "session.sqlite"),
            "ZCODE_DATA_BASE_DIR": str(self.home), "ZCODE_LOG_DIR": str(self.directory / "logs"),
        })
        if os.name != "nt":
            env["PWD"] = str(self.workspace)
        return env

    def assert_intact(self, *, require_persisted_session: bool = False) -> None:
        for directory in (self.root, self.root / "sessions", self.directory, self.home, self.storage):
            _private(directory, directory=True)
        if _read_json(self.directory / "context.json") != self.binding:
            raise ZCodeContextError("ZCode context binding changed")
        if _read_json(self.directory / ".active") != {"token": self.lock_token}:
            raise ZCodeContextError("ZCode context claim changed")
        if _sha(_bytes(_read_json(self.config_file))) != self.config_sha256:
            raise ZCodeContextError("ZCode isolated configuration changed")
        for path in self.storage.glob("session.sqlite*"):
            check_no_links(path)
            if path.stat().st_nlink != 1:
                raise ZCodeContextError("ZCode session database must not be linked")
        if self.session_id and _read_json(self._locator()) != {**self.binding, "session_id": self.session_id}:
            raise ZCodeContextError("ZCode session locator changed")
        if self.session_id:
            try:
                self.native_rule_evidence = verify_bash_restriction(
                    self.storage / "session.sqlite", self.workspace, self.session_id,
                    bootstrap=self._native_session_pending and not require_persisted_session)
            except (OSError, ValueError, sqlite3.Error) as exc:
                raise ZCodeContextError("ZCode native Bash restriction verification failed") from exc
            if self.native_rule_evidence["session_persisted"]:
                self._native_session_pending = False
        _workspace(str(self.workspace))

    def _locator(self) -> Path:
        return self.root / "sessions" / f"{_sha(self.session_id.encode())}.json"

    def bind_session(self, session_id: str) -> None:
        if not SESSION_ID_RE.fullmatch(session_id) or self.session_id and session_id != self.session_id:
            raise ZCodeContextError("ZCode provider session identity mismatch")
        if self.session_id is None:
            try:
                self.native_rule_evidence = initialize_bash_restriction(
                    self.storage / "session.sqlite", self.workspace, session_id)
            except (OSError, ValueError, sqlite3.Error) as exc:
                raise ZCodeContextError("ZCode native Bash restriction initialization failed") from exc
            self._native_session_pending = not self.native_rule_evidence["session_persisted"]
        self.session_id = session_id
        locator = self._locator()
        value = {**self.binding, "session_id": session_id}
        if locator.exists() and _read_json(locator) != value:
            raise ZCodeContextError("ZCode session locator already belongs to another context")
        _atomic_json(locator, value)

    def close(self) -> None:
        check_no_links(self.config_file.parent)
        self.config_file.unlink(missing_ok=True)
        claim = self.directory / ".active"
        if _read_json(claim) != {"token": self.lock_token}:
            raise ZCodeContextError("ZCode context claim changed during cleanup")
        claim.unlink()

    def record_permissions(self, input_id: str, decisions: list[dict[str, Any]]) -> None:
        if not re.fullmatch(r"input_[0-9a-f]{32}", input_id):
            raise ZCodeContextError("Invalid ZCode permission evidence input identity")
        safe = self.provider.redact_value(decisions)
        _atomic_json(self.directory / f"permissions-{input_id}.json", {"version": 1, "decisions": safe})


def prepare_context(config: AgentConfig, cwd: str, resume_session_id: str | None = None) -> ZCodeContext:
    if config.zcode_permission_mode != "build":
        raise ZCodeContextError("ZCode app-server permissions require build mode")
    if not isinstance(config.zcode_context_root, str):
        raise ZCodeContextError("ZCode app-server requires zcode_context_root")
    workspace = _workspace(str(Path.cwd() / cwd) if not Path(cwd).is_absolute() else cwd)
    root = absolute_path(config.zcode_context_root)
    if is_within(root, workspace) or is_within(workspace, root):
        raise ZCodeContextError("ZCode context root must be separate from the workspace")
    commands = config.zcode_permission_commands
    if not isinstance(commands, list) or any(not isinstance(c, str) or not c.strip() or "\x00" in c for c in commands):
        raise ZCodeContextError("ZCode permission commands must be exact strings")
    source_home = absolute_path(config.zcode_home_dir or str(Path.home()))
    provider = read_provider(source_home)
    _mkdir(root)
    _mkdir(root / "sessions")
    policy = ZCodePermissionPolicy(workspace, root, tuple(commands))
    fingerprint = _sha(_bytes({"version": POLICY_VERSION, "native_bash_rule_sha256": RULE_SHA256, "commands": commands, "worker": config.id,
                              "source_home": path_key(source_home), "provider": provider.identity_sha256}))
    context_id, binding = _context_binding(root, workspace, fingerprint, resume_session_id)
    directory = root / context_id
    if resume_session_id:
        _verify_resume_directory(directory, binding)
    else:
        _new_directory(directory, binding)
    token = _claim(directory)
    home = directory / "home"
    config_file = home / ".zcode" / "cli" / "config.json"
    try:
        isolated = _isolated_config(provider, directory / "storage")
        _atomic_json(config_file, isolated)
        context = ZCodeContext(root, directory, workspace, policy, provider, binding, _sha(_bytes(isolated)), token, resume_session_id)
        context.assert_intact()
        return context
    except BaseException:
        check_no_links(config_file.parent)
        config_file.unlink(missing_ok=True)
        (directory / ".active").unlink(missing_ok=True)
        raise


def _context_binding(root: Path, workspace: Path, policy_sha: str, session_id: str | None) -> tuple[str, dict[str, Any]]:
    if not session_id:
        context_id = "ctx_" + uuid.uuid4().hex
        return context_id, {"version": 1, "context_id": context_id, "workspace": path_key(workspace), "policy_sha256": policy_sha}
    if not SESSION_ID_RE.fullmatch(session_id):
        raise ZCodeContextError("Invalid ZCode native resume session")
    locator = root / "sessions" / f"{_sha(session_id.encode())}.json"
    if not locator.exists():
        raise ZCodeContextError("ZCode native resume requires a session created by this client")
    value = _read_json(locator)
    if set(value) != {"version", "context_id", "workspace", "policy_sha256", "session_id"}:
        raise ZCodeContextError("Unsupported ZCode session locator")
    context_id = value["context_id"]
    if not isinstance(context_id, str) or not CONTEXT_ID_RE.fullmatch(context_id):
        raise ZCodeContextError("Invalid ZCode context locator path")
    if value != {"version": 1, "context_id": context_id, "workspace": path_key(workspace), "policy_sha256": policy_sha, "session_id": session_id}:
        raise ZCodeContextError("ZCode resume workspace/provider/policy mismatch")
    return context_id, {key: val for key, val in value.items() if key != "session_id"}


def _new_directory(directory: Path, binding: dict[str, Any]) -> None:
    directory.mkdir(mode=0o700)
    _private(directory, new=True, directory=True)
    for name in ("home", "storage", "tmp", "logs", "home/.zcode", "home/.zcode/cli",
                 "home/AppData", "home/AppData/Roaming", "home/AppData/Local"):
        _mkdir(directory / name)
    _atomic_json(directory / "context.json", binding)


def _verify_resume_directory(directory: Path, binding: dict[str, Any]) -> None:
    _private(directory, directory=True)
    if _read_json(directory / "context.json") != binding:
        raise ZCodeContextError("ZCode context does not match its session locator")
    for parent, directories, files in os.walk(directory, followlinks=False):
        for name in (*directories, *files):
            check_no_links(Path(parent) / name)
    if not (directory / "storage" / "session.sqlite").is_file():
        raise ZCodeContextError("ZCode native session database unavailable")


def _claim(directory: Path) -> str:
    path = directory / ".active"
    check_no_links(path)
    token = uuid.uuid4().hex
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ZCodeContextError("ZCode context is already active; do not concurrently resume it") from exc
    with os.fdopen(fd, "wb") as stream:
        stream.write(_bytes({"token": token}))
    _private(path, new=True)
    return token
