import math
from types import SimpleNamespace

import pytest

from arbitrage.opportunity import RelationSpec
from backtest.events import BookConfirm, BookUpdate
from backtest.feed import ListFeed
from backtest.market_info import MarketInfo
from market.contracts import Rules, Side, Strike
from market.fees import FeeBook, FeeSchedule
from market.relationships import MutuallyExclusive
from market.semantics import EvidenceLevel
from research import arbitrage_analysis as A
from research import latency_analysis as L
from research.scanner import SEC, ScanConfig, Scanner, merge_results, standard_variants

T0 = 1_800_000_000 * SEC
FEES = FeeBook({}, FeeSchedule(known=False))
P = A.Params(n_boot=300)


def c(n):
    return n * 100


def bu(sec, t, yes=()):
    return BookUpdate(T0 + int(sec * SEC), t, tuple(yes), ())


def cf(sec):
    return BookConfirm(T0 + int(sec * SEC))


def spec(a, b, ev, level=EvidenceLevel.PROVEN):
    return RelationSpec(MutuallyExclusive([a, b]), level, ev, "KX")


def run(events, specs, categories=None, deltas=(), gap_s=1e9):
    cfg = ScanConfig(standard_variants(FEES), gap_s=gap_s, probe_deltas_ns=tuple(deltas))
    return Scanner(specs, categories or {}, {}, cfg).run(ListFeed(events))


def world():
    """Three events observed for one hour.  E1: a standing violation (600 s..1800 s).  E2: a
    momentary one (0.2 s, gone before the next cycle).  E3: never violated."""
    quiet = lambda t: (t, [(2000, c(500))])  # noqa: E731
    ev = []
    for t in "ABCDEF":
        ev.append(bu(0, t, [(2000, c(500))]))
    ev.append(cf(0))
    for k in range(1, 7):
        ev.append(cf(600 * k))
    ev += [bu(600, "A", [(5500, c(500))]), bu(600, "B", [(5200, c(500))])]  # E1 violated
    ev += [bu(1800, "B", [(2000, c(500))])]  # ...and repaired
    ev += [
        bu(1200, "C", [(5200, c(500))]),
        bu(1200.2, "D", [(5000, c(500))]),
    ]  # E2: a 2c flicker in...
    ev += [bu(1200.4, "D", [(2000, c(500))])]  # ...and out inside one cycle
    del quiet
    return ev


SPECS = [spec("A", "B", "E1"), spec("C", "D", "E2"), spec("E", "F", "E3")]
CATS = {t: "Sports" for t in "ABCD"} | {"E": "Crypto", "F": "Crypto"}


# ------------------------------------------------------------------ Experiment A
def test_events_that_never_show_a_violation_stay_in_the_denominator():
    res = run(world(), SPECS, CATS)
    # 3 relations x 1 hour = 3 relation-hours observed; 2 episodes started (E1 standing, E2 flicker)
    assert A.exposure_summary(res, P)["relation_hours"] == pytest.approx(3.0)
    sight = A.frequency(res, P, confirmed=False)
    conf = A.frequency(res, P, confirmed=True)
    assert sight["episodes"] == 2 and conf["episodes"] == 1
    assert sight["per_1000_relation_hours"]["value"] == pytest.approx(2 / 3 * 1000)
    assert conf["per_1000_relation_hours"]["value"] == pytest.approx(1 / 3 * 1000)
    assert sight["events_observed"] == 3 and sight["events_with_episodes"] == 2


def test_sightings_and_confirmed_separate_a_standing_edge_from_a_flicker():
    res = run(world(), SPECS, CATS)
    by_event = {e.event: e for e in res.episodes}
    assert by_event["E1"].persisted and not by_event["E2"].persisted
    assert not by_event["E2"].persisted and by_event["E2"].hi_s == pytest.approx(0.2, abs=1e-6)
    diag = A.sighting_lifetime_diagnostic(A.select(res, P, confirmed=False))
    assert diag["persisted"] == 1 and diag["not_persisted"] == 1
    assert diag["short_upper_bound_s"]["share_under_1s"] == 1.0  # the chunk-skew fingerprint


def test_zero_episodes_get_an_upper_bound_not_a_false_zero_interval():
    res = run([*world()[:14]], SPECS, CATS)  # nothing violated in this slice
    f = A.frequency(res, P, confirmed=False)
    assert f["episodes"] == 0
    z = f["zero_bounds"]
    assert z["events_share_upper"] == pytest.approx(1 - 0.05 ** (1 / 3))  # 0.63 with only 3 events
    hours = A.exposure_summary(res, P)["relation_hours"]
    assert z["per_1000_relation_hours_upper_naive"] == pytest.approx(-math.log(0.05) / hours * 1000)
    assert A.zero_bounds(3, 3, 1.0, 0.05) is None  # a nonzero count needs no such bound


def test_left_censored_episodes_do_not_count_as_new_starts():
    ev = [bu(0, "A", [(5500, c(500))]), bu(0, "B", [(5200, c(500))]), cf(0), cf(600), cf(1200)]
    res = run(ev, [spec("A", "B", "E1")], CATS)
    assert res.episodes[0].left_censored
    f = A.frequency(res, P, confirmed=False)
    assert f["episodes"] == 0  # present before we started looking: its start is unknown


def test_category_table_separates_categories_and_flags_thin_evidence():
    res = run(world(), SPECS, CATS)
    rows = {r["category"]: r for r in A.category_table(res, P)}
    assert (
        rows["Sports"]["sightings"]["episodes"] == 2
        and rows["Sports"]["confirmed"]["episodes"] == 1
    )
    assert rows["Crypto"]["sightings"]["episodes"] == 0
    assert rows["Crypto"]["sightings"]["zero_bounds"] is not None
    assert rows["Crypto"]["low_n"] and rows["Sports"]["low_n"]  # only 1-2 events each: not trusted
    assert rows["Sports"]["relation_hours"] == pytest.approx(2.0)


def test_pooling_keeps_the_same_event_from_two_recordings_as_two_clusters():
    a = run(world(), SPECS, CATS)
    b = run(world(), SPECS, CATS)
    pooled = merge_results([("d1", a), ("d2", b)])
    assert {e.event for e in pooled.episodes} == {"d1:E1", "d1:E2", "d2:E1", "d2:E2"}
    assert pooled.recording_s == pytest.approx(a.recording_s + b.recording_s)
    assert {e.event for e in a.episodes} == {"E1", "E2"}  # the originals are untouched


# ------------------------------------------------------------------ Experiment B
def test_funnel_is_monotone_and_fees_are_what_remove_this_edge():
    res = run(world(), SPECS, CATS)
    eps = A.select(res, P, confirmed=False)
    for v in ("baseline", "fee_free"):
        counts = [s["episodes"] for s in A.funnel(eps, v, P)["stages"]]
        assert counts == sorted(counts, reverse=True) and counts[0] == 2
    free = [s["episodes"] for s in A.funnel(eps, "fee_free", P)["stages"]]
    base = [s["episodes"] for s in A.funnel(eps, "baseline", P)["stages"]]
    assert (
        free[2] == 2 and base[2] == 1
    )  # the 2c flicker is eaten by ~3.5c of fees, the 7c edge is not
    assert base[1] == 2  # liquid, but not profitable after fees


def test_executability_by_covariate_reports_each_bin_with_both_interval_types():
    res = run(world(), SPECS, CATS)
    eps = A.select(res, P, confirmed=False)
    out = A.executability_by_covariate(eps, "fee_free", P)
    assert out["overall"]["n"] == 2
    assert {b["bin"] for b in out["edge"]} == {"<=1c", "1-2c", ">2c"}
    assert sum(b["n"] for b in out["edge"]) == 2  # every episode is in exactly one bin
    assert sum(b["n"] for b in out["time_to_expiry"]) == 2
    assert all(b["wilson"][0] <= b["wilson"][1] for b in out["edge"] if b["n"])


def test_net_edge_distribution_is_capped_at_the_send_size_not_the_book_depth():
    res = run(world(), SPECS, CATS)
    eps = A.select(res, P, confirmed=False)
    d = A.net_edge_distribution(eps, "fee_free", P)
    assert d["best_size_contracts"]["max"] == 100  # the books hold 500; the cap is 100 contracts
    assert d["gross_edge_cents_per_contract"]["p50"] == pytest.approx(4.5)  # median of 7c and 2c
    assert d["total_net_usd"] == pytest.approx(7.0 + 2.0)  # 100 contracts x (7c + 2c), no fees


# ------------------------------------------------------------------ realised outcomes
def test_a_proven_relation_pays_at_least_what_it_promised_in_every_world():
    res = run(world(), SPECS, CATS)
    e1 = next(e for e in res.episodes if e.event == "E1")
    for winner in (
        "A",
        "B",
        None,
    ):  # exactly one wins, or neither (mutually exclusive allows both NO)
        settled = {"A": 10_000 if winner == "A" else 0, "B": 10_000 if winner == "B" else 0}
        out = A.realised_check([e1], settled, "fee_free")
        assert out["PROVEN"]["realised_below_promised"] == 0


def test_settlement_falsifies_a_wrongly_assumed_relation():
    """Both 'mutually exclusive' outcomes settle YES: the structure was wrong, and the realised P&L
    falls short of the promised net edge.  Settlement is the empirical test of DECLARED claims."""
    res = run(world(), [spec("A", "B", "E1", EvidenceLevel.DECLARED)], CATS)
    e1 = res.episodes[0]
    out = A.realised_check([e1], {"A": 10_000, "B": 10_000}, "fee_free")
    assert out["DECLARED"]["realised_below_promised"] == 1
    assert out["DECLARED"]["worst_usd"] < 0  # paid ~93c per bundle for nothing
    assert A.realised_check([e1], {}, "fee_free") == {}  # unsettled markets are not scored


# ------------------------------------------------------------------ Experiment C
def test_lifetime_bounds_bracket_and_are_monotone():
    res = run(world(), SPECS, CATS)
    eps = A.select(res, P, confirmed=False)
    out = A.lifetime_analysis(eps, P)
    assert out["n"] == 2
    lows = [g["point"][0] for g in out["survival_grid"]]
    ups = [g["point"][1] for g in out["survival_grid"]]
    assert all(lo <= up for lo, up in zip(lows, ups, strict=True))
    assert lows == sorted(lows, reverse=True) and ups == sorted(ups, reverse=True)
    assert out["km_median_s"]["pessimistic"] is not None
    assert out["km_median_s"]["pessimistic"] <= out["km_median_s"]["optimistic"]


def test_confirmed_episodes_have_no_information_below_one_cycle():
    """Conditioning on 'persisted' makes S(t)=1 for t below a cycle BY CONSTRUCTION - the analysis
    reports that as a bound, and the doc says it is not evidence of fast survival."""
    res = run(world(), SPECS, CATS)
    out = A.lifetime_analysis(A.select(res, P, confirmed=True), P)
    assert out["survival_grid"][1]["point"] == [1.0, 1.0]  # t = 0.1 s


def test_end_causes_split_taken_edges_from_moved_quotes():
    res = run(world(), SPECS, CATS)
    out = A.end_causes(A.select(res, P, confirmed=False), P)
    assert out["ended"] == 2 and out["counts"] == {"quote_change": 2}
    assert out["consumed_share"]["value"] == 0.0


# ------------------------------------------------------------------ Experiment D
def _latency_world():
    """Violated at t=0 with 500 contracts; at t=2 B's depth is pulled to 40; gone at t=6."""
    return [
        bu(0, "A", [(5500, c(500))]),
        bu(0, "B", [(5200, c(500))]),
        cf(0),
        bu(2, "B", [(5200, c(40))]),
        cf(3),
        bu(6, "B", [(2000, c(500))]),
        cf(6),
        cf(9),
    ]


def test_execution_table_brackets_fill_and_net_by_the_two_readings():
    ev = _latency_world()
    res = run(ev, [spec("A", "B", "E1")], CATS, deltas=[int(1 * SEC), int(5 * SEC)])
    rows = {r["delta_s"]: r for r in L.execution_table(res.episodes, "fee_free", P)}
    one = rows[1]
    # D=1s: the book observed by then is the detection book (fills fully); the next observation
    # (t=2) has B thinned to 40 of 100 contracts, so the pessimistic reading fills nothing whole.
    assert one["full_fill"]["lo"]["value"] == 0.0 and one["full_fill"]["hi"]["value"] == 1.0
    assert one["min_leg_fill_fraction"]["lo"]["value"] == pytest.approx(0.4)
    assert one["min_leg_fill_fraction"]["hi"]["value"] == pytest.approx(1.0)
    assert one["net_usd_per_detected"]["hi"]["value"] == pytest.approx(7.0)  # 100 x 7c, no fees
    assert one["net_usd_per_detected"]["lo"]["value"] == 0.0
    assert one["expired_share"]["lo"]["value"] == 0.0 and one["expired_share"]["hi"]["value"] == 1.0
    # D=5s: state at t=3 is still thin, the next (t=6) is gone -> nothing whole fills either way
    assert rows[5]["full_fill"]["hi"]["value"] == 0.0


def test_probes_the_data_cannot_observe_are_excluded_not_invented():
    res = run(_latency_world(), [spec("A", "B", "E1")], CATS, deltas=[int(60 * SEC)])
    pairs, skipped = L.pairs_at(res.episodes, 60, gap_s=30)
    assert pairs == [] and skipped == 1  # 60 s after detection is past the end of the recording
    row = next(r for r in L.survival_table(res.episodes, P) if r["delta_s"] == 60)
    assert row["n"] == 0 and row["alive"] == {"lo": None, "hi": None}


def test_survival_by_covariate_uses_the_same_bracket():
    res = run(_latency_world(), [spec("A", "B", "E1")], CATS, deltas=[int(3 * SEC)])
    out = L.survival_by_covariate(res.episodes, 3, A.edge_bins(), P)
    assert sum(b["n"] for b in out) == 1
    b = next(b for b in out if b["n"])
    assert b["alive"]["lo"]["value"] <= b["alive"]["hi"]["value"]


def test_extrapolation_is_a_labelled_model_and_absent_without_a_rate():
    assert L.extrapolated_survival(None) == []
    rows = L.extrapolated_survival(0.1)
    assert rows[0]["survival_model"] == pytest.approx(math.exp(-0.1 * 0.001))
    assert [r["survival_model"] for r in rows] == sorted(
        (r["survival_model"] for r in rows), reverse=True
    )


# ------------------------------------------------------------------ engine sweep (leg risk)
class StubDataset:
    def __init__(self, events, tickers, specs):
        self.feed = ListFeed(events)
        self.feed.markets = [SimpleNamespace(ticker=t) for t in tickers]
        self.feed.infos = {
            t: MarketInfo(t, "E", "KX", "C", "t", "y", "n", Strike(), Rules(), None, None, ())
            for t in tickers
        }
        self.specs, self.fees = specs, FEES


def test_engine_sweep_shows_leg_risk_appear_as_latency_grows():
    """B's depth vanishes 1 s after the violation appears.  With ~zero latency both legs fill; at
    2 s latency only A's leg does - a riskless arbitrage has become a directional bet."""
    ev = [
        bu(0, "A", [(5500, c(500))]),
        bu(0, "B", [(5200, c(500))]),
        cf(0),
        bu(1, "B", []),  # B's book empties: nothing left to buy
        cf(3),
    ]
    ds = StubDataset(ev, ["A", "B"], [spec("A", "B", "E1")])
    fast, slow = L.engine_latency_sweep(
        ds, [0, 2000], min_level=EvidenceLevel.DECLARED, fee_scale=0
    )
    assert fast["bundles"] == 1 and fast["fully_filled"] == 1 and fast["leg_risk_share"] == 0.0
    assert slow["bundles"] == 1 and slow["partially_filled"] == 1 and slow["leg_risk_share"] == 1.0
    assert slow["net_usd"] <= fast["net_usd"]


def test_leg_outcomes_groups_orders_into_bundles():
    from backtest.engine import TIF, OrderRecord

    def rec(i, ts, qty, filled):
        return OrderRecord(i, ts, ts, "A", Side.NO, 4500, qty, TIF.IOC, "arb:x", "filled", filled)

    res = SimpleNamespace(
        orders=[
            rec(1, 10, 100, 100),
            rec(2, 10, 100, 100),
            rec(3, 20, 100, 100),
            rec(4, 20, 100, 0),
            rec(5, 30, 100, 0),
            rec(6, 30, 100, 0),
        ]
    )
    out = L.leg_outcomes(res)
    assert (out["bundles"], out["fully_filled"], out["partially_filled"], out["unfilled"]) == (
        3,
        1,
        1,
        1,
    )
    assert out["leg_risk_share"] == pytest.approx(1 / 3)


# ------------------------------------------------------------------ mutation-driven additions
def test_realised_pnl_includes_fees_with_the_right_sign():
    """Baseline (fees on): for a proven relation with exactly one winner the bundle pays exactly
    $1 per contract, so realised == the promised net edge to the microdollar."""
    res = run(world(), SPECS, CATS)
    e1 = next(e for e in res.episodes if e.event == "E1")
    net = e1.first.views["baseline"].net_micro
    assert net > 0 and e1.first.views["baseline"].fee_micro > 0
    out = A.realised_check([e1], {"A": 10_000, "B": 0}, "baseline")
    assert out["PROVEN"]["realised_usd"] * 1_000_000 == pytest.approx(net)
    assert out["PROVEN"]["promised_usd"] * 1_000_000 == pytest.approx(net)


def test_end_causes_do_not_count_censored_episodes_as_ended():
    ev = [
        b for b in world() if not (getattr(b, "ticker", "") == "B" and b.ts_ns == T0 + 1800 * SEC)
    ]
    res = run(ev, SPECS, CATS)  # E1 is never repaired: still open when the data ends
    out = A.end_causes(A.select(res, P, confirmed=False), P)
    assert out["ended"] == 1 and out["censored"] == 1
    assert out["censored_reasons"] == {"end_of_data": 1}


def test_sighting_diagnostic_counts_persisted_and_not_persisted_separately():
    extra = [
        bu(600, "E", [(5500, c(500))]),
        bu(600, "F", [(5200, c(500))]),
        bu(1800, "F", [(2000, c(500))]),
    ]
    res = run([*world(), *extra], SPECS, CATS)  # E1 and E3 stand; E2 flickers
    diag = A.sighting_lifetime_diagnostic(A.select(res, P, confirmed=False))
    assert (diag["persisted"], diag["not_persisted"]) == (2, 1)
    assert diag["persisted_share"] == pytest.approx(2 / 3)


def test_edge_bins_use_inclusive_upper_boundaries():
    def bids(a, b, ev):  # bids sum to 1 + margin: margin ticks = a + b - 10000
        return [bu(600, ev[0], [(a, c(500))]), bu(600, ev[1], [(b, c(500))])]

    start = [bu(0, t, [(2000, c(500))]) for t in "ABCDEF"] + [cf(0), cf(600), cf(1200)]
    ev = (
        start + bids(5100, 5000, "AB") + bids(5200, 5000, "CD") + bids(5300, 5000, "EF")
    )  # 1c, 2c, 3c
    res = run(ev, SPECS, CATS)
    eps = A.select(res, P, confirmed=False)
    assert sorted(e.first.top_margin for e in eps) == [100, 200, 300]
    out = A.executability_by_covariate(eps, "fee_free", P)
    assert {b["bin"]: b["n"] for b in out["edge"]} == {"<=1c": 1, "1-2c": 1, ">2c": 1}


def test_probes_in_a_recording_dark_stretch_are_unobservable():
    ev = [bu(0, "A", [(5500, c(500))]), bu(0, "B", [(5200, c(500))]), cf(0), cf(3), cf(200)]
    res = run(ev, [spec("A", "B", "E1")], CATS, deltas=[int(60 * SEC)])
    pairs, skipped = L.pairs_at(res.episodes, 60, gap_s=30)
    assert (
        pairs == [] and skipped == 1
    )  # last book confirmed 57 s before the probe: nothing to test
    pairs, skipped = L.pairs_at(res.episodes, 60, gap_s=1000)
    assert len(pairs) == 1 and skipped == 0  # observable once the tolerance allows it


def test_latency_stage_of_the_funnel_counts_what_is_still_fillable_after_the_delay():
    res = run(_latency_world(), [spec("A", "B", "E1")], CATS, deltas=[int(1 * SEC), int(5 * SEC)])
    out = L.latency_funnel(res.episodes, "fee_free", P, deltas_s=(1.0, 5.0))
    assert out["displayed"] == 1 and out["stages_at_first_sight"] == [1, 1, 1, 1, 1]
    one, five = out["latency"]
    # D=1s: fills whole against the detection book, but not against the next one (B thinned)
    assert (one["survive_lo"], one["survive_hi"]) == (0, 1)
    # D=5s: neither the last observed book (thin) nor the next (gone) fills whole
    assert (five["survive_lo"], five["survive_hi"]) == (0, 0)
    base = L.latency_funnel(res.episodes, "baseline", P, deltas_s=(1.0,))
    assert base["stages_at_first_sight"][4] == 1  # 7c edge survives baseline fees at 100 contracts


def test_bundle_pnl_shows_leg_risk_where_per_market_pnl_cannot():
    """A hedged bundle books a loss on the market whose NO leg loses and a gain on the other; only the
    *bundle* P&L says whether the hedge held.  Fully filled: >= the promised edge in every world.
    Only A's leg filled: a naked NO on A - it wins if A loses and loses everything if A wins."""
    from backtest.events import Settlement

    def sweep(winner, latency_ms):
        ev = [
            bu(0, "A", [(5500, c(500))]),
            bu(0, "B", [(5200, c(500))]),
            cf(0),
            bu(1, "B", []),
            cf(3),
            Settlement(T0 + 10 * SEC, "A", 10_000 if winner == "A" else 0),
            Settlement(T0 + 10 * SEC, "B", 10_000 if winner == "B" else 0),
        ]
        ds = StubDataset(ev, ["A", "B"], [spec("A", "B", "E1")])
        (r,) = L.engine_latency_sweep(
            ds, [latency_ms], min_level=EvidenceLevel.DECLARED, fee_scale=0
        )
        return r

    for winner in ("A", "B"):  # hedged: 100 contracts x 7c, whichever side wins
        fast = sweep(winner, 0)
        assert fast["fully_filled"] == 1 and fast["bundles_scored"] == 1
        assert fast["full_bundles_net_usd"] == pytest.approx(7.0)  # the promised edge, exactly
        assert fast["worst_bundle_usd"] == pytest.approx(7.0)  # although one MARKET lost ~$45
    a_wins, b_wins = sweep("A", 2000), sweep("B", 2000)  # only A's NO leg fills
    assert a_wins["partially_filled"] == 1 and b_wins["partially_filled"] == 1
    assert a_wins["partial_bundles_net_usd"] == pytest.approx(
        -45.0
    )  # naked NO on A: 100 x 45c lost
    assert b_wins["partial_bundles_net_usd"] == pytest.approx(55.0)  # ...or 100 x (1 - 45c) won
    assert a_wins["worst_partial_bundle_usd"] < 0 < b_wins["worst_partial_bundle_usd"]


def test_unsettled_bundles_are_not_scored_and_are_counted():
    ev = [bu(0, "A", [(5500, c(500))]), bu(0, "B", [(5200, c(500))]), cf(0), cf(3)]
    ds = StubDataset(ev, ["A", "B"], [spec("A", "B", "E1")])
    (r,) = L.engine_latency_sweep(ds, [0], min_level=EvidenceLevel.DECLARED, fee_scale=0)
    assert r["bundles"] == 1 and r["bundles_scored"] == 0 and r["bundles_unsettled"] == 1
    assert r["worst_bundle_usd"] is None
