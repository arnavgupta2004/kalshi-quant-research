"""Conventions found by running the semantics over 108k real markets (each string below is real)."""

from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from data.normalization.normalizer import normalize_market
from kalshi_client.models import ApiMarket, validate_rest
from market.contracts import Event
from market.relationships import RelationKind
from market.semantics import (
    EvidenceLevel,
    Interval,
    analyze_event,
    grid_step,
    numeric_view,
    parse_rules,
)
from tests.fakes import make_market

CLOSE = datetime(2026, 9, 17, 16, tzinfo=UTC)


def mk(fx, ticker, rules, strike_type, floor=None, cap=None, event="E-1"):
    raw = make_market(fx, ticker, event=event, close=CLOSE)
    raw.update(rules_primary=rules, strike_type=strike_type)
    raw.pop("floor_strike", None)
    raw.pop("cap_strike", None)
    if floor is not None:
        raw["floor_strike"] = floor
    if cap is not None:
        raw["cap_strike"] = cap
    return normalize_market(validate_rest(ApiMarket, raw))


def gt(x):
    return Interval(D(x), False, None, False)


def ge(x):
    return Interval(D(x), True, None, False)


@pytest.mark.parametrize(
    "text,expect",
    [
        ("If Post Malone has Above 4.5B Worldwide Streams during 2026", gt("4500000000")),
        ("If Future has above 4M Global daily views on YouTube", gt("4000000")),
        ("if the album sells above 10K equivalent units", gt("10000")),
        ("Kendrick Bourne records 15+ receiving yards in the game", ge("15")),
        ("Alika Williams records 1+ total hits + runs + rbis in the game", ge("1")),
        ("ARI Cardinals D/ST scores at least 1+ touchdowns in the game", ge("1")),
    ],
)
def test_unit_suffixes_and_bare_plus_thresholds(text, expect):
    assert parse_rules(text).interval == expect


def test_suffix_letters_inside_words_are_not_units():
    assert parse_rules("If Bitcoin is above 5 Billion").interval == gt(
        "5"
    )  # 'Billion' is not the 'B' suffix
    assert parse_rules("is above 5M").interval == gt("5000000")


def test_half_integer_strikes_mean_an_integer_valued_variable():
    assert grid_step([D("0.5"), D("1")]) == 1
    assert grid_step([D("17.5"), D("22.5")]) == 1
    assert grid_step([D("3.6")]) == D("0.1")  # 3.60% is a genuine one-decimal quantity
    assert grid_step([D("26899.99"), D("26900")]) == D("0.01")
    assert grid_step([D("80"), D("81")]) == 1


def test_index_threshold_encodings_agree_on_the_cent_grid_but_not_on_the_real_line(fx):
    # real KXNASDAQ100U market: strike says >= 26900, text says > 26899.99
    m = mk(
        fx,
        "KXNASDAQ100U-1200-T26900",
        "If the Nasdaq 100 index value on Sep 17, 2026 at 12pm EDT is above 26899.99, then the market resolves to Yes.",
        "greater_or_equal",
        floor=26900.0,
    )
    nm, why = numeric_view(m)
    assert why is None and nm is not None
    assert not nm.exact and nm.step == D("0.01")  # honest: equal only on the grid
    assert nm.interval == ge("26900")


def test_count_prop_strike_half_integer_agrees_with_plus_text_on_integers(fx):
    m = mk(
        fx,
        "KXUCLGOAL-X-CHR",
        "If Andreas Christensen records at least 1 goal during the entire game of the match, then Yes.",
        "greater",
        floor=0.5,
    )
    nm, why = numeric_view(m)
    assert why is None and not nm.exact and nm.step == 1


def test_genuine_disagreement_is_still_a_conflict(fx):
    m = mk(
        fx,
        "KXUCLGOAL-X-CHR",
        "If Andreas Christensen records 2+ goals during the entire game",
        "greater",
        floor=0.5,
    )
    nm, why = numeric_view(m)
    assert nm is None and "strike says" in why  # (0.5, inf) is not [2, inf) even on integers


def test_wrong_unit_reading_is_caught_by_the_strike_cross_check_not_silently_used(fx):
    # "100M" meaning metres would parse as 100,000,000; the strike (100) exposes it
    m = mk(fx, "DASH-X-A", "If the winner runs the 100M dash in under 9.9 seconds", "less", cap=9.9)
    assert numeric_view(m)[0] is None or numeric_view(m)[0].interval == Interval(
        None, False, D("9.9"), False
    )
    m2 = mk(
        fx, "DASH-X-B", "If the runner completes the race above 100M meters", "greater", floor=100.0
    )
    nm, why = numeric_view(m2)
    assert nm is None and "strike says" in why


def test_relations_built_on_grid_only_agreement_are_capped_at_lattice_not_proven(fx):
    """A ladder of 'N+' markets is a chain, but its inclusion proof leans on the integer grid."""
    ms = [
        mk(
            fx,
            f"KXMLBHIT-X-P{k}",
            f"If Alika Williams records {k}+ hits in the game, then the market resolves to Yes.",
            "greater",
            floor=k - 0.5,
        )
        for k in (1, 2, 3)
    ]
    ev = Event(
        "E-1", "KXMLBHIT", "hits", "", "Sports", False, market_tickers=tuple(m.ticker for m in ms)
    )
    a = analyze_event(ev, ms)
    [chain] = [x for x in a.assessments if x.relation.kind is RelationKind.CHAIN]
    assert chain.level is EvidenceLevel.LATTICE and len(chain.relation.markets) == 3
    assert chain.relation.markets == (
        "KXMLBHIT-X-P3",
        "KXMLBHIT-X-P2",
        "KXMLBHIT-X-P1",
    )  # 3+ ⊆ 2+ ⊆ 1+
    assert any("grid of step 1" in s for s in chain.assumptions)
    assert not a.conflicts


def test_exact_ladders_remain_proven(fx):
    ms = [
        mk(
            fx,
            f"KXAAAGASD-X-T{k}",
            f"If average regular gas prices are strictly greater than ${k:.4f} on Sep 11, 2026, then Yes.",
            "greater",
            floor=k,
        )
        for k in (4.25, 4.275, 4.3)
    ]
    ev = Event(
        "E-1",
        "KXAAAGASD",
        "gas",
        "",
        "Economics",
        False,
        market_tickers=tuple(m.ticker for m in ms),
    )
    [chain] = [
        x for x in analyze_event(ev, ms).assessments if x.relation.kind is RelationKind.CHAIN
    ]
    assert chain.level is EvidenceLevel.PROVEN


def test_exact_value_markets_are_point_intervals_and_the_time_of_day_is_not_a_threshold(fx):
    """Real KXTRUMPAPPROVE rules: 'is exactly 39.4% at exactly 1:00 PM ET' has ONE numeric clause."""
    p = parse_rules(
        "If Trump's RCP approval average is exactly 39.4% at exactly 1:00 PM ET on Sep 19, 2026, then Yes."
    )
    assert p.comparator == "eq" and p.interval == Interval(D("39.4"), True, D("39.4"), True)
    assert "1:00 PM" in p.template  # the time of day stays part of the identity of the variable
    below = parse_rules(
        "If Trump's RCP approval average is below 39.4% at exactly 1:00 PM ET on Sep 19, 2026, then Yes."
    )
    assert below.template == p.template  # same variable, same instant -> comparable


def test_approval_rating_event_becomes_a_proven_partition_on_the_tenth_of_a_point_grid(fx):
    def m(sfx, rules, st, floor=None, cap=None):
        return mk(
            fx,
            f"KXTRUMPAPPROVE-26SEP19-{sfx}",
            rules,
            st,
            floor=floor,
            cap=cap,
            event="KXTRUMPAPPROVE-26SEP19",
        )

    head = "If Trump's RCP approval average is {} at exactly 1:00 PM ET on Sep 19, 2026, then the market resolves to Yes."
    ms = [m("U39.4", head.format("below 39.4%"), "less", cap=39.4)]
    ms += [
        m(f"E{v}", head.format(f"exactly {v}%"), "between", floor=v, cap=v)
        for v in (39.4, 39.5, 39.6)
    ]
    ms += [
        m("E40.0", head.format("exactly 40.0%"), "between", floor=40.0, cap=40.0),
        m("A40.0", head.format("above 40.0%"), "greater", floor=40.0),
    ]
    ms += [
        m(f"E{v}", head.format(f"exactly {v}%"), "between", floor=v, cap=v)
        for v in (39.7, 39.8, 39.9)
    ]
    ev = Event(
        "KXTRUMPAPPROVE-26SEP19",
        "KXTRUMPAPPROVE",
        "approval",
        "",
        "Politics",
        True,
        market_tickers=tuple(x.ticker for x in ms),
    )
    a = analyze_event(ev, ms)
    [part] = [x for x in a.assessments if x.relation.kind is RelationKind.PARTITION]
    assert part.level is EvidenceLevel.LATTICE and len(part.relation.markets) == 9
    assert any("step 0.1" in s for s in part.assumptions) and not a.conflicts


def test_analysis_does_not_depend_on_the_order_markets_arrive_in(fx):
    """Regression: two markets sharing a lower bound ([40,40] and (40,inf)) were tiled in ticker order,
    which sometimes looked like a gap.  Every permutation must give the identical relations."""
    import itertools
    import random

    def m(sfx, rules, st, floor=None, cap=None):
        return mk(
            fx,
            f"KXTRUMPAPPROVE-26SEP19-{sfx}",
            rules,
            st,
            floor=floor,
            cap=cap,
            event="KXTRUMPAPPROVE-26SEP19",
        )

    head = "If Trump's RCP approval average is {} at exactly 1:00 PM ET on Sep 19, 2026, then the market resolves to Yes."
    ms = [
        m("U39.4", head.format("below 39.4%"), "less", cap=39.4),
        m("A40.0", head.format("above 40.0%"), "greater", floor=40.0),
    ]
    ms += [
        m(f"E{v}", head.format(f"exactly {v}%"), "between", floor=v, cap=v)
        for v in (39.4, 39.5, 39.6, 39.7, 39.8, 39.9, 40.0)
    ]
    ev = Event(
        "KXTRUMPAPPROVE-26SEP19",
        "KXTRUMPAPPROVE",
        "approval",
        "",
        "Politics",
        True,
        market_tickers=tuple(x.ticker for x in ms),
    )

    def signature(markets):
        a = analyze_event(ev, markets)
        return sorted(
            (x.relation.kind.value, x.level.name, tuple(sorted(x.relation.tickers())))
            for x in a.assessments
        )

    base = signature(ms)
    assert ("partition", "LATTICE", tuple(sorted(x.ticker for x in ms))) in base
    rng = random.Random(3)
    for _ in range(40):
        shuffled = ms[:]
        rng.shuffle(shuffled)
        assert signature(shuffled) == base
    for pair in itertools.permutations([ms[1], ms[8]]):  # the two markets that tie at 40.0
        assert ("partition", "LATTICE", tuple(sorted(x.ticker for x in ms))) in signature(
            [*pair, *ms[2:8], ms[0]]
        )
