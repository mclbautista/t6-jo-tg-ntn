<!-- Mirror of https://github.com/mclbautista/t6-jo-tg-ntn/issues/26. Keep identical: change the issue and this file together. -->

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
- [ ] The Jev adapter implements the interface under Technical details; its wire format is taken from Jev's API documentation and pinned down by recorded fixtures before the adapter is merged.

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

The request carries one Choice question per candidate, keyed by `page_id`, with the candidate's name in the question text, options `match`, `maybe`, `no`. A response missing a candidate key, adding an unknown key, or with probabilities that don't sum to 1 ± 0.01 is a failure.

**Open item:** the project documents don't record Jev's endpoint or request/response format. The owner supplies the API documentation link (see Open questions in #28). Until then, the adapter is built against this interface with a fake, and the HTTP layer waits for the docs.

### Decision rule (pure `core/`)

For each candidate: `link` if `p_match` is the highest of the three and `p_match ≥ t_link`; else `hint` if `p_match + p_maybe ≥ t_hint`; else `none`. Two candidates whose names match after casefolding and whitespace collapse can never be `link`; either becomes `hint` when it would have linked. The JO-level heartbeat outcome is `linked` if any link was written, else `hinted` if any hint, else `none`; `skipped` when the call failed. Unresolved Project or zero candidates count as `none`.

### Storage

- `stage_results` row with `stage = 'match'`: candidates, decisions, outcome, and whether linking was enabled.
- `llm_calls` row with `provider = 'jev'`, `purpose = 'deliverable_match'`, and `detail_json` holding candidates and probabilities.

### Configuration and secrets

- `.env`: `JEV_API_KEY`.
- `config.yaml`: `deliverables_data_source_id` (`0a4ff084-3138-429c-8dbe-4d802aded74f`), `jev_base_url`, `jev_model` (`jev-latest`), `t_link` (0.90), `t_hint` (0.50), `deliverable_link_enabled` (false).

## Blocked by

- #25
