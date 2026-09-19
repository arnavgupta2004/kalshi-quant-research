"""YAML configuration for collector jobs.  Unknown keys are errors: a typo must not silently
fall back to a default and collect the wrong dataset."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


def _dt(v: Any, name: str) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, date):
        return datetime.combine(v, time(), tzinfo=UTC)
    if isinstance(v, str):
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    raise ConfigError(f"{name}: cannot interpret {v!r} as a date/time")


def _strict(cls, data: dict[str, Any], where: str):
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)} (valid: {sorted(names)})")
    return cls(**data)


@dataclass
class UniverseSpec:
    """Which markets make up the dataset.  Selection never looks at outcomes (``result``)."""

    status: str = "settled"
    close_after: datetime | None = None
    close_before: datetime | None = None
    min_volume: float = 100.0  # contracts, lifetime
    sample_fraction: float = 1.0
    seed: int = 42
    max_per_series: int | None = None
    max_markets: int | None = None
    series: list[str] = field(default_factory=list)  # allow-list of series prefixes
    exclude_series: list[str] = field(default_factory=list)
    created_lookback_days: int = 7  # historical partition is scanned by created_time (desc)
    historical_max_scan: int = 500_000  # hard guard on the scan size

    def __post_init__(self) -> None:
        if not 0 < self.sample_fraction <= 1:
            raise ConfigError("sample_fraction must be in (0, 1]")
        if self.close_after and self.close_before and self.close_after >= self.close_before:
            raise ConfigError("close_after must be before close_before")
        if self.status == "settled" and (self.close_after is None or self.close_before is None):
            raise ConfigError("a settled-market universe needs close_after and close_before")


@dataclass
class HistoryConfig:
    universe: UniverseSpec  # required: there is no sensible default dataset
    job: str = "history"
    db_path: str = "var/kalshi.duckdb"
    requests_per_second: float = 10.0
    concurrency: int = 6
    trade_page_size: int = 1000
    overlap_s: int = 120  # re-fetch margin behind the checkpoint (dedup makes overlap free)
    fetch_events: bool = True
    # long jobs must ride out network blips: 10 retries with backoff capped at 30 s ~ 2.5 minutes
    max_retries: int = 10
    backoff_max_s: float = 30.0
    checkpoint_pages: int = 25  # persist scan progress every N pages (~25k markets)
    scan_reuse_hours: float = 24.0  # reuse a completed scan of the same spec for this long


@dataclass
class BookSelection:
    top_events: int = 15  # events ranked by summed 24h volume
    max_tickers: int = 120
    min_event_markets: int = 1  # e.g. 2 to keep only multi-outcome events
    include_siblings: bool = True  # all markets of a chosen event (needed for exhaustive arb)
    exclude_series: list[str] = field(default_factory=list)
    include_series: list[str] = field(default_factory=list)  # if set, ONLY these series


@dataclass
class BooksConfig:
    job: str = "books"
    db_path: str = "var/kalshi.duckdb"
    mode: str = "rest"  # rest | ws
    interval_s: float = 5.0
    duration_s: float | None = None  # None = run until interrupted
    heartbeat_s: float = 60.0  # re-store an unchanged book at least this often
    metadata_refresh_s: float = 600.0
    requests_per_second: float = 10.0
    batch_size: int = 50
    max_consecutive_failures: int = 20
    tickers: list[str] = field(default_factory=list)  # explicit list overrides selection
    selection: BookSelection = field(default_factory=BookSelection)

    def __post_init__(self) -> None:
        if self.mode not in ("rest", "ws"):
            raise ConfigError("mode must be 'rest' or 'ws'")


@dataclass
class EventsConfig:
    """Complete settled events (all sibling markets) for relationship research."""

    job: str = "events"
    db_path: str = "var/relations.duckdb"
    series_from_db: str | None = "var/kalshi.duckdb"  # take the series list from another dataset
    series: list[str] = field(default_factory=list)  # ...or list them explicitly
    per_series: int = 12  # most recent settled events per series
    requests_per_second: float = 10.0
    concurrency: int = 4
    max_retries: int = 10
    backoff_max_s: float = 30.0


def load_config(path: str | Path) -> HistoryConfig | BooksConfig | EventsConfig:
    data = yaml.safe_load(Path(path).read_text()) or {}
    job = data.get("job")
    if job == "history":
        u = data.pop("universe", {}) or {}
        for k in ("close_after", "close_before"):
            u[k] = _dt(u.get(k), k)
        return _strict(
            HistoryConfig, {**data, "universe": _strict(UniverseSpec, u, "universe")}, "history"
        )
    if job == "books":
        s = data.pop("selection", {}) or {}
        return _strict(
            BooksConfig, {**data, "selection": _strict(BookSelection, s, "selection")}, "books"
        )
    if job == "events":
        return _strict(EventsConfig, data, "events")
    raise ConfigError(f"config needs `job: history|books|events`, got {job!r}")


def to_plain(obj: Any) -> Any:
    """Config -> JSON-able dict (recorded with each run for reproducibility)."""
    if dataclasses.is_dataclass(obj):
        return {f.name: to_plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, list):
        return [to_plain(x) for x in obj]
    return obj
