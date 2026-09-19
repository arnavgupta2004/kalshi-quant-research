import math
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arbitrage.detector import detect
from arbitrage.opportunity import RelationSpec
from backtest.events import BookConfirm, BookUpdate, MarketClose, TradeTick
from backtest.feed import ListFeed
from market.contracts import Side, Trade
from market.fees import FeeBook, FeeSchedule
from market.relationships import Exhaustive, MutuallyExclusive, Partition
from market.semantics import EvidenceLevel
from research.scanner import (
    SEC,
    ScanConfig,
    Scanner,
    constraint_id,
    standard_variants,
)

T0 = 1_800_000_000 * SEC
FEES = FeeBook({}, FeeSchedule(known=False))
ME = MutuallyExclusive(["A", "B"])
SPEC = RelationSpec(ME, EvidenceLevel.PROVEN, "EV", "KX")


def c(n):
    return n * 100


def bu(sec, t, yes=(), no=()):
    return BookUpdate(T0 + int(sec * SEC), t, tuple(yes), tuple(no))


def cf(sec):
    return BookConfirm(T0 + int(sec * SEC))


def scan(
    events, specs=(SPEC,), *, deltas=(), gap_s=30.0, categories=None, ends=None, observer=None
):
    cfg = ScanConfig(standard_variants(FEES), gap_s=gap_s, probe_deltas_ns=tuple(deltas))
    sc = Scanner(list(specs), categories or {}, ends or {}, cfg, observer=observer)
    return sc.run(ListFeed(events)), sc


# A/B are mutually exclusive: YES bids .55 + .52 > 1 -> buy NO on both for .45 + .48 = .93 < $1
VIOLATED = [bu(0, "A", yes=[(5500, c(500))]), bu(0, "B", yes=[(5200, c(500))])]


# ------------------------------------------------------------------ lifecycle
def test_episode_lifecycle_from_first_sighting_to_disappearance():
    ev = [
        *VIOLATED,
        cf(0),
        cf(3),
        cf(6),
        bu(6, "B", yes=[(3000, c(500))]),  # B reprices at the same instant as the next confirm
    ]
    res, _ = scan(ev)
    [ep] = res.episodes
    assert ep.first_seen_ns == T0 and ep.end_ns == T0 + 6 * SEC
    assert ep.end_reason == "quote_change"
    assert ep.lo_s == 3.0 and ep.hi_s == 6.0  # alive at 3 s, seen dead at 6 s: lifetime in [3, 6)
    assert (
        ep.n_confirms == 2 and ep.persisted
    )  # the confirm that shares its instant with death is not counted
    assert ep.left_censored  # detected in the very first cycle: true start unknown
    assert ep.kind == "mutually_exclusive" and ep.level == "PROVEN" and ep.event == "EV"
    assert ep.first.top_margin == 700  # 7c per bundle at the best asks
    assert [leg[:2] for leg in ep.legs] == [("A", "no"), ("B", "no")]


def test_a_violation_that_lasts_only_one_composite_is_not_persisted():
    """Chunk 1 of a poll cycle delivers A's new price while B is still last cycle's: a composite.
    B's own update arrives a second later and the 'violation' is gone before the cycle ends."""
    ev = [
        bu(0, "A", yes=[(5000, c(500))]),
        bu(0, "B", yes=[(4000, c(500))]),
        cf(0),
        bu(10, "A", yes=[(6000, c(500))]),  # composite: A new + B stale (.45 -> sum 1.05)
        bu(10, "B", yes=[(4500, c(500))]),
        cf(11),
    ]
    ev[4] = bu(10, "B", yes=[(4500, c(500))])
    ev = [
        bu(0, "A", yes=[(5000, c(500))]),
        bu(0, "B", yes=[(4500, c(500))]),
        cf(0),
        bu(10, "A", yes=[(6000, c(500))]),  # A moves first: 0.60 + 0.45 = 1.05 -> looks violated
        bu(11, "B", yes=[(3000, c(500))]),  # B's chunk arrives: 0.60 + 0.30 -> fine
        cf(11),
    ]
    res, _ = scan(ev)
    [ep] = res.episodes
    assert ep.first_seen_ns == T0 + 10 * SEC and ep.end_ns == T0 + 11 * SEC
    assert not ep.persisted and ep.n_confirms == 0
    assert not ep.left_censored  # a genuine detection after the first cycle


def test_events_with_one_timestamp_are_one_observation_no_phantom_arbitrage():
    """The Stage 5 phantom: a goal reprices two books that arrive in ONE response.  Applied one at a
    time the pair looks violated (A new .66 + B stale .70 = 1.36); applied together it is not."""
    before = [bu(0, "A", yes=[(2000, c(500))]), bu(0, "B", yes=[(7000, c(500))]), cf(0)]
    atomic = before + [bu(5, "A", yes=[(6600, c(500))]), bu(5, "B", yes=[(2100, c(500))]), cf(5)]
    res, _ = scan(atomic)
    assert res.episodes == []
    # ...whereas 1 ns apart the intermediate cross-section IS observable, and is reported as such
    apart = before + [
        BookUpdate(T0 + 5 * SEC, "A", ((6600, c(500)),), ()),
        BookUpdate(T0 + 5 * SEC + 1, "B", ((2100, c(500)),), ()),
        BookConfirm(T0 + 5 * SEC + 1),
    ]
    res, _ = scan(apart)
    [ep] = res.episodes
    assert ep.end_ns - ep.first_seen_ns == 1 and not ep.persisted


def test_identical_constraints_from_several_relations_are_one_episode_credited_to_the_best_evidence():
    weak = RelationSpec(ME, EvidenceLevel.DECLARED, "EV", "KX")
    res, _ = scan([*VIOLATED, cf(0)], specs=[weak, SPEC])
    [ep] = res.episodes
    assert ep.level == "PROVEN"


def test_variants_disagree_exactly_where_fees_matter():
    # 7c displayed edge on 5 contracts: fee-free keeps all of it, the standard fee (~2 x 1.7c) eats it
    res, _ = scan([*VIOLATED, cf(0)])
    [ep] = res.episodes
    v = ep.first.views
    assert v["fee_free"].net_micro > 0 and v["fee_free"].gross_micro == v["fee_free"].net_micro
    assert v["baseline"].fee_micro > 0
    assert v["fee_free"].net_micro > v["fees_half"].net_micro > v["baseline"].net_micro
    assert ep.best_stage["fee_free"] >= ep.best_stage["baseline"]


# ------------------------------------------------------------------ recording gaps
def test_a_recording_gap_censors_open_episodes_and_is_excluded_from_exposure():
    ev = [*VIOLATED, cf(0), cf(3), cf(6), cf(200), cf(203)]  # dark from 6 s to 200 s
    res, _ = scan(ev, gap_s=30)
    [ep] = res.episodes
    assert ep.end_reason == "gap" and ep.censored and ep.end_ns is None
    assert ep.lo_s == 6.0 and math.isinf(ep.hi_s)  # alive until we lost sight of it
    assert res.gaps == [(T0 + 6 * SEC, T0 + 200 * SEC)]
    [ex] = res.exposures
    assert ex.seconds == pytest.approx(6 + 3)  # 0..6 before the gap, 200..203 after; never the gap


def test_first_detection_after_a_gap_is_left_censored():
    ev = [
        bu(0, "A", yes=[(2000, c(500))]),
        bu(0, "B", yes=[(2000, c(500))]),
        cf(0),
        cf(3),
        bu(300, "A", yes=[(5500, c(500))]),
        bu(300, "B", yes=[(5200, c(500))]),
        cf(300),
        cf(303),
    ]
    res, _ = scan(ev, gap_s=30)
    [ep] = res.episodes
    assert ep.left_censored  # we cannot know it did not begin during the blind interval


# ------------------------------------------------------------------ what ended it
def _trade(sec, ticker, side, yes_price):
    return TradeTick(
        T0 + int(sec * SEC),
        ticker,
        Trade(
            "t",
            ticker,
            yes_price,
            10_000 - yes_price,
            c(50),
            side,
            None,
            datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )


def test_a_trade_that_lifts_the_edge_attributes_the_end_to_consumption():
    # we buy NO on B at 0.48 (= 1 - 0.52); a NO taker printing at no_price >= .48 took that liquidity
    ev = [
        *VIOLATED,
        cf(0),
        cf(3),
        _trade(4, "B", Side.NO, 4800),
        bu(6, "B", yes=[(3000, c(500))]),
        cf(6),
    ]
    res, _ = scan(ev)
    assert res.episodes[0].end_reason == "consumed"


def test_a_trade_on_the_wrong_side_or_outside_the_gap_is_not_consumption():
    wrong_side = [
        *VIOLATED,
        cf(0),
        _trade(1, "B", Side.YES, 5200),
        bu(3, "B", yes=[(3000, c(500))]),
        cf(3),
    ]
    assert scan(wrong_side)[0].episodes[0].end_reason == "quote_change"
    stale = [
        *VIOLATED,
        _trade(-1, "B", Side.NO, 4800),
        cf(0),
        cf(3),
        bu(6, "B", yes=[(3000, c(500))]),
    ]
    assert scan(stale)[0].episodes[0].end_reason == "quote_change"


def test_market_close_censors_rather_than_counting_as_decay():
    ev = [*VIOLATED, cf(0), cf(3), MarketClose(T0 + 4 * SEC, "A"), cf(6)]
    res, _ = scan(ev)
    [ep] = res.episodes
    assert ep.end_reason == "closed" and ep.censored and math.isinf(ep.hi_s)
    assert ep.lo_s == 3.0


def test_open_episodes_are_censored_at_the_end_of_the_data():
    res, _ = scan([*VIOLATED, cf(0), cf(3)])
    [ep] = res.episodes
    assert ep.end_reason == "end_of_data" and ep.censored and ep.lo_s == 3.0


# ------------------------------------------------------------------ latency probes
def test_probes_bracket_the_survival_between_the_last_and_the_next_observation():
    ev = [*VIOLATED, cf(0), cf(3), bu(3, "B", yes=[(3000, c(500))]), cf(6)]
    res, _ = scan(ev, deltas=[int(1 * SEC), int(5 * SEC)])
    [ep] = res.episodes
    by = {(p.delta_ns // SEC, p.kind): p for p in ep.probes}
    # D=1s: last book observed by then still violated (optimistic); the next observation (t=3) is not
    assert by[(1, "before")].alive and not by[(1, "after")].alive
    ok, frac, net = by[(1, "before")].variants["fee_free"]
    assert ok and frac == 1.0 and net == ep.first.views["fee_free"].net_micro
    assert by[(1, "after")].variants["fee_free"][:2] == (False, 0.0)
    # D=5s: both bracketing observations are already past the disappearance
    assert not by[(5, "before")].alive and not by[(5, "after")].alive


def test_a_probe_reports_a_partial_fill_when_depth_is_pulled():
    ev = [
        *VIOLATED,
        cf(0),
        bu(2, "B", yes=[(5200, c(40))]),
        cf(3),
    ]  # B: 500 -> 40 contracts (< the 100 sent)
    res, _ = scan(ev, deltas=[int(1 * SEC)])
    [ep] = res.episodes
    after = next(p for p in ep.probes if p.kind == "after")
    full, frac, net = after.variants["fee_free"]
    assert not full and net is None
    assert ep.first.views["fee_free"].bundles == c(100)  # sized at the cap, not the book's 500
    assert frac == pytest.approx(0.4)  # 40 of 100 contracts on the thinner leg


def test_probes_past_the_end_of_the_data_are_flagged_unobservable():
    res, _ = scan([*VIOLATED, cf(0), cf(3)], deltas=[int(60 * SEC)])
    [ep] = res.episodes
    assert [p.beyond_data for p in ep.probes] == [True]


def test_every_observable_probe_comes_in_before_after_pairs():
    ev = [*VIOLATED, cf(0), cf(3), cf(6), cf(9), cf(12)]
    res, _ = scan(ev, deltas=[int(0.25 * SEC), int(4 * SEC)])
    [ep] = res.episodes
    kinds = sorted((p.delta_ns, p.kind) for p in ep.probes)
    assert kinds == [
        (int(0.25 * SEC), "after"),
        (int(0.25 * SEC), "before"),
        (4 * SEC, "after"),
        (4 * SEC, "before"),
    ]


def test_sub_cadence_delays_see_the_same_book_by_construction():
    """The identification limit made concrete: with 3 s polls, any D < 3 s evaluates the very same
    state as detection, so 'survival' is 1 - which is why the analysis reports it as a BOUND."""
    ev = [*VIOLATED, cf(0), cf(3), bu(3, "B", yes=[(3000, c(500))]), cf(6)]
    res, _ = scan(ev, deltas=[int(x * SEC) for x in (0.001, 0.05, 0.25, 1.0, 2.9)])
    before = [p for p in res.episodes[0].probes if p.kind == "before"]
    assert all(p.alive for p in before)


# ------------------------------------------------------------------ metadata & exposure
def test_category_time_to_expiry_and_exposure_are_recorded():
    ends = {"A": T0_dt(3600), "B": T0_dt(7200)}
    cats = {"A": "Sports", "B": "Sports"}
    res, _ = scan([*VIOLATED, cf(0), cf(3), cf(6)], categories=cats, ends=ends)
    [ep] = res.episodes
    assert ep.category == "Sports"
    assert ep.hours_to_expiry == pytest.approx(1.0)  # the earliest scheduled end, not close_time
    [ex] = res.exposures
    assert (ex.seconds, ex.cycles, ex.n_constraints, ex.kind) == (
        6.0,
        3,
        len(ME.constraints()),
        "mutually_exclusive",
    )
    mixed, _ = scan([*VIOLATED, cf(0)], categories={"A": "Sports", "B": "Crypto"})
    assert mixed.episodes[0].category == "mixed"


def T0_dt(offset_s):
    return datetime.fromtimestamp(T0 / SEC, tz=UTC) + timedelta(seconds=offset_s)


def test_exposure_starts_only_once_every_book_of_the_relation_exists():
    ev = [bu(0, "A", yes=[(2000, c(5))]), cf(0), bu(4, "B", yes=[(2000, c(5))]), cf(4), cf(10)]
    res, _ = scan(ev)
    [ex] = res.exposures
    assert ex.seconds == pytest.approx(6.0)  # 4 -> 10


# ------------------------------------------------------------------ correctness against a full re-scan
@st.composite
def random_feeds(draw):
    tickers = ["A", "B", "C"]
    events, t = [], 0.0
    for _ in range(draw(st.integers(3, 25))):
        t += draw(st.sampled_from([0.0, 0.0, 1.0, 3.0, 7.0]))
        kind = draw(st.sampled_from(["book"] * 9 + ["confirm"] * 3 + ["close"]))
        tk = draw(st.sampled_from(tickers))
        if kind == "book":
            yes = draw(st.lists(st.integers(30, 75), min_size=1, max_size=3, unique=True))
            no = draw(st.lists(st.integers(5, 45), min_size=0, max_size=3, unique=True))
            # keep the book uncrossed: best YES bid + best NO bid <= 99c
            if yes and no and max(yes) + max(no) > 99:
                no = [n for n in no if n + max(yes) <= 99]
            events.append(
                bu(
                    t,
                    tk,
                    [(y * 100, c(draw(st.integers(1, 20)))) for y in sorted(yes, reverse=True)],
                    [(n * 100, c(draw(st.integers(1, 20)))) for n in sorted(no, reverse=True)],
                )
            )
        elif kind == "confirm":
            events.append(cf(t))
        else:
            events.append(MarketClose(T0 + int(t * SEC), tk))
    return events


SPECS = [
    RelationSpec(MutuallyExclusive(["A", "B", "C"]), EvidenceLevel.PROVEN, "EV", "KX"),
    RelationSpec(Exhaustive(["A", "B"]), EvidenceLevel.DECLARED, "EV", "KX"),
    RelationSpec(Partition(["B", "C"]), EvidenceLevel.PROVEN, "EV2", "KX"),
]


def test_incremental_scan_equals_a_full_rescan_after_every_batch():
    """The optimisation (re-price only relations touching a changed book) must not change the answer:
    after every batch the open episodes are exactly the violations a from-scratch detect finds.
    The counter proves the property was exercised on feeds that actually contain violations."""
    seen = {"batches": 0, "with_violation": 0}
    mismatches = []

    def observer(sc: Scanner, ts: int) -> None:
        usable = {t: b for t, b in sc.books.items() if t not in sc.closed}
        ops, _ = detect(SPECS, usable, sc.driver, ts_ns=ts)
        full = {constraint_id(op.constraint) for op in ops}
        seen["batches"] += 1
        seen["with_violation"] += bool(full)
        if full != sc.displayed_now():
            mismatches.append((ts, full ^ sc.displayed_now()))

    @settings(max_examples=150, deadline=None)
    @given(random_feeds())
    def prop(events):
        scan(events, specs=SPECS, observer=observer)

    prop()
    assert mismatches == []
    assert seen["with_violation"] >= 100, seen  # not vacuous: many batches really had violations
    assert seen["with_violation"] < seen["batches"], seen  # ...and many did not


@settings(max_examples=100, deadline=None)
@given(random_feeds(), st.integers(0, 60))
def test_detection_time_facts_depend_only_on_the_past(events, cut_s):
    """Causality: truncating the feed at T never changes anything about an episode detected by T
    (its start, size, edge, class); only what happened *after* T (its end) can differ."""
    cut = T0 + cut_s * SEC

    def facts(res):
        return {
            (e.key, e.first_seen_ns): (
                e.first.top_margin,
                e.first.top_qty,
                e.first.capacity,
                tuple(
                    (v, vw.cls, vw.net_micro, vw.bundles) for v, vw in sorted(e.first.views.items())
                ),
                e.left_censored,
            )
            for e in res.episodes
            if e.first_seen_ns <= cut
        }

    full, _ = scan(events, specs=SPECS)
    part, _ = scan([e for e in events if e.ts_ns <= cut], specs=SPECS)
    assert facts(full) == facts(part)


def test_a_scanner_needs_at_least_one_cost_variant():
    with pytest.raises(ValueError):
        Scanner([SPEC], {}, {}, ScanConfig(variants={}))


def test_a_trade_is_not_a_book_observation_for_the_pessimistic_probe():
    """'after' means the first BOOK observed later.  A trade print in between tells us nothing new
    about the book, so it must not stand in for that observation."""
    ev = [
        *VIOLATED,
        cf(0),
        _trade(1.5, "A", Side.YES, 5500),
        cf(3),
        bu(3, "B", yes=[(3000, c(500))]),
    ]
    res, _ = scan(ev, deltas=[int(1 * SEC)])
    after = next(p for p in res.episodes[0].probes if p.kind == "after")
    assert not after.alive  # evaluated at the t=3 observation, where the edge is gone


def test_peak_tracks_the_best_edge_seen_and_observations_only_record_changes():
    ev = [
        *VIOLATED,
        cf(0),
        cf(3),  # unchanged: no new observation
        bu(4, "B", yes=[(5800, c(500))]),  # edge widens: 0.55 + 0.58 -> NO asks .45 + .42
        cf(6),
        # a book change that touches no leg's ask (A's NO bids only move A's YES asks): not a new observation
        bu(7, "A", yes=[(5500, c(500))], no=[(3000, c(100))]),
        bu(8, "B", yes=[(4700, c(500))]),  # narrows again but still violated: 1.02 of bids
        cf(9),
    ]
    res, _ = scan(ev)
    [ep] = res.episodes
    margins = [o.top_margin for o in ep.obs]
    assert margins == [700, 1300, 200]
    assert ep.peak["fee_free"].net_micro == max(
        o.views["fee_free"].net_micro for o in ep.obs if o.views["fee_free"].net_micro is not None
    )
    assert ep.peak["fee_free"].net_micro > ep.first.views["fee_free"].net_micro


def test_one_confirmation_is_the_detection_cycle_itself_not_persistence():
    """Detected mid-cycle, still there at that cycle's end, gone before the next cycle's end: the
    composite was never re-fetched in full, so it is NOT counted as persisted."""
    ev = [
        bu(0, "A", yes=[(2000, c(500))]),
        bu(0, "B", yes=[(2000, c(500))]),
        cf(0),
        bu(10, "A", yes=[(5500, c(500))]),
        bu(10.5, "B", yes=[(5200, c(500))]),
        cf(10.5),  # end of the detecting cycle
        bu(12, "B", yes=[(3000, c(500))]),  # gone before the next cycle ends
        cf(13.5),
    ]
    res, _ = scan(ev)
    [ep] = res.episodes
    assert ep.n_confirms == 1 and not ep.persisted


def test_a_probe_scheduled_exactly_at_a_book_update_sees_that_update():
    """As-of means events at or before the probe time are visible (the engine's BOOK < ORDER_ARRIVAL
    priority): an order arriving at the instant B reprices meets the new B."""
    ev = [*VIOLATED, cf(0), bu(3, "B", yes=[(3000, c(500))]), cf(3)]
    res, _ = scan(ev, deltas=[int(3 * SEC)])
    before = next(p for p in res.episodes[0].probes if p.kind == "before")
    assert not before.alive


def test_probes_carry_the_edge_that_remained_not_just_whether_it_survived():
    # the edge WIDENS after detection (B's bid rises): the as-of margin is what the book then showed
    ev = [*VIOLATED, cf(0), bu(2, "B", yes=[(5800, c(500))]), cf(3)]
    res, _ = scan(ev, deltas=[int(1 * SEC), int(2.5 * SEC)])
    by = {(p.delta_ns, p.kind): p for p in res.episodes[0].probes}
    assert by[(int(1 * SEC), "before")].margin == 700  # still the detected book
    assert by[(int(1 * SEC), "after")].margin == 1300  # the first book observed afterwards
    assert by[(int(2.5 * SEC), "before")].margin == 1300
