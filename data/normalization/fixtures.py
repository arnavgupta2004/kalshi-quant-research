"""Load complete real events captured from the live API (used by tests and demos)."""

from __future__ import annotations

import json
from pathlib import Path

from data.normalization.normalizer import normalize_event, normalize_market
from kalshi_client.models import ApiEvent, validate_rest
from market.contracts import Event, Market

EVENT_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "events"


def load_event_fixture(name: str) -> tuple[Event, list[Market]]:
    d = json.loads((EVENT_FIXTURES / f"{name}.json").read_text())
    raw = dict(d["event"])
    raw["markets"] = d["markets"]
    api = validate_rest(ApiEvent, raw)
    event = normalize_event(api)
    return event, [normalize_market(m, event=event) for m in api.markets]
