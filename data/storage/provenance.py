"""Code-version provenance recorded with every collection run and experiment."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def git_commit() -> str:
    """``<sha>`` or ``<sha>+dirty`` if the working tree has uncommitted changes."""
    sha = _git("rev-parse", "HEAD")
    if sha is None:
        return "no-commits" if _git("rev-parse", "--git-dir") is not None else "not-a-repo"
    return f"{sha}+dirty" if _git("status", "--porcelain") else sha
