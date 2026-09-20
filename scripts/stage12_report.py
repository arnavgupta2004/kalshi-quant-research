"""Render docs/research_report.md from docs/research_report.tmpl.md and the result files.

python -m scripts.stage12_report            # write the report
python -m scripts.stage12_report --check    # exit 1 if the committed report is stale
"""

from __future__ import annotations

import argparse
import sys

from research import final_report as F

TEMPLATE = F.ROOT / "docs" / "research_report.tmpl.md"
OUT = F.ROOT / "docs" / "research_report.md"


def build() -> str:
    text = F.render(TEMPLATE.read_text(), F.load_results())
    if "PLACEHOLDER" in text:
        raise SystemExit("the template still contains a PLACEHOLDER section")
    return text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    text = build()
    if a.check:
        if not OUT.exists() or OUT.read_text() != text:
            print("docs/research_report.md is stale: run python -m scripts.stage12_report")
            sys.exit(1)
        print("report is up to date")
        return
    OUT.write_text(text)
    n_pending = text.count(F.PENDING)
    print(f"wrote {OUT} ({len(text.splitlines())} lines, {n_pending} pending value(s))")


if __name__ == "__main__":
    main()
