# JO Telegram Watchdog

Durable Telegram-to-Notion capture for Job Orders. One Python 3.12 asyncio
process polls a configured Telegram intake topic, commits each in-scope message
to a SQLite ledger before advancing the polling offset, plans the exact Notion
payload, writes it once, reads it back, and acknowledges with a fixed-meaning
"captured" reaction.

This repository is being built ticket-by-ticket. The walking skeleton (this
slice) proves the ledger, the commit-before-offset ordering, and the Notion
adapter's shape. Classification (DeepSeek), credential masking, resolution,
verification, reliability, and Deliverable matching arrive in later tickets.

## Layout

```
jo_watchdog/
  core/       pure: plan builder, payload builder, read-back compare, masker seam
  ledger/     SQLite (WAL, versioned schema), offsets, state machine
  adapters/   Telegram (aiogram-as-client), Notion (httpx, pinned version)
  app/        config, worker loop, entry point
tests/        unittest (runs under pytest too); real SQLite + fake adapters
smoke.py      manual real-account smoke test (never in CI)
docs/         ledger schema contract
```

## Run

```sh
cp config.example.yaml config.yaml   # fill in intake chat/topic ids
cp .env.example .env                 # fill in secrets
pip install -e '.[dev]'
python -m jo_watchdog.app --config config.yaml
```

## Test

```sh
python -m unittest discover -s tests -t .    # or: pytest
```

## Smoke test (real accounts)

```sh
NOTION_API_KEY=ntn_... python smoke.py            # Notion write/read-back/compare
TELEGRAM_BOT_TOKEN=... python smoke.py --full --config config.yaml
```

See `docs/ledger-schema.md` for the versioned ledger contract and
`docs/adr/` (once added) for decision records. Design sources live in `map/`
(mirrors of the closed design issues #1–#17). The PRD and open build tickets
are mirrored in `docs/issues/` (issues #20–#28).
