import math
import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pricing.scoring import (
    EPS,
    base_rate_log_loss,
    brier,
    clip,
    expit,
    log_loss,
    logit,
    per_row_brier,
    per_row_log_loss,
)


def test_known_values():
    assert brier([0.5, 0.5], [1, 0]) == pytest.approx(0.25)
    assert brier([1.0, 0.0], [1, 0]) == 0.0
    assert brier([0.0, 1.0], [1, 0]) == 1.0
    assert log_loss([0.5, 0.5], [1, 0]) == pytest.approx(math.log(2))
    assert log_loss([0.8], [1]) == pytest.approx(-math.log(0.8))
    assert log_loss([0.8], [0]) == pytest.approx(-math.log(0.2))


def test_a_confident_miss_costs_a_large_finite_log_loss_not_infinity():
    """Probability bounds: exactly 0 or 1 is clipped to one tick, so the penalty is ln(1/EPS)."""
    assert log_loss([0.0], [1]) == pytest.approx(-math.log(EPS))
    assert log_loss([1.0], [0]) == pytest.approx(-math.log(EPS))
    assert math.isfinite(log_loss([0.0, 1.0], [1, 0]))


def test_clip_keeps_probabilities_strictly_inside_the_unit_interval():
    assert clip(0.0) == EPS and clip(1.0) == 1 - EPS and clip(0.4) == 0.4
    assert clip(-3.0) == EPS and clip(9.0) == 1 - EPS
    with pytest.raises(ValueError):
        clip(float("nan"))


@given(st.floats(min_value=-1e6, max_value=1e6, allow_nan=False))
def test_clip_is_idempotent_and_bounded(p):
    c = clip(p)
    assert EPS <= c <= 1 - EPS and clip(c) == c


def test_inputs_are_validated():
    with pytest.raises(ValueError):
        brier([0.5], [1, 0])  # length mismatch
    with pytest.raises(ValueError):
        brier([0.5], [2])  # not a binary outcome (a scalar market has no label)
    with pytest.raises(ValueError):
        log_loss([1.2], [1])  # not a probability
    with pytest.raises(ValueError):
        brier([], [])


def test_per_row_losses_average_to_the_scores():
    rng = random.Random(1)
    p = [rng.random() for _ in range(50)]
    y = [rng.randrange(2) for _ in range(50)]
    assert sum(per_row_log_loss(p, y)) / 50 == pytest.approx(log_loss(p, y))
    assert sum(per_row_brier(p, y)) / 50 == pytest.approx(brier(p, y))


def test_the_scoring_rules_are_proper():
    """Expected loss is minimised by reporting the true probability (that is what 'proper' means)."""
    rng = random.Random(3)
    true_p = 0.3
    y = [1 if rng.random() < true_p else 0 for _ in range(20_000)]
    q = sum(y) / len(y)
    grid = [i / 20 for i in range(1, 20)]
    best_ll = min(grid, key=lambda g: log_loss([g] * len(y), y))
    best_br = min(grid, key=lambda g: brier([g] * len(y), y))
    assert abs(best_ll - q) <= 0.05 and abs(best_br - q) <= 0.05
    # and the loss of the true frequency is the entropy, the floor for a model that knows only that
    assert log_loss([q] * len(y), y) == pytest.approx(base_rate_log_loss(y))
    assert base_rate_log_loss([1, 1, 1]) == 0.0  # no uncertainty, no loss


@given(st.floats(0.0001, 0.9999))
def test_logit_and_expit_are_inverses(p):
    assert expit(logit(p)) == pytest.approx(p, abs=1e-9)
    assert expit(1000) == pytest.approx(1.0) and expit(-1000) == pytest.approx(0.0, abs=1e-12)


@given(st.lists(st.tuples(st.floats(0, 1), st.integers(0, 1)), min_size=1, max_size=40))
def test_scores_are_non_negative_and_brier_is_at_most_one(pairs):
    p, y = [a for a, _ in pairs], [b for _, b in pairs]
    assert 0 <= brier(p, y) <= 1 and log_loss(p, y) >= 0
