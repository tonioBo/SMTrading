"""1-minute bars on the existing Alpaca socket (intraday mode B, Deviation 4): asked only
for symbols already subscribed to quotes and trades; without bars the subscription is the
one of today, byte for byte. A loopback websocket server stands in for Alpaca."""

import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import websockets

from smtrading.marketdata.minute_bars import StreamBar
from smtrading.marketdata.providers.alpaca import AlpacaProvider, parse_alpaca_bars
from smtrading.marketdata.types import Bar, Quote

CONFIG = {"key_id": "KID", "secret_key": "SEC"}
BAR = {"T": "b", "S": "AAPL", "t": "2026-12-01T15:00:00Z", "o": 1, "h": 2, "l": 0.5,
       "c": 1.5, "v": 100}


async def _run(items, frames, **kw):
    """Subscription messages received by the fake server, items the stream yielded."""
    received: list = []

    async def handler(ws):
        await ws.send('[{"T":"success","msg":"connected"}]')
        received.append(await ws.recv())
        await ws.send('[{"T":"success","msg":"authenticated"}]')
        received.append(await ws.recv())
        for f in frames:
            await ws.send(f)

    out = []
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient() as http:
            p = AlpacaProvider({**CONFIG, "ws_url": f"ws://127.0.0.1:{port}"}, http)
            with pytest.raises(ConnectionError):
                async for item in p.stream(items, **kw):
                    out.append(item)
    return received, out


def test_alpaca_supports_stream_bars():
    from smtrading.marketdata.provider import DataProvider

    assert AlpacaProvider.supports_stream_bars is True
    assert DataProvider.supports_stream_bars is False


async def test_subscribe_message_with_bars():
    items = [("AAPL", "AAPL"), ("TSLA", "TSLA")]
    received, out = await _run(items, [json.dumps([BAR])], bars=frozenset({"AAPL", "MSFT"}))
    assert json.loads(received[1]) == {
        "action": "subscribe", "quotes": ["AAPL", "TSLA"], "trades": ["AAPL", "TSLA"],
        "bars": ["AAPL"],  # MSFT: not in quotes/trades, never asked
    }
    assert out == [StreamBar("AAPL", Bar(ts=datetime(2026, 12, 1, 15, 0, tzinfo=UTC),
                                         open=Decimal(1), high=Decimal(2),
                                         low=Decimal("0.5"), close=Decimal("1.5"),
                                         volume=Decimal(100)))]


async def test_subscribe_message_without_bars_is_todays():
    items = [("AAPL", "AAPL"), ("TSLA", "TSLA")]
    quote = '[{"T":"q","S":"AAPL","bp":1,"ap":2,"t":"2026-12-01T15:00:00Z"}]'
    for kw in ({}, {"bars": frozenset()}):
        received, out = await _run(items, [quote, json.dumps([BAR])], **kw)
        # byte for byte the message of today: no "bars" key
        assert received[1] == json.dumps({"action": "subscribe", "quotes": ["AAPL", "TSLA"],
                                          "trades": ["AAPL", "TSLA"]})
        assert all(isinstance(q, Quote) for q in out) and len(out) == 1  # bar frame ignored


def test_parse_bar_frame():
    msgs = json.loads(json.dumps([BAR, {"T": "q", "S": "AAPL"}]), parse_float=Decimal)
    [sb] = parse_alpaca_bars(msgs, {"AAPL": "AAPL"})
    assert sb == StreamBar("AAPL", Bar(ts=datetime(2026, 12, 1, 15, 0, tzinfo=UTC),
                                       open=Decimal(1), high=Decimal(2), low=Decimal("0.5"),
                                       close=Decimal("1.5"), volume=Decimal(100)))
    assert all(isinstance(v, Decimal) for v in (sb.bar.open, sb.bar.high, sb.bar.low,
                                                sb.bar.close, sb.bar.volume))
    # unknown symbol, garbage time or price: dropped
    assert parse_alpaca_bars([{**BAR, "S": "MSFT"}], {"AAPL": "AAPL"}) == []
    assert parse_alpaca_bars([{**BAR, "t": "nope"}], {"AAPL": "AAPL"}) == []
    assert parse_alpaca_bars([{**BAR, "c": None}], {"AAPL": "AAPL"}) == []


def test_crypto_bar_symbol_mapped():
    msg = {**BAR, "S": "BTC/USD"}
    [sb] = parse_alpaca_bars([msg], {"BTC/USD": "BTC-USD"})
    assert sb.symbol == "BTC-USD"
    [sb] = parse_alpaca_bars([msg], None)
    assert sb.symbol == "BTC-USD"


async def test_crypto_stream_bars_mapped_back():
    received, out = await _run([("BTC-USD", "BTC/USD")],
                               [json.dumps([{**BAR, "S": "BTC/USD"}])],
                               bars=frozenset({"BTC/USD"}))
    assert json.loads(received[1])["bars"] == ["BTC/USD"]
    assert [sb.symbol for sb in out] == ["BTC-USD"]
