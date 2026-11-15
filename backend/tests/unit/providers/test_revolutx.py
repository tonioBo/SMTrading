import base64
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

import smtrading.marketdata.providers  # noqa: F401
from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import ProviderUnsupported
from smtrading.marketdata.providers.revolutx import RevolutXProvider, parse_tickers, sign_request
from smtrading.marketdata.registry import PROVIDERS
from smtrading.marketdata.types import Capability

FIX = Path(__file__).parents[2] / "fixtures" / "marketdata"
URL = "https://revx.revolut.com/api/1.0/tickers"


def fixture(name: str) -> str:
    return (FIX / name).read_text()


@pytest.fixture(scope="module")
def keypair():
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    return pem, key.public_key()


@pytest.fixture
async def provider(keypair):
    async with httpx.AsyncClient() as http:
        yield RevolutXProvider({"api_key": "K" * 64, "private_key_pem": keypair[0]}, http)


def test_registered_and_metadata():
    assert PROVIDERS["revolutx"] is RevolutXProvider
    assert RevolutXProvider.capabilities == {Capability.quote}
    assert RevolutXProvider.asset_classes == {AssetClass.crypto}
    assert RevolutXProvider.requires_key is True
    assert RevolutXProvider.config_fields == ("api_key", "private_key_pem")
    assert RevolutXProvider.default_rate_limit_per_min == 60


def test_no_order_endpoints():
    assert not any("order" in n.lower() and not n.startswith("_") for n in dir(RevolutXProvider)
                   if n not in {"default_rate_limit_per_min"})


def test_sign_request_verifies(keypair):
    pem, public = keypair
    sig = sign_request(pem, 1785313433816, "get", "/api/1.0/tickers", "symbols=BTC-USD", "")
    public.verify(base64.b64decode(sig), b"1785313433816GET/api/1.0/tickers" + b"symbols=BTC-USD")
    body = '{"a":1}'
    sig = sign_request(pem, "1", "POST", "/api/1.0/x", "", body)
    public.verify(base64.b64decode(sig), b"1POST/api/1.0/x" + body.encode())
    from cryptography.exceptions import InvalidSignature
    with pytest.raises(InvalidSignature):
        public.verify(base64.b64decode(sig), b"tampered")
    # a leading "?" is not part of the signed message
    assert sign_request(pem, 1, "GET", "/p", "?a=b") == sign_request(pem, 1, "GET", "/p", "a=b")


def test_map_symbol(provider):
    assert provider.map_symbol("BTC-USD", AssetClass.crypto, None) == "BTC-USD"
    assert provider.map_symbol("BTC-USD", AssetClass.crypto, "BTC-EUR") == "BTC-EUR"
    for sym, ac in [("AAPL", AssetClass.crypto), ("BTC-USD", AssetClass.us_equity), ("EURUSD", AssetClass.forex)]:
        with pytest.raises(ProviderUnsupported):
            provider.map_symbol(sym, ac, None)


def test_parse_tickers():
    import json
    payload = json.loads(fixture("revolutx_tickers.json"), parse_float=Decimal)
    out = parse_tickers(payload, {"BTC/USD": "BTC-USD"})
    assert set(out) == {"BTC-USD"}
    q = out["BTC-USD"]
    assert (q.bid, q.ask, q.last) == (Decimal("119950.00"), Decimal("120050.00"), Decimal("119980.00"))
    assert q.ts == datetime.fromtimestamp(1785313433.816, UTC) and q.provider == "revolutx"


@respx.mock
async def test_get_quotes_signed_request(provider, keypair):
    route = respx.get(URL).respond(200, text=fixture("revolutx_tickers.json"))
    quotes = await provider.get_quotes([("BTC-USD", "BTC-USD"), ("ETH-USD", "ETH-USD")])
    assert quotes["ETH-USD"].last == Decimal("4001.00") and isinstance(quotes["BTC-USD"].bid, Decimal)
    req = route.calls.last.request
    assert req.headers["X-Revx-API-Key"] == "K" * 64
    ts, query = req.headers["X-Revx-Timestamp"], req.url.query.decode()
    assert query == "symbols=BTC-USD%2CETH-USD"
    keypair[1].verify(
        base64.b64decode(req.headers["X-Revx-Signature"]), f"{ts}GET/api/1.0/tickers{query}".encode())


@respx.mock
async def test_http_error_and_health(provider):
    respx.get(URL).respond(401)
    with pytest.raises(httpx.HTTPStatusError):
        await provider.health_check()


async def test_no_credentials_unsupported():
    async with httpx.AsyncClient() as http:
        p = RevolutXProvider({}, http)
        with pytest.raises(ProviderUnsupported):
            await p.get_quotes([("BTC-USD", "BTC-USD")])
        with pytest.raises(ProviderUnsupported):
            await RevolutXProvider({"api_key": "k"}, http).health_check()


def test_parse_tickers_needs_a_timestamp_and_a_positive_price():
    data = [{"symbol": "BTC/USD", "last_price": "100"}, {"symbol": "ETH/USD", "last_price": "0"}]
    wanted = {"BTC/USD": "BTC-USD", "ETH/USD": "ETH-USD"}
    assert parse_tickers({"data": data}, wanted) == {}
    assert parse_tickers({"data": data, "metadata": {"timestamp": "x"}}, wanted) == {}
    out = parse_tickers({"data": data, "metadata": {"timestamp": 1700000000000}}, wanted)
    assert list(out) == ["BTC-USD"]
    assert out["BTC-USD"].ts == datetime.fromtimestamp(1700000000, UTC)
