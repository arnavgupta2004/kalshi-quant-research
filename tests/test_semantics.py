"""Relation inference, tested on complete REAL Kalshi events captured from the live API."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from data.normalization.normalizer import normalize_event, normalize_market
from kalshi_client.models import ApiEvent, validate_rest
from market.contracts import Event, Market
from market.evidence import StatsBook
from market.relationships import (
    RelationKind,
    Union,
    admissible_worlds,
)
from market.semantics import (
    EvidenceLevel,
    Interval,
    analyze_event,
    exact_union,
    find_unions,
    grid_step,
    lattice_range,
    numeric_view,
    parse_rules,
    strike_interval,
)

FIX = Path(__file__).parent / "fixtures" / "events"
D = Decimal


def load(name: str) -> tuple[Event, list[Market]]:
    d = json.loads((FIX / f"{name}.json").read_text())
    ev_raw = dict(d["event"])
    ev_raw["markets"] = d["markets"]
    api = validate_rest(ApiEvent, ev_raw)
    event = normalize_event(api)
    return event, [normalize_market(m, event=event) for m in api.markets]


def by_kind(analysis, kind):
    return [a for a in analysis.assessments if a.relation.kind is kind]


# ------------------------------------------------------------------------- intervals
def iv(lo, lc, hi, hc):
    return Interval(None if lo is None else D(lo), lc, None if hi is None else D(hi), hc)


def test_interval_algebra_boundaries():
    gt5, ge5, lt5, le5 = (
        iv(5, False, None, False),
        iv(5, True, None, False),
        iv(None, False, 5, False),
        iv(None, False, 5, True),
    )
    assert gt5.subset_of(ge5) and not ge5.subset_of(gt5)  # (5,inf) ⊂ [5,inf)
    assert lt5.disjoint(gt5) and lt5.disjoint(ge5) and not le5.disjoint(ge5)  # 5 is in both
    assert le5.disjoint(gt5) and not le5.disjoint(iv(4, True, 6, True))
    assert iv(2, True, 3, True).subset_of(iv(1, False, None, False))
    assert not iv(None, False, 3, True).subset_of(iv(1, True, None, False))
    assert iv(3, False, 3, True).is_empty and not iv(3, True, 3, True).is_empty
    assert str(iv(1, True, None, False)) == "[1, +inf)"


def test_lattice_ranges_turn_open_ends_into_grid_points():
    one = D(1)
    assert lattice_range(iv(80, False, None, False), one) == (81, None)  # >80 on integers
    assert lattice_range(iv(None, False, 80, False), one) == (None, 79)  # <80
    assert lattice_range(iv(80, True, 81, True), one) == (80, 81)
    assert (
        lattice_range(iv(3, False, 4, False), one) == (4, 3)
        or lattice_range(iv(3, False, 4, False), one) is None
    )
    cent = D("0.01")
    assert lattice_range(iv("68599.99", False, None, False), cent) == (6860000, None)
    assert lattice_range(iv("68700", True, "68799.99", True), cent) == (6870000, 6879999)
    assert grid_step([D("68799.99"), D("68800")]) == cent and grid_step([D("80.0"), D("81")]) == 1


def test_strike_interval_covers_every_numeric_strike_type():
    from market.contracts import Strike

    assert strike_interval(Strike("greater", floor=4.275)) == iv("4.275", False, None, False)
    assert strike_interval(Strike("greater_or_equal", floor=3)) == iv(3, True, None, False)
    assert strike_interval(Strike("less", cap=85.0)) == iv(None, False, 85, False)
    assert strike_interval(Strike("less_or_equal", cap=3.6)) == iv(None, False, "3.6", True)
    assert strike_interval(Strike("between", floor=79100.0, cap=79199.99)) == iv(
        79100, True, "79199.99", True
    )
    assert (
        strike_interval(Strike("structured")) is None
        and strike_interval(Strike("between", floor=1.0)) is None
    )


# ------------------------------------------------------------------------- rules text
@pytest.mark.parametrize(
    "text,kind,expect",
    [
        ("If the price is above 68599.99 at 4 AM", "gt", iv("68599.99", False, None, False)),
        ("is below 68700 at 4 AM", "lt", iv(None, False, 68700, False)),
        ("is between 79100-79199.99 at 7 AM", "between", iv(79100, True, "79199.99", True)),
        ("temperature is between 84-85° fahrenheit", "between", iv(84, True, 85, True)),
        ("temperature is less than 80° fahrenheit", "lt", iv(None, False, 80, False)),
        ("are strictly greater than $4.2750 on Sep 11", "gt", iv("4.2750", False, None, False)),
        ("SOFR ... is at most 3.60%, then", "le", iv(None, False, "3.60", True)),
        ("number of games is at least 22", "ge", iv(22, True, None, False)),
        ("Los Angeles D wins by more than 3.5 runs in", "gt", iv("3.5", False, None, False)),
    ],
)
def test_rules_comparison_parsing(text, kind, expect):
    p = parse_rules(text)
    assert p.comparator == kind and p.interval == expect and "«CMP»" in p.template


def test_ambiguous_or_missing_comparison_is_never_guessed():
    assert parse_rules("resolves Yes if the winner is Alexander Zverev").comparator is None
    assert parse_rules("is above 5 and also below 3").comparator is None  # two clauses
    assert (
        parse_rules("at least the simple average of the sixty seconds of BNBUSDRTI").comparator
        is None
    )


def test_template_ignores_only_the_comparison_clause_not_the_date_or_team():
    a = parse_rules(
        "If the index before 4 AM EDT is above 68599.99 at 4 AM EDT on Sep 19, 2026, then Yes."
    )
    b = parse_rules(
        "If the index before 4 AM EDT is below 68700 at 4 AM EDT on Sep 19, 2026, then Yes."
    )
    c = parse_rules(
        "If the index before 4 AM EDT is below 68700 at 4 AM EDT on Sep 20, 2026, then Yes."
    )
    assert a.template == b.template != c.template
    lad = parse_rules("If Los Angeles D wins by more than 3.5 runs in the game, then Yes.")
    sf = parse_rules("If San Francisco wins by more than 3.5 runs in the game, then Yes.")
    assert lad.template != sf.template  # same strike, different variable


# ------------------------------------------------------------------------- real events
def test_nyc_temperature_is_a_partition_only_on_the_integer_lattice():
    event, markets = load("nyc_high")
    a = analyze_event(event, markets)
    assert not a.conflicts and not a.excluded
    [me] = [x for x in by_kind(a, RelationKind.MUTUALLY_EXCLUSIVE)]
    assert (
        me.level is EvidenceLevel.PROVEN and len(me.relation.markets) == 6
    )  # disjoint on the real line
    [part] = by_kind(a, RelationKind.PARTITION)
    # (81,82) is uncovered on the real line: exhaustiveness needs integer-valued temperature
    assert part.level is EvidenceLevel.LATTICE
    assert any("quantised to a grid of step 1" in s for s in part.assumptions)
    assert "gaps" in part.evidence[0] and "81" in part.evidence[0]
    assert not by_kind(a, RelationKind.CHAIN)  # buckets are not nested


def test_nyc_temperature_settled_outcome_is_admissible():
    """The proof would be wrong if the real settled world violated the proven partition."""
    event, markets = load("nyc_high")
    [part] = by_kind(analyze_event(event, markets), RelationKind.PARTITION)
    world = {m.ticker: int(m.result.value == "yes") for m in markets}
    assert part.relation.admissible(world) and sum(world.values()) == 1


def test_incomplete_event_never_claims_exhaustiveness():
    event, markets = load("nyc_high")
    partial = analyze_event(event, markets[:4])  # we hold only four of the six markets
    assert by_kind(partial, RelationKind.MUTUALLY_EXCLUSIVE)  # a subset is still exclusive
    assert not by_kind(partial, RelationKind.PARTITION) and not by_kind(
        partial, RelationKind.EXHAUSTIVE
    )


def test_event_with_unknown_membership_is_treated_as_incomplete():
    event, markets = load("nyc_high")
    from dataclasses import replace

    blind = replace(event, market_tickers=())
    assert not by_kind(analyze_event(blind, markets), RelationKind.PARTITION)


def test_btc_ladder_is_a_proven_chain_and_not_exclusive():
    event, markets = load("btc_ladder")
    a = analyze_event(event, markets)
    [chain] = by_kind(a, RelationKind.CHAIN)
    assert chain.level is EvidenceLevel.PROVEN and len(chain.relation.markets) == 188
    order = [next(m for m in markets if m.ticker == t).strike.floor for t in chain.relation.markets]
    assert order == sorted(order, reverse=True)  # narrowest (highest strike) first
    assert not by_kind(a, RelationKind.MUTUALLY_EXCLUSIVE) and not by_kind(
        a, RelationKind.PARTITION
    )
    world = {m.ticker: int(m.result.value == "yes") for m in markets}
    assert chain.relation.admissible(world)  # the real settlement respects the nesting


def test_btc_range_event_is_a_partition_on_the_cent_lattice():
    event, markets = load("btc_range")
    a = analyze_event(event, markets)
    [part] = by_kind(a, RelationKind.PARTITION)
    assert part.level is EvidenceLevel.LATTICE and len(part.relation.markets) == 188
    assert any("step 0.01" in s for s in part.assumptions)
    assert sum(m.result.value == "yes" for m in markets) == 1
    world = {m.ticker: int(m.result.value == "yes") for m in markets}
    assert part.relation.admissible(world)


def test_ladder_and_range_events_link_through_exact_unions():
    """KXBTCD 'above X' == the union of KXBTC buckets above X: a cross-event, checkable identity."""
    ladder_ev, ladder = load("btc_ladder")
    range_ev, rng = load("btc_range")
    res = find_unions([(ladder_ev, ladder), (range_ev, rng)])
    unions = [a for a in res.assessments if isinstance(a.relation, Union)]
    assert len(unions) > 100  # nearly every ladder rung aligns with a bucket edge
    for a in unions:
        assert a.level is EvidenceLevel.LATTICE and a.relation.disjoint
    # verify a union against the real settlement: whole == OR(parts) in the realised world
    world = {m.ticker: int(m.result.value == "yes") for m in ladder + rng}
    assert all(a.relation.admissible(world) for a in unions)
    # and one by hand: T68699.99 (>= 68,700) is exactly buckets 68700.. plus the open top tail
    target = next(a for a in unions if a.relation.whole.endswith("T68699.99"))
    assert len(target.relation.parts) > 100 and all("KXBTC-" in p for p in target.relation.parts)


def test_epl_three_way_is_declared_exclusive_and_exhaustiveness_needs_history():
    event, markets = load("epl_3way")
    a = analyze_event(event, markets)
    [me] = by_kind(a, RelationKind.MUTUALLY_EXCLUSIVE)
    assert me.level is EvidenceLevel.DECLARED and "exchange flag" in me.evidence[0]
    [cover] = by_kind(a, RelationKind.EXHAUSTIVE)
    assert cover.level is EvidenceLevel.UNVERIFIED  # nothing proves home/away/tie is exhaustive
    [part] = by_kind(a, RelationKind.PARTITION)
    assert (
        part.level is EvidenceLevel.UNVERIFIED
    )  # a partition is only as strong as its weakest half


def test_epl_becomes_empirical_with_enough_clean_history():
    event, markets = load("epl_3way")
    good = StatsBook()
    for _ in range(40):
        good.add_event("KXEPLGAME", markets)  # 40 settled events, exactly one YES each
    a = analyze_event(event, markets, stats=good)
    [cover] = by_kind(a, RelationKind.EXHAUSTIVE)
    assert cover.level is EvidenceLevel.EMPIRICAL and "0/40" in cover.evidence[0]
    [part] = by_kind(a, RelationKind.PARTITION)
    assert part.level is EvidenceLevel.EMPIRICAL  # a partition is as strong as its weaker half
    few = StatsBook()
    few.add_event("KXEPLGAME", markets)
    assert (
        by_kind(analyze_event(event, markets, stats=few), RelationKind.EXHAUSTIVE)[0].level
        is EvidenceLevel.UNVERIFIED
    )


def test_history_that_contradicts_the_exchange_flag_is_surfaced():
    event, markets = load("epl_3way")
    from dataclasses import replace

    from market.contracts import SettlementResult

    two_yes = [
        replace(m, result=SettlementResult.YES, settlement_value=10000) for m in markets[:2]
    ] + [markets[2]]
    book = StatsBook()
    for _ in range(9):
        book.add_event("KXEPLGAME", markets)
    book.add_event("KXEPLGAME", two_yes)
    a = analyze_event(event, markets, stats=book)
    assert a.conflicts and "had >=2 YES" in a.conflicts[0]
    assert by_kind(a, RelationKind.MUTUALLY_EXCLUSIVE)[0].level is EvidenceLevel.UNVERIFIED


def test_two_player_match_is_declared_exclusive():
    event, markets = load("atp_match")
    a = analyze_event(event, markets)
    assert by_kind(a, RelationKind.MUTUALLY_EXCLUSIVE)[0].level is EvidenceLevel.DECLARED
    assert len(admissible_worlds(by_kind(a, RelationKind.PARTITION)[0].relation)) == 2


def test_spread_event_does_not_chain_different_teams_together():
    """Six 'greater' markets, two variables: chaining across teams would assert false implications."""
    event, markets = load("mlb_spread")
    a = analyze_event(event, markets)
    chains = by_kind(a, RelationKind.CHAIN)
    assert len(chains) == 2
    teams = [{t.rsplit("-", 1)[1][:-1] for t in c.relation.markets} for c in chains]
    assert all(len(s) == 1 for s in teams) and teams[0] != teams[1]  # each chain is one team
    for c in chains:
        assert c.level is EvidenceLevel.PROVEN and len(c.relation.markets) == 3
    assert not by_kind(a, RelationKind.MUTUALLY_EXCLUSIVE)  # flag is False and strikes overlap
    world = {m.ticker: int(m.result.value == "yes") for m in markets}
    assert all(c.relation.admissible(world) for c in chains)


def test_totals_ladder_chain():
    event, markets = load("tennis_total_games")
    [chain] = by_kind(analyze_event(event, markets), RelationKind.CHAIN)
    floors = [
        next(m for m in markets if m.ticker == t).strike.floor for t in chain.relation.markets
    ]
    assert floors == [22.5, 17.5]  # over 22.5 games implies over 17.5


def test_strike_that_contradicts_the_rules_text_is_a_reported_conflict_not_a_relation():
    event, markets = load("nyc_high")
    from dataclasses import replace

    bad = replace(
        markets[1], strike=replace(markets[1].strike, floor=79.0)
    )  # rules still say 80-81
    a = analyze_event(event, [markets[0], bad, *markets[2:]])
    assert a.conflicts and "strike says" in a.conflicts[0]
    assert bad.ticker in a.excluded
    assert numeric_view(bad)[0] is None
    # the five clean buckets stay provably exclusive, but NOTHING trusted may include the bad market
    for x in a.assessments:
        if bad.ticker in x.relation.tickers() and x.relation.kind is not RelationKind.COMPLEMENT:
            assert x.level is EvidenceLevel.UNVERIFIED, x.relation.statement()
    proven = [
        x for x in by_kind(a, RelationKind.MUTUALLY_EXCLUSIVE) if x.level is EvidenceLevel.PROVEN
    ]
    assert len(proven) == 1 and len(proven[0].relation.markets) == 5


def test_flagged_exclusive_event_whose_buckets_overlap_is_a_conflict_not_a_relation():
    event, markets = load("nyc_high")
    from dataclasses import replace

    m = markets[2]  # [82, 83] widened to [82, 84.5]: now overlaps the [84, 85] bucket
    wide = replace(
        m,
        strike=replace(m.strike, cap=84.5),
        rules=replace(m.rules, primary=m.rules.primary.replace("82-83", "82-84.5")),
    )
    a = analyze_event(event, [*markets[:2], wide, *markets[3:]])
    assert numeric_view(wide)[0] is not None  # strike and rules agree, so it is not excluded
    assert any("flagged mutually exclusive but" in c and "overlaps" in c for c in a.conflicts)
    assert not any(
        x.level is EvidenceLevel.PROVEN for x in by_kind(a, RelationKind.MUTUALLY_EXCLUSIVE)
    )
    assert all(
        x.level is EvidenceLevel.UNVERIFIED for x in by_kind(a, RelationKind.MUTUALLY_EXCLUSIVE)
    )
    assert all(x.level is EvidenceLevel.UNVERIFIED for x in by_kind(a, RelationKind.PARTITION))


def test_exact_union_requires_an_unbroken_tiling_from_edge_to_edge():
    _, buckets = load("btc_range")
    _, ladder = load("btc_ladder")
    tiles = [numeric_view(m)[0] for m in buckets]
    rung = next(n for n in (numeric_view(m)[0] for m in ladder) if n.interval.lo == D("68699.99"))

    got = exact_union(rung, tiles)
    assert got is not None
    parts, step = got
    assert step == D("0.01") and parts[0].interval.lo == D("68700")
    assert parts[-1].interval.hi is None  # ends in the open top tail

    hole = next(t for t in tiles if t.interval.lo == D("70000"))
    assert exact_union(rung, [t for t in tiles if t is not hole]) is None  # a missing bucket
    top = max((t for t in tiles if t.interval.hi is None), key=lambda t: t.interval.lo)
    assert exact_union(rung, [t for t in tiles if t is not top]) is None  # missing the open tail
    assert exact_union(rung, tiles[:3]) is None  # too few tiles to reach the target


def test_every_inferred_relation_survives_all_realised_settlements():
    """Falsification test: no PROVEN/LATTICE relation may be contradicted by the real outcome."""
    for name in (
        "nyc_high",
        "btc_range",
        "btc_ladder",
        "epl_3way",
        "atp_match",
        "mlb_spread",
        "tennis_total_games",
    ):
        event, markets = load(name)
        world = {m.ticker: int(m.result.value == "yes") for m in markets}
        for a in analyze_event(event, markets).assessments:
            if a.level >= EvidenceLevel.DECLARED and a.relation.kind is not RelationKind.COMPLEMENT:
                assert a.relation.admissible(world), (name, a.relation.statement())
