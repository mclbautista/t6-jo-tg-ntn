# Orphan model-call reconciliation (#128)

Historical creation cause is **unknown**. An old orphan can block migration 10,
which rebuilds outbox, without having been created by that migration. Attribution
is separate from recovery. This command is preservation-only: no deletion,
fictional parent messages, replay, offset reset, retries or provider calls.

## Gates and compatibility

Production repair/deployment is **not authorized by this implementation issue**.
Obtain separate owner approval for the exact ledger, maintenance window, consistent
backup, private manifest, operator and reason before any live apply. Quiesce all
writers through an independently approved procedure. Do not stop production as
part of a staging rehearsal. Test a disposable copy first.

Supports contiguous schema versions 1–9, 1–10 and 1–11 only. Other schema versions,
unsupported relationships, partial/unknown audit schemas and integrity errors stop
reconciliation. Unsupported FK violations are never repaired. A manifest containing
blockers is not approval to repair its otherwise supported candidates.

Audit storage is **operator-bootstrap schema format 1**, separate from the numbered
application migration stream. Only explicit apply creates
`call_reconciliation_batches` and `call_reconciliation_evidence`, transactionally
with the detachment, before migration 10. This is not a rewritten shipped migration,
a migration-12 workaround, or a startup repair. Application migrations 1–11 and
schema_version records are unchanged. The additive tables persist across normal
startup and remain inspectable using SQLite. Future audit schema changes must use
an explicit forward-only format upgrade; this command fails on unknown formats.

## Backup and preview

1. Establish an access-controlled working directory (0700), restrictive umask
   (077), and adequate space for temporary copies of the complete ledger and WAL.
   TMPDIR must also be private. Keep backups, manifests, and evidence on the same
   protected host as the database; never commit/upload them or print their rows.
2. Take a **consistent SQLite backup** with SQLite's backup API or an approved
   quiesced procedure. Never copy only a live `.db` while its WAL is in use.
   Verify integrity and restoration on a disposable copy; retain the unmodified
   backup and original files. Preview is not a backup operation.
3. Select an absolute ledger path explicitly; do not invoke application startup
   first. From the candidate environment:

   ```sh
   umask 077
   python -m jo_watchdog.ledger.reconciliation preview \
     --ledger /private/maintenance/ledger.db \
     --manifest /private/maintenance/approval.json
   ```

   Preview copies DB+WAL into a private temporary directory without opening the
   source with SQLite. It checks source DB/WAL/SHM bytes, existence, mode, inode,
   size and modification time around the copy and rejects concurrent changes.
   SQLite only opens the private copy; source permissions and companion existence
   remain unchanged, including for an old ledger. No migrations or masking
   backfills run. Quiesce writers: this is not a long-lived live snapshot service.
   The command prints candidate/blocker **counts**, not identities. A blocker
   count greater than zero means **stop**. The private artifact lists exact FK
   violations, complete original call records, selected path, format and logical
   schema/data SHA-256. It is created exclusively at 0600; existing artifacts are
   not overwritten. Do not hand-edit a manifest to expand/reduce its scope.
4. Privately review every candidate and trustworthy recovery sources. Recover exact
   parents only with independently proven original state using a separately
   approved operation, then generate a new preview. No existing record is presumed
   disposable. When recovery is not supported by trustworthy evidence, obtain
   owner approval to detach precisely the manifest's broken links, retaining calls.
   This manifest is an approval artifact, **not a cryptographic signature**:
   ownership/access control and the separate approval record are essential.

## Separately approved apply

```sh
python -m jo_watchdog.ledger.reconciliation apply \
  --ledger /private/maintenance/ledger.db \
  --manifest /private/maintenance/approval.json \
  --operator 'approved-operator' --reason 'approved unrecoverable links; approval reference'
```

Apply requires owner-only artifact permissions, exact selected path and explicit
nonblank operator/reason. BEGIN IMMEDIATE serializes writers; a complete logical
schema/data fingerprint and the exact candidate set are revalidated **after** the
lock. Any new violation or changed state aborts. Do not automatically regenerate
approval on failure: review a fresh preview separately.

The atomic transaction creates any required audit tables, stores the complete
manifest and its SHA-256 with UTC timestamp/operator/reason, records every full
original call and parent reference, then NULLs only approved still-broken links.
It verifies global FK integrity and that no other business schema/data changed.
Synchronous FULL plus SQLite journaling protect commit; exceptions/process death
before commit roll back DDL, evidence and links together. A completed retry verifies
actual call/evidence state and all current FK violations before returning a no-op;
it does not duplicate evidence or approve new orphans. Reruns allow subsequent
normal migrations and unrelated valid application activity, but changed approved
call fields or new violations remain blockers.

## Verify and normal upgrade

Privately inspect the audit join (do not publish row output):

```sql
SELECT c.*, e.original_message_id, e.original_call_json,
       b.operator, b.reason, b.applied_at, b.manifest_sha256
FROM llm_calls AS c
LEFT JOIN call_reconciliation_evidence AS e ON e.call_id = c.id
LEFT JOIN call_reconciliation_batches AS b USING (manifest_sha256);
PRAGMA integrity_check;
PRAGMA foreign_key_check;
```

Calls remain in `llm_calls`, including ordinary time/provider history queries.
All non-link fields and call counts must match the backup. Evidence holds the
original full records and original FK even though the live FK is NULL. Check
valid links, pre-existing NULLs, message/stage counts, cursor values and outbox
counts independently. Only after this verification should an **independently
approved** normal startup upgrade run in the intended gateway/direct mode.
Pre-existing FK violations now fail before migration mutations with an offline
preview action; violations introduced during a migration still fail its transaction.
The global protection is retained (and checked for every migration).

Migrations 10/11 must finish, integrity must be `ok`, and FK check must be empty.
Gateway mode preserves historical gateway references separately from Telegram IDs;
direct mode must retain real Telegram IDs. Neither the reconciliation command nor
the isolated staging harness starts a poller/worker or imports provider configuration.

## Isolated copied-data rehearsal

The repository harness verifies both approved detachment and the subsequent shipped
upgrade, including every original table, call non-link fields, cursors, outbox
interpretation, rerun and restoration into another new disposable copy:

```sh
python -m tools.rehearse_orphan_reconciliation preview \
  --source /private/backup/original.db --work /private/rehearsal/new-gateway --mode gateway
# Review the protected new-gateway/approval.json; obtain separate apply approval.
python -m tools.rehearse_orphan_reconciliation apply \
  --source /private/backup/original.db --work /private/rehearsal/new-gateway --mode gateway \
  --operator 'approved-operator' --reason 'approved disposable copied-data rehearsal'
```

Use a standalone consistent migrations-1–9 backup with no WAL/SHM companions and
at least one approved orphan; the work directory must not exist before preview.
Run direct mode separately with another new work directory. The harness is a strict
acceptance assertion tool, not a production repair command; do not run Python with
`-O` (which disables its assertions). Keep all artifacts on the protected host.
For container rehearsal use a unique image/container, network `none`, no ports,
no production mounts, no credentials, no Docker socket, restart `no`, 0.5 CPU and
512 MB. Mount only the immutable staging backup read-only and new disposable work
and candidate code. The harness imports ledger code only, never application loops.
Gateway conversion applies only to historical `sent` rows with an ID; pending and
failed IDs stay unchanged. Migration 11 adds nullable `poll_seq` (not zero).

## Recovery limits

Keep the pre-operation consistent backup and exact candidate version. To abandon a
failed uncommitted apply, close it and let SQLite recover its journal; verify the
original state before retrying. Never delete journal/WAL files to force recovery.
A successful apply is not undone by putting back an old image. Restoring original
broken FK values would intentionally reintroduce violations and is **not** an
approved automatic rollback. Evidence supports forensic inspection and separately
planned restoration if original parents can later be proved.

After migration 10, outbox semantics changed. **An old image alone is not a safe
rollback**. Restore the entire consistent pre-upgrade ledger with its matching
application/configuration under separate owner approval, accounting for all work
since the backup and duplicate-delivery risks. Do not combine old DB, new WAL, or
mixed schema tables. Copied-data proof is offline recovery evidence, not provider
integration, live maintenance authorization, or downgrade-safety proof.
