"""Restrictive Bash routing for the pinned ZCode private native database."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .zcode_permissions import check_no_links

RULESET = '{"ask":[{"toolName":"Bash"}],"version":1}'
RULE_SHA256 = hashlib.sha256(RULESET.encode()).hexdigest()
_REQUIRED = {
    'session': {'id': ('TEXT', 1), 'project_id': ('TEXT', 0), 'workspace_id': ('TEXT', 0),
                'path': ('TEXT', 0), 'directory': ('TEXT', 0)},
    'permission': {'project_id': ('TEXT', 1), 'data': ('TEXT', 0),
                   'time_created': ('INTEGER', 0), 'time_updated': ('INTEGER', 0)},
    'local_setting': {'scope': ('TEXT', 1), 'scope_id': ('TEXT', 2), 'namespace': ('TEXT', 3),
                      'key': ('TEXT', 4), 'value': ('TEXT', 0), 'schema_version': ('INTEGER', 0),
                      'time_created': ('INTEGER', 0), 'time_updated': ('INTEGER', 0)},
}


class ZCodeNativeRuleError(ValueError):
    """The private native restriction could not be established or verified."""


def native_project_id(workspace: Path) -> str:
    # FEe/qae in the SHA-pinned bundle: slugify, then 80 ASCII characters.
    slug = re.sub(r'[^a-z0-9._-]+', '-', str(workspace).lower()).strip('-') or 'session'
    return 'proj_' + slug[:80]


def _check_files(database: Path) -> None:
    if not database.is_absolute() or database.name != 'session.sqlite':
        raise ZCodeNativeRuleError('Invalid ZCode private database path')
    check_no_links(database)
    parent = database.parent.stat()
    if os.name == 'nt':
        from .zcode_windows import verify_private
        verify_private(database.parent)
    elif parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
        raise ZCodeNativeRuleError('ZCode database directory is not private')
    for path in (database, database.with_name(database.name + '-wal'), database.with_name(database.name + '-shm')):
        check_no_links(path)
        if path != database and not path.exists():
            continue
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ZCodeNativeRuleError('ZCode database files must not be linked')
        if os.name == 'nt':
            verify_private(path, allow_inherited_file=True)
        elif info.st_uid != os.geteuid():
            raise ZCodeNativeRuleError('ZCode database has a different owner')


@contextmanager
def _connection(database: Path, *, write: bool = False):
    _check_files(database)
    mode = 'rw' if write else 'ro'
    db = sqlite3.connect(database.as_uri() + '?mode=' + mode, uri=True, timeout=1.0)
    try:
        db.execute('PRAGMA trusted_schema=OFF')
        if write:
            db.execute('PRAGMA synchronous=FULL')  # FULL commit fsyncs the WAL.
        if db.execute('PRAGMA journal_mode').fetchone()[0] != 'wal':
            raise ZCodeNativeRuleError('Unsupported ZCode database journal mode')
        _schema(db)
        yield db
    finally:
        db.close()


def _schema(db: sqlite3.Connection) -> None:
    for table, expected in _REQUIRED.items():
        columns = {row[1]: (row[2].upper(), row[5]) for row in db.execute('PRAGMA table_info(' + table + ')')}
        if any(columns.get(name) != shape for name, shape in expected.items()):
            raise ZCodeNativeRuleError('Unsupported ZCode restriction database schema')
        if table != 'session' and columns.keys() != expected.keys():
            raise ZCodeNativeRuleError('Unsupported ZCode restriction database columns')
        kind = db.execute('SELECT type FROM sqlite_master WHERE name=?', (table,)).fetchone()
        if kind != ('table',) or db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND tbl_name=?', ('trigger', table)).fetchone():
            raise ZCodeNativeRuleError('Unsupported ZCode restriction database object')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ZCodeNativeRuleError('Duplicate ZCode restriction JSON field')
        result[key] = value
    return result


def _exact_json(value, expected: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        decoded = json.loads(value, object_pairs_hook=_unique_object)
        return json.dumps(decoded, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False) == expected
    except (ValueError, TypeError):
        return False


def _audit(db: sqlite3.Connection, workspace: Path, session_id: str, *, bootstrap: bool, initializing: bool) -> dict:
    project = native_project_id(workspace)
    sessions = db.execute('SELECT id,project_id,workspace_id,path,directory FROM session').fetchall()
    expected = (session_id, project, None, str(workspace), str(workspace))
    if initializing and sessions:
        raise ZCodeNativeRuleError('New ZCode native session was unexpectedly persisted')
    if sessions != [expected] and not (bootstrap and sessions == []):
        raise ZCodeNativeRuleError('ZCode restriction session/workspace binding mismatch')
    if db.execute('SELECT 1 FROM permission LIMIT 1').fetchone():
        raise ZCodeNativeRuleError('Legacy ZCode permission rules are not supported')
    rules = None
    for scope, scope_id, key, value, version in db.execute(
            "SELECT scope,scope_id,key,value,schema_version FROM local_setting WHERE namespace='permission'"):
        if scope != 'project' or scope_id != project or version != 1 or key not in ('mode', 'ruleset'):
            raise ZCodeNativeRuleError('Foreign ZCode permission setting')
        if key == 'mode' and (not sessions or not _exact_json(value, '{"mode":"build"}')):
            raise ZCodeNativeRuleError('ZCode private mode setting is not build')
        if key == 'ruleset':
            rules = value
    if initializing:
        if rules is not None:
            raise ZCodeNativeRuleError('New ZCode context already has permission rules')
    elif not _exact_json(rules, RULESET):
        raise ZCodeNativeRuleError('ZCode native Bash restriction changed or is missing')
    return {'source': 'zcode_private_ruleset_0_16_5', 'rule_sha256': RULE_SHA256,
            'project_id': project, 'session_id': session_id, 'session_persisted': bool(sessions), 'journal_mode': 'wal'}


def initialize_bash_restriction(database: Path, workspace: Path, session_id: str) -> dict:
    if os.name == 'nt':
        return _windows_restriction('initialize', database, workspace, session_id)
    return _initialize_bash_restriction(database, workspace, session_id)


def _initialize_bash_restriction(database: Path, workspace: Path, session_id: str) -> dict:
    with _connection(database, write=True) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            _audit(db, workspace, session_id, bootstrap=True, initializing=True)
            stamp = int(time.time() * 1000)
            db.execute("INSERT INTO local_setting(scope,scope_id,namespace,key,value,schema_version,time_created,time_updated) VALUES('project',?,'permission','ruleset',?,1,?,?)",
                       (native_project_id(workspace), RULESET, stamp, stamp))
            db.commit()
        except BaseException:
            db.rollback()
            raise
    # A separate connection proves the committed rule is visible to native reads.
    return _verify_bash_restriction(database, workspace, session_id, bootstrap=True)


def verify_bash_restriction(database: Path, workspace: Path, session_id: str, *, bootstrap: bool = False) -> dict:
    if os.name == 'nt':
        return _windows_restriction('verify', database, workspace, session_id, bootstrap=bootstrap)
    return _verify_bash_restriction(database, workspace, session_id, bootstrap=bootstrap)


def _verify_bash_restriction(database: Path, workspace: Path, session_id: str, *, bootstrap: bool = False) -> dict:
    with _connection(database) as db:
        return _audit(db, workspace, session_id, bootstrap=bootstrap, initializing=False)


def _windows_restriction(action: str, database: Path, workspace: Path, session_id: str, *, bootstrap: bool = False) -> dict:
    # Even a read-only SQLite WAL connection may create sidecars. Keep those
    # creations in a child with the same private default owner as native files.
    try:
        result = subprocess.run(
            [sys.executable, '-I', str(Path(__file__).with_name('zcode_windows.py')),
             '--restriction', action, str(database), str(workspace), session_id, '1' if bootstrap else '0'],
            capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode or len(result.stdout) > 4096:
            raise ZCodeNativeRuleError('ZCode private restriction process failed')
        evidence = json.loads(result.stdout)
        if not isinstance(evidence, dict):
            raise ZCodeNativeRuleError('Invalid ZCode private restriction evidence')
        return evidence
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise ZCodeNativeRuleError('ZCode private restriction process failed') from exc
