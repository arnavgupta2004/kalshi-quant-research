"""Single source of truth for the local dataset schema.

Every table is declared once as a ``Table``; the same declaration generates the DDL *and*
the typed column map used for bulk ingestion, so the two can never drift apart.

Conventions
  * Times are ``TIMESTAMP`` **in UTC** (DuckDB's ``TIMESTAMPTZ`` needs ``pytz`` to read back
    into Python).  ``Store`` converts to/from tz-aware UTC datetimes at the boundary.
  * Prices are INTEGER ticks (1/10,000 $); quantities are BIGINT centi-contracts.
  * Two clocks are never mixed: ``*_ts`` / ``created_time`` are *exchange* time, while
    ``recv_ts_ns`` / ``ingested_at`` / ``*_seen_at`` are *local* time.
  * ``raw`` JSON columns hold the *validated* payload (prices as ticks, times ISO-8601 UTC,
    unknown exchange fields preserved) so nothing is lost to normalisation.
"""

from __future__ import annotations

from dataclasses import dataclass

SCHEMA_VERSION = 2  # v2 (additive): series_meta


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[tuple[str, str], ...]
    pk: tuple[str, ...]
    doc: str = ""

    @property
    def col_names(self) -> list[str]:
        return [c for c, _ in self.columns]

    def ddl(self) -> str:
        cols = ",\n  ".join(f"{c} {t}" for c, t in self.columns)
        return f"CREATE TABLE IF NOT EXISTS {self.name} (\n  {cols},\n  PRIMARY KEY ({', '.join(self.pk)})\n)"


T = Table

TABLES: dict[str, Table] = {
    t.name: t
    for t in (
        T(
            "collection_runs",
            (
                ("run_id", "VARCHAR"),
                ("job", "VARCHAR"),
                ("started_at", "TIMESTAMP"),
                ("ended_at", "TIMESTAMP"),
                ("status", "VARCHAR"),  # running | ok | interrupted | failed | abandoned
                ("git_commit", "VARCHAR"),
                ("config", "JSON"),
                ("note", "VARCHAR"),
            ),
            ("run_id",),
            "One row per collector process: provenance + the coverage interval it was alive for.",
        ),
        T(
            "events",
            (
                ("event_ticker", "VARCHAR"),
                ("series_ticker", "VARCHAR"),
                ("title", "VARCHAR"),
                ("sub_title", "VARCHAR"),
                ("category", "VARCHAR"),
                ("mutually_exclusive", "BOOLEAN"),
                ("strike_period", "VARCHAR"),
                ("collateral_return_type", "VARCHAR"),
                ("exchange_index", "INTEGER"),
                ("market_tickers", "VARCHAR[]"),
                ("settlement_sources", "JSON"),
                ("raw", "JSON"),
                ("first_seen_at", "TIMESTAMP"),
                ("last_seen_at", "TIMESTAMP"),
                ("run_id", "VARCHAR"),
            ),
            ("event_ticker",),
            "Real-world question grouping markets. `raw.missing=true` marks an event the API 404'd.",
        ),
        T(
            "markets",
            (
                ("ticker", "VARCHAR"),
                ("event_ticker", "VARCHAR"),
                ("title", "VARCHAR"),
                ("yes_sub_title", "VARCHAR"),
                ("no_sub_title", "VARCHAR"),
                ("market_type", "VARCHAR"),
                ("status", "VARCHAR"),
                ("result", "VARCHAR"),  # yes | no | scalar | '' (unsettled)
                ("settlement_value", "INTEGER"),  # YES payoff in ticks; NULL until settled
                ("strike_type", "VARCHAR"),
                ("floor_strike", "DOUBLE"),
                ("cap_strike", "DOUBLE"),
                ("functional_strike", "VARCHAR"),
                ("custom_strike", "JSON"),
                ("rules_primary", "VARCHAR"),
                ("rules_secondary", "VARCHAR"),
                ("early_close_condition", "VARCHAR"),
                ("can_close_early", "BOOLEAN"),
                ("open_time", "TIMESTAMP"),
                ("close_time", "TIMESTAMP"),
                ("expected_expiration_time", "TIMESTAMP"),
                ("expiration_time", "TIMESTAMP"),
                ("settlement_ts", "TIMESTAMP"),
                ("price_level_structure", "VARCHAR"),
                ("price_ranges", "JSON"),
                ("exchange_index", "INTEGER"),
                ("is_multivariate", "BOOLEAN"),
                ("volume", "BIGINT"),  # lifetime contracts traded, centi-contracts
                ("open_interest", "BIGINT"),
                (
                    "partition",
                    "VARCHAR",
                ),  # listing endpoint it was fetched from (live | historical); not a property of the market
                ("raw", "JSON"),
                ("first_seen_at", "TIMESTAMP"),
                ("last_seen_at", "TIMESTAMP"),
                ("run_id", "VARCHAR"),
            ),
            ("ticker",),
            "Latest known metadata per market (upserted). Point-in-time state is in market_status_log.",
        ),
        T(
            "market_status_log",
            (
                ("ticker", "VARCHAR"),
                ("observed_at", "TIMESTAMP"),  # LOCAL time we observed it - never exchange time
                ("source", "VARCHAR"),  # rest | ws_lifecycle
                ("status", "VARCHAR"),
                ("result", "VARCHAR"),
                ("event_type", "VARCHAR"),  # lifecycle event (created, activated, settled, ...)
                ("settlement_value", "INTEGER"),
                ("run_id", "VARCHAR"),
            ),
            ("ticker", "observed_at", "source", "status", "event_type"),
            "Append-only: a row is written only when the observed state differs from the last one.",
        ),
        T(
            "trades",
            (
                ("trade_id", "VARCHAR"),
                ("ticker", "VARCHAR"),
                ("created_time", "TIMESTAMP"),  # exchange time
                ("yes_price", "INTEGER"),
                ("no_price", "INTEGER"),
                ("count", "BIGINT"),
                ("taker_side", "VARCHAR"),
                ("taker_book_side", "VARCHAR"),
                ("is_block_trade", "BOOLEAN"),
                ("source", "VARCHAR"),  # rest_live | rest_historical | ws
                ("ingested_at", "TIMESTAMP"),  # local time
                ("run_id", "VARCHAR"),
            ),
            ("trade_id",),
            "Public trades, deduplicated on the exchange's trade_id across all sources.",
        ),
        T(
            "trade_sync",
            (
                ("ticker", "VARCHAR"),
                ("partition", "VARCHAR"),
                ("complete_through", "TIMESTAMP"),
                ("n_trades", "BIGINT"),
                ("completed_at", "TIMESTAMP"),
                ("run_id", "VARCHAR"),
            ),
            ("ticker", "partition"),
            "Resume checkpoint: advanced only after a partition was paginated to completion.",
        ),
        T(
            "series_meta",
            (
                ("series_ticker", "VARCHAR"),
                ("title", "VARCHAR"),
                ("category", "VARCHAR"),
                ("frequency", "VARCHAR"),
                ("fee_type", "VARCHAR"),
                ("fee_multiplier", "VARCHAR"),  # decimal string: exact, never a float
                ("raw", "JSON"),
                ("fetched_at", "TIMESTAMP"),
            ),
            ("series_ticker",),
            "Per-series fee schedule (fee_type, fee_multiplier) - what a trade in that series costs.",
        ),
        T(
            "scan_state",
            (
                ("scan_key", "VARCHAR"),  # hash of the universe spec
                ("partition", "VARCHAR"),
                ("page_cursor", "VARCHAR"),  # next page to fetch; NULL when done
                ("n_scanned", "BIGINT"),
                ("passed", "BIGINT"),
                ("first_ticker", "VARCHAR"),  # first market of page 1: detects an ignored cursor
                (
                    "items",
                    "JSON",
                ),  # selected [key, ticker] pairs so far (winners re-fetched by ticker)
                ("done", "BOOLEAN"),
                ("updated_at", "TIMESTAMP"),
                ("run_id", "VARCHAR"),
            ),
            ("scan_key", "partition"),
            "Universe-scan progress: lets an interrupted scan resume, and a finished one be reused.",
        ),
        T(
            "book_snapshots",
            (
                ("ticker", "VARCHAR"),
                ("recv_ts_ns", "BIGINT"),  # local receive time
                ("source", "VARCHAR"),  # rest_poll | ws
                (
                    "req_ts_ns",
                    "BIGINT",
                ),  # rest_poll: request sent; state is somewhere in [req, recv]
                ("epoch", "INTEGER"),
                ("sid", "BIGINT"),
                ("seq", "BIGINT"),
                ("exchange_ts", "TIMESTAMP"),
                ("yes_px", "INTEGER[]"),  # YES bids, best first
                ("yes_qty", "BIGINT[]"),
                ("no_px", "INTEGER[]"),  # NO bids, best first
                ("no_qty", "BIGINT[]"),
                ("book_hash", "VARCHAR"),
                ("run_id", "VARCHAR"),
            ),
            ("ticker", "recv_ts_ns", "source"),
            "Full-depth book keyframes. REST snapshots are stored on change (or heartbeat).",
        ),
        T(
            "book_deltas",
            (
                ("run_id", "VARCHAR"),
                ("epoch", "INTEGER"),
                ("sid", "BIGINT"),
                ("seq", "BIGINT"),
                ("ticker", "VARCHAR"),
                ("recv_ts_ns", "BIGINT"),
                ("exchange_ts", "TIMESTAMP"),
                ("side", "VARCHAR"),
                ("price", "INTEGER"),
                ("delta", "BIGINT"),
            ),
            ("run_id", "epoch", "sid", "seq", "ticker"),
            "WebSocket orderbook_delta events, exactly as received (replay with BookStreamProcessor).",
        ),
        T(
            "stream_events",
            (
                ("run_id", "VARCHAR"),
                ("recv_ts_ns", "BIGINT"),
                ("epoch", "INTEGER"),
                ("kind", "VARCHAR"),  # connected | disconnected | gap | resync | inconsistent ...
                ("detail", "VARCHAR"),
            ),
            ("run_id", "recv_ts_ns", "kind", "epoch"),
            "Connection/sequence incidents, so replays can skip invalid segments.",
        ),
        T(
            "poll_log",
            (
                ("run_id", "VARCHAR"),
                ("recv_ts_ns", "BIGINT"),
                ("n_tickers", "INTEGER"),
                ("n_changed", "INTEGER"),
                ("n_missing", "INTEGER"),
                ("latency_ms", "DOUBLE"),
            ),
            ("run_id", "recv_ts_ns"),
            "One row per REST poll cycle: proves the poller was alive when a book did not change.",
        ),
        T(
            "quarantine",
            (
                ("run_id", "VARCHAR"),
                ("recv_ts_ns", "BIGINT"),
                ("seq_no", "INTEGER"),
                ("source", "VARCHAR"),
                ("error", "VARCHAR"),
                ("raw", "VARCHAR"),
            ),
            ("run_id", "recv_ts_ns", "seq_no"),
            "Payloads that failed validation. Never silently dropped, never mixed into real tables.",
        ),
    )
}

VIEWS = {
    "markets_enriched": """
        CREATE OR REPLACE VIEW markets_enriched AS
        SELECT m.*, e.category, e.series_ticker, e.mutually_exclusive, e.title AS event_title
        FROM markets m LEFT JOIN events e USING (event_ticker)
    """,
}

META_DDL = "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER, applied_at TIMESTAMP)"
