"""RSA-PSS request signing for the Kalshi API.

Signed string: ``<timestamp_ms><HTTP METHOD><path>`` where ``path`` is the URL path
*including* the ``/trade-api/...`` prefix and *excluding* any query string.
Algorithm: RSA-PSS, SHA-256, MGF1(SHA-256), salt length = digest length; base64 encoded.

Credentials come from the environment (optionally populated from a gitignored ``.env``):

    KALSHI_API_KEY_ID        API key id
    KALSHI_PRIVATE_KEY_PATH  path to the PEM private key file      (or)
    KALSHI_PRIVATE_KEY_PEM   the PEM text itself

Nothing in this package ever logs or persists key material.
"""

from __future__ import annotations

import base64
import os
import time
from collections.abc import Mapping
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from kalshi_client.exceptions import ConfigurationError

HEADER_KEY = "KALSHI-ACCESS-KEY"
HEADER_SIGNATURE = "KALSHI-ACCESS-SIGNATURE"
HEADER_TIMESTAMP = "KALSHI-ACCESS-TIMESTAMP"


class KalshiAuth:
    def __init__(self, key_id: str, private_key: RSAPrivateKey) -> None:
        if not key_id:
            raise ConfigurationError("empty API key id")
        self.key_id = key_id
        self._key = private_key

    def __repr__(self) -> str:  # never leak key material through repr/logging
        return f"KalshiAuth(key_id={self.key_id[:4]}…)"

    @classmethod
    def from_pem(cls, key_id: str, pem: str | bytes) -> KalshiAuth:
        data = pem.encode() if isinstance(pem, str) else pem
        try:
            key = serialization.load_pem_private_key(data, password=None)
        except (ValueError, TypeError) as exc:
            raise ConfigurationError(f"could not load PEM private key: {exc}") from exc
        if not isinstance(key, RSAPrivateKey):
            raise ConfigurationError("Kalshi requires an RSA private key")
        return cls(key_id, key)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> KalshiAuth | None:
        """Build from environment variables; returns ``None`` if no key id is configured."""
        env = os.environ if env is None else env
        key_id = env.get("KALSHI_API_KEY_ID", "").strip()
        if not key_id:
            return None
        pem = env.get("KALSHI_PRIVATE_KEY_PEM", "")
        path = env.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
        if pem:
            return cls.from_pem(key_id, pem.replace("\\n", "\n"))
        if path:
            p = Path(path).expanduser()
            if not p.is_file():
                raise ConfigurationError(f"KALSHI_PRIVATE_KEY_PATH does not exist: {p}")
            return cls.from_pem(key_id, p.read_bytes())
        raise ConfigurationError(
            "KALSHI_API_KEY_ID is set but neither KALSHI_PRIVATE_KEY_PATH nor "
            "KALSHI_PRIVATE_KEY_PEM is"
        )

    def sign(self, message: str) -> str:
        sig = self._key.sign(
            message.encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()

    def headers(self, method: str, path: str, *, timestamp_ms: int | None = None) -> dict[str, str]:
        """Auth headers for a request.  ``path`` must not contain a query string."""
        if "?" in path:
            raise ValueError("sign the path without its query string")
        ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
        return {
            HEADER_KEY: self.key_id,
            HEADER_TIMESTAMP: ts,
            HEADER_SIGNATURE: self.sign(f"{ts}{method.upper()}{path}"),
        }
