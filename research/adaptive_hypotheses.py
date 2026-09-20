"""Score the pre-specified Stage 10 hypotheses (docs/adaptive_market_maker.md s.8) against one block
of a results JSON (``blocks["confirmatory"]``, or ``blocks["validation"]`` in a development run).

The criteria are transcribed from the pre-registration, written after the development run and
before the confirmatory recording was opened.  Each row prints the observed value beside the
criterion.  A claim resting on fewer than ``MIN_CLUSTERS`` independent events is "inconclusive".

    python -m research.adaptive_hypotheses results/stage10/confirm/adaptive.json
"""

from __future__ import annotations

import json
import sys

MIN_CLUSTERS = 20
FULL = "6 + time-to-resolution skew (= FULL)"
BASE = "0 baseline (Stage 9)"


def _f(x, d: int = 2) -> str:
    if not x or x.get("value") is None:
        return "n/a"
    lo, hi = x.get("lo"), x.get("hi")
    ci = "" if lo is None or hi is None else f" [{lo:.{d}f}, {hi:.{d}f}]"
    return f"{x['value']:.{d}f}{ci} ({x.get('n_clusters', '?')} events)"


def evaluate(block: dict) -> list[tuple[str, str, str, str]]:
    """(id, verdict, observed, criterion)."""
    out: list[tuple[str, str, str, str]] = []
    rows, con, sig = block["ladder"]["rows"], block["ladder"]["contrasts"], block["signal"]

    def add(hid, ok, observed, criterion, clusters=None):
        if clusters is not None and clusters < MIN_CLUSTERS:
            verdict = f"inconclusive (< {MIN_CLUSTERS} events)"
        else:
            verdict = "consistent" if ok else "NOT consistent"
        out.append((hid, verdict, observed, criterion))

    st = sig["fair_value_staleness"]
    add(
        "S1",
        st.get("lo") is not None and st["lo"] > 0,
        f"skill {100 * st.get('skill_vs_zero', float('nan')):.2f}% "
        f"[{100 * (st.get('lo') or float('nan')):.2f}, {100 * (st.get('hi') or float('nan')):.2f}]",
        "the frozen staleness fair-value model beats 'the mid does not move' at 5 s: interval > 0",
        st.get("events", 0),
    )
    fu = sig["fair_value_all_direction_features"]
    add(
        "S2",
        fu.get("skill_vs_zero", 0) <= st.get("skill_vs_zero", 0),
        f"all-features {100 * fu.get('skill_vs_zero', float('nan')):.2f}% vs staleness "
        f"{100 * st.get('skill_vs_zero', float('nan')):.2f}%",
        "adding order-book imbalance, microprice, flow and momentum does not beat staleness alone "
        "(point comparison)",
    )
    sc = sig["scale_of_next_move"]
    add(
        "S3",
        sc.get("lo") is not None and sc["lo"] > 0,
        f"skill {100 * sc.get('skill_vs_mean', float('nan')):.1f}% "
        f"[{100 * (sc.get('lo') or float('nan')):.1f}, {100 * (sc.get('hi') or float('nan')):.1f}]",
        "realised movement predicts the size of the next move (10 s): interval above 0",
        sc.get("events", 0),
    )
    full, base = rows[FULL], rows[BASE]
    vb = full["vs_baseline"]
    n_ev = full["settled_pnl_cents_per_contract"].get("n_clusters", 0)
    add(
        "T1",
        full["net_usd"] < 0 and full["settled_pnl_total_usd"] < 0,
        f"net ${full['net_usd']:.1f}; settled P&L ${full['settled_pnl_total_usd']:.1f}",
        "the adaptive maker still loses money (net and settled P&L both < 0)",
    )
    d = vb["settled_cents_per_contract"]
    add(
        "T2",
        d.get("lo") is None or d["lo"] <= 0,
        f"{_f(d)} cents/contract (FULL - baseline)",
        "no significant improvement in settled P&L per contract: lower bound <= 0",
        d.get("n_clusters", n_ev),
    )
    add(
        "T3",
        full["contracts"] < base["contracts"]
        and vb["event_pnl_total_usd"]["value"] > 0
        and d["value"] <= 0,
        f"contracts {full['contracts']:.0f} vs {base['contracts']:.0f}; event P&L "
        f"{_f(vb['event_pnl_total_usd'])} $; per contract {d['value']:.2f}c",
        "any loss reduction comes from trading less: fewer contracts, better total P&L per event, "
        "per-contract change <= 0",
    )
    c = con.get("2 vs 1", {}).get("settled_cents_per_contract", {})
    add(
        "T4",
        c.get("lo") is not None and c["lo"] <= 0 <= c["hi"],
        f"{_f(c)} cents/contract",
        "the staleness correction moves settled P&L per contract by nothing detectable (CI has 0)",
        c.get("n_clusters", 0),
    )
    c = con.get("3 vs 2", {}).get("settled_cents_per_contract", {})
    add(
        "T5",
        c.get("value") is not None and c["value"] <= 0,
        f"{_f(c)} cents/contract",
        "order book, flow and momentum in the fair value do not improve on staleness alone (point)",
    )
    c = con.get("6 vs 5", {}).get("settled_cents_per_contract", {})
    add(
        "T6",
        c.get("lo") is not None and c["lo"] <= 0 <= c["hi"],
        f"{_f(c)} cents/contract",
        "the time-to-resolution skew moves settled P&L per contract by nothing detectable",
        c.get("n_clusters", 0),
    )
    m = full["markout_30s_cents_per_contract"]
    add(
        "T7",
        m.get("hi") is not None and m["hi"] < 0,
        f"{_f(m)} cents/contract",
        "the adaptive maker's fills are still adversely selected: 30 s markout interval below 0",
        m.get("n_clusters", 0),
    )
    cells = block["fill_sensitivity"]
    neg = sum(1 for x in cells if x["full_net_usd"] < 0)
    add(
        "T8",
        neg == len(cells),
        f"net < 0 in {neg}/{len(cells)} fill-model x latency cells",
        "the adaptive maker loses money under every fill model and latency",
    )
    return out


if __name__ == "__main__":
    res = json.load(open(sys.argv[1]))
    key = "confirmatory" if "confirmatory" in res["blocks"] else "validation"
    for hid, verdict, observed, crit in evaluate(res["blocks"][key]):
        print(f"{hid:4s} {verdict:34s} {observed}\n     criterion: {crit}")
