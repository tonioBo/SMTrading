"""Protection alarm, pure part: which open positions lack a valid exit stop."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import httpx
import pytest

from smtrading.common.fingerprints import FINGERPRINT
from smtrading.engine.models import OrderSide, OrderStatus, OrderType, TimeInForce
from smtrading.paperclip.config import PaperclipConfig
from smtrading.paperclip.issues import WRITE_REFUSED, PaperclipIssues, find_agent_id
from smtrading.positions.protection import (
    DAY_TIF,
    NO_STOP,
    SHORT_STOP,
    Finding,
    OrderView,
    PositionView,
    day_window,
    evaluate,
    issue_description,
    issue_title,
    telegram_text,
)

ROME = ZoneInfo("Europe/Rome")
# Monday 16/11/2026, NASDAQ open 15:30-22:00 Rome
NOW = datetime(2026, 11, 16, 18, 0, tzinfo=UTC)  # 19:00 Rome
CLOSE = datetime(2026, 11, 16, 21, 0, tzinfo=UTC)
OPEN = datetime(2026, 11, 16, 14, 30, tzinfo=UTC)


def pos(qty="10", *, market="NASDAQ", opened=OPEN + timedelta(minutes=10), symbol="AAPL"):
    return PositionView(uuid.UUID(int=1), "Aggressivo", symbol, market, "USD", D(qty),
                        D("180.5"), opened)


def order(*, side=OrderSide.sell, type=OrderType.stop, tif=TimeInForce.gtc,
          status=OrderStatus.pending, remaining="10", expires_at=None, group=None,
          role="stop_loss", **kw):
    return OrderView(id=uuid.uuid4(), side=side, type=type, tif=tif, status=status,
                     remaining=None if remaining is None else D(remaining),
                     stop_price=D(170) if type != OrderType.limit else None,
                     limit_price=D(200) if type == OrderType.limit else None,
                     expires_at=expires_at, oco_group_id=group, role=role,
                     reduce_only=True, **kw)


def kinds(p, orders, now=NOW):
    return [x.kind for x in evaluate(p, orders, now, ROME)]


def test_gtc_stop_covering_the_qty_is_fine():
    assert kinds(pos(), [order()]) == []


def test_no_orders_is_no_stop():
    assert kinds(pos(), []) == [NO_STOP]


def test_stop_on_the_wrong_side_or_a_limit_does_not_protect():
    buy_stop = order(side=OrderSide.buy)
    target = order(type=OrderType.limit, role="take_profit")
    assert kinds(pos(), [buy_stop, target]) == [NO_STOP]


def test_short_position_needs_a_buy_stop():
    assert kinds(pos("-5"), [order(side=OrderSide.buy, remaining="5")]) == []
    assert kinds(pos("-5"), [order(remaining="5")]) == [NO_STOP]


def test_stop_limit_and_trailing_stop_protect():
    assert kinds(pos(), [order(type=OrderType.stop_limit)]) == []
    assert kinds(pos(), [order(type=OrderType.trailing_stop)]) == []


def test_partial_cover_and_sum_of_stops():
    assert kinds(pos(), [order(remaining="4")]) == [SHORT_STOP]
    assert kinds(pos(), [order(remaining="4"), order(remaining="6")]) == []
    text = evaluate(pos("10.5"), [order(remaining="4")], NOW, ROME)[0].text
    assert text == "lo stop copre 4 su 10,5"


def test_canceled_held_or_canceling_stops_do_not_protect():
    assert kinds(pos(), [order(status=OrderStatus.held)]) == [NO_STOP]
    assert kinds(pos(), [order(canceling=True)]) == [NO_STOP]


def test_held_stop_of_a_partially_filled_entry_is_named():
    [p] = evaluate(pos("3"), [order(status=OrderStatus.held, parent_partial=True)], NOW, ROME)
    assert p.kind == NO_STOP and "ingresso è eseguito solo in parte" in p.text


def test_day_stop_far_from_the_close_on_a_position_of_today_is_fine():
    assert kinds(pos(), [order(tif=TimeInForce.day, expires_at=CLOSE)]) == []


def test_day_stop_within_30_minutes_of_the_close():
    late = CLOSE - timedelta(minutes=20)
    [p] = evaluate(pos(), [order(tif=TimeInForce.day, expires_at=CLOSE)], late, ROME)
    assert p.kind == DAY_TIF
    assert p.text == "lo stop vale solo per la giornata (tif day, scade il 16/11 22:00)"


def test_day_stop_on_a_position_of_a_previous_session():
    yesterday = OPEN - timedelta(days=3)  # Friday
    assert kinds(pos(opened=yesterday), [order(tif=TimeInForce.day, expires_at=CLOSE)]) \
        == [DAY_TIF]


def test_day_target_of_the_stop_oco_group():
    g = uuid.uuid4()
    stop = order(group=g)
    target = order(type=OrderType.limit, role="take_profit", tif=TimeInForce.day,
                   expires_at=CLOSE, group=g)
    late = CLOSE - timedelta(minutes=10)
    [p] = evaluate(pos(), [stop, target], late, ROME)
    assert p.kind == DAY_TIF and p.text.startswith("il target")
    other = order(type=OrderType.limit, role="take_profit", tif=TimeInForce.day,
                  expires_at=CLOSE)  # not in the group: not the stop's target
    assert kinds(pos(), [stop, other], late) == []


def test_extra_day_stop_beside_a_covering_gtc_stop_is_fine():
    late = CLOSE - timedelta(minutes=5)
    extra = order(tif=TimeInForce.day, expires_at=CLOSE, remaining="5")
    assert kinds(pos(), [order(), extra], late) == []
    assert kinds(pos(), [order(remaining="5"), extra], late) == [DAY_TIF]


def test_short_cover_and_day_tif_together():
    late = CLOSE - timedelta(minutes=5)
    assert kinds(pos(), [order(tif=TimeInForce.day, expires_at=CLOSE, remaining="2")], late) \
        == [SHORT_STOP, DAY_TIF]


def test_crypto_day_without_expiry_ends_at_midnight_utc():
    o = order(tif=TimeInForce.day)
    start, end = day_window(o, "CRYPTO", NOW)
    assert end == datetime(2026, 11, 17, tzinfo=UTC)
    assert start == end - timedelta(hours=24)
    p = pos(market="CRYPTO", symbol="BTC-USD", opened=NOW - timedelta(hours=1))
    assert kinds(p, [o]) == []
    assert kinds(p, [o], datetime(2026, 11, 16, 23, 40, tzinfo=UTC)) == [DAY_TIF]
    old = pos(market="CRYPTO", symbol="BTC-USD", opened=datetime(2026, 11, 15, 20, tzinfo=UTC))
    assert kinds(old, [o]) == [DAY_TIF]


def test_exchange_day_window_uses_the_session_open():
    o = order(tif=TimeInForce.day, expires_at=CLOSE)
    start, end = day_window(o, "NASDAQ", NOW)
    assert (start, end) == (OPEN, CLOSE)
    no_expiry = order(tif=TimeInForce.day)
    assert day_window(no_expiry, "NASDAQ", NOW) == (OPEN, CLOSE)


def test_texts_and_no_fingerprints():
    p = pos()
    o = order(tif=TimeInForce.day, expires_at=CLOSE)
    problems = evaluate(p, [o], CLOSE - timedelta(minutes=10), ROME)
    assert telegram_text(p, problems[0]) == (
        "⚠️ Posizione senza protezione — Aggressivo · AAPL: lo stop vale solo per la "
        "giornata (tif day, scade il 16/11 22:00)")
    assert issue_title(p) == "PROTEGGI · Aggressivo · AAPL"
    desc = issue_description(Finding(p, [o], problems), ROME)
    assert "qty: 10 · prezzo medio: 180,5 USD" in desc
    assert str(o.id) in desc and "tif gtc" in desc and "reduce_only" in desc
    assert not FINGERPRINT.search(desc)
    assert not FINGERPRINT.search(issue_title(p))
    empty = issue_description(Finding(p, [], evaluate(p, [], NOW, ROME)), ROME)
    assert "- nessuno" in empty and not FINGERPRINT.search(empty)


def test_find_agent_id_by_name():
    agents = [{"id": "a1", "name": "CEO"}, {"id": "a2", "name": " risk  manager "}]
    assert find_agent_id(agents) == "a2"
    assert find_agent_id([{"id": "a1", "name": "CEO"}]) is None


CFG = PaperclipConfig(base_url="https://pc.example.test/base", company_id="c 1",
                      board_key="pcp_board_SECRET", enabled=True)


async def test_issues_client_posts_only_to_issues():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.raw_path.decode(), request.headers["authorization"]))
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": "rm", "name": "Risk Manager"}])
        return httpx.Response(201, json={"identifier": "SMT-42", "id": "x"})

    async with PaperclipIssues(CFG, transport=httpx.MockTransport(handler)) as client:
        assert find_agent_id(await client.agents()) == "rm"
        ident = await client.create_issue(title="t", description="d", assignee_agent_id="rm")
        assert ident == "SMT-42"
        with pytest.raises(RuntimeError, match=WRITE_REFUSED):
            await client._http.post("/api/companies/c%201/agents", json={})
        with pytest.raises(RuntimeError, match=WRITE_REFUSED):
            await client._http.delete("/api/companies/c%201/issues")
    assert seen == [("GET", "/base/api/companies/c%201/agents", "Bearer pcp_board_SECRET"),
                    ("POST", "/base/api/companies/c%201/issues", "Bearer pcp_board_SECRET")]


async def test_issues_client_errors_have_no_key():
    from smtrading.paperclip.client import PaperclipError

    def handler(request):
        return httpx.Response(500, text="pcp_board_SECRET")

    async with PaperclipIssues(CFG, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PaperclipError) as e:
            await client.create_issue(title="t", description="d", assignee_agent_id="rm")
    assert e.value.message == "HTTP 500"


def test_issue_description_names_the_real_tools_and_never_double_covers():
    p = pos()
    desc = issue_description(Finding(p, [], evaluate(p, [], NOW, ROME)), ROME)
    assert "get_orders" not in desc
    assert "list_orders" in desc and "cancel_order" in desc and "modify_order" in desc
    assert "place_oco_order" in desc and "non è reduce_only" in desc
    assert "ingresso" not in desc  # no partially filled bracket entry here
    p3 = pos("3")
    held = order(status=OrderStatus.held, parent_partial=True)
    partial = issue_description(Finding(p3, [held], evaluate(p3, [held], NOW, ROME)), ROME)
    assert "si attivano quando l'ingresso è eseguito" in partial
    assert "non aggiungere un altro OCO" in partial
    assert not FINGERPRINT.search(desc) and not FINGERPRINT.search(partial)


async def test_issues_client_finds_an_open_issue_by_title():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.raw_path.decode().startswith("/base/api/companies/c%201/issues")
        return httpx.Response(200, json=[
            {"identifier": "SMT-1", "title": "PROTEGGI · A · X", "status": "done"},
            {"identifier": "SMT-2", "title": "PROTEGGI · A · Y", "status": "todo"},
            {"id": "u3", "title": "PROTEGGI · A · X", "status": "in_progress"},
        ])

    async with PaperclipIssues(CFG, transport=httpx.MockTransport(handler)) as client:
        assert await client.open_issue_with_title("PROTEGGI · A · X") == "u3"
        assert await client.open_issue_with_title("PROTEGGI · A · Z") is None
