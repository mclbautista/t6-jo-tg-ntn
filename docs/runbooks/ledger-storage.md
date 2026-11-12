# Ledger storage, backup and restore (#131)

## What went wrong

On 2026-11-12 the production `ledger.db` on Sixto failed `PRAGMA integrity_check`.
The file lived in `./data`, a bind mount from macOS into the Colima VM. SQLite's
file locks and its shared-memory WAL index do not work across that mount:

- `sqlite3` on the macOS side saw a stale view of the file (an empty WAL while the
  VM had a 2.7 MB one).
- A capture on 2026-11-11 ran `sqlite3` from macOS against the live database,
  including a WAL checkpoint. A write from outside the VM while the watchdog holds
  the file open is the likely cause.

## Rules

1. The ledger lives in the `jo-watchdog-data` named volume, inside the VM. Do not
   change `compose.yaml` back to a host path. The volume is declared `external`,
   so `docker compose up` fails until it exists (a deploy cannot start on an empty
   ledger by accident) and `docker compose down -v` cannot delete it.
2. Nothing on macOS opens the live ledger: no `sqlite3`, no DB browser, no `cp`
   of a running database, no sync or backup tool pointed at it.
3. To read it while the watchdog runs, read it in the watchdog's container
   (`docker compose exec watchdog python -c "import sqlite3; ..."`), or take a
   backup and read the backup.
4. Copies leave the container only as a finished backup file (below).

## Back up (watchdog running or stopped)

```sh
docker compose exec watchdog python -m jo_watchdog.app backup /tmp/ledger-backup.db
docker compose cp watchdog:/tmp/ledger-backup.db "./ledger-backup-$(date +%F).db"
docker compose exec watchdog rm /tmp/ledger-backup.db
chmod 600 ledger-backup-*.db
```

`backup` uses SQLite's online backup API from a connection in the same container,
so the copy is consistent while the worker keeps writing. It refuses to overwrite
an existing file, runs `integrity_check` on the copy, and exits 1 if the copy is
damaged. The result is one file with no `-wal` or `-shm`, safe to copy to macOS.
It holds raw JO text: keep it owner-only and never commit it.

If the watchdog is stopped, `docker compose run --rm --no-deps watchdog backup
/app/data/ledger-backup.db` leaves the copy in the volume, next to the ledger.

## Startup check

Every start runs `PRAGMA quick_check` before any migration or backfill. A damaged
file stops the boot with:

```
ledger: the ledger at /app/data/ledger.db failed its startup integrity check (...)
```

The exit code is 1, so the container restarts and repeats that line; nothing is
written to the file. Run `docker compose stop watchdog`, keep the damaged file,
and restore.

## Restore a backup

A helper container mounts the backup file read-only and installs it in the volume.
The damaged files are moved aside in the volume, never deleted.

```sh
docker compose stop watchdog
docker compose run --rm --no-deps --user root --entrypoint sh \
  -v "$PWD/ledger-backup-YYYY-MM-DD.db:/restore.db:ro" watchdog -c '
  set -e
  cd /app/data
  aside="damaged-$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir "$aside"
  for f in ledger.db ledger.db-wal ledger.db-shm; do
    if [ -e "$f" ]; then mv "$f" "$aside/"; fi
  done
  cp /restore.db ledger.db
  chown -R 1000:1000 ledger.db "$aside"
  chmod 600 ledger.db
'
docker compose up -d
docker compose logs --tail 20 watchdog
```

Messages newer than the backup are fetched again: the offset in the backup is
older, the upsert only moves it forward, and `telegram_key` is unique, so replayed
updates are deduplicated.

## Move an existing `./data` into the volume (once per host)

For a host that still has the ledger in the bind-mounted `./data`. This changes
production data, so it needs the owner's approval and a quiet moment. Until it is
done, the new `compose.yaml` refuses to start (`external volume "jo-watchdog-data"
not found`), which is the intended guard: a watchdog that started on an empty
volume would begin from offset 0.

```sh
docker compose stop watchdog              # nothing may write during the copy
git pull                                  # brings the compose.yaml with the named volume
docker volume create jo-watchdog-data
docker compose run --rm --no-deps --user root --entrypoint sh \
  -v "$PWD/data:/from:ro" watchdog -c '
  set -e
  test ! -e /app/data/ledger.db           # never overwrite a ledger already in the volume
  cp -a /from/. /app/data/
  chown -R 1000:1000 /app/data
  chmod 700 /app/data
  find /app/data -type f -exec chmod 600 {} +
'
docker compose run --rm --no-deps --entrypoint python watchdog -c "
import sqlite3
print(sqlite3.connect('/app/data/ledger.db').execute('PRAGMA integrity_check').fetchone()[0])
"                                         # must print: ok
docker compose up -d --build
docker compose ps                         # (healthy) within about two minutes
```

The copy reads the old folder with the watchdog stopped, so no lock is involved.
Keep the old `./data` untouched until the watchdog has run healthy for a day,
then move it somewhere private; it still holds raw JO text.
