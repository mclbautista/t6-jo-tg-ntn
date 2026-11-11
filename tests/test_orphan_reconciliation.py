"""Synthetic operational acceptance tests; no real records or providers."""
import sqlite3

import pytest

from jo_watchdog.ledger.db import Ledger
from jo_watchdog.ledger.migrations import MIGRATIONS


def deployed(path):
    conn = sqlite3.connect(path, isolation_level=None)
    for version, migration in MIGRATIONS.items():
        if version > 9:
            break
        conn.execute("BEGIN")
        if callable(migration):
            migration(conn)
        else:
            for statement in migration:
                conn.execute(statement)
        conn.execute("INSERT INTO schema_version VALUES (?, 'synthetic')", (version,))
        conn.execute("COMMIT")
    conn.execute("INSERT INTO offsets VALUES (1, 123)")
    conn.execute("INSERT INTO llm_calls (id,message_id,provider,purpose,requested_model,"
                 "prompt_tag,masker_version,outcome,detail_json,created_at) VALUES "
                 "(1,999,'synthetic','extract','model','tag','v1','timeout','{}','synthetic')")
    return conn


def test_preview_old_schema_is_private_and_read_only(tmp_path):
    from jo_watchdog.ledger.reconciliation import preview

    path = tmp_path / 'ledger.db'
    conn = deployed(path)
    before = path.read_bytes()
    manifest = tmp_path / 'approval.json'
    assert preview(path, manifest) == {'candidates': 1, 'blockers': 0}
    assert path.read_bytes() == before
    assert not (tmp_path / 'ledger.db-wal').exists()
    assert not (tmp_path / 'ledger.db-shm').exists()
    assert manifest.stat().st_mode & 0o777 == 0o600
    assert conn.execute('SELECT max(version) FROM schema_version').fetchone()[0] == 9
    conn.close()


@pytest.mark.parametrize('gateway', [True, False])
def test_preserve_apply_rerun_and_upgrade(tmp_path, gateway):
    import json

    from jo_watchdog.ledger.reconciliation import apply, preview

    path, manifest = tmp_path / 'ledger.db', tmp_path / 'approval.json'
    conn = deployed(path)
    conn.execute("INSERT INTO messages (id,chat_id,message_id,telegram_key,update_id,raw_source,"
                 "text,first_line,state,masked_text,masker_version,masked_count,"
                 "created_at,updated_at) "
                 "VALUES (2,1,2,'1:2',5,'{}','synthetic','synthetic','done',"
                 "'synthetic','v1',0,'synthetic','synthetic')")
    for call_id, parent in [(2, 2), (3, None)]:
        conn.execute('INSERT INTO llm_calls SELECT ?,?,provider,purpose,requested_model,'
                     'returned_model,prompt_tag,masker_version,input_tokens,output_tokens,'
                     'latency_ms,outcome,error,detail_json,created_at FROM llm_calls WHERE id=1',
                     (call_id, parent))
    conn.execute("INSERT INTO outbox (kind,chat_id,topic_id,text,state,telegram_message_id,"
                 "created_at) VALUES ('heartbeat',1,1,'synthetic','sent',42,'synthetic')")
    conn.row_factory = sqlite3.Row
    before = [dict(r) for r in conn.execute('SELECT * FROM llm_calls ORDER BY id')]
    other = {t: [tuple(r) for r in conn.execute(f'SELECT * FROM {t}')]
             for t in ('messages', 'offsets', 'stage_results')}
    preview(path, manifest)
    assert apply(path, manifest, operator='synthetic', reason='approved unrecoverable') == {
        'detached': 1, 'already_applied': 0}
    assert [dict(r) for r in conn.execute('SELECT * FROM llm_calls ORDER BY id')] == [
        {**before[0], 'message_id': None}, *before[1:]]
    evidence = conn.execute('SELECT * FROM call_reconciliation_evidence').fetchone()
    assert evidence['original_message_id'] == 999
    assert json.loads(evidence['original_call_json']) == before[0]
    batch = conn.execute('SELECT * FROM call_reconciliation_batches').fetchone()
    assert batch['operator'] == 'synthetic'
    assert batch['reason'] == 'approved unrecoverable'
    assert batch['applied_at']
    upgraded = Ledger(path, gateway_mode=gateway)
    assert conn.execute('SELECT max(version) FROM schema_version').fetchone()[0] == 11
    assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    outbox = conn.execute('SELECT * FROM outbox').fetchone()
    assert outbox['telegram_message_id'] == (None if gateway else 42)
    assert outbox['gateway_ref'] == (42 if gateway else None)
    for table, expected in other.items():
        assert [tuple(r) for r in conn.execute(f'SELECT * FROM {table}')] == expected
    assert apply(path, manifest, operator='other', reason='rerun') == {
        'detached': 0, 'already_applied': 1}
    assert conn.execute('SELECT count(*) FROM call_reconciliation_evidence').fetchone()[0] == 1
    upgraded.close()
    conn.close()


@pytest.mark.parametrize('gateway', [True, False])
@pytest.mark.parametrize('parent', ['valid', 'null'])
def test_unbroken_deployed_controls_upgrade(tmp_path, gateway, parent):
    path = tmp_path / 'ledger.db'
    conn = deployed(path)
    if parent == 'valid':
        conn.execute("INSERT INTO messages (id,chat_id,message_id,telegram_key,update_id,"
                     "raw_source,text,first_line,state,masked_text,masker_version,masked_count,"
                     "created_at,updated_at) VALUES (999,1,2,'1:2',5,'{}','synthetic',"
                     "'synthetic','done','synthetic','v1',0,'synthetic','synthetic')")
    else:
        conn.execute('UPDATE llm_calls SET message_id=NULL')
    before = list(conn.execute('SELECT * FROM llm_calls'))
    Ledger(path, gateway_mode=gateway).close()
    assert list(conn.execute('SELECT * FROM llm_calls')) == before
    assert conn.execute('SELECT max(version) FROM schema_version').fetchone()[0] == 11
    assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    conn.close()


@pytest.mark.parametrize('schema', ['partial', 'trigger'])
def test_unknown_audit_schema_blocks_without_mutation(tmp_path, schema):
    from jo_watchdog.ledger.reconciliation import AUDIT_DDL, ReconciliationError, apply, preview

    path, manifest = tmp_path / 'ledger.db', tmp_path / 'approval.json'
    conn = deployed(path)
    preview(path, manifest)
    conn.execute(AUDIT_DDL[0])
    if schema == 'trigger':
        conn.execute(AUDIT_DDL[1])
        conn.execute('CREATE TRIGGER unapproved AFTER INSERT ON call_reconciliation_batches '
                     'BEGIN UPDATE offsets SET last_committed_offset=0; END')
    before = list(conn.iterdump())
    with pytest.raises(ReconciliationError, match='audit schema'):
        apply(path, manifest, operator='synthetic', reason='approved')
    assert list(conn.iterdump()) == before
    with pytest.raises(ReconciliationError, match='audit schema'):
        preview(path, tmp_path / 'new.json')
    assert not (tmp_path / 'new.json').exists()
    conn.close()


def test_startup_diagnoses_preexisting_before_migration(tmp_path):
    path = tmp_path / 'ledger.db'
    conn = deployed(path)
    before = list(conn.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match='pre-existing.*reconciliation'):
        Ledger(path)
    assert list(conn.iterdump()) == before
    conn.close()


@pytest.mark.parametrize('change', ['call', 'new_orphan', 'unsupported', 'cursor', 'valid_parent'])
def test_changed_state_rejected_atomically(tmp_path, change):
    from jo_watchdog.ledger.reconciliation import ReconciliationError, apply, preview

    path, manifest = tmp_path / 'ledger.db', tmp_path / 'approval.json'
    conn = deployed(path)
    preview(path, manifest)
    if change == 'call':
        conn.execute("UPDATE llm_calls SET error='new state'")
    elif change == 'new_orphan':
        conn.execute('UPDATE llm_calls SET id=2')
    elif change == 'unsupported':
        conn.execute("INSERT INTO stage_results VALUES (1,999,'extract','{}','synthetic')")
    elif change == 'cursor':
        conn.execute('UPDATE offsets SET last_committed_offset=124')
    else:
        conn.execute('UPDATE llm_calls SET message_id=NULL')
    before = list(conn.iterdump())
    with pytest.raises(ReconciliationError):
        apply(path, manifest, operator='synthetic', reason='approved')
    assert list(conn.iterdump()) == before
    conn.close()


def test_preview_active_wal_and_permissions_unchanged(tmp_path):
    from jo_watchdog.ledger.reconciliation import preview

    path = tmp_path / 'ledger.db'
    conn = deployed(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute("UPDATE llm_calls SET error='uncheckpointed'")
    paths = [path, tmp_path / 'ledger.db-wal', tmp_path / 'ledger.db-shm']
    for item in paths:
        item.chmod(0o400)
    tmp_path.chmod(0o500)
    before = [(p.read_bytes(), p.stat().st_mode) for p in paths]
    # Artifact is deliberately outside the protected source directory.
    manifest = tmp_path.parent / (tmp_path.name + '-approval.json')
    try:
        assert preview(path, manifest) == {'candidates': 1, 'blockers': 0}
        assert [(p.read_bytes(), p.stat().st_mode) for p in paths] == before
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            'ledger.db', 'ledger.db-shm', 'ledger.db-wal']
    finally:
        tmp_path.chmod(0o700)
        for item in paths:
            item.chmod(0o600)
        conn.close()


def test_unsupported_preview_is_explicit_blocker(tmp_path):
    from jo_watchdog.ledger.reconciliation import ReconciliationError, apply, preview

    path, manifest = tmp_path / 'ledger.db', tmp_path / 'approval.json'
    conn = deployed(path)
    conn.execute("INSERT INTO stage_results VALUES (1,999,'extract','{}','synthetic')")
    before = list(conn.iterdump())
    assert preview(path, manifest) == {'candidates': 1, 'blockers': 1}
    with pytest.raises(ReconciliationError, match='unsupported'):
        apply(path, manifest, operator='synthetic', reason='approved')
    assert list(conn.iterdump()) == before
    conn.close()


def test_interruption_rolls_back_evidence_schema_and_link(tmp_path):
    from jo_watchdog.ledger.reconciliation import apply, preview

    path, manifest = tmp_path / 'ledger.db', tmp_path / 'approval.json'
    conn = deployed(path)
    conn.execute("CREATE TRIGGER interrupt_detachment BEFORE UPDATE ON llm_calls "
                 "BEGIN SELECT RAISE(ABORT, 'synthetic interruption'); END")
    preview(path, manifest)
    before = list(conn.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match='synthetic interruption'):
        apply(path, manifest, operator='synthetic', reason='approved')
    assert list(conn.iterdump()) == before
    conn.close()


def test_rerun_cannot_approve_new_orphan(tmp_path):
    from jo_watchdog.ledger.reconciliation import ReconciliationError, apply, preview

    path, manifest = tmp_path / 'ledger.db', tmp_path / 'approval.json'
    conn = deployed(path)
    preview(path, manifest)
    apply(path, manifest, operator='synthetic', reason='approved')
    conn.execute('UPDATE llm_calls SET message_id=998')
    before = list(conn.iterdump())
    with pytest.raises(ReconciliationError, match='no longer matches'):
        apply(path, manifest, operator='synthetic', reason='rerun')
    assert list(conn.iterdump()) == before
    conn.close()


def test_trigger_cannot_change_unapproved_fields(tmp_path):
    from jo_watchdog.ledger.reconciliation import ReconciliationError, apply, preview

    path, manifest = tmp_path / 'ledger.db', tmp_path / 'approval.json'
    conn = deployed(path)
    conn.execute("CREATE TRIGGER unexpected_change AFTER UPDATE ON llm_calls "
                 "BEGIN UPDATE offsets SET last_committed_offset=456; END")
    preview(path, manifest)
    before = list(conn.iterdump())
    with pytest.raises(ReconciliationError, match='unapproved'):
        apply(path, manifest, operator='synthetic', reason='approved')
    assert list(conn.iterdump()) == before
    conn.close()


def test_new_nonrebuild_migration_violation_is_detected(tmp_path, monkeypatch):
    import jo_watchdog.ledger.db as db

    path = tmp_path / 'ledger.db'
    conn = deployed(path)
    conn.execute('UPDATE llm_calls SET message_id=NULL')
    before = list(conn.iterdump())
    # A buggy migration could turn enforcement off itself before opening a new
    # transaction, so simulate a deferred FK that only the global check catches.
    def broken(connection):
        connection.execute('PRAGMA defer_foreign_keys=ON')
        connection.execute('UPDATE llm_calls SET message_id=999')
    monkeypatch.setattr(db, 'MIGRATIONS', {10: broken})
    monkeypatch.setattr(db, 'REBUILDS_TABLES', set())
    with pytest.raises(sqlite3.IntegrityError, match='migration 10 left dangling'):
        Ledger(path)
    assert list(conn.iterdump()) == before
    conn.close()


def test_crash_mid_batch_recovers_without_partial_audit(tmp_path):
    import subprocess
    import sys

    from jo_watchdog.ledger.reconciliation import apply, preview

    path, manifest = tmp_path / 'ledger.db', tmp_path / 'approval.json'
    conn = deployed(path)
    # Two calls ensure this is a genuinely partial batch before process death.
    conn.execute('INSERT INTO llm_calls SELECT 2,message_id,provider,purpose,requested_model,'
                 'returned_model,prompt_tag,masker_version,input_tokens,output_tokens,'
                 'latency_ms,outcome,error,detail_json,created_at FROM llm_calls WHERE id=1')
    conn.execute("CREATE TRIGGER crash_second BEFORE UPDATE ON llm_calls WHEN OLD.id=2 "
                 "BEGIN SELECT crash_now(); END")
    preview(path, manifest)
    before = list(conn.iterdump())
    code = '''
import os, signal, sqlite3, sys
from jo_watchdog.ledger.reconciliation import apply
original = sqlite3.connect
def connect(*args, **kwargs):
    conn = original(*args, **kwargs)
    conn.create_function('crash_now', 0, lambda: os.kill(os.getpid(), signal.SIGKILL))
    return conn
sqlite3.connect = connect
apply(sys.argv[1], sys.argv[2], operator='synthetic', reason='approved crash test')
'''
    result = subprocess.run([sys.executable, '-c', code, str(path), str(manifest)],
                            capture_output=True)
    assert result.returncode == -9
    assert list(conn.iterdump()) == before
    conn.close()
    # A normal apply cannot execute the unknown UDF: it must still roll back.
    with pytest.raises(sqlite3.OperationalError):
        apply(path, manifest, operator='synthetic', reason='retry')
    with sqlite3.connect(path) as check:
        assert list(check.iterdump()) == before


def test_concurrent_writer_is_revalidated_after_lock(tmp_path):
    import subprocess
    import sys

    from jo_watchdog.ledger.reconciliation import preview

    path, manifest = tmp_path / 'ledger.db', tmp_path / 'approval.json'
    conn = deployed(path)
    preview(path, manifest)
    conn.execute('BEGIN IMMEDIATE')
    code = '''
import sys
from jo_watchdog.ledger.reconciliation import main
print('ready', flush=True)
sys.exit(main(['apply','--ledger',sys.argv[1],'--manifest',sys.argv[2],
              '--operator','synthetic','--reason','approved']))
'''
    process = subprocess.Popen([sys.executable, '-c', code, str(path), str(manifest)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == 'ready'
        conn.execute('UPDATE offsets SET last_committed_offset=789')
        conn.execute('COMMIT')
        before = list(conn.iterdump())
        output, _ = process.communicate(timeout=10)
        assert process.returncode == 1
        assert 'stale manifest' in output
        assert list(conn.iterdump()) == before
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        conn.close()


@pytest.mark.parametrize('violation', ['new', 'changed'])
def test_completed_rerun_verifies_actual_calls(tmp_path, violation):
    from jo_watchdog.ledger.reconciliation import ReconciliationError, apply, preview

    path, manifest = tmp_path / 'ledger.db', tmp_path / 'approval.json'
    conn = deployed(path)
    preview(path, manifest)
    apply(path, manifest, operator='synthetic', reason='approved')
    if violation == 'new':
        conn.execute('INSERT INTO llm_calls SELECT 2,888,provider,purpose,requested_model,'
                     'returned_model,prompt_tag,masker_version,input_tokens,output_tokens,'
                     'latency_ms,outcome,error,detail_json,created_at FROM llm_calls WHERE id=1')
    else:
        conn.execute("UPDATE llm_calls SET outcome='changed'")
    before = list(conn.iterdump())
    with pytest.raises(ReconciliationError):
        apply(path, manifest, operator='synthetic', reason='rerun')
    assert list(conn.iterdump()) == before
    conn.close()


@pytest.mark.parametrize('mode', ['gateway', 'direct'])
def test_rehearsal_cli_preserves_shipped_outbox_semantics(tmp_path, mode):
    import json
    import subprocess
    import sys
    from pathlib import Path

    source = tmp_path / 'source.db'
    conn = deployed(source)
    for state in ('sent', 'pending', 'failed'):
        conn.execute("INSERT INTO outbox (kind,chat_id,topic_id,text,state,telegram_message_id,"
                     "created_at) VALUES ('heartbeat',1,1,'synthetic',?,42,'synthetic')", (state,))
    conn.close()
    work = tmp_path / mode
    command = [sys.executable, '-m', 'tools.rehearse_orphan_reconciliation']
    common = ['--source', str(source), '--work', str(work), '--mode', mode]
    root = Path(__file__).resolve().parents[1]
    for phase in ('preview', 'apply'):
        extra = (['--operator', 'synthetic', '--reason', 'approved fixture']
                 if phase == 'apply' else [])
        result = subprocess.run([*command, phase, *common, *extra], cwd=root,
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        output = json.loads(result.stdout)
        assert output['phase'] == phase
        assert output['source_unchanged'] is True
    assert output['outbox_mode_semantics_verified'] is True
    assert output['cursors_unchanged'] is True
    assert output['rerun_verified_noop'] is True


def test_new_migration_violation_rolls_back(tmp_path, monkeypatch):
    import jo_watchdog.ledger.db as db

    path = tmp_path / 'ledger.db'
    conn = deployed(path)
    conn.execute('UPDATE llm_calls SET message_id=NULL')
    before = list(conn.iterdump())
    def broken_migration(connection):
        connection.execute('UPDATE llm_calls SET message_id=999')
    monkeypatch.setattr(db, 'MIGRATIONS', {10: broken_migration})
    monkeypatch.setattr(db, 'REBUILDS_TABLES', {10})
    with pytest.raises(sqlite3.IntegrityError, match='migration 10 left dangling'):
        Ledger(path)
    assert list(conn.iterdump()) == before
    conn.close()
