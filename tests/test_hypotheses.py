"""Pin the hypothesis scorer to the recorded Stage 6 results (skips on a fresh checkout)."""

import json
from pathlib import Path

import pytest

from research.hypotheses import evaluate

DEV = Path("results/stage6/dev/arbitrage_research.json")
CONFIRM = Path("results/stage6/confirm/arbitrage_research.json")


@pytest.mark.skipif(not DEV.exists(), reason="results not present")
def test_the_hypotheses_were_formed_from_the_development_data_and_hold_there():
    rows = evaluate(json.load(open(DEV)))
    assert len(rows) == 15 and all(v == "consistent" for _, v, _, _ in rows)


@pytest.mark.skipif(not CONFIRM.exists(), reason="results not present")
def test_the_confirmatory_verdicts_are_the_recorded_ones():
    rows = {h: v for h, v, _, _ in evaluate(json.load(open(CONFIRM)))}
    failed = sorted(h for h, v in rows.items() if v != "consistent")
    assert failed == ["A3", "A4", "C2", "D1"]  # reported as failures in docs/arbitrage_research.md


def test_every_criterion_is_scored_exactly_once():
    if not DEV.exists():
        pytest.skip("results not present")
    ids = [h for h, *_ in evaluate(json.load(open(DEV)))]
    assert len(ids) == len(set(ids)) and set(ids) <= {
        "A1",
        "A2",
        "A3",
        "A4",
        "A6",
        "B1",
        "B2",
        "B3",
        "C1",
        "C2",
        "C3",
        "D1",
        "D2",
        "D3",
        "D4",
    }
