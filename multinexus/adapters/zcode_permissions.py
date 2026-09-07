"""The opt-in ZCode Write/Edit/exact-command policy; not an OS sandbox."""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any


def path_key(value: str | Path, *, windows: bool = os.name == "nt") -> str:
    module = ntpath if windows else os.path
    return module.normcase(module.normpath(str(value)))


def is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((path_key(path), path_key(root))) == path_key(root)
    except ValueError:
        return False


def check_no_links(path: Path) -> None:
    """lstat every existing component, including dangling links/reparse points."""
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("ZCode paths must not contain symlinks or reparse points")


def absolute_path(value: str, *, base: Path | None = None) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("ZCode path must be a non-empty string")
    win = PureWindowsPath(value)
    if os.name != "nt" and (win.drive or "\\" in value):
        raise ValueError("Foreign path syntax is not supported")
    if os.name == "nt":
        check_windows_path(value)
    candidate = Path(value)
    if not candidate.is_absolute():
        if base is None:
            raise ValueError("ZCode path must be absolute")
        candidate = base / candidate
    # Check before normalizing: link/../file must not hide a followed link.
    check_no_links(candidate)
    candidate = Path(os.path.abspath(candidate))
    check_no_links(candidate)
    return candidate


def check_windows_path(value: str) -> None:
    win = PureWindowsPath(value)
    if value.startswith(("\\\\", "//")) or (win.drive and not win.is_absolute()):
        raise ValueError("ZCode paths must use an absolute local drive")
    parts = win.parts[1:] if win.anchor else win.parts
    for part in parts:
        if part.endswith((".", " ")) or any(char in part for char in '<>:"|?*'):
            raise ValueError("Ambiguous Windows path or alternate data stream")
        if any(ord(char) < 32 for char in part) or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", part, re.I):
            raise ValueError("Windows device paths are not workspace files")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    reason: str
    input_summary: dict[str, Any]


@dataclass(frozen=True)
class ZCodePermissionPolicy:
    workspace: Path
    context_root: Path
    commands: tuple[str, ...]

    def decide(self, tool: str, value: Any) -> PermissionDecision:
        try:
            if not isinstance(value, dict):
                raise ValueError("Unsupported tool input schema")
            if tool in ("Write", "Edit"):
                summary = self._edit(tool, value)
            elif tool == "Bash":
                summary = self._command(value)
            else:
                raise ValueError("Tool is outside the ZCode permission allow scope")
            return PermissionDecision(True, "Allowed by the exact ZCode workspace policy", summary)
        except (ValueError, OSError):
            return PermissionDecision(False, "Denied by the ZCode workspace policy", {"input_sha256": _digest(value)})

    def _edit(self, tool: str, value: dict[str, Any]) -> dict[str, Any]:
        required = {"file_path", "content"} if tool == "Write" else {"file_path", "old_string", "new_string"}
        optional = set() if tool == "Write" else {"replace_all"}
        if not required <= value.keys() or value.keys() - required - optional:
            raise ValueError("Unsupported edit schema")
        if any(not isinstance(value[key], str) for key in required):
            raise ValueError("Edit fields must be strings")
        if "replace_all" in value and not isinstance(value["replace_all"], bool):
            raise ValueError("replace_all must be a boolean")
        target = absolute_path(value["file_path"], base=self.workspace)
        if not is_within(target, self.workspace) or is_within(target, self.context_root):
            raise ValueError("Target is outside the workspace")
        parts = tuple(part.casefold() for part in target.parts)
        if ".git" in parts or "zcode.json" in parts or ".zcode" in parts:
            raise ValueError("ZCode and Git configuration is protected")
        if target == self.workspace or target.is_dir():
            raise ValueError("Edit target must be a file")
        if target.exists() and target.stat().st_nlink != 1:
            raise ValueError("Linked edit targets are not allowed")
        return {"path": str(target.relative_to(self.workspace)), "input_sha256": _digest(value)}

    def _command(self, value: dict[str, Any]) -> dict[str, Any]:
        permitted = {"command", "description", "cwd", "timeout", "run_in_background"}
        if value.keys() - permitted or not isinstance(value.get("command"), str):
            raise ValueError("Unsupported command schema")
        if value["command"] not in self.commands:
            raise ValueError("Command is not an exact configured test")
        if "description" in value and not isinstance(value["description"], str):
            raise ValueError("Invalid command description")
        if "run_in_background" in value and value["run_in_background"] is not False:
            raise ValueError("Background commands are not allowed")
        if "timeout" in value and (type(value["timeout"]) is not int or not 0 < value["timeout"] <= 600000):
            raise ValueError("Invalid command timeout")
        cwd = absolute_path(value.get("cwd", str(self.workspace)), base=self.workspace)
        if path_key(cwd) != path_key(self.workspace):
            raise ValueError("Command cwd must equal the workspace")
        return {"command": value["command"], "cwd": str(self.workspace), "input_sha256": _digest(value)}
