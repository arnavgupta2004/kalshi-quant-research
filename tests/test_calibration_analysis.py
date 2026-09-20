import math
import xml.etree.ElementTree as ET

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pricing.plots import Point, reliability_svg
from research import calibration_analysis as C
from scripts import stage8_calibration as S8
from tests.test_pricing_models import inst, obs, tf


def instances(n, *, slope=1.0, intercept=0.0, seed=0, per_event=1, events_offset=0):
    """Instances whose market reference is p and whose outcome is drawn from the TRUE probability
    sigmoid(intercept + slope * logit(p)); ``per_event`` markets share one event and one outcome."""
    rng = np.random.default_rng(seed)
    out = []
    for e in range(n // per_event):
        p = float(rng.uniform(0.05, 0.95))
        q = 1 / (1 + math.exp(-(intercept + slope * math.log(p / (1 - p)))))
        y = int(rng.random() < q)
        for _ in range(per_event):
            out.append(inst(obs(ref=p, trade=tf(last=p), event=f"E{events_offset + e}"), y))
    return out


def frame(ins):
    return C.Frame.build("f", ins, [i.obs.ref for i in ins])


# ------------------------------------------------------------------ the frame
def test_frame_clips_forecasts_and_tracks_events():
    ins = instances(50, per_event=2)
    fr = C.Frame.build("f", ins, [0.0] * 25 + [1.0] * 25)
    assert fr.p.min() > 0 and fr.p.max() < 1 and len(fr) == 50 and len(set(fr.groups)) == 25
    sub = fr.where(fr.p > 0.5)
    assert len(sub) == 25 and set(sub.groups) <= set(fr.groups)


# ------------------------------------------------------------------ summarize
def test_a_calibrated_forecaster_is_reported_as_calibrated():
    s = C.summarize(frame(instances(4000, seed=1)), n_boot=300, seed=1)
    assert s["calibration_in_the_large"]["lo"] <= 0 <= s["calibration_in_the_large"]["hi"]
    assert (
        s["slope"]["lo"] <= 1 <= s["slope"]["hi"]
        and s["intercept"]["lo"] <= 0 <= s["intercept"]["hi"]
    )
    assert s["murphy"]["reliability"] < 0.003 and abs(s["murphy"]["identity_residual"]) < 1e-9
    assert s["n"] == 4000 and s["events"] == 4000


def test_an_overconfident_forecaster_has_a_slope_interval_that_excludes_one():
    s = C.summarize(frame(instances(4000, slope=0.6, seed=2)), n_boot=300, seed=1)
    assert s["slope"]["hi"] < 0.75 and s["murphy"]["reliability"] > 0.003


def test_a_biased_forecaster_shows_up_in_the_large():
    s = C.summarize(frame(instances(4000, intercept=0.6, seed=3)), n_boot=300, seed=1)
    ci = s["calibration_in_the_large"]
    assert ci["lo"] > 0.05  # YES happened far more often than the forecasts said
    assert s["intercept"]["lo"] > 0.3


def test_the_effective_sample_is_events_not_instances():
    """Four markets share each event and outcome: intervals must be wide, and report 1/4 as many events."""
    many = C.summarize(frame(instances(4000, seed=4, per_event=1)), n_boot=300, seed=1)
    dup = C.summarize(frame(instances(4000, seed=4, per_event=4)), n_boot=300, seed=1)
    assert dup["events"] == 1000
    w = lambda s: s["slope"]["hi"] - s["slope"]["lo"]  # noqa: E731
    assert w(dup) > 1.4 * w(many)


# ------------------------------------------------------------------ bins and subgroups
def test_bins_table_counts_every_forecast_once_and_intervals_bracket_the_frequency():
    fr = frame(instances(1500, seed=5))
    t = C.bins_table(fr, n_boot=200, seed=1)
    assert sum(b["n"] for b in t) == 1500
    for b in t:
        f = b["freq_yes"]
        assert f["lo"] <= f["value"] <= f["hi"] and b["events"] <= b["n"]


def test_a_subgroup_with_too_few_events_gets_no_interval_and_a_slope_needs_forty_events():
    fr = frame(instances(300, seed=6))
    tiny = C.subgroup_row(fr, np.arange(300) < 5, "tiny", n_boot=100, seed=1)
    assert "note" in tiny and "calibration_in_the_large" not in tiny
    mid = C.subgroup_row(fr, np.arange(300) < 30, "mid", n_boot=100, seed=1)
    assert mid["slope"] is None and mid["calibration_in_the_large"]["value"] is not None
    big = C.subgroup_row(fr, np.arange(300) < 250, "big", n_boot=100, seed=1)
    assert big["slope"] is not None
    no_slope = C.subgroup_row(fr, np.arange(300) < 250, "range", n_boot=100, seed=1, slope=False)
    assert (
        no_slope["slope"] is None
    )  # groups defined by the forecast itself have no identified slope


@settings(max_examples=60, deadline=None)
@given(st.lists(st.floats(0, 5000), min_size=30, max_size=80), st.floats(0, 0.6))
def test_tercile_groups_partition_the_values_exactly_once(vals, zero_share):
    vals = [0.0 if i < zero_share * len(vals) else v for i, v in enumerate(vals)]
    if len({round(v, 6) for v in vals if v > 0}) < 3:
        return
    ins = [inst(obs(trade=tf(vol=v)), 0) for v in vals]
    groups = C._tercile_groups(vals, lambda i: i.obs.trade.volume[3600], "vol")
    for i in ins:
        assert sum(1 for _, f in groups if f(i)) == 1  # in exactly one group, never zero, never two


def test_dimensions_partition_probabilities_and_horizons():
    research = instances(400, seed=7)
    dims = C.dimensions(research, {i.obs.ticker: 5000.0 for i in research})
    assert set(dims) == {
        "category", "probability_range", "time_to_resolution",
        "liquidity_trailing_hour_volume", "market_size_lifetime_volume",
    }  # fmt: skip
    for p in (0.0, 0.03, 0.05, 0.19999, 0.2, 0.5, 0.8, 0.95, 0.9999, 1.0):
        i = inst(obs(ref=p), 0)
        assert sum(f(i) for _, f in dims["probability_range"]) == 1, p
    for h in (-3, 0.0, 0.49, 0.5, 1.4, 1.5, 3.9, 4.0, 11.9, 12.0, 70):
        i = inst(obs(h=h), 0)
        assert sum(f(i) for _, f in dims["time_to_resolution"]) == 1, h
    assert sum(f(inst(obs(cat="Sports"), 0)) for _, f in dims["category"]) == 1


# ------------------------------------------------------------------ recalibration
def test_recalibration_fitted_on_research_repairs_an_overconfident_holdout():
    research = frame(instances(4000, slope=0.6, seed=8))
    holdout = frame(instances(4000, slope=0.6, seed=9, events_offset=10_000))
    r = C.recalibration_test(research, holdout, n_boot=200, seed=1)
    assert r["platt"]["log"]["hi"] < -0.005  # a real, clearly-negative (better) loss difference
    assert r["platt"]["fitted_slope"] == pytest.approx(0.6, abs=0.1)
    fine = C.recalibration_test(
        frame(instances(4000, seed=10)),
        frame(instances(4000, seed=11, events_offset=10_000)),
        n_boot=200,
        seed=1,
    )
    assert (
        fine["platt"]["log"]["lo"] <= 0.0 <= fine["platt"]["log"]["hi"] + 0.002
    )  # nothing to repair


def test_cross_fitted_recalibration_never_uses_the_instance_it_scores():
    r = C.recalibration_cv(frame(instances(3000, slope=0.6, seed=12)), n_boot=200, seed=1)
    assert r["platt"]["log"]["hi"] < 0 and len(r["platt"]["fitted_a_b_per_fold"]) == 5


# ------------------------------------------------------------------ the SVG diagram
def test_the_reliability_svg_is_well_formed_deterministic_and_escapes_text():
    pts = {
        "a & b": [
            Point(0.1, 0.12, 0.05, 0.2, 100),
            Point(0.5, 0.45, 0.35, 0.55, 300),
            Point(0.9, 0.95, None, None, 50),
        ]
    }
    svg = reliability_svg(pts, "Title <x>", "sub")
    root = ET.fromstring(svg)  # raises if malformed
    assert root.tag.endswith("svg") and svg == reliability_svg(pts, "Title <x>", "sub")
    assert "a &amp; b" in svg and "Title &lt;x&gt;" in svg and svg.count("<circle") == 3
    assert reliability_svg({}, "empty").startswith("<svg")


# ------------------------------------------------------------------ the guards in the driver
def test_the_stage7_files_still_hash_to_the_value_the_hypotheses_were_frozen_under():
    assert S8.stage7_fingerprint() == S8.STAGE7_FROZEN


def test_the_driver_refuses_if_stage7_changed_or_its_own_code_did(monkeypatch):
    monkeypatch.setattr("sys.argv", ["s8", "--role", "development", "--out", "/tmp/x"])
    monkeypatch.setattr(S8, "stage7_fingerprint", lambda: "0" * 16)
    with pytest.raises(SystemExit) as e:
        S8.main()
    assert "frozen" in str(e.value)
    monkeypatch.setattr(S8, "stage7_fingerprint", lambda: S8.STAGE7_FROZEN)
    monkeypatch.setattr(
        "sys.argv",
        ["s8", "--role", "confirmatory", "--out", "/tmp/x", "--expect-fingerprint", "0" * 16],
    )
    with pytest.raises(SystemExit) as e:
        S8.main()
    assert "pre-specified" in str(e.value)


def _block(gap=0.0, slope=1.0, big_gap=0.0):
    est = lambda v, lo, hi: {"value": v, "lo": lo, "hi": hi, "n_rows": 1, "n_clusters": 1}  # noqa: E731
    return {
        "summary": {
            "market_reference": {
                "calibration_in_the_large": est(gap, gap - 0.02, gap + 0.02),
                "murphy": {"reliability": 0.001, "resolution": 0.1},
            },
            "A_microstructure": {"slope": est(1.0, 0.9, 1.1)},
            "blend_market_and_B": {"slope": est(1.0, 0.9, 1.1)},
        },
        "reliability_market": [
            {
                "bin": "[0.3, 0.4)",
                "n": 200,
                "mean_forecast": 0.35,
                "freq_yes": est(0.35 + big_gap, 0.2, 0.5),
            }
        ],
        "subgroups_market": {
            "time_to_resolution": [{"group": "<0.5h", "slope": est(slope, 0.9, 1.5)}],
            "market_size_lifetime_volume": [
                {
                    "group": "lifetime: low (<= 1)",
                    "calibration_in_the_large": est(-0.02, -0.04, 0.0),
                }
            ],
        },
    }


def test_the_stage8_criteria_are_scored_as_written():
    ins = [inst(obs(ref=0.1), 0) for _ in range(20)]
    fc = {"market_reference": [0.1] * 20}
    recal = {"isotonic": {"log": {"value": -0.001}}}
    good = {r["id"]: r["verdict"] for r in S8.hypotheses_stage8(_block(), fc, ins, recal, {})}
    assert (
        good["C1"]
        == good["C2"]
        == good["C3"]
        == good["C5"]
        == good["C6"]
        == good["C7"]
        == "consistent"
    )
    assert good["C4"] == "consistent"  # cheap contracts all resolved NO: YES minus price = -0.1 < 0
    winners = [inst(obs(ref=0.1), 1) for _ in range(20)]
    flipped = {
        r["id"]: r["verdict"] for r in S8.hypotheses_stage8(_block(), fc, winners, recal, {})
    }
    assert (
        flipped["C4"] == "NOT consistent"
    )  # cheap contracts that all paid: the opposite of a longshot bias
    assert good["C8"] == "not testable"  # no recorded books were supplied
    bad = {
        r["id"]: r["verdict"]
        for r in S8.hypotheses_stage8(
            _block(gap=0.05, slope=1.9, big_gap=0.15),
            fc,
            ins,
            {"isotonic": {"log": {"value": -0.02}}},
            {},
        )
    }
    assert bad["C1"] == bad["C3"] == bad["C5"] == bad["C6"] == "NOT consistent"


def test_a_mass_of_zeros_is_its_own_group_and_the_rest_split_at_their_median():
    vals = [0.0] * 60 + list(range(1, 41))
    ins = [inst(obs(trade=tf(vol=float(v))), 0) for v in vals]
    groups = C._tercile_groups(vals, lambda i: i.obs.trade.volume[3600], "vol")
    sizes = {name: sum(f(i) for i in ins) for name, f in groups}
    assert [n.split(":")[1].strip().split(" ")[0] for n, _ in groups] == ["none", "low", "high"]
    assert sizes["vol: none"] == 60 and sorted(sizes.values()) == [20, 20, 60]


def test_cross_fitting_really_holds_the_scored_instance_out():
    """A PERFECTLY calibrated forecaster cannot be improved.  Out of fold, an isotonic calibrator can only
    overfit noise and do worse; one that had seen the instances it scores would memorise them and look
    helpful.  So a positive loss difference here proves the fold logic does not leak."""
    rng = np.random.default_rng(3)
    ins = []
    for e in range(3000):
        p = float(rng.uniform(0.1, 0.9))
        ins.append(inst(obs(ref=p, trade=tf(last=p), event=f"E{e}"), int(rng.random() < p)))
    r = C.recalibration_cv(frame(ins), n_boot=200, seed=1)
    assert r["isotonic"]["log"]["value"] > 0


def test_c1_requires_the_interval_to_contain_zero_not_just_a_small_point_estimate():
    ins = [inst(obs(ref=0.1), 0) for _ in range(20)]
    fc = {"market_reference": [0.1] * 20}
    recal = {"isotonic": {"log": {"value": -0.001}}}
    rows = {
        r["id"]: r["verdict"] for r in S8.hypotheses_stage8(_block(gap=0.025), fc, ins, recal, {})
    }
    assert (
        rows["C1"] == "NOT consistent"
    )  # gap 0.025 is under 0.03, but its interval [0.005, 0.045] excludes 0


def test_every_fold_trains_on_a_strict_subset_that_excludes_the_held_out_events(monkeypatch):
    seen = []
    real = C.PlattCalibrator.fit

    def spy(self, p, y):
        seen.append(len(p))
        return real(self, p, y)

    monkeypatch.setattr(C.PlattCalibrator, "fit", spy)
    ins = instances(1000, seed=13)
    C.recalibration_cv(frame(ins), n_boot=50, seed=1)
    assert (
        len(seen) == 5 and all(n < 1000 for n in seen) and sum(seen) == 4 * 1000
    )  # each row trains 4 of 5 folds
