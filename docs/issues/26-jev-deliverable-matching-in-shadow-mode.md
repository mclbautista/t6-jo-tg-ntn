<!-- Mirror of https://github.com/mclbautista/t6-jo-watchdog/issues/26. Keep identical: change the issue and this file together. -->

# 9. Jev Deliverable matching in shadow mode

## Parent

#28

## What to build

Add project-scoped Deliverable matching through Jev’s typed Choice interface while keeping automatic linking disabled by default. Every eligible candidate must produce a traceable probability decision that becomes a link, an ops hint, or a silent no-match according to deterministic thresholds and shadow-mode policy.

## Acceptance criteria

- [ ] Matching runs after extraction and Project resolution but before the one Notion write, and is skipped when Project is unresolved or no related Deliverables exist.
- [ ] Candidates come from a live, paginated `relation_contains` query on the resolved Project’s `🗺️ Projects` relation; the six-hour caches and Status/Class filters are not used.
- [ ] The watchdog never creates a Deliverable and performs no automated backfill after capture.
- [ ] Jev receives masked source text and resolved Project name, then answers one typed Choice question per candidate keyed by page ID with `match`, `maybe`, and `no` options.
- [ ] No candidate-name echo or generated reason is requested or accepted.
- [ ] Deterministic code links only when `match` is top choice and meets configurable `T_link`, initially 0.90; it hints when `match + maybe` meets configurable `T_hint`, initially 0.50; lower outcomes remain silent.
- [ ] Duplicate normalized candidate names never link automatically and instead produce a probability-based hint.
- [ ] Zero-to-many candidates may independently qualify, and the exact candidate snapshot and outcome are stored in the planned payload.
- [ ] One inline retry handles matcher failure; continued failure leaves Deliverable blank, does not block the Notion write, and queues one masked ops line.
- [ ] Returned model ID, requested alias, prompt tag, token counts, candidate page IDs/names, and probabilities are stored for every call.
- [ ] `deliverable_link_enabled` defaults off: Jev runs, decisions and hints are retained, heartbeat counts are updated, but no Deliverable relation is written until the owner enables it.
- [ ] Hint messages show match probability rather than a generated explanation, and tag no one.
- [ ] The three known real JO-to-Deliverable pairs are permanent regression fixtures.
- [ ] Recorded Jev contract tests cover typed responses, malformed responses, probability thresholds, candidate-key mapping, service failure, model metadata, duplicate names, zero/many matches, and shadow mode without network access in CI.
- [ ] Whole-pipeline tests confirm matcher failure never delays capture and shadow mode never writes a relation.
- [ ] The Jev adapter implements the interface under Technical details and calls OpenRouter's Decisions API in the format specified there; recorded fixtures pin that format.

## Technical details

### Adapter interface

```python
class Candidate(BaseModel):
    page_id: str
    name: str

class CandidateDecision(BaseModel):
    page_id: str
    p_match: float
    p_maybe: float
    p_no: float

class MatchResult(BaseModel):
    decisions: list[CandidateDecision]
    requested_model: str
    returned_model: str | None
    input_tokens: int | None
    output_tokens: int | None

class DeliverableMatcher(Protocol):
    async def match(self, state: str, project_name: str,
                    candidates: list[Candidate]) -> MatchResult: ...
```

A response missing a candidate, adding an unknown key, or with probabilities that don't sum to 1 ± 0.01 is a failure.

### Wire format: OpenRouter Decisions API

Jev (TypeSafe) is served through OpenRouter's Decisions API, not chat completions: it has no `messages`, `temperature`, `response_format` or `logprobs`, and it returns per-option probabilities natively. Sources: [Jev model page](https://openrouter.ai/typesafe/jev-1.14), [Decisions API reference](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-questions-and-answers-request), [Jev tutorial](https://openrouter.ai/docs/guides/community/jev-tutorial). `typesafe/jev-router` is a different product (a chat router) and is not used.

Request: `POST https://openrouter.ai/api/alpha/decisions` with `Authorization: Bearer OPENROUTER_API_KEY` and `Content-Type: application/json`.

```json
{
  "model": "~typesafe/jev-latest",
  "state": {
    "project": "Moonglow",
    "job_order": "masked message text"
  },
  "questions": {
    "c0": {
      "type": "choice",
      "instructions": "Does this Job Order belong to the Deliverable 'Moonglow FTR SCR'?",
      "criteria": {
        "match": "The Job Order is clearly work for this Deliverable.",
        "maybe": "The Job Order could be for this Deliverable, but the text doesn't make it clear.",
        "no": "The Job Order is not for this Deliverable."
      }
    },
    "c1": { "type": "choice", "instructions": "…'Mainpic DCP'?", "criteria": { "…": "same three options" } }
  }
}
```

- Question keys are local ids `c0`, `c1`, … mapped to Notion page ids in code, because the docs don't define a key format and the key is never shown to the model. The candidate's name goes in `instructions`.
- All questions in one request are answered independently; one request per Job Order. The request limit is 64k tokens, far above a single project's Deliverables.
- `criteria` wording above is part of the prompt and versioned by `prompt_tag` (e.g. `match-v1`).

Response:

```json
{
  "id": "gen-dec-…",
  "model": "typesafe/jev-1.14-20261014",
  "provider": "TypeSafe",
  "answers": {
    "c0": { "type": "choice", "choice": "match", "confidence": 0.86,
            "probabilities": { "match": 0.93, "maybe": 0.06, "no": 0.01 } },
    "c1": { "type": "choice", "choice": "no", "confidence": 0.95,
            "probabilities": { "match": 0.0, "maybe": 0.03, "no": 0.97 } }
  },
  "usage": { "input_tokens": 240, "output_tokens": 40, "cost": 0.00001 }
}
```

- Read probabilities by option name, never by position. `confidence` is ignored: it measures how concentrated the answer is, not whether it is right.
- `model` is stored as `returned_model` (a dated snapshot); `usage.input_tokens` and `usage.output_tokens` fill the token columns.
- Errors come back as `{"error": {"code": …, "message": …}}` with statuses 400, 401, 402, 403, 404, 413, 429, 500, 502, 503, 524, 529. Any error, timeout or invalid body counts as one failed matcher call (one inline retry, then `matcher_skipped`). On 429, honor `Retry-After` or `X-RateLimit-Reset` if present for the inline retry, capped at 30 seconds.
- The endpoint is marked alpha; the Pydantic response model rejects anything that doesn't match the shape above.
- Model alias: `~typesafe/jev-latest` follows the owner's "latest, no pinning" rule. It is OpenRouter's alias (newest release); TypeSafe's own `jev-latest` lags behind it, so the leading `~` matters. Probabilities can move a few hundredths between runs and versions, which shadow mode is there to observe.

### Decision rule (pure `core/`)

For each candidate: `link` if `p_match` is the highest of the three and `p_match ≥ t_link`; else `hint` if `p_match + p_maybe ≥ t_hint`; else `none`. Two candidates whose names match after casefolding and whitespace collapse can never be `link`; either becomes `hint` when it would have linked. The JO-level heartbeat outcome is `linked` if any link was written, else `hinted` if any hint, else `none`; `skipped` when the call failed. Unresolved Project or zero candidates count as `none`.

### Storage

- `stage_results` row with `stage = 'match'`: candidates, decisions, outcome, and whether linking was enabled.
- `llm_calls` row with `provider = 'jev'`, `purpose = 'deliverable_match'`, and `detail_json` holding candidates and probabilities.

### Configuration and secrets

- `.env`: `OPENROUTER_API_KEY`.
- `config.yaml`: `deliverables_data_source_id` (`0a4ff084-3138-429c-8dbe-4d802aded74f`), `jev_url` (`https://openrouter.ai/api/alpha/decisions`), `jev_model` (`~typesafe/jev-latest`), `jev_timeout_seconds` (30), `t_link` (0.90), `t_hint` (0.50), `deliverable_link_enabled` (false).

## Blocked by

- #25
