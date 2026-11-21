"""«Prova generale»: a battery of test orders of every type and option, driven through the
MCP endpoint as an agent, on the ISOLATED e2e stack only (scripts/e2e.sh,
`SMT_E2E_BATTERY=<file>`; never production).

    docker compose -p smtrading-e2e ... run --rm --no-deps -T --entrypoint python \
        -e SMT_E2E_TOTP_SECRET -v "$PWD/scripts:/scripts:ro" -v <dir>:/out \
        api /scripts/order_battery.py --out /out/battery.jsonl

(or simply `SMT_E2E_BATTERY=<file.jsonl> bash scripts/e2e.sh`)

It runs in a container of the e2e backend image on the e2e network (the api service's
environment: SMT_LIVE_FAKE_BROKER=1, the throwaway admin, the e2e database) and talks to the
in-network `http://api:8000`. Refused unless the fake broker flag is on (never set in
production).

Setup (admin API, logged in with the e2e admin and its throwaway TOTP): an agent with its
MCP token, three portfolios with their risk profiles («Batteria A», EUR 500; «Batteria
B», EUR 100, BTC only; «Batteria R», EUR 500, linked to an eToro real-money link on the
in-memory FakeLiveBroker) and the agent's `trade` permission on them.

Every test, as soon as it completes, appends one JSON line to `--out` (flushed):
{"n", "total", "name", "expected", "got", "ok" (true/false/null = not applicable), "note"},
in Italian; at the end {"done": true, "passed", "failed", "na"}. Nothing secret is written.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

import httpx
import pyotp
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

API = "http://api:8000"
MCP_URL = f"{API}/mcp"
SYM = "BTC-USD"
SYM2 = "ETH-USD"
STOCK = "AAPL"
# the words that would give the simulation away (smtrading.common.fingerprints.FINGERPRINT)
FINGERPRINT = re.compile(
    r"\bsim\b|simul|paper|demo|\bdry|\bprova\b|\btest|fake|mock|sandbox|virtual|smoke",
    re.IGNORECASE,
)
OPEN = {"new", "held", "queued", "pending", "triggered"}
E2E_API_KEY = "E2EBATTERYETOROKEY0001"
E2E_USER_KEY = "E2EBATTERYETOROUSER-not-a-key"


class Fail(Exception):
    """A test outcome that is not the expected one (its text is the observed result)."""


class NotApplicable(Exception):
    """The case does not exist in the code: reported as «non applicabile»."""


def d(v: Any) -> Decimal:
    return Decimal(str(v))


def short(v: Any, n: int = 160) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[: n - 1] + "…"


# --- feed ---------------------------------------------------------------------------------


class Feed:
    def __init__(self, path: str, total: int) -> None:
        self.path, self.total = path, total
        self.n = 0
        self.passed = self.failed = self.na = 0
        self.rows: list[dict] = []

    def write(self, row: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def result(self, name: str, expected: str, got: str, ok: bool | None, note: str = "",
               telegram: int | None = None) -> None:
        """`telegram`: the Telegram notifications queued while the test ran (never sent
        from the e2e stack)."""
        self.n += 1
        row = {"n": self.n, "total": self.total, "name": name, "expected": expected,
               "got": got, "ok": ok, "note": note, "telegram": telegram}
        if ok is True:
            self.passed += 1
        elif ok is False:
            self.failed += 1
        else:
            self.na += 1
        self.rows.append(row)
        self.write(row)
        mark = {True: "OK ", False: "KO ", None: "N/A"}[ok]
        print(f"[{self.n:>2}/{self.total}] {mark} {name}: {got}", flush=True)

    def done(self) -> None:
        self.write({"done": True, "passed": self.passed, "failed": self.failed, "na": self.na})


# --- TOTP ---------------------------------------------------------------------------------


class Totp:
    """Each code is accepted once: a spent 30 s window is waited out. The current window
    counts as spent at the start (the e2e spec may have just used it)."""

    def __init__(self, secret: str) -> None:
        self._totp = pyotp.TOTP(secret)
        self._last = int(time.time() // 30)

    async def fresh(self) -> str:
        counter = int(time.time() // 30)
        while counter <= self._last:
            await asyncio.sleep(30 - time.time() % 30 + 0.3)
            counter = int(time.time() // 30)
        self._last = counter
        return self._totp.at(counter * 30)


# --- admin API ------------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"HTTP {status}: {short(body)}")
        self.status, self.body = status, body

    @property
    def code(self) -> str | None:
        return self.body.get("code") if isinstance(self.body, dict) else None


class Admin:
    def __init__(self, totp: Totp) -> None:
        self.http = httpx.AsyncClient(base_url=API, timeout=60,
                                      headers={"User-Agent": "smtrading-order-battery"})
        self.totp = totp

    async def login(self, email: str, password: str) -> None:
        r = await self.http.post("/api/auth/login", json={
            "email": email, "password": password, "totp": await self.totp.fresh()})
        if r.status_code != 200:
            raise ApiError(r.status_code, r.json())

    async def req(self, method: str, path: str, body: Any = None, *,
                  idem: bool = False, params: dict | None = None) -> Any:
        headers = {"X-CSRF-Token": self.http.cookies.get("smt_csrf") or ""}
        if idem:
            headers["Idempotency-Key"] = str(uuid.uuid4())
        r = await self.http.request(method, path, json=body, headers=headers, params=params)
        try:
            data = r.json()
        except ValueError:
            data = r.text
        if r.status_code >= 400:
            raise ApiError(r.status_code, data)
        return data

    get = lambda self, path, **kw: self.req("GET", path, **kw)
    post = lambda self, path, body=None, **kw: self.req("POST", path, body, **kw)
    put = lambda self, path, body=None, **kw: self.req("PUT", path, body, **kw)
    patch = lambda self, path, body=None, **kw: self.req("PATCH", path, body, **kw)
    delete = lambda self, path, **kw: self.req("DELETE", path, **kw)


# --- MCP agent ------------------------------------------------------------------------------


@dataclass
class Reply:
    error: bool
    data: Any
    text: str

    @property
    def code(self) -> str | None:
        return self.data.get("code") if self.error and isinstance(self.data, dict) else None


_PLACING = {"place_order", "place_bracket_order", "place_oco_order", "close_position"}


@dataclass
class Agent:
    cs: ClientSession
    outputs: list[str] = field(default_factory=list)  # every agent-facing text (hide mode)
    orders: dict[str, str] = field(default_factory=dict)  # order id -> portfolio id

    async def call(self, tool: str, **args: Any) -> Reply:
        args = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in args.items()
                if v is not None}
        result = await self.cs.call_tool(tool, args)
        text = "".join(c.text for c in result.content if c.type == "text")
        self.outputs.append(text)
        try:
            data = json.loads(text)
        except ValueError:
            data = text
        if tool in _PLACING:  # the orders placed, rejected ones included
            items = data if isinstance(data, list) else [data]
            for it in items:
                if not isinstance(it, dict):
                    continue
                oid = it.get("id") or (it.get("details") or {}).get("order_id")
                if oid:
                    self.orders[oid] = args.get("portfolio_id", "")
        return Reply(bool(result.is_error), data, text)

    async def ok(self, tool: str, **args: Any) -> Any:
        r = await self.call(tool, **args)
        if r.error:
            raise Fail(f"{tool} in errore: {short(r.data)}")
        return r.data


# --- the battery -------------------------------------------------------------------------------


@dataclass
class State:
    admin: Admin
    agent: Agent | None = None
    pa: str = ""  # Batteria A: EUR 500
    pb: str = ""  # Batteria B: EUR 100, BTC only
    pr: str = ""  # Batteria R: eToro real money (FakeLiveBroker)
    link: str = ""
    link_name: str = ""
    tick: Decimal = Decimal("0.01")
    lot: Decimal = Decimal("0.00000001")
    ids: dict[str, str] = field(default_factory=dict)
    t0: datetime = field(default_factory=lambda: datetime.now(UTC))
    closure_id: int | None = None
    decision_subject: str = ""

    def px(self, v: Decimal) -> Decimal:
        return (v / self.tick).to_integral_value(ROUND_DOWN) * self.tick

    def qty(self, v: Decimal) -> Decimal:
        return (v / self.lot).to_integral_value(ROUND_DOWN) * self.lot


async def quote(s: State, symbol: str = SYM) -> tuple[Decimal, Decimal]:
    q = (await s.agent.ok("get_quote", symbols=[symbol]))[symbol]
    if "error" in q:
        raise Fail(f"quotazione {symbol} non disponibile: {q}")
    bid = d(q.get("bid") or q["last"])
    ask = d(q.get("ask") or q["last"])
    return bid, ask


async def qty_for(s: State, eur: Decimal, symbol: str = SYM) -> Decimal:
    """The quantity worth about `eur` (USD ~ EUR is close enough for a sizing)."""
    _, ask = await quote(s, symbol)
    fx = d((await s.agent.ok("get_fx_rate", base="USD", quote="EUR"))["rate"])
    return s.qty(eur / (ask * fx))


async def order(s: State, oid: str) -> dict:
    return await s.agent.ok("get_order", order_id=oid)


async def wait_status(s: State, oid: str, statuses: set[str], timeout: float = 90) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        o = await order(s, oid)
        if o["status"] in statuses:
            return o
        if time.monotonic() > deadline:
            raise Fail(f"stato «{o['status']}» dopo {int(timeout)} s (atteso {sorted(statuses)})")
        await asyncio.sleep(2)


async def position(s: State, pid: str, symbol: str = SYM) -> Decimal:
    rows = await s.agent.ok("get_positions", portfolio_id=pid)
    items = rows if isinstance(rows, list) else rows.get("positions", [])
    return sum((d(p["qty"]) for p in items if p["symbol"] == symbol), Decimal(0))


async def place(s: State, pid: str, **kw: Any) -> Reply:
    return await s.agent.call("place_order", portfolio_id=pid, **kw)


def expect_code(r: Reply, code: str) -> str:
    if not r.error:
        raise Fail(f"accettato (stato {r.data.get('status') if isinstance(r.data, dict) else '?'})"
                   f", atteso il rifiuto {code}")
    if r.code != code:
        raise Fail(f"rifiutato con {r.code}: {short(r.data.get('message'))}")
    return f"rifiutato {code}: {short(r.data.get('message'), 90)}"


async def in_feed(s: State, oid: str, types: tuple[str, ...]) -> bool:
    feed = await s.agent.ok("get_activity_feed", since=s.t0.isoformat(), limit=200)
    return any(
        it.get("type") in types and oid in json.dumps(it.get("data"), default=str)
        for it in feed["items"])


async def listed(s: State, pid: str, oid: str) -> bool:
    rows = await s.agent.ok("list_orders", portfolio_id=pid, limit=200)
    return any(o["id"] == oid for o in rows)


async def checks_after(s: State, pid: str, oid: str, feed_types: tuple[str, ...]) -> str:
    """get_order / list_orders / activity feed after a step."""
    notes = []
    if not await listed(s, pid, oid):
        raise Fail("ordine assente da list_orders")
    notes.append("in list_orders")
    deadline = time.monotonic() + 20
    while not await in_feed(s, oid, feed_types):
        if time.monotonic() > deadline:
            raise Fail(f"nessuna voce {feed_types} nel feed attività")
        await asyncio.sleep(2)
    notes.append("nel feed attività")
    return ", ".join(notes)


# --- setup ------------------------------------------------------------------------------------


async def setup(s: State) -> None:
    a = s.admin
    stamp = datetime.now().strftime("%H%M%S")
    await a.put("/api/settings/app", {"mcp_hide_mode": True})
    prof_a = await a.post("/api/risk-profiles", {
        "name": f"Batteria A {stamp}", "max_order_notional_eur": "150",
        "max_daily_loss_pct": "20", "short_allowed": False})
    prof_b = await a.post("/api/risk-profiles", {
        "name": f"Batteria B {stamp}", "max_order_notional_eur": "1000",
        "allowed_symbols": [SYM], "short_allowed": False})
    prof_r = await a.post("/api/risk-profiles", {
        "name": f"Batteria R {stamp}", "max_order_notional_eur": "100",
        "max_daily_loss_pct": "5", "short_allowed": False})
    pa = await a.post("/api/portfolios", {"name": f"Batteria A {stamp}", "initial_cash": "500",
                                          "risk_profile_id": prof_a["id"]}, idem=True)
    pb = await a.post("/api/portfolios", {"name": f"Batteria B {stamp}", "initial_cash": "100",
                                          "risk_profile_id": prof_b["id"]}, idem=True)
    pr = await a.post("/api/portfolios", {"name": f"Batteria R {stamp}", "initial_cash": "500",
                                          "risk_profile_id": prof_r["id"]}, idem=True)
    s.pa, s.pb, s.pr = pa["id"], pb["id"], pr["id"]
    created = await a.post("/api/agents", {"name": f"Agente batteria {stamp}"})
    agent_id, token = created["agent"]["id"], created["token"]
    await a.put(f"/api/agents/{agent_id}/portfolios", [
        {"portfolio_id": pid, "permission": "trade"} for pid in (s.pa, s.pb, s.pr)])
    s.link_name = f"eToro batteria {stamp}"
    link = await a.post("/api/live-links", {
        "name": s.link_name, "kind": "etoro_live", "api_key": E2E_API_KEY,
        "user_key": E2E_USER_KEY})
    s.link = link["id"]
    s.ids["token"] = token


# --- tests ------------------------------------------------------------------------------------

Test = tuple[str, str, Callable[[State], Awaitable[str]]]
TESTS: list[Test] = []


def test(name: str, expected: str):
    def deco(fn: Callable[[State], Awaitable[str]]):
        TESTS.append((name, expected, fn))
        return fn
    return deco


@test("agente vede i suoi portafogli", "3 portafogli, nomi senza parole da simulazione")
async def t_list(s: State) -> str:
    rows = await s.agent.ok("list_portfolios")
    ids = {p["id"] for p in rows}
    if not {s.pa, s.pb, s.pr} <= ids:
        raise Fail(f"visti {len(rows)} portafogli, mancano quelli della batteria")
    return f"{len(rows)} portafogli visibili"


@test("anteprima market buy per quantità", "ok=true con prezzo stimato")
async def t_preview_market(s: State) -> str:
    q = await qty_for(s, Decimal(40))
    r = await s.agent.ok("preview_order", portfolio_id=s.pa, symbol=SYM, side="buy",
                         type="market", qty=q)
    if r.get("ok") is not True:
        raise Fail(f"ok={r.get('ok')} {r.get('code')}")
    return f"ok, prezzo stimato {r['estimated_price']}, ~{d(r['estimated_notional_eur']):.2f} EUR"


@test("market buy per quantità", "eseguito subito, fill presente, in elenco e nel feed")
async def t_market_qty(s: State) -> str:
    q = await qty_for(s, Decimal(40))
    r = await place(s, s.pa, symbol=SYM, side="buy", type="market", qty=q)
    if r.error:
        raise Fail(f"rifiutato {r.code}: {short(r.data)}")
    o = await order(s, r.data["id"])
    if o["status"] != "filled" or not o["fills"]:
        raise Fail(f"stato {o['status']}, fill {len(o['fills'])}")
    s.ids["buy1"] = o["id"]
    extra = await checks_after(s, s.pa, o["id"], ("order.filled", "fill"))
    return f"eseguito {o['filled_qty']} a {o['avg_fill_price']}; {extra}"


@test("market buy per importo (frazionario)", "eseguito, quantità frazionaria")
async def t_market_notional(s: State) -> str:
    pv = await s.agent.ok("preview_order", portfolio_id=s.pa, symbol=SYM, side="buy",
                          type="market", notional="30")
    if pv.get("ok") is not True:
        raise Fail(f"anteprima ok={pv.get('ok')} {pv.get('code')}")
    r = await place(s, s.pa, symbol=SYM, side="buy", type="market", notional="30")
    if r.error:
        raise Fail(f"rifiutato {r.code}: {short(r.data)}")
    o = await order(s, r.data["id"])
    if o["status"] != "filled" or d(o["filled_qty"]) >= 1:
        raise Fail(f"stato {o['status']}, quantità {o['filled_qty']}")
    return f"30 USD → {o['filled_qty']} {SYM} a {o['avg_fill_price']}"


@test("limit buy sotto il prezzo, poi annullato", "resta aperto (pending), poi annullato")
async def t_limit_below(s: State) -> str:
    _, ask = await quote(s)
    lp = s.px(ask * Decimal("0.9"))
    q = await qty_for(s, Decimal(30))
    pv = await s.agent.ok("preview_order", portfolio_id=s.pa, symbol=SYM, side="buy",
                          type="limit", qty=q, limit_price=lp)
    if pv.get("ok") is not True:
        raise Fail(f"anteprima ok={pv.get('ok')} {pv.get('code')}")
    r = await place(s, s.pa, symbol=SYM, side="buy", type="limit", qty=q, limit_price=lp)
    if r.error or r.data["status"] != "pending":
        raise Fail(f"{'rifiutato ' + str(r.code) if r.error else 'stato ' + r.data['status']}")
    await asyncio.sleep(3)
    if (await order(s, r.data["id"]))["status"] != "pending":
        raise Fail("non più aperto dopo 3 s")
    c = await s.agent.ok("cancel_order", order_id=r.data["id"])
    if c["status"] != "canceled":
        raise Fail(f"dopo l'annullo: {c['status']}")
    return f"aperto a {lp} (−10%), poi annullato"


@test("limit buy sopra il prezzo", "eseguito subito (prezzo ≤ limite)")
async def t_limit_above(s: State) -> str:
    _, ask = await quote(s)
    lp = s.px(ask * Decimal("1.02"))
    q = await qty_for(s, Decimal(30))
    r = await place(s, s.pa, symbol=SYM, side="buy", type="limit", qty=q, limit_price=lp)
    if r.error:
        raise Fail(f"rifiutato {r.code}")
    o = await wait_status(s, r.data["id"], {"filled", "rejected", "canceled"}, 30)
    if o["status"] != "filled" or d(o["avg_fill_price"]) > lp:
        raise Fail(f"stato {o['status']} a {o.get('avg_fill_price')}")
    return f"eseguito a {o['avg_fill_price']} (limite {lp})"


@test("stop buy sopra il prezzo", "resta aperto (pending)")
async def t_stop_buy(s: State) -> str:
    _, ask = await quote(s)
    sp = s.px(ask * Decimal("1.1"))
    q = await qty_for(s, Decimal(20))
    r = await place(s, s.pa, symbol=SYM, side="buy", type="stop", qty=q, stop_price=sp)
    if r.error or r.data["status"] != "pending":
        raise Fail(f"{'rifiutato ' + str(r.code) if r.error else 'stato ' + r.data['status']}")
    s.ids["stop_buy"] = r.data["id"]
    return f"aperto, stop a {sp} (+10%)"


@test("stop buy già superato", "scatta subito ed è eseguito a mercato")
async def t_stop_buy_hit(s: State) -> str:
    _, ask = await quote(s)
    sp = s.px(ask * Decimal("0.99"))
    q = await qty_for(s, Decimal(20))
    r = await place(s, s.pa, symbol=SYM, side="buy", type="stop", qty=q, stop_price=sp)
    if r.error:
        raise Fail(f"rifiutato {r.code}")
    o = await wait_status(s, r.data["id"], {"filled", "rejected", "canceled"}, 30)
    if o["status"] != "filled":
        raise Fail(f"stato {o['status']}")
    return f"stop {sp} scattato, eseguito a {o['avg_fill_price']}"


@test("stop sell sotto il prezzo", "resta aperto (protegge la posizione)")
async def t_stop_sell(s: State) -> str:
    bid, _ = await quote(s)
    sp = s.px(bid * Decimal("0.9"))
    held = await position(s, s.pa)
    q = s.qty(held / 4)
    r = await place(s, s.pa, symbol=SYM, side="sell", type="stop", qty=q, stop_price=sp)
    if r.error or r.data["status"] != "pending":
        raise Fail(f"{'rifiutato ' + str(r.code) if r.error else 'stato ' + r.data['status']}")
    s.ids["stop_sell"] = r.data["id"]
    return f"aperto, stop a {sp} (−10%) su {q}"


@test("stop-limit buy, poi modificato", "aperto; stop e limite cambiati con modify_order")
async def t_stop_limit(s: State) -> str:
    _, ask = await quote(s)
    sp, lp = s.px(ask * Decimal("1.1")), s.px(ask * Decimal("1.12"))
    q = await qty_for(s, Decimal(20))
    pv = await s.agent.ok("preview_order", portfolio_id=s.pa, symbol=SYM, side="buy",
                          type="stop_limit", qty=q, stop_price=sp, limit_price=lp)
    if pv.get("ok") is not True:
        raise Fail(f"anteprima ok={pv.get('ok')} {pv.get('code')}")
    r = await place(s, s.pa, symbol=SYM, side="buy", type="stop_limit", qty=q,
                    stop_price=sp, limit_price=lp)
    if r.error or r.data["status"] != "pending":
        raise Fail(f"{'rifiutato ' + str(r.code) if r.error else 'stato ' + r.data['status']}")
    sp2, lp2 = s.px(ask * Decimal("1.15")), s.px(ask * Decimal("1.17"))
    m = await s.agent.ok("modify_order", order_id=r.data["id"], stop_price=sp2, limit_price=lp2)
    if d(m["stop_price"]) != sp2 or d(m["limit_price"]) != lp2 or m["status"] != "pending":
        raise Fail(f"dopo modify: stop {m['stop_price']} limite {m['limit_price']} {m['status']}")
    s.ids["stop_limit"] = r.data["id"]
    return f"aperto {sp}/{lp}, modificato in {sp2}/{lp2}"


@test("stop-limit già superato", "scatta e si esegue come limite")
async def t_stop_limit_hit(s: State) -> str:
    _, ask = await quote(s)
    sp, lp = s.px(ask * Decimal("0.99")), s.px(ask * Decimal("1.02"))
    q = await qty_for(s, Decimal(20))
    r = await place(s, s.pa, symbol=SYM, side="buy", type="stop_limit", qty=q,
                    stop_price=sp, limit_price=lp)
    if r.error:
        raise Fail(f"rifiutato {r.code}")
    o = await wait_status(s, r.data["id"], {"filled", "rejected", "canceled"}, 30)
    if o["status"] != "filled" or d(o["avg_fill_price"]) > lp:
        raise Fail(f"stato {o['status']} a {o.get('avg_fill_price')}")
    return f"scattato ed eseguito a {o['avg_fill_price']} (limite {lp})"


@test("trailing stop sell per importo", "aperto, prezzo di riferimento registrato")
async def t_trail_amount(s: State) -> str:
    bid, _ = await quote(s)
    amount = s.px(bid * Decimal("0.08"))
    q = s.qty(await position(s, s.pa) / 5)
    pv = await s.agent.ok("preview_order", portfolio_id=s.pa, symbol=SYM, side="sell",
                          type="trailing_stop", qty=q, trail_amount=amount)
    if pv.get("ok") is not True:
        raise Fail(f"anteprima ok={pv.get('ok')} {pv.get('code')}")
    r = await place(s, s.pa, symbol=SYM, side="sell", type="trailing_stop", qty=q,
                    trail_amount=amount)
    if r.error or r.data["status"] != "pending" or r.data.get("trail_ref_price") is None:
        raise Fail(short(r.data))
    s.ids["trail_amount"] = r.data["id"]
    return f"aperto, distanza {amount}, riferimento {r.data['trail_ref_price']}"


@test("trailing stop sell in percentuale", "aperto con trail_pct 5%")
async def t_trail_pct(s: State) -> str:
    q = s.qty(await position(s, s.pa) / 5)
    r = await place(s, s.pa, symbol=SYM, side="sell", type="trailing_stop", qty=q, trail_pct="5")
    if r.error or r.data["status"] != "pending" or d(r.data["trail_pct"]) != 5:
        raise Fail(short(r.data))
    s.ids["trail_pct"] = r.data["id"]
    return f"aperto, 5%, riferimento {r.data['trail_ref_price']}"


@test("validità day e gtc", "day con scadenza, gtc senza scadenza")
async def t_tif(s: State) -> str:
    _, ask = await quote(s)
    lp = s.px(ask * Decimal("0.8"))
    q = await qty_for(s, Decimal(10))
    day = await place(s, s.pa, symbol=SYM, side="buy", type="limit", qty=q, limit_price=lp,
                      tif="day")
    gtc = await place(s, s.pa, symbol=SYM, side="buy", type="limit", qty=q, limit_price=lp,
                      tif="gtc")
    if day.error or gtc.error:
        raise Fail(f"rifiutato: {short(day.data if day.error else gtc.data)}")
    if not day.data.get("expires_at") or gtc.data.get("expires_at"):
        raise Fail(f"day scade {day.data.get('expires_at')}, gtc {gtc.data.get('expires_at')}")
    s.ids["tif_day"], s.ids["tif_gtc"] = day.data["id"], gtc.data["id"]
    return f"day scade {day.data['expires_at'][:16]}, gtc senza scadenza"


@test("modify di un ordine aperto (prezzo e quantità)", "prezzo e quantità aggiornati")
async def t_modify(s: State) -> str:
    oid = s.ids["tif_gtc"]
    before = await order(s, oid)
    lp = s.px(d(before["limit_price"]) * Decimal("0.98"))
    q = s.qty(d(before["qty"]) * 2)
    m = await s.agent.ok("modify_order", order_id=oid, qty=q, limit_price=lp)
    if d(m["qty"]) != q or d(m["limit_price"]) != lp:
        raise Fail(f"qty {m['qty']} limite {m['limit_price']}")
    return f"qty {before['qty']}→{m['qty']}, limite {before['limit_price']}→{m['limit_price']}"


@test("modify oltre il limite di rischio", "rifiuto RISK_LIMIT_EXCEEDED, ordine invariato")
async def t_modify_risk(s: State) -> str:
    oid = s.ids["tif_gtc"]
    before = await order(s, oid)
    r = await s.agent.call("modify_order", order_id=oid, qty=s.qty(d(before["qty"]) * 50))
    got = expect_code(r, "RISK_LIMIT_EXCEEDED")
    after = await order(s, oid)
    if after["qty"] != before["qty"] or after["status"] != "pending":
        raise Fail(f"ordine cambiato: qty {after['qty']} {after['status']}")
    return got + "; ordine invariato"


@test("anteprima di ogni tipo", "ok=true per market, limit, stop, stop-limit, trailing")
async def t_preview_all(s: State) -> str:
    _, ask = await quote(s)
    q = await qty_for(s, Decimal(10))
    cases = {
        "market": {"side": "buy"},
        "limit": {"side": "buy", "limit_price": s.px(ask * Decimal("0.95"))},
        "stop": {"side": "buy", "stop_price": s.px(ask * Decimal("1.05"))},
        "stop_limit": {"side": "buy", "stop_price": s.px(ask * Decimal("1.05")),
                       "limit_price": s.px(ask * Decimal("1.06"))},
        "trailing_stop": {"side": "sell", "trail_pct": "3"},
    }
    bad = []
    for typ, kw in cases.items():
        r = await s.agent.ok("preview_order", portfolio_id=s.pa, symbol=SYM, type=typ, qty=q, **kw)
        if r.get("ok") is not True:
            bad.append(f"{typ}:{r.get('code')}")
    if bad:
        raise Fail("anteprime non ok: " + ", ".join(bad))
    return "5 anteprime ok"


@test("ordine incompleto", "rifiuto INVALID_ORDER (limit senza prezzo)")
async def t_invalid(s: State) -> str:
    r = await place(s, s.pa, symbol=SYM, side="buy", type="limit", qty="0.0001")
    return expect_code(r, "INVALID_ORDER")


@test("client_order_id ripetuto", "stesso ordine restituito, nessun doppione")
async def t_idempotent(s: State) -> str:
    _, ask = await quote(s)
    coid = f"batt-{uuid.uuid4().hex[:10]}"
    kw = {"symbol": SYM, "side": "buy", "type": "limit",
          "qty": await qty_for(s, Decimal(10)),
          "limit_price": s.px(ask * Decimal("0.7")), "client_order_id": coid}
    a = await place(s, s.pa, **kw)
    b = await place(s, s.pa, **kw)
    if a.error or b.error or a.data["id"] != b.data["id"]:
        raise Fail(f"{short(a.data, 60)} / {short(b.data, 60)}")
    await s.agent.ok("cancel_order", order_id=a.data["id"])
    return "stesso id alla seconda chiamata"


@test("vendita parziale", "eseguita, posizione ridotta")
async def t_sell_partial(s: State) -> str:
    before = await position(s, s.pa)
    q = s.qty(before / 3)
    o = await s.agent.ok("close_position", portfolio_id=s.pa, symbol=SYM, qty=q)
    o = await wait_status(s, o["id"], {"filled", "rejected"}, 30)
    after = await position(s, s.pa)
    if o["status"] != "filled" or after != before - q:
        raise Fail(f"stato {o['status']}, posizione {before}→{after}")
    return f"venduti {q}, posizione {before}→{after}"


@test("ordine bracket (ingresso + take profit + stop loss)",
      "ingresso eseguito; preso il take profit, lo stop loss annullato")
async def t_bracket(s: State) -> str:
    bid, ask = await quote(s)
    q = await qty_for(s, Decimal(25))
    tp, sl = s.px(ask * Decimal("1.2")), s.px(bid * Decimal("0.8"))
    r = await s.agent.call("place_bracket_order", portfolio_id=s.pa, symbol=SYM, side="buy",
                           qty=str(q), take_profit=str(tp), stop_loss=str(sl))
    if r.error:
        raise Fail(f"rifiutato {r.code}: {short(r.data)}")
    entry, tpo, slo = (o["id"] for o in r.data)
    e = await wait_status(s, entry, {"filled", "rejected"}, 30)
    if e["status"] != "filled":
        raise Fail(f"ingresso {e['status']}")
    await wait_status(s, tpo, {"pending"}, 30)
    # take profit moved below the bid: it becomes marketable and fills
    bid, _ = await quote(s)
    m = await s.agent.call("modify_order", order_id=tpo, limit_price=str(s.px(bid * Decimal("0.99"))))
    if m.error:
        raise Fail(f"modify del take profit rifiutato: {short(m.data)}")
    t = await wait_status(s, tpo, {"filled", "canceled", "rejected"}, 90)
    st = await wait_status(s, slo, {"canceled", "filled", "rejected"}, 30)
    if t["status"] != "filled" or st["status"] != "canceled":
        raise Fail(f"take profit {t['status']}, stop loss {st['status']}")
    return f"ingresso a {e['avg_fill_price']}, take profit eseguito a {t['avg_fill_price']}, stop loss annullato"


@test("ordine OCO", "una gamba eseguita, l'altra annullata")
async def t_oco(s: State) -> str:
    bid, _ = await quote(s)
    q = s.qty(await position(s, s.pa) / 6)
    a = {"type": "limit", "limit_price": str(s.px(bid * Decimal("1.2")))}
    b = {"type": "stop", "stop_price": str(s.px(bid * Decimal("0.8")))}
    r = await s.agent.call("place_oco_order", portfolio_id=s.pa, symbol=SYM, side="sell",
                           qty=str(q), a=a, b=b)
    if r.error:
        raise Fail(f"rifiutato {r.code}: {short(r.data)}")
    la, lb = (o["id"] for o in r.data)
    if {o["status"] for o in r.data} != {"pending"}:
        raise Fail(f"stati {[o['status'] for o in r.data]}")
    bid, _ = await quote(s)
    m = await s.agent.call("modify_order", order_id=la, limit_price=str(s.px(bid * Decimal("0.99"))))
    if m.error:
        raise Fail(f"modify rifiutato: {short(m.data)}")
    oa = await wait_status(s, la, {"filled", "canceled", "rejected"}, 90)
    ob = await wait_status(s, lb, {"filled", "canceled", "rejected"}, 30)
    if oa["status"] != "filled" or ob["status"] != "canceled":
        raise Fail(f"gamba A {oa['status']}, gamba B {ob['status']}")
    return f"limite eseguito a {oa['avg_fill_price']}, stop annullato"


@test("cancel_all_orders per simbolo e per portafoglio",
      "prima solo gli ordini del simbolo, poi tutti gli altri")
async def t_cancel_all(s: State) -> str:
    _, ask2 = await quote(s, SYM2)
    q2 = await qty_for(s, Decimal(10), SYM2)
    r = await place(s, s.pa, symbol=SYM2, side="buy", type="limit", qty=q2,
                    limit_price=s.px(ask2 * Decimal("0.7")))
    if r.error:
        raise Fail(f"ordine {SYM2} rifiutato: {short(r.data)}")
    open_before = await s.agent.ok("list_orders", portfolio_id=s.pa, status=sorted(OPEN))
    n_sym = sum(1 for o in open_before if o["symbol"] == SYM)
    c1 = await s.agent.ok("cancel_all_orders", portfolio_id=s.pa, symbol=SYM)
    left = await s.agent.ok("list_orders", portfolio_id=s.pa, status=sorted(OPEN))
    if c1["canceled"] != n_sym or any(o["symbol"] == SYM for o in left) or not left:
        raise Fail(f"annullati {c1['canceled']} di {n_sym}; aperti rimasti {len(left)}")
    c2 = await s.agent.ok("cancel_all_orders", portfolio_id=s.pa)
    rest = await s.agent.ok("list_orders", portfolio_id=s.pa, status=sorted(OPEN))
    if rest or c2["canceled"] != len(left):
        raise Fail(f"seconda passata: annullati {c2['canceled']}, aperti {len(rest)}")
    return f"{c1['canceled']} annullati su {SYM}, poi {c2['canceled']} su tutto il portafoglio"


@test("contante insufficiente", "rifiuto INSUFFICIENT_BUYING_POWER")
async def t_cash(s: State) -> str:
    return expect_code(await place(s, s.pb, symbol=SYM, side="buy", type="market",
                                   notional="200"), "INSUFFICIENT_BUYING_POWER")


@test("oltre il limite per ordine", "rifiuto RISK_LIMIT_EXCEEDED")
async def t_risk(s: State) -> str:
    return expect_code(await place(s, s.pa, symbol=SYM, side="buy", type="market",
                                   notional="400"), "RISK_LIMIT_EXCEEDED")


@test("vendita allo scoperto", "rifiuto SHORT_NOT_ALLOWED")
async def t_short(s: State) -> str:
    return expect_code(await place(s, s.pa, symbol=SYM2, side="sell", type="market",
                                   qty=await qty_for(s, Decimal(10), SYM2)), "SHORT_NOT_ALLOWED")


@test("simbolo non consentito dal profilo", "rifiuto SYMBOL_NOT_ALLOWED")
async def t_symbol(s: State) -> str:
    return expect_code(await place(s, s.pb, symbol=SYM2, side="buy", type="market",
                                   notional="10"), "SYMBOL_NOT_ALLOWED")


@test("mercato chiuso", "ordine a mercato accodato (queued) fino all'apertura")
async def t_closed(s: State) -> str:
    st = await s.agent.ok("get_market_status", market="NASDAQ")
    st = st[0] if isinstance(st, list) else st
    if st.get("is_open"):
        raise NotApplicable("NASDAQ è aperto ora: il caso del mercato chiuso non si può provare")
    r = await place(s, s.pa, symbol=STOCK, side="buy", type="market", qty="0.1")
    if r.error:
        raise Fail(f"rifiutato {r.code}: {short(r.data)}")
    if r.data["status"] != "queued":
        raise Fail(f"stato {r.data['status']}")
    await s.agent.ok("cancel_order", order_id=r.data["id"])
    return f"accodato (queued), prossima apertura {str(st.get('next_open'))[:16]}; poi annullato"


@test("chiusura straordinaria (Task 2)", "il giorno chiuso a mano è saltato da get_market_status")
async def t_closure(s: State) -> str:
    before = await s.agent.ok("get_market_status", market="NASDAQ")
    before = before[0] if isinstance(before, list) else before
    nxt = datetime.fromisoformat(str(before["next_open"]))
    c = await s.admin.post("/api/market-closures", {
        "market": "NASDAQ", "day": nxt.date().isoformat(), "kind": "closed",
        "note": "batteria e2e"})
    s.closure_id = c["id"]
    after = await s.agent.ok("get_market_status", market="NASDAQ")
    after = after[0] if isinstance(after, list) else after
    nxt2 = datetime.fromisoformat(str(after["next_open"]))
    await s.admin.delete(f"/api/market-closures/{c['id']}")
    s.closure_id = None
    if nxt2.date() <= nxt.date():
        raise Fail(f"prossima apertura invariata: {nxt2.date()}")
    return f"apertura {nxt.date()} chiusa a mano → prossima {nxt2.date()}; chiusura poi rimossa"


@test("LSE in get_market_status", "presente, solo orari")
async def t_lse(s: State) -> str:
    st = await s.agent.ok("get_market_status", market="LSE")
    st = st[0] if isinstance(st, list) else st
    if st.get("market") != "LSE":
        raise Fail(short(st))
    return f"LSE aperto={st.get('is_open')}, prossima apertura {str(st.get('next_open'))[:16]}"


@test("kill switch attivo", "apertura rifiutata KILL_SWITCH_ACTIVE, chiusura consentita")
async def t_kill(s: State) -> str:
    await s.admin.post("/api/kill-switch", {"on": True})
    try:
        got = expect_code(await place(s, s.pa, symbol=SYM, side="buy", type="market",
                                      notional="10"), "KILL_SWITCH_ACTIVE")
        q = s.qty(await position(s, s.pa) / 4)
        r = await place(s, s.pa, symbol=SYM, side="sell", type="market", qty=q)
        if r.error:
            raise Fail(f"chiusura rifiutata: {r.code}")
        o = await wait_status(s, r.data["id"], {"filled", "rejected"}, 30)
        if o["status"] != "filled":
            raise Fail(f"chiusura {o['status']}")
    finally:
        await s.admin.post("/api/kill-switch", {"on": False})
    return got.split(":")[0] + "; vendita di riduzione eseguita"


@test("blocco aperture (trading halt)", "apertura rifiutata TRADING_HALTED, chiusura consentita")
async def t_halt(s: State) -> str:
    h = await s.admin.post("/api/halts", {"scope": "portfolio", "portfolio_id": s.pa,
                                          "reason": "batteria e2e"})
    try:
        got = expect_code(await place(s, s.pa, symbol=SYM, side="buy", type="market",
                                      notional="10"), "TRADING_HALTED")
        q = s.qty(await position(s, s.pa) / 4)
        r = await place(s, s.pa, symbol=SYM, side="sell", type="market", qty=q)
        if r.error:
            raise Fail(f"chiusura rifiutata: {r.code}")
        o = await wait_status(s, r.data["id"], {"filled", "rejected"}, 30)
        if o["status"] != "filled":
            raise Fail(f"chiusura {o['status']}")
    finally:
        await s.admin.post(f"/api/halts/{h['halt']['id']}/clear")
    return got.split(":")[0] + "; vendita di riduzione eseguita"


@test("vendita totale", "posizione chiusa (quantità 0)")
async def t_sell_all(s: State) -> str:
    before = await position(s, s.pa)
    o = await s.agent.ok("close_position", portfolio_id=s.pa, symbol=SYM)
    o = await wait_status(s, o["id"], {"filled", "rejected"}, 30)
    after = await position(s, s.pa)
    if o["status"] != "filled" or after != 0:
        raise Fail(f"stato {o['status']}, posizione {after}")
    return f"venduti {o['filled_qty']}, posizione {before}→0"


@test("ordine manuale admin su simulato", "anteprima ok, poi eseguito senza TOTP")
async def t_admin_sim(s: State) -> str:
    body = {"symbol": SYM, "side": "buy", "type": "market", "notional": "20"}
    pv = await s.admin.post(f"/api/portfolios/{s.pa}/orders/preview", body)
    if pv.get("ok") is not True or pv.get("real"):
        raise Fail(f"anteprima {short(pv)}")
    o = await s.admin.post(f"/api/portfolios/{s.pa}/orders", body, idem=True)
    if o["status"] != "filled" or o["source"] != "admin":
        raise Fail(f"stato {o['status']} fonte {o['source']}")
    return f"anteprima ok, eseguito {o['filled_qty']} (fonte admin)"


@test("broker eToro demo finto", "market buy e vendita parziale con chiusura FIFO")
async def t_etoro_demo(s: State) -> str:
    raise NotApplicable(
        "lo stack e2e non ha un broker demo finto: un collegamento etoro_demo parlerebbe con "
        "l'API vera di eToro; lo stesso protocollo eToro è provato sotto sul conto reale finto")


@test("eToro reale finto: test e prova a secco", "test in sola lettura ok, prova a secco avviata")
async def t_live_dry(s: State) -> str:
    t = await s.admin.post(f"/api/live-links/{s.link}/test")
    dr = await s.admin.post(f"/api/live-links/{s.link}/dry-run", {"on": True})
    if dr.get("live_state") != "dry_run":
        raise Fail(f"stato {dr.get('live_state')}")
    await s.admin.put(f"/api/portfolios/{s.pr}/live-link",
                      {"link_id": s.link, "totp": await s.admin.totp.fresh()})
    return f"test ok (vuoto={t.get('empty')}), prova a secco attiva, portafoglio collegato (TOTP)"


@test("prova a secco: ordine dell'agente",
      "per l'agente un rifiuto «normale» del broker; per l'umano LIVE_DRY_RUN")
async def t_live_dry_agent(s: State) -> str:
    r = await place(s, s.pr, symbol=SYM, side="buy", type="market", qty=await qty_for(s, Decimal(20)))
    if r.error:
        agent_code, oid = r.code, (r.data.get("details") or {}).get("order_id")
    else:
        o = await wait_status(s, r.data["id"], {"rejected", "filled", "canceled"}, 90)
        agent_code, oid = o["reject_code"], o["id"]
    if oid is None:
        raise Fail(f"nessun ordine: {short(r.data)}")
    rows = await s.admin.get(f"/api/portfolios/{s.pr}/orders")
    human = next((o for o in rows if o["id"] == oid), None)
    if human is None:
        raise Fail("ordine non visibile all'admin")
    if FINGERPRINT.search(json.dumps(await order(s, oid))):
        raise Fail("l'agente vede parole da simulazione")
    if agent_code == "LIVE_DRY_RUN" or human["reject_code"] != "LIVE_DRY_RUN":
        raise Fail(f"agente {agent_code}, umano {human['reject_code']}")
    return f"agente: {agent_code}; umano: LIVE_DRY_RUN"


@test("ordine manuale admin su reale (prova a secco)", "senza TOTP rifiutato; con TOTP LIVE_DRY_RUN")
async def t_admin_dry(s: State) -> str:
    body = {"symbol": SYM, "side": "buy", "type": "market", "qty": str(await qty_for(s, Decimal(20)))}
    pv = await s.admin.post(f"/api/portfolios/{s.pr}/orders/preview", body)
    if not pv.get("real"):
        raise Fail(f"anteprima non segnata reale: {short(pv)}")
    try:
        await s.admin.post(f"/api/portfolios/{s.pr}/orders", body, idem=True)
        raise Fail("accettato senza TOTP")
    except ApiError as e:
        no_totp = e.code
    o = await s.admin.post(f"/api/portfolios/{s.pr}/orders",
                           {**body, "totp": await s.admin.totp.fresh()}, idem=True)
    rows = await s.admin.get(f"/api/portfolios/{s.pr}/orders")
    deadline = time.monotonic() + 90
    while True:
        cur = next(x for x in rows if x["id"] == o["id"])
        if cur["status"] not in OPEN or time.monotonic() > deadline:
            break
        await asyncio.sleep(2)
        rows = await s.admin.get(f"/api/portfolios/{s.pr}/orders")
    if cur["reject_code"] != "LIVE_DRY_RUN":
        raise Fail(f"senza TOTP {no_totp}; con TOTP {cur['status']} {cur['reject_code']}")
    return f"anteprima «reale»; senza TOTP {no_totp}; con TOTP rifiutato LIVE_DRY_RUN"


@test("eToro reale finto: attivazione (TOTP)", "collegamento armato")
async def t_arm(s: State) -> str:
    a = await s.admin.post(f"/api/live-links/{s.link}/arm", {
        "totp": await s.admin.totp.fresh(), "confirm_name": s.link_name, "accept_risks": True,
        "dedicated_account": True, "limits_set": True})
    if a.get("live_state") != "armed":
        raise Fail(f"stato {a.get('live_state')}")
    return "armato"


@test("eToro reale finto: limit seguito da SMTrading", "accettato, in attesa, annullabile")
async def t_live_limit(s: State) -> str:
    _, ask = await quote(s)
    r = await place(s, s.pr, symbol=SYM, side="buy", type="limit",
                    qty=await qty_for(s, Decimal(20)), limit_price=s.px(ask * Decimal("0.9")))
    if r.error or r.data.get("status") != "pending" or r.data.get("type") != "limit":
        raise Fail(short(r.data))
    c = await s.agent.ok("cancel_order", order_id=r.data["id"])
    if c.get("status") != "canceled":
        raise Fail(f"annullo: {short(c)}")
    return "limit accettato in attesa (seguito da SMTrading), poi annullato"


@test("eToro reale finto: due market buy", "due posizioni aperte presso il broker")
async def t_live_buys(s: State) -> str:
    got = []
    for _ in range(2):
        r = await place(s, s.pr, symbol=SYM, side="buy", type="market",
                        qty=await qty_for(s, Decimal(20)))
        if r.error:
            raise Fail(f"rifiutato {r.code}: {short(r.data)}")
        o = await wait_status(s, r.data["id"], {"filled", "rejected", "canceled"}, 120)
        if o["status"] != "filled":
            raise Fail(f"stato {o['status']} {o.get('reject_code')}")
        got.append(o["filled_qty"])
    return f"eseguiti {got[0]} e {got[1]} (prezzo del broker finto)"


@test("eToro reale finto: vendita parziale (FIFO)", "eseguita, chiude la prima posizione e parte della seconda")
async def t_live_fifo(s: State) -> str:
    held = await position(s, s.pr)
    q = s.qty(held * Decimal("0.75"))
    o = await s.agent.ok("close_position", portfolio_id=s.pr, symbol=SYM, qty=q)
    o = await wait_status(s, o["id"], {"filled", "rejected", "canceled"}, 120)
    after = await position(s, s.pr)
    if o["status"] != "filled" or after != held - q:
        raise Fail(f"stato {o['status']} {o.get('reject_code')}, posizione {held}→{after}")
    return f"venduti {q} di {held}, resta {after}"


@test("ordine manuale admin su reale armato (TOTP)", "anteprima ok, eseguito presso il broker finto")
async def t_admin_live(s: State) -> str:
    body = {"symbol": SYM, "side": "buy", "type": "market", "qty": str(await qty_for(s, Decimal(20)))}
    pv = await s.admin.post(f"/api/portfolios/{s.pr}/orders/preview", body)
    if pv.get("ok") is not True or not pv.get("real"):
        raise Fail(f"anteprima {short(pv)}")
    o = await s.admin.post(f"/api/portfolios/{s.pr}/orders",
                           {**body, "totp": await s.admin.totp.fresh()}, idem=True)
    cur = await wait_status(s, o["id"], {"filled", "rejected", "canceled"}, 120)
    if cur["status"] != "filled":
        raise Fail(f"stato {cur['status']} {cur.get('reject_code')}")
    return f"anteprima ok, eseguito {cur['filled_qty']}"


@test("eToro reale finto: chiusura di tutte le posizioni", "posizione a zero, conto di nuovo vuoto")
async def t_live_close_all(s: State) -> str:
    orders = await s.agent.ok("close_all_positions", portfolio_id=s.pr)
    for o in orders:
        await wait_status(s, o["id"], {"filled", "rejected", "canceled"}, 120)
    left = await position(s, s.pr)
    if left != 0:
        raise Fail(f"posizione rimasta {left}")
    return f"{len(orders)} ordini di chiusura eseguiti, posizione 0"


@test("disattiva denaro reale", "un clic, senza TOTP; nuovo ordine dell'agente rifiutato")
async def t_disarm(s: State) -> str:
    dis = await s.admin.post(f"/api/live-links/{s.link}/disarm")
    if dis.get("live_state") == "armed":
        raise Fail("ancora armato")
    r = await place(s, s.pr, symbol=SYM, side="buy", type="market", qty=await qty_for(s, Decimal(20)))
    if not r.error:
        o = await wait_status(s, r.data["id"], {"rejected", "filled", "canceled"}, 90)
        if o["status"] != "rejected":
            raise Fail(f"ordine {o['status']} dopo la disattivazione")
        code = o["reject_code"]
    else:
        code = r.code
    return f"stato {dis.get('live_state')}; ordine rifiutato ({code})"


@test("ogni ordine in get_order, list_orders e nel feed",
      "tutti gli ordini dell'agente ritrovati, ciascuno con la sua voce nel feed")
async def _feed_text(s: State) -> str:
    """The data of every feed item since the battery's start (settled rows only)."""
    feed: list[dict] = []
    after = None
    while True:  # oldest first from the battery's start
        page = await s.agent.ok("get_activity_feed", since=s.t0.isoformat(), after_id=after,
                                limit=200)
        feed += page["items"]
        if not page["items"] or page.get("next_after_id") in (None, after):
            break
        after = page["next_after_id"]
    return json.dumps([it.get("data") for it in feed], default=str)


async def t_all_orders(s: State) -> str:
    deadline = time.monotonic() + 30  # the feed shows a row once it has settled
    in_feed = await _feed_text(s)
    while any(o not in in_feed for o in s.agent.orders) and time.monotonic() < deadline:
        await asyncio.sleep(3)
        in_feed = await _feed_text(s)
    listed_ids: set[str] = set()
    for pid in {s.pa, s.pb, s.pr}:
        listed_ids |= {o["id"] for o in await s.agent.ok("list_orders", portfolio_id=pid,
                                                         limit=500)}
    missing_list = [o for o in s.agent.orders if o not in listed_ids]
    missing_feed = [o for o in s.agent.orders if o not in in_feed]
    seen = {oid: await order(s, oid) for oid in s.agent.orders}
    if missing_list or missing_feed:
        what = ", ".join(f"{seen[o]['type']} {seen[o]['side']} {seen[o]['status']} "
                         f"{seen[o].get('reject_code') or ''}".strip()
                         for o in missing_list + missing_feed)
        raise Fail(f"{len(missing_list)} assenti da list_orders, {len(missing_feed)} dal feed "
                   f"(su {len(s.agent.orders)}): {what}")
    return f"{len(s.agent.orders)} ordini: tutti in get_order, list_orders e nel feed"


@test("nessuna parola da simulazione per l'agente", "mcp_hide_mode: nessuna traccia nelle risposte")
async def t_fingerprints(s: State) -> str:
    hits = sorted({m.group(0).lower() for text in s.agent.outputs
                   for m in FINGERPRINT.finditer(text)})
    if hits:
        raise Fail(f"trovate: {', '.join(hits)}")
    return f"{len(s.agent.outputs)} risposte controllate, nessuna traccia"


@test("notifiche Telegram nell'outbox", "eseguiti/rifiutati accodati, niente inviato davvero")
async def t_outbox(s: State) -> str:
    rows = (await s.admin.get("/api/telegram/outbox", params={"limit": 200}))["items"]
    recent = [r for r in rows if r["created_at"] >= s.t0.isoformat()[:19]]
    sent = [r for r in recent if r["status"] == "sent"]
    if not recent:
        raise Fail("nessuna notifica generata")
    if sent:
        raise Fail(f"{len(sent)} risultano inviate")
    by_status: dict[str, int] = {}
    for r in recent:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    return f"{len(recent)} notifiche generate ({by_status}), nessuna inviata"


@test("log_decision con esito, oggetto e motivo", "salvata e riletta con outcome/subject/reason")
async def t_decision(s: State) -> str:
    s.decision_subject = f"Ribilanciamento BTC {uuid.uuid4().hex[:4]}"
    r = await s.agent.ok("log_decision", kind="allocation", summary="Riduco BTC al 5%",
                         portfolio_id=s.pa, symbols=[SYM], outcome="approvata",
                         subject=s.decision_subject, reason="volatilità alta")
    got = await s.agent.ok("get_decisions", portfolio_id=s.pa, limit=5)
    dec = next((x for x in got["decisions"] if x["id"] == r["decision_id"]), None)
    if not dec or (dec["outcome"], dec["subject"], dec["reason"]) != (
            "approvata", s.decision_subject, "volatilità alta"):
        raise Fail(short(dec))
    return "decisione salvata: approvata, oggetto e motivo presenti"


@test("riepilogo giornaliero", "«Decisioni di oggi» con la decisione e gli obiettivi")
async def t_summary(s: State) -> str:
    from smtrading.common.db import get_sessionmaker
    from smtrading.telegram import summaries

    async with get_sessionmaker()() as session:
        text = await summaries.build_summary(session, "daily", datetime.now(UTC))
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    if "Decisioni di oggi" not in text or s.decision_subject not in text:
        raise Fail("manca la sezione o la decisione: " + short(text, 120))
    if "biettivo" not in text:
        raise Fail("mancano gli obiettivi (riga «Obiettivo …»)")
    return "sezione «Decisioni di oggi» con la decisione, obiettivi presenti"


@test("registro attività", "azioni admin registrate (ordini, kill switch, blocchi, chiusure)")
async def t_activity(s: State) -> str:
    rows = await s.admin.get("/api/activity-log", params={"limit": 200})
    recent = [r for r in rows if r["ts"] >= s.t0.isoformat()[:19]]
    routes = " ".join(f"{r['method']} {r['route']}" for r in recent)
    want = {"ordine manuale": "/orders", "kill switch": "/kill-switch", "blocco": "/halts",
            "chiusura": "/market-closures", "attivazione": "/arm"}
    missing = [k for k, frag in want.items() if frag not in routes]
    if missing:
        raise Fail("mancano: " + ", ".join(missing))
    return f"{len(recent)} azioni registrate, tutte le attese presenti"


@test("pre-apertura eseguita a mano", "✅ oppure un elenco di problemi coerente")
async def t_preopen(s: State) -> str:
    from zoneinfo import ZoneInfo

    import redis.asyncio as aioredis

    from smtrading.common.config import get_settings
    from smtrading.common.db import get_sessionmaker
    from smtrading.engine.quotesource import MemoQuoteSource  # noqa: F401
    from smtrading.marketdata.router import MarketDataRouter  # noqa: F401
    from smtrading.telegram import preopen

    settings = get_settings()
    redis = aioredis.from_url(settings.redis_url)
    try:
        qs = _BatteryQuotes(s)
        report = await preopen.build_report(get_sessionmaker(), qs, redis, datetime.now(UTC),
                                            ZoneInfo(settings.tz))
    finally:
        await redis.aclose()
    text = preopen.render_report(report)
    if report.problems:
        return f"{len(report.problems)} problemi: " + short("; ".join(report.problems), 140)
    if "✅" not in text:
        raise Fail(short(text, 120))
    return "✅ " + short(text.splitlines()[0], 100)


class _BatteryQuotes:
    """The quote source of the pre-open check, through the agent's get_quote (MCP)."""

    def __init__(self, s: State) -> None:
        self.s = s

    async def get_quotes(self, symbols: list[str], *, max_age_s: int = 5) -> dict:
        from smtrading.marketdata.types import Quote

        raw = await self.s.agent.ok("get_quote", symbols=symbols)
        out = {}
        for sym, q in raw.items():
            if "error" in q:
                continue
            out[sym] = Quote(symbol=sym, bid=_dn(q.get("bid")), ask=_dn(q.get("ask")),
                             last=_dn(q.get("last")), ts=datetime.fromisoformat(q["ts"]),
                             provider=str(q.get("provider") or "mcp"))
        return out

    async def get_fx_rate(self, base: str, quote: str) -> Decimal:
        return d((await self.s.agent.ok("get_fx_rate", base=base, quote=quote))["rate"])

    async def get_calendar(self, symbols, start, end) -> list:
        return []


def _dn(v: Any) -> Decimal | None:
    return None if v is None else d(v)


# --- main -------------------------------------------------------------------------------------


async def outbox_mark(s: State) -> int:
    try:
        items = (await s.admin.get("/api/telegram/outbox", params={"limit": 1}))["items"]
    except Exception:  # noqa: BLE001
        return -1
    return items[0]["id"] if items else 0


async def outbox_since(s: State, mark: int) -> int | None:
    """The notifications queued after `mark` (a short wait for the post-commit ones)."""
    if mark < 0:
        return None
    await asyncio.sleep(1)
    try:
        items = (await s.admin.get("/api/telegram/outbox", params={"limit": 200}))["items"]
    except Exception:  # noqa: BLE001
        return None
    return sum(1 for it in items if it["id"] > mark)


async def cleanup(s: State) -> None:
    """Best effort: the kill switch off and a leftover manual closure removed."""
    calls = [lambda: s.admin.post("/api/kill-switch", {"on": False})]
    if s.closure_id is not None:
        calls.append(lambda: s.admin.delete(f"/api/market-closures/{s.closure_id}"))
    for call in calls:
        try:
            await call()
        except Exception as e:  # noqa: BLE001
            print(f"pulizia non riuscita: {short(str(e))}", file=sys.stderr)


async def run(out: str) -> int:
    email = os.environ["SMT_ADMIN_EMAIL"]
    password = os.environ["SMT_ADMIN_PASSWORD"]
    totp = Totp(os.environ["SMT_E2E_TOTP_SECRET"])
    feed = Feed(out, len(TESTS))
    s = State(Admin(totp))
    try:
        await s.admin.login(email, password)
        await setup(s)
    except Exception as e:  # noqa: BLE001
        feed.result("preparazione (admin, agente, portafogli)", "pronta",
                    f"non riuscita: {short(str(e), 200)}", False)
        feed.done()
        return 1
    token = s.ids.pop("token")
    async with (
        create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http,
        streamable_http_client(MCP_URL, http_client=http) as (read, write),
        ClientSession(read, write) as cs,
    ):
        await cs.initialize()
        s.agent = Agent(cs)
        info = await s.agent.ok("get_instrument", symbol=SYM)
        s.tick, s.lot = d(info.get("tick_size") or "0.01"), d(info.get("lot_size") or "0.00000001")
        try:
            for name, expected, fn in TESTS:
                mark = await outbox_mark(s)
                try:
                    got, ok, note = await asyncio.wait_for(fn(s), 300), True, ""
                except NotApplicable as e:
                    got, ok, note = "non applicabile", None, str(e)
                except Fail as e:
                    got, ok, note = str(e), False, ""
                except ApiError as e:
                    got, ok, note = f"errore API {e.status}: {short(e.body, 140)}", False, ""
                except Exception as e:  # noqa: BLE001
                    got, ok, note = f"errore: {type(e).__name__}: {short(str(e), 140)}", False, ""
                feed.result(name, expected, got, ok, note, telegram=await outbox_since(s, mark))
        finally:
            await cleanup(s)
    feed.done()
    return 0 if feed.failed == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="SMTrading order battery (e2e stack only)")
    parser.add_argument("--out", required=True, help="JSON lines, one per test")
    parser.add_argument("--only", help="comma-separated test numbers (debug)")
    args = parser.parse_args()
    if os.environ.get("SMT_LIVE_FAKE_BROKER") != "1":
        print("rifiutato: solo nello stack e2e (SMT_LIVE_FAKE_BROKER=1)", file=sys.stderr)
        sys.exit(2)
    if args.only:
        keep = {int(x) for x in args.only.split(",")}
        TESTS[:] = [t for i, t in enumerate(TESTS, 1) if i in keep]
    sys.exit(asyncio.run(run(args.out)))


if __name__ == "__main__":
    main()
