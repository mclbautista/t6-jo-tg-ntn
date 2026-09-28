---
title: Secrets in raw text
type: grilling
mode: HITL
status: closed
assignee: claude
blocked_by: []
---

## Question

JOs can carry passwords and share links, and the page body keeps the raw message text verbatim (Lossless capture). What does the watchdog do about credentials in raw text, and where?

Facts from the sample corpus (77 JOs): one JO (`PP EP 2 (APC) - TOC - #0004`) carries a bare transfer password on a `PW:` line (redacted in the shared copy). Several JOs carry Frame.io, Dropbox (`rlkey=`), TransferNow and Drive links. "dl with pw", "DL w pw", "with password" and "nondl with pw" appear often as link-setting phrases, and the password itself almost never follows.

Where raw text lands today: the Notion page body, the SQLite ledger, the LLM call, and (as first lines) ops reports, the heartbeat's ignored list and logs.

## Resolution

Resolved 2026-09-28 in a grilling session with the owner. All eight questions taken at the recommended answer.

**What we protect, and from whom.** The intake topic's members already see everything, so Telegram is not the exposure. The new audiences are anyone with access to the Job Orders database, the LLM provider, host logs, and the ops topic. If the text is leaving the ledger, it is masked first. Principle: **Mask at the boundary, keep the source**.

**What counts as a secret.** Passwords and credentials only: a value that follows a label of `pw`, `pass`, `password`, `passcode` or `PIN` (case-insensitive). Links stay as posted, including links with embedded keys (`rlkey=`, `?token=`), because the team needs them to do the job and masking them would break the JO. If it is clearly a credential, mask it; if it is merely link-shaped, leave it. Nothing is guessed.

**Detection is deterministic code, never the LLM.** If the text matches a label pattern followed by a value, the value is masked. Principle: **Resolve, don't generate** (a model can miss a password or invent one; a regex-based masker can be unit-tested).

Rules, each as if-then:

- **Label match**: the label must be immediately followed by `:` or `=`. If the label has no colon or equals sign after it, it is not a credential label and nothing is masked. This is what keeps "dl with pw", "DL w pw, 14 days expiry", "With password" and "nondl with pw" untouched (verified against the corpus).
- **Value span**: if a value follows the separator on the same line, the rest of that line is the value. If the separator ends the line, the next non-blank line is the value. If neither exists, nothing is masked.
- **Placeholder**: the value becomes `[password withheld – see Telegram message]`. The label stays, so the row shows that a credential existed and where to find it. Principle: **Never drop silently**, applied to a redaction.
- **Corpus fixture**: `PW: [REDACTED]` from the `PP EP 2 (APC)` JO is the test case for a masked line. The "dl with pw" JOs are the test cases for no-mask.

**Order of operations: mask before the LLM call.** If a message is about to be sent to the model, the masker runs first. The model does not need a password to classify or extract, so credentials never reach the third-party API, and no extracted field (Task, Notes, Mode of Transfer) can contain one. This means no second masking pass on fields.

**One masker, everywhere text leaves the ledger.** The same function is applied to: the LLM input, the Notion body (raw text block), ops-topic reports, the heartbeat's ignored first lines, and log output. The unmasked text lives only in the ledger's raw-text column on the host bind-mount, with owner-only permissions (directory 0700, database file 0600). Lossless capture still holds: the full original is in Telegram and in the ledger.

**Reporting.** No per-message ops post. Masking is the system working as designed, not a problem. The daily heartbeat gains one line: `Passwords withheld: N`. Principles: **Annotate, don't gate** (masking never blocks a write); **Never drop silently** (the count is the safety net); no per-message noise, so the ops topic stays a worklist.

**Known false negatives, accepted in v1.** A password with no label (a bare token on its own line), or one behind a label outside the list, is not masked. That is the same exposure as before this ticket, so v1 is no worse than the status quo. A false positive costs a placeholder the coordinator can resolve by opening the Telegram message. Widening the label list is a config change, not a code change.

**Implication for replay.** Because extraction runs on masked text, a replay from the ledger must re-apply the masker before the LLM call. The masker version tag is stored per message next to the model id and prompt tag. Principle: **Reproducible by default**.

**Build-time additions.** The masker unit tests (fixture above plus the no-mask phrases) go on the smoke-test checklist. The heartbeat mock-up in the ops-topic mock-ups ticket needs the one new line.

Principles carried into this ticket: **Mask at the boundary, keep the source**, **Resolve, don't generate**, **Never drop silently**, **Annotate, don't gate**, **Reproducible by default**, **Lossless capture** (preserved at the source, not at every exit).
