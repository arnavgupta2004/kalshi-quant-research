"""Endpoint and credential configuration.

Defaults are the production public-data endpoints.  Every URL can be overridden with an
environment variable so the client can be pointed at the demo environment or at a local
mock server in tests.

    KALSHI_ENV       "prod" (default) | "demo"
    KALSHI_REST_URL  override REST base URL
    KALSHI_WS_URL    override WebSocket URL
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse

from dotenv import load_dotenv

from kalshi_client.auth import KalshiAuth
from kalshi_client.exceptions import ConfigurationError

# NOTE: the docs also list a newer WS host (wss://external-api-ws.kalshi.com/...).  It reset the
# TCP connection from the development network, so the legacy host - still documented as
# supported and verified to answer the WS handshake (401 without credentials) - is the default.
ENDPOINTS = {
    "prod": {
        "rest": "https://api.elections.kalshi.com/trade-api/v2",
        "ws": "wss://api.elections.kalshi.com/trade-api/ws/v2",
    },
    "demo": {
        "rest": "https://demo-api.kalshi.co/trade-api/v2",
        "ws": "wss://demo-api.kalshi.co/trade-api/ws/v2",
    },
}


@dataclass(frozen=True)
class KalshiConfig:
    rest_url: str = ENDPOINTS["prod"]["rest"]
    ws_url: str = ENDPOINTS["prod"]["ws"]
    auth: KalshiAuth | None = field(default=None, repr=False)

    @property
    def rest_path_prefix(self) -> str:
        return urlparse(self.rest_url).path.rstrip("/")

    @property
    def ws_path(self) -> str:
        return urlparse(self.ws_url).path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *, load_dotenv_file: bool = True):
        if env is None:
            if load_dotenv_file:
                load_dotenv()  # gitignored .env; never overrides real environment variables
            env = os.environ
        name = env.get("KALSHI_ENV", "prod").lower()
        if name not in ENDPOINTS:
            raise ConfigurationError(f"KALSHI_ENV must be one of {sorted(ENDPOINTS)}, got {name!r}")
        return cls(
            rest_url=env.get("KALSHI_REST_URL", ENDPOINTS[name]["rest"]).rstrip("/"),
            ws_url=env.get("KALSHI_WS_URL", ENDPOINTS[name]["ws"]),
            auth=KalshiAuth.from_env(env),
        )
