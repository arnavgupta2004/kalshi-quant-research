"""Stage 12 tooling: the reproducibility manifest and the final out-of-sample runner's freeze."""

import json

from scripts import stage12_final_oos as OOS
from scripts import stage12_manifest as M


def test_the_manifest_lists_every_result_and_flags_frozen_code_correctly():
    rows = {r["result"]: r for r in M.experiments()}
    assert "results/stage9/confirm/market_making.json" in rows
    s9 = rows["results/stage9/confirm/market_making.json"]
    assert s9["frozen_code_unchanged"] is True  # Stage 9's frozen code still hashes to its result
    s10 = rows["results/stage10/dev/adaptive.json"]
    assert (
        s10["frozen_code_unchanged"] is True
        and s10["code_fingerprint"] == s10["frozen_fingerprint_now"]
    )
    s6 = rows["results/stage6/confirm/arbitrage_research.json"]
    assert s6["frozen_code_unchanged"] is None and s6["reproduced_by_rerun"] is True


def test_a_result_written_by_different_code_is_flagged(tmp_path, monkeypatch):
    fake = tmp_path / "results" / "stage9" / "confirm"
    fake.mkdir(parents=True)
    d = json.loads((M.ROOT / "results/stage9/confirm/market_making.json").read_text())
    d["code_fingerprint"] = "0000000000000000"
    (fake / "market_making.json").write_text(json.dumps(d))
    monkeypatch.setattr(M, "ROOT", tmp_path)
    monkeypatch.setattr(M, "FILES", {"s9": "results/stage9/confirm/market_making.json"})
    (row,) = M.experiments()
    assert row["frozen_code_unchanged"] is False


def test_the_final_runner_pins_exactly_the_stage_6_analysis_files():
    assert len(OOS.STAGE6_FILES) == 33  # 32 modules Stage 6 hashed + its driver
    assert all((M.ROOT / f).exists() for f in OOS.STAGE6_FILES)
    assert not any(f.endswith("report.py") for f in OOS.STAGE6_FILES)  # presentation only


def test_the_final_runner_fingerprint_is_deterministic_and_covers_the_stage_10_code():
    assert OOS.code_fingerprint() == OOS.code_fingerprint()
    assert all((M.ROOT / f).exists() for f in OOS.S10.CODE)
