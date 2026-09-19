import pytest

from scripts import stage6_arbitrage_research as S


def test_fingerprint_is_stable_and_ignores_presentation_only_code():
    assert S.code_fingerprint() == S.code_fingerprint()
    assert len(S.code_fingerprint()) == 16
    assert "report.py" in S.NOT_ANALYSIS and "scanner.py" not in S.NOT_ANALYSIS


def test_confirmatory_run_refuses_when_the_code_changed_since_it_was_frozen(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        [
            "stage6",
            "--db",
            "does-not-matter.duckdb",
            "--out",
            "/tmp/x",
            "--role",
            "confirmatory",
            "--expect-fingerprint",
            "0000000000000000",
        ],
    )
    with pytest.raises(SystemExit) as e:
        S.main()
    assert "pre-specified" in str(e.value)  # refused before touching any data
