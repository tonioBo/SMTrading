"""«Fiume dei flussi» v2: the replay window (closed, paged by cursor, no event lost or
repeated across pages), the «oggi» width window and the tool statistics of the lens."""
# ruff: noqa: F811  (the `agent` and `symbol` fixtures of test_flow_events, imported then requested)

from datetime import UTC, datetime

import pytest
from test_flow_events import (  # noqa: F401  (agent, symbol: fixtures)
    TZ,
    S,
    T,
    _fill,
    _link,
    _mcp,
    _order,
    _portfolio,
    agent,
    symbol,
)

from smtrading.flow.events import decode_cursor, encode_cursor, replay_events
from smtrading.flow.summary import flow_summary, flow_tools, today_start


async def _pages(session, since, until, limit):
    out, cursor, pages = [], None, 0
    while True:
        events, cursor = await replay_events(session, since=since, until=until, limit=limit,
                                             cursor=cursor)
        out += events
        pages += 1
        if cursor is None:
            return out, pages
        assert pages < 100


async def test_replay_pages_cover_the_window_once(db_session, agent):
    rows = [await _mcp(db_session, agent, "get_quote", at=T + i * S, latency=0)
            for i in range(25)]
    # same ts for three calls: the cursor breaks ties by id
    rows += [await _mcp(db_session, agent, "get_bars", at=T + 30 * S, latency=0)
             for _ in range(3)]
    events, pages = await _pages(db_session, T, T + 40 * S, 4)
    got = [e["id"] for e in events]
    assert got == sorted(set(got), key=got.index)  # no repeat
    assert set(got) == {f"mcp:{r.id}" for r in rows}
    assert pages == 7
    assert [e["ts"] for e in events] == sorted(e["ts"] for e in events)


async def test_replay_window_is_closed(db_session, agent):
    before = await _mcp(db_session, agent, at=T - S, latency=0)
    first = await _mcp(db_session, agent, at=T, latency=0)  # ts == from: inside
    last = await _mcp(db_session, agent, at=T + 10 * S, latency=0)  # ts == to: inside
    after = await _mcp(db_session, agent, at=T + 11 * S, latency=0)
    # a long call written inside the window but started before it: outside (ts)
    long = await _mcp(db_session, agent, "run_backtest", at=T + 5 * S, latency=60_000)
    events, cursor = await replay_events(db_session, since=T, until=T + 10 * S, limit=100,
                                         cursor=None)
    assert cursor is None
    got = {e["id"] for e in events}
    assert got == {f"mcp:{first.id}", f"mcp:{last.id}"}
    assert f"mcp:{before.id}" not in got and f"mcp:{after.id}" not in got
    assert f"mcp:{long.id}" not in got


async def test_replay_mixes_sources(db_session, agent, symbol):
    link = await _link(db_session)
    p = await _portfolio(db_session, agent, link=link)
    o = await _order(db_session, p, symbol, link=link, sent_at=T + S)
    f = await _fill(db_session, o, at=T + 3 * S, source="broker")
    m = await _mcp(db_session, agent, "place_order", at=T + 2 * S, latency=0)
    events, _ = await _pages(db_session, T, T + 5 * S, 1)
    assert [e["id"] for e in events] == [f"order:{o.id}:sent", f"mcp:{m.id}", f"fill:{f.id}"]


def test_cursor_round_trip_and_garbage():
    c = encode_cursor(T, "mcp:12")
    assert decode_cursor(c) == (T, "mcp:12")
    for bad in ("", "x", "ieri|mcp:1", "2031-03-04T15:00:00+00:00|"):
        with pytest.raises(ValueError):
            decode_cursor(bad)


async def test_summary_today_counts_since_local_midnight(db_session, agent):
    start = today_start(T, TZ)  # 2031-03-04 00:00 Rome = 23:00 UTC of the day before
    assert start == datetime(2031, 3, 3, 23, tzinfo=UTC)
    await _mcp(db_session, agent, "get_quote", at=start + S, latency=0)
    await _mcp(db_session, agent, "get_quote", at=T - 10 * 60 * S, latency=0)
    await _mcp(db_session, agent, "get_quote", at=start - S, latency=0)  # yesterday
    s = await flow_summary(db_session, window="today", now=T, tz=TZ)
    assert s["links"]["mcp|px"]["calls"] == 2
    minutes = (T - start).total_seconds() / 60
    assert s["head"]["calls_per_min"] == round(2 / minutes, 2)
    assert s["head"]["truncated"] is False
    five = await flow_summary(db_session, window=5, now=T, tz=TZ)
    assert "mcp|px" not in five["links"]


async def test_tools_of_the_lens(db_session, agent, symbol):
    await _mcp(db_session, agent, "get_quote", at=T - 10 * S, latency=100)
    await _mcp(db_session, agent, "get_quote", at=T - 9 * S, latency=300, outcome="NOT_FOUND")
    await _mcp(db_session, agent, "place_order", at=T - 8 * S, latency=50)
    await _mcp(db_session, agent, "mystery_tool", at=T - 7 * S, latency=10)
    link = await _link(db_session)
    p = await _portfolio(db_session, agent, link=link)
    await _order(db_session, p, symbol, link=link, sent_at=T - 5 * S)
    out = await flow_tools(db_session, window=1, now=T, tz=TZ)
    fams = {f["key"]: f for f in out["families"]}
    assert list(fams) == ["orders", "market", "risk", "research", "decisions", "messages"]
    tools = {t["tool"]: t for f in out["families"] for t in f["tools"]}
    assert tools["get_quote"] == {"tool": "get_quote", "dest": "px", "calls": 2, "errors": 1,
                                  "avg_ms": 200}
    assert tools["place_order"]["calls"] == 1 and tools["place_order"]["dest"] == "eng"
    assert tools["send_telegram"] == {"tool": "send_telegram", "dest": "tg", "calls": 0,
                                      "errors": 0, "avg_ms": None}
    assert [t["tool"] for t in out["other"]] == ["mystery_tool"]
    px = out["links"]["mcp|px"]
    assert px == [{"tool": "get_quote", "calls": 2, "errors": 1, "avg_ms": 200}]
    assert out["links"]["eng|sync"] == [{"tool": "ordine inviato", "calls": 1, "errors": 0,
                                         "avg_ms": None}]
    assert out["window"] == 1 and out["truncated"] is False
