---
label: wayfinder:build-tickets
title: Build tickets (tracer-bullet order)
---

# Build tickets

For Hermes Agent + the Matt Pocock coding workflow. Tracer-bullet order: each ticket ends with something runnable end-to-end against real accounts, widening scope rather than building in horizontal layers. Tickets 2 and 4 only touch `core/` and can run in parallel with 3.

## 1. Walking skeleton
Poll one real message from the intake topic → ledger row → minimal plan (Name + raw-text body only) → Notion write → read-back → reaction. Real Telegram and Notion accounts, thinnest possible slice. Proves the ledger, the offset-then-commit ordering, and the Notion adapter's shape before anything else is built on top.

## 2. Masker
Deterministic password/credential masker from `secrets-in-raw-text.md`. Full corpus as fixtures, including the required-to-mask `PW:` line in `PP EP 2 (APC) - TOC - #0004` and the required-not-to-mask phrases ("dl with pw", "DL w pw, 14 days expiry", "With password"). Wired into every exit path (Notion body, ops, heartbeat, logs) per §13 of the design ticket.

## 3. DeepSeek classify/extract
The structured call: verdict/confidence/reasons + verbatim raw fields, roster-blind, JSON mode + Pydantic validation, empty-content handled as a classifier failure. Ignore log for `not_jo`. `unsure` writes and flags. No-text forwards short-circuit to `no_text`.

## 4. Name / JO-number / department / deadline building (pure `core/`)
Header parsing, canonical Name construction, JO-number parsing for the reuse check, the hand-rolled deadline-phrase parser (including the new End-of-Shift 23:59 fallback from design-ticket §9), Notes assembly. All pure functions, corpus-driven golden tests.

## 5. People / Projects / Departments caches + resolver
6-hour cache refresh with persisted snapshots (design-ticket §10). Person match chain: TelegramUserID → TelegramHandle → Contacts `Aliases` → guarded fuzzy on Contacts page title (design-ticket §8). Project match against the Project Code select, longest-code-first. Department read live, alias map in config. Empty-cache-blocks-planning behavior.

## 6. Verification, outbox, ops formats
The nine deterministic verification checks. Durable `outbox` table, same transaction as `written`. The five locked ops-topic formats plus the amendments: probability-based Deliverable hint, `Model changed` line, fuzzy-match line, `Passwords withheld` line.

## 7. Forwards, edits, duplicates, reuse
Four forward-origin types; source-block "posted by" / "originally from"; non-`user` origin reporting. Edits received and reported, never applied. Content-level duplicate detection (project+department+number as parsed integer); exact re-post or reused number always creates a new row, flagged both ways.

## 8. Reliability
5-attempt backoff (30s→15m), the `writing`-state Notion reconciliation query, the in-process deadman (10-minute stale heartbeat → non-zero exit), the 07:00 Asia/Manila daily heartbeat as a pure SQL query over the last 24h of ledger rows.

## 9. Jev matcher, shadow mode
Live `relation_contains` query on `🗺️ Projects`, one Choice question per candidate, probability-based link/hint/silent decision, `deliverable_link_enabled` flag (default off), the 3 fixed regression pairs, token-count storage.

## 10. Packaging, dry-run, smoke test
`python:3.12-slim` non-root Docker image, Compose with bind-mounted `./data` (0700) and `.env`, `HEALTHCHECK`, forward-only/backward-compatible migrations. `dry_run: true` path (stop at `planned`, post plan to ops, no write, no reaction). `smoke.py` walking the full checklist in design-ticket §17.
