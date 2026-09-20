"""The final report is rendered from the result files: every placeholder must resolve, nothing may be
hand-typed, and a missing result must render as (pending) rather than crash."""

import pytest

from research import final_report as F

TEMPLATE = (F.ROOT / "docs" / "research_report.tmpl.md").read_text()


def test_every_placeholder_in_the_template_is_defined():
    for kind, key in F.placeholders(TEMPLATE):
        if kind == "n":
            assert key in F.CLAIMS, f"unknown claim {kind}.{key}"
        elif kind == "t":
            assert key in F.TABLES, f"unknown table {key}"
        elif kind in ("h6", "h8", "h9", "h10v", "h10c", "v6", "v8", "v9", "v10v", "v10c"):
            assert key  # a hypothesis id: checked against the results below
        elif kind == "score":
            assert key in ("6", "8", "9", "10v", "10c")
        else:
            pytest.fail(f"unknown placeholder kind {kind!r}")


def test_the_template_resolves_against_the_real_results():
    R = F.load_results()
    text = F.render(TEMPLATE, R)
    assert "{{" not in text and "}}" not in text
    for kind, key in F.placeholders(TEMPLATE):
        if kind in ("h6", "h8", "h9", "v6", "v8", "v9"):
            st = kind[1:]
            assert F._hyp(R, st, key) is not None, f"hypothesis {st}:{key} not in the results"


def test_missing_results_render_as_pending_not_as_errors():
    empty = dict.fromkeys(F.FILES)
    text = F.render(TEMPLATE, empty)
    assert F.PENDING in text and "{{" not in text


def test_an_unknown_claim_or_table_is_an_error_not_silent():
    with pytest.raises(KeyError):
        F.render("{{n.no_such_claim}}", F.load_results())
    with pytest.raises(KeyError):
        F.render("{{t.no_such_table}}", F.load_results())
    with pytest.raises(KeyError):
        F.render("{{zz.A1}}", F.load_results())


def test_formatters():
    assert F.ci({"value": 12.886, "lo": 5.9, "hi": 22.5}, 1) == "12.9 [5.9, 22.5]"
    assert F.ci({"value": 0.5, "lo": None, "hi": None}, 2) == "0.50"
    assert F.ci(None) == "n/a"
    assert F.usd(-317.4) == "-$317" and F.usd(5.5, 2) == "$5.50" and F.usd(None) == "n/a"


def test_claims_agree_with_the_hypothesis_scoreboards():
    R = F.load_results()
    for stage in ("6", "8", "9"):
        rows = F.hypotheses(R, stage)
        assert rows, f"no hypotheses for stage {stage}"
        want = f"{sum(r[1] == 'consistent' for r in rows)} of {len(rows)}"
        assert F.resolve("score", stage, R).startswith(want)


def test_the_committed_report_is_up_to_date():
    """Regenerate with `python -m scripts.stage12_report` after any result changes."""
    from scripts.stage12_report import OUT, build

    if "PLACEHOLDER" in TEMPLATE:
        pytest.skip("report template still being written")
    assert OUT.exists() and OUT.read_text() == build()
