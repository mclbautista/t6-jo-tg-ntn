"""Offline copied-snapshot proof. Output contains only counts and check results.

Mount a standalone SQLite backup read-only as --source and a NEW writable --work
folder. Run preview, review its private artifact, then separately authorized apply.
No config/environment secrets or provider/poller imports are needed.
"""
import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from jo_watchdog.ledger.db import Ledger
from jo_watchdog.ledger.reconciliation import apply, preview


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(conn):
    conn.row_factory = sqlite3.Row
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return {table: [dict(r) for r in conn.execute('SELECT * FROM "' +
                    table.replace('"', '""') + '" ORDER BY rowid')]
            for table in tables}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['preview', 'apply'])
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--mode', choices=['gateway', 'direct'], required=True)
    parser.add_argument('--operator')
    parser.add_argument('--reason')
    args = parser.parse_args()
    os.umask(0o077)
    assert args.source.is_file()
    assert not Path(f'{args.source}-wal').exists(), 'use standalone consistent backup'
    assert not Path(f'{args.source}-shm').exists(), 'use standalone consistent backup'
    original_digest = digest(args.source)
    ledger, manifest = args.work / 'ledger.db', args.work / 'approval.json'
    if args.phase == 'preview':
        args.work.mkdir(mode=0o700, parents=False, exist_ok=False)
        # immutable is safe only because this is a standalone consistent backup.
        with sqlite3.connect(f'{args.source.resolve().as_uri()}?immutable=1', uri=True) as src:
            assert src.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            with sqlite3.connect(ledger) as dst:
                src.backup(dst)
        before_digest = digest(ledger)
        result = preview(ledger, manifest)
        assert digest(ledger) == before_digest
        assert not Path(f'{ledger}-wal').exists()
        assert not Path(f'{ledger}-shm').exists()
        assert manifest.stat().st_mode & 0o777 == 0o600
        assert digest(args.source) == original_digest
        print(json.dumps({'phase': 'preview', 'mode': args.mode, **result,
                          'source_unchanged': True, 'fixture_preview_unchanged': True}))
        return
    assert args.operator and args.reason
    assert ledger.is_file() and manifest.is_file()
    with sqlite3.connect(ledger) as conn:
        before = snapshot(conn)
    result = apply(ledger, manifest, operator=args.operator, reason=args.reason)
    assert result['detached'] > 0 and result['already_applied'] == 0
    with sqlite3.connect(ledger) as conn:
        reconciled = snapshot(conn)
        evidence = reconciled['call_reconciliation_evidence']
        originals = {r['id']: r for r in before['llm_calls']}
        approved = {r['call_id'] for r in evidence}
        for row in evidence:
            assert json.loads(row['original_call_json']) == originals[row['call_id']]
            assert row['original_message_id'] == originals[row['call_id']]['message_id']
        assert reconciled['llm_calls'] == [
            {**r, 'message_id': None} if r['id'] in approved else r
            for r in before['llm_calls']]
        for table in before:
            if table != 'llm_calls':
                assert reconciled[table] == before[table]
    Ledger(ledger, gateway_mode=args.mode == 'gateway').close()
    with sqlite3.connect(ledger) as conn:
        after = snapshot(conn)
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    assert max(r['version'] for r in after['schema_version']) == 11
    for table in before:
        if table not in ('schema_version', 'outbox', 'llm_calls'):
            assert after[table] == before[table]
    assert after['llm_calls'] == reconciled['llm_calls']
    assert len(after['outbox']) == len(before['outbox'])
    for old, new in zip(before['outbox'], after['outbox'], strict=True):
        expected = {**old, 'gateway_ref': None, 'accepted_at': None, 'poll_seq': None}
        if (args.mode == 'gateway' and old['state'] == 'sent' and
                old['telegram_message_id'] is not None):
            expected.update(gateway_ref=old['telegram_message_id'], telegram_message_id=None)
        assert new == expected
    assert apply(ledger, manifest, operator=args.operator, reason=args.reason) == {
        'detached': 0, 'already_applied': len(evidence)}
    with sqlite3.connect(ledger) as conn:
        assert snapshot(conn) == after
    # Restore a NEW disposable copy, never replace source or reconciled fixture.
    recovery = args.work / 'restoration-proof.db'
    assert not recovery.exists()
    with sqlite3.connect(f'{args.source.resolve().as_uri()}?immutable=1', uri=True) as src:
        with sqlite3.connect(recovery) as dst:
            src.backup(dst)
            recovered = snapshot(dst)
        assert recovered == snapshot(src)
    assert digest(args.source) == original_digest
    print(json.dumps({'phase': 'apply', 'mode': args.mode, **result,
                      'schema_version': 11, 'integrity_check': 'ok', 'fk_violations': 0,
                      'calls_preserved': len(before['llm_calls']),
                      'messages_preserved': len(before['messages']),
                      'stage_results_preserved': len(before['stage_results']),
                      'outbox_count_preserved': len(before['outbox']),
                      'all_call_nonlink_fields_preserved': True, 'evidence_verified': True,
                      'all_other_preapply_tables_preserved': True, 'cursors_unchanged': True,
                      'outbox_mode_semantics_verified': True, 'rerun_verified_noop': True,
                      'source_unchanged': True, 'backup_restoration_verified': True}))


if __name__ == '__main__':
    main()
