import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_client.auth import HEADER_KEY, HEADER_SIGNATURE, HEADER_TIMESTAMP, KalshiAuth
from kalshi_client.config import KalshiConfig
from kalshi_client.exceptions import ConfigurationError


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return key, pem


def test_signature_verifies_against_documented_construction(keypair):
    key, pem = keypair
    auth = KalshiAuth.from_pem("kid", pem)
    h = auth.headers("get", "/trade-api/v2/portfolio/balance", timestamp_ms=1700000000123)
    assert h[HEADER_KEY] == "kid" and h[HEADER_TIMESTAMP] == "1700000000123"
    # must verify as RSA-PSS/SHA256/MGF1(SHA256)/salt=digest length over ts+METHOD+path
    key.public_key().verify(
        base64.b64decode(h[HEADER_SIGNATURE]),
        b"1700000000123GET/trade-api/v2/portfolio/balance",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_query_string_must_not_be_signed(keypair):
    with pytest.raises(ValueError):
        KalshiAuth.from_pem("kid", keypair[1]).headers("GET", "/x?a=1")


def test_repr_does_not_leak_key(keypair):
    r = repr(KalshiAuth.from_pem("abcdef123", keypair[1]))
    assert "PRIVATE" not in r and "abcdef123" not in r


def test_from_env_variants(keypair, tmp_path):
    _, pem = keypair
    assert KalshiAuth.from_env({}) is None
    with pytest.raises(ConfigurationError):
        KalshiAuth.from_env({"KALSHI_API_KEY_ID": "k"})
    with pytest.raises(ConfigurationError):
        KalshiAuth.from_env({"KALSHI_API_KEY_ID": "k", "KALSHI_PRIVATE_KEY_PATH": "/nope"})
    f = tmp_path / "k.pem"
    f.write_bytes(pem)
    assert KalshiAuth.from_env({"KALSHI_API_KEY_ID": "k", "KALSHI_PRIVATE_KEY_PATH": str(f)})
    inline = pem.decode().replace("\n", "\\n")  # .env-style single-line PEM
    assert KalshiAuth.from_env({"KALSHI_API_KEY_ID": "k", "KALSHI_PRIVATE_KEY_PEM": inline})
    with pytest.raises(ConfigurationError):
        KalshiAuth.from_pem("k", "not a key")


def test_config_paths_and_env():
    cfg = KalshiConfig.from_env({"KALSHI_ENV": "demo"}, load_dotenv_file=False)
    assert "demo" in cfg.rest_url and cfg.ws_path == "/trade-api/ws/v2"
    assert cfg.rest_path_prefix == "/trade-api/v2" and cfg.auth is None
    with pytest.raises(ConfigurationError):
        KalshiConfig.from_env({"KALSHI_ENV": "moon"}, load_dotenv_file=False)
