"""Held-out extraction report: ``python -m jo_watchdog.app.extraction_report``.

Runs the live DeepSeek classifier over every corpus Job Order that is not a
few-shot example and prints the per-field exact-match rate (whitespace
normalised) against the golden fixtures. It is a report, never a CI gate: the
owner chose no pass bar (#28, Owner decisions). Classification precision and
recall are not reported until real chatter and near-miss fixtures exist.

``--save`` keeps the raw outputs; ``--from`` re-scores a saved run offline.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from jo_watchdog.core.extraction import EXTRACTED_FIELDS, field_matches
from jo_watchdog.core.masker import outbound_text

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def held_out_cases(fixtures: Path = FIXTURES) -> list[dict]:
    """Golden cases minus the few-shot examples, each with its masked corpus text."""
    corpus = json.loads((fixtures / "redacted_corpus.json").read_text(encoding="utf-8"))
    golden = json.loads((fixtures / "extraction_golden.json").read_text(encoding="utf-8"))
    fewshot = set(json.loads((fixtures / "fewshot_ids.json").read_text())["corpus_indices"])
    return [
        {**case, "masked_text": outbound_text(corpus[case["corpus_index"]]).text}
        for case in golden["cases"]
        if case["corpus_index"] not in fewshot
    ]


def score(cases: list[dict], outputs: dict[int, dict | None]) -> dict[str, float]:
    """Per-field exact-match rate; a failed call counts as a miss on every field."""
    hits = dict.fromkeys(EXTRACTED_FIELDS, 0)
    for case in cases:
        actual = outputs.get(case["corpus_index"])
        if actual is None:
            continue
        for field, ok in field_matches(case["expected"], actual).items():
            hits[field] += ok
    total = len(cases) or 1
    return {field: hits[field] / total for field in EXTRACTED_FIELDS}


def render(rates: dict[str, float], n: int, failures: int, verdict_jo: int) -> str:
    lines = [
        f"Held-out extraction report: {n} Job Orders, {failures} failed calls,"
        f" {verdict_jo} classified jo",
        "",
    ]
    width = max(len(field) for field in rates)
    lines += [f"{field.ljust(width)}  {rate:6.1%}" for field, rate in rates.items()]
    lines += ["", "Report only; no pass bar. Classification precision/recall not yet measured."]
    return "\n".join(lines)


async def _run_live(cases: list[dict], config_path: str) -> dict[int, dict | None]:
    from jo_watchdog.adapters.deepseek import DeepSeekClassifier
    from jo_watchdog.app.config import deepseek_token, load_config

    config = load_config(config_path)
    classifier = DeepSeekClassifier(
        deepseek_token(),
        base_url=config.deepseek_base_url,
        model=config.deepseek_model,
        timeout_seconds=config.deepseek_timeout_seconds,
    )
    outputs: dict[int, dict | None] = {}
    try:
        for case in cases:
            result = await classifier.classify(case["masked_text"], None)
            outputs[case["corpus_index"]] = (
                result.extraction.model_dump() if result.extraction else None
            )
    finally:
        await classifier.close()
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--save", help="write raw outputs to this JSON file")
    parser.add_argument("--from", dest="from_file", help="re-score a saved outputs file")
    args = parser.parse_args()

    cases = held_out_cases()
    if args.from_file:
        saved = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
        outputs = {int(k): v for k, v in saved.items()}
    else:
        outputs = asyncio.run(_run_live(cases, args.config))
        if args.save:
            Path(args.save).write_text(json.dumps(outputs, indent=1, ensure_ascii=False))

    failures = sum(1 for c in cases if outputs.get(c["corpus_index"]) is None)
    verdict_jo = sum(
        1 for c in cases if (outputs.get(c["corpus_index"]) or {}).get("verdict") == "jo"
    )
    print(render(score(cases, outputs), len(cases), failures, verdict_jo))


if __name__ == "__main__":
    main()
