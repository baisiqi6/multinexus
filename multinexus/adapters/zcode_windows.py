"""Owner-only ACLs for ZCode's private Windows session context files."""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path


def _apis():
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                         wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)]
    advapi.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return advapi, kernel


@lru_cache(maxsize=1)
def _current_sid() -> str:
    advapi, kernel = _apis()
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise OSError("Cannot inspect ZCode context owner")
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        data = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, data, size, ctypes.byref(size)):
            raise OSError("Cannot inspect ZCode context owner")
        sid = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise OSError("Cannot inspect ZCode context owner")
        try:
            return text.value
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.CloseHandle(token)


def protect_private(path: Path, *, directory: bool) -> None:
    advapi, kernel = _apis()
    sid = _current_sid()
    flags = "OICI" if directory else ""
    descriptor = ctypes.c_void_p()
    sddl = f"O:{sid}D:P(A;{flags};FA;;;{sid})"
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise OSError("Cannot prepare private ZCode ACL")
    try:
        if not advapi.SetFileSecurityW(str(path), 0x80000005, descriptor):
            raise OSError("Cannot protect ZCode context ACL")
    finally:
        kernel.LocalFree(descriptor)


def _read_sddl(path: Path) -> str:
    advapi, kernel = _apis()
    size = wintypes.DWORD()
    advapi.GetFileSecurityW(str(path), 5, None, 0, ctypes.byref(size))
    descriptor = ctypes.create_string_buffer(size.value)
    if not advapi.GetFileSecurityW(str(path), 5, descriptor, size, ctypes.byref(size)):
        raise OSError("Cannot inspect ZCode context ACL")
    text = wintypes.LPWSTR()
    if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 5, ctypes.byref(text), None):
        raise OSError("Cannot inspect ZCode context ACL")
    try:
        return text.value
    finally:
        kernel.LocalFree(text)


def verify_private(path: Path, *, allow_inherited_file: bool = False) -> None:
    sid = _current_sid()
    sddl = _read_sddl(path)
    if _owner_only_sddl(sddl, sid):
        return
    # Native atomic saves inherit the private directory ACL. GetFileSecurityW
    # may omit ID on those ACEs; check effective access, not that marker alone.
    # Accept that file
    # only when both its effective access and the protected parent stay private.
    if (allow_inherited_file and path.is_file()
            and _owner_only_sddl(sddl, sid, inherited_file=True)
            and _owner_only_sddl(_read_sddl(path.parent), sid)):
        return
    raise ValueError("ZCode context must have an owner-only ACL")


def _owner_only_sddl(sddl: str, sid: str, *, inherited_file: bool = False) -> bool:
    sid = {"S-1-5-18": "SY", "S-1-5-19": "LS", "S-1-5-20": "NS"}.get(sid, sid)
    if not sddl.startswith(f"O:{sid}D:"):
        return False
    dacl = sddl[len(f"O:{sid}D:"):]
    entries = re.findall(r"\(([^()]*)\)", dacl)
    allowed_flags = ("", "AI", "AIAR", "AR") if inherited_file else ("P", "PAI", "PAIAR", "PAR")
    if not entries or re.sub(r"\([^()]*\)", "", dacl) not in allowed_flags:
        return False
    for entry in entries:
        parts = entry.split(";")
        if inherited_file and (len(parts) != 6 or parts[1] not in ("", "ID")):
            return False
        if len(parts) != 6 or parts[0] != "A" or parts[3:5] != ["", ""] or parts[5] != sid:
            return False
    return True


def _set_current_process_owner() -> None:
    """Set only this launcher process's default owner to its existing user SID."""
    advapi, kernel = _apis()
    advapi.SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    advapi.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0088, ctypes.byref(token)):
        raise OSError("Cannot open ZCode launcher owner token")
    try:
        def information(kind):
            size = wintypes.DWORD()
            advapi.GetTokenInformation(token, kind, None, 0, ctypes.byref(size))
            data = ctypes.create_string_buffer(size.value)
            if not advapi.GetTokenInformation(token, kind, data, size, ctypes.byref(size)):
                raise OSError("Cannot inspect ZCode launcher owner token")
            return data

        user = information(1)  # TOKEN_USER starts with SID_AND_ATTRIBUTES.Sid.
        user_sid = ctypes.cast(user, ctypes.POINTER(ctypes.c_void_p))[0]
        owner = ctypes.c_void_p(user_sid)  # TOKEN_OWNER is a single PSID.
        if not advapi.SetTokenInformation(token, 4, ctypes.byref(owner), ctypes.sizeof(owner)):
            raise OSError("Cannot set ZCode launcher default owner")
        observed = information(4)
        if not advapi.EqualSid(user_sid, ctypes.cast(observed, ctypes.POINTER(ctypes.c_void_p))[0]):
            raise OSError("ZCode launcher default owner mismatch")
    finally:
        kernel.CloseHandle(token)


def _run_native_child(argv: list[str]) -> int:
    # NSSM may supply SYSTEM with Administrators as TokenOwner. Changing this
    # separate launcher's token keeps native files private without changing the
    # agentd token, granting privileges, or relaxing the file ACL checks.
    _set_current_process_owner()
    return subprocess.run(argv).returncode  # Inherit the native byte streams; keep the tree leader alive.


def _run_restriction_child(argv: list[str]) -> dict:
    if len(argv) != 5 or argv[0] not in ('initialize', 'verify') or argv[4] not in ('0', '1'):
        raise ValueError('Invalid ZCode restriction operation')
    _set_current_process_owner()
    # -I removes cwd/environment imports. Restore only this trusted source root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from multinexus.adapters.zcode_native_rules import _initialize_bash_restriction, _verify_bash_restriction
    action, database, workspace, session_id, bootstrap = argv
    if action == 'initialize':
        return _initialize_bash_restriction(Path(database), Path(workspace), session_id)
    return _verify_bash_restriction(Path(database), Path(workspace), session_id, bootstrap=bootstrap == '1')


if __name__ == "__main__":
    if os.name != "nt" or len(sys.argv) < 2:
        raise SystemExit(125)
    try:
        if sys.argv[1] == '--restriction':
            print(json.dumps(_run_restriction_child(sys.argv[2:])))
            raise SystemExit(0)
        raise SystemExit(_run_native_child(sys.argv[1:]))
    except Exception:
        print("ZCode native launcher initialization failed", file=sys.stderr)
        raise SystemExit(125)
