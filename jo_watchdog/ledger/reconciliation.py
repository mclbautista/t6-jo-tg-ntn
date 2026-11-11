"""Offline, preservation-only orphan-call reconciliation (issue #128).

Never opens a Ledger, imports app configuration, or contacts a provider. Audit
storage is an explicit operator bootstrap, not a modification of shipped migrations.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

AUDIT_TABLES = {'call_reconciliation_batches', 'call_reconciliation_evidence'}
AUDIT_DDL = (
    "CREATE TABLE call_reconciliation_batches ("
    "manifest_sha256 TEXT PRIMARY KEY, format_version INTEGER NOT NULL CHECK(format_version=1),"
    "manifest_json TEXT NOT NULL, operator TEXT NOT NULL, reason TEXT NOT NULL,"
    "applied_at TEXT NOT NULL)",
    "CREATE TABLE call_reconciliation_evidence ("
    "call_id INTEGER PRIMARY KEY REFERENCES llm_calls(id),"
    "original_message_id INTEGER NOT NULL, original_call_json TEXT NOT NULL,"
    "manifest_sha256 TEXT NOT NULL REFERENCES call_reconciliation_batches(manifest_sha256))",
)


class ReconciliationError(RuntimeError):
    """A privacy-safe diagnostic: no private row identities in command output."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      default=lambda b: {'sqlite_blob_base64': base64.b64encode(b).decode()})


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _files(path):
    """Stable-copy guard; SHM is observed but never opened by SQLite."""
    result = []
    for name in (path, Path(f'{path}-wal'), Path(f'{path}-shm')):
        try:
            stat = name.stat()
            result.append((stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_mode,
                           hashlib.sha256(name.read_bytes()).hexdigest()))
        except FileNotFoundError:
            result.append(None)
    return result


@contextmanager
def _snapshot(path):
    # mode=ro on the source can create companions. Copy DB+WAL instead, and
    # reject changes during copying. Operators must quiesce writers for review.
    if not path.is_file():
        raise ReconciliationError('selected ledger does not exist')
    with tempfile.TemporaryDirectory(prefix='jo-reconcile-') as directory:
        copy = Path(directory) / 'ledger.db'
        before = _files(path)
        shutil.copyfile(path, copy)
        wal = Path(f'{path}-wal')
        if wal.exists():
            shutil.copyfile(wal, Path(f'{copy}-wal'))
        if before != _files(path):
            raise ReconciliationError('ledger changed during preview; quiesce writers and retry')
        conn = sqlite3.connect(copy, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            yield conn
        finally:
            conn.close()


def _state(conn, *, detach=(), include_audit=True):
    schema = [tuple(r) for r in conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")
        if include_audit or r['tbl_name'] not in AUDIT_TABLES]
    tables = [r[1] for r in schema if r[0] == 'table']
    rows = {}
    for table in tables:
        quoted = table.replace('"', '""')
        records = []
        for row in conn.execute(f'SELECT * FROM "{quoted}"'):
            values = dict(row)
            if table == 'llm_calls' and values['id'] in detach:
                values['message_id'] = None
            records.append(_json(tuple(values.values())))
        rows[table] = sorted(records)
    return _digest({'schema': schema, 'rows': rows})


def _inspect(conn):
    versions = [r[0] for r in conn.execute('SELECT version FROM schema_version ORDER BY version')]
    if versions not in [list(range(1, n + 1)) for n in (9, 10, 11)]:
        raise ReconciliationError(
            'unsupported schema; expected contiguous migrations 1–9, 10 or 11')
    if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise ReconciliationError('ledger integrity check failed')
    audit_schema = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name IN "
        "('call_reconciliation_batches','call_reconciliation_evidence') "
        "AND type IN ('table','trigger')")]
    if audit_schema and set(audit_schema) != set(AUDIT_DDL):
        raise ReconciliationError('unsupported reconciliation audit schema')
    candidates, blockers = [], []
    for violation in conn.execute('PRAGMA foreign_key_check'):
        table, rowid, parent, fk_id = violation
        keys = list(conn.execute('PRAGMA foreign_key_list(llm_calls)'))
        supported = (table == 'llm_calls' and parent == 'messages' and
                     any(k[0] == fk_id and k[1] == 0 and k[3] == 'message_id' and
                         k[4] == 'id' for k in keys))
        if not supported:
            blockers.append(tuple(violation))
        else:
            row = conn.execute('SELECT * FROM llm_calls WHERE rowid=?', (rowid,)).fetchone()
            candidates.append(dict(row))
    return sorted(candidates, key=lambda r: r['id']), blockers


def preview(ledger: str | Path, manifest: str | Path) -> dict:
    """Create a new owner-only exact-state approval artifact; never overwrite one."""
    path = Path(ledger).resolve()
    target = Path(manifest)
    # Manifest must not replace the source or its companions, even via symlink.
    if target.resolve() in {path, Path(f'{path}-wal'), Path(f'{path}-shm')}:
        raise ReconciliationError('manifest must be separate from ledger files')
    with _snapshot(path) as conn:
        calls, blockers = _inspect(conn)
        payload = {'format_version': 1, 'ledger': str(path), 'state_sha256': _state(conn),
                   'calls': calls, 'blockers': blockers}
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(_json(payload) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    return {'candidates': len(calls), 'blockers': len(blockers)}


def apply(ledger: str | Path, manifest: str | Path, *, operator: str, reason: str) -> dict:
    """Apply separately approved evidence atomically under an immediate writer lock."""
    if not operator.strip() or not reason.strip():
        raise ReconciliationError('operator and reason are required')
    path = Path(ledger).resolve()
    artifact = Path(manifest)
    if artifact.stat().st_mode & 0o077:
        raise ReconciliationError('manifest must be owner-only')
    payload = json.loads(artifact.read_text())
    if (set(payload) != {'format_version', 'ledger', 'state_sha256', 'calls', 'blockers'} or
            payload['format_version'] != 1 or payload['ledger'] != str(path)):
        raise ReconciliationError('manifest format or selected ledger does not match')
    if not path.is_file():
        raise ReconciliationError('selected ledger does not exist')
    sha = _digest(payload)
    conn = sqlite3.connect(f'{path.as_uri()}?mode=rw', uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('BEGIN IMMEDIATE')
        calls, blockers = _inspect(conn)
        if blockers or payload['blockers']:
            raise ReconciliationError('unsupported foreign-key violations block reconciliation')
        audit_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                    "AND name='call_reconciliation_batches'").fetchone()
        completed = (conn.execute('SELECT * FROM call_reconciliation_batches '
                                  'WHERE manifest_sha256=?', (sha,)).fetchone()
                     if audit_exists else None)
        if completed:
            if completed['format_version'] != 1 or completed['manifest_json'] != _json(payload):
                raise ReconciliationError('completed reconciliation provenance changed')
            evidence = list(conn.execute('SELECT * FROM call_reconciliation_evidence '
                                         'WHERE manifest_sha256=? ORDER BY call_id', (sha,)))
            if calls or len(evidence) != len(payload['calls']):
                raise ReconciliationError('completed manifest no longer matches ledger')
            for original, stored in zip(payload['calls'], evidence, strict=True):
                current = conn.execute('SELECT * FROM llm_calls WHERE id=?',
                                       (original['id'],)).fetchone()
                if (stored['call_id'] != original['id'] or current is None or
                        dict(current) != {**original, 'message_id': None} or
                        stored['original_call_json'] != _json(original) or
                        stored['original_message_id'] != original['message_id']):
                    raise ReconciliationError('completed reconciliation evidence or call changed')
            conn.execute('ROLLBACK')
            return {'detached': 0, 'already_applied': len(evidence)}
        if calls != payload['calls'] or _state(conn) != payload['state_sha256']:
            raise ReconciliationError('stale manifest; state or candidate set changed')
        expected_state = _state(conn, detach={call['id'] for call in calls},
                                include_audit=False)
        if not audit_exists:
            for statement in AUDIT_DDL:
                conn.execute(statement)
        conn.execute('INSERT INTO call_reconciliation_batches VALUES (?,1,?,?,?,?)',
                     (sha, _json(payload), operator, reason, datetime.now(UTC).isoformat()))
        for call in calls:
            conn.execute('INSERT INTO call_reconciliation_evidence VALUES (?,?,?,?)',
                         (call['id'], call['message_id'], _json(call), sha))
            conn.execute('UPDATE llm_calls SET message_id=NULL WHERE id=? AND message_id=?',
                         (call['id'], call['message_id']))
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ReconciliationError('foreign-key violation after reconciliation; rolled back')
        if _state(conn, include_audit=False) != expected_state:
            raise ReconciliationError('unapproved state change; reconciliation rolled back')
        conn.execute('COMMIT')
        return {'detached': len(calls), 'already_applied': 0}
    finally:
        # close rolls back even BaseException/interruption, including transactional DDL.
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['preview', 'apply'])
    parser.add_argument('--ledger', required=True)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--operator')
    parser.add_argument('--reason')
    args = parser.parse_args(argv)
    try:
        if args.operation == 'preview':
            result = preview(args.ledger, args.manifest)
        else:
            if not args.operator or not args.reason:
                parser.error('apply requires --operator and --reason')
            result = apply(args.ledger, args.manifest, operator=args.operator, reason=args.reason)
        print(_json(result))
        return 0
    except ReconciliationError as exc:
        print(str(exc))
        return 1
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
        # Raw SQLite/JSON/path errors may contain private values. Detail is in the
        # protected manifest; command failures never print row identities or data.
        print('Reconciliation refused; check schema, permissions, blockers and manifest state.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
