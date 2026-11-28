"""«Schermo TV» routes: each user's own area (admins and viewers), the admins' list and
revocation, the display link (valid, rotated, revoked, wrong scope, disabled user), the
screens' data, the sounds, the «Fiume dei flussi» node; against a fake Samsung TV on
127.0.0.1 (never the real TV; Wake-on-LAN never leaves the test)."""
# ruff: noqa: F811  (the `env` fixture of test_admin_users, imported then requested)

import asyncio
import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import httpx
import pytest
from fake_samsung_tv import FakeSamsungTv
from sqlalchemy import select
from test_admin_users import env  # noqa: F401  (the fixture)

from smtrading.agents.models import McpAuditLog
from smtrading.engine.models import Fill, Order, OrderSide, OrderStatus, OrderType
from smtrading.flow.cache import clear_all
from smtrading.instruments.models import AssetClass, Instrument
from smtrading.ledger.models import Journal, JournalType
from smtrading.portfolios.models import Portfolio
from smtrading.tv import display, link, samsung, sounds
from smtrading.tv import service as tvs
from smtrading.tv.models import TvDisplay, TvEvent, TvScreen
from smtrading.useractivity.models import UserActivity

HOST = "127.0.0.1"


@pytest.fixture(autouse=True)
def _loopback_tv(monkeypatch):
    """The fake TV lives on 127.0.0.1: accepted only through this test-only flag."""
    monkeypatch.setattr(samsung, "ALLOW_LOOPBACK", True)


@pytest.fixture
async def tv(monkeypatch):
    fake = await FakeSamsungTv().start()
    fake.point(monkeypatch)
    monkeypatch.setattr(tvs, "POWER_WAIT_S", 1.0)
    monkeypatch.setattr(tvs, "WOL_WAIT_S", 0.5)
    monkeypatch.setattr(tvs, "POLL_STEP_S", 0.1)
    monkeypatch.setattr(samsung, "LAUNCH_ACK_S", 0.5)
    yield fake
    await fake.stop()


@pytest.fixture(autouse=True)
def _no_wol(monkeypatch):
    """Wake-on-LAN packets are recorded, never sent (a broadcast would reach the LAN)."""
    sent: list[tuple[str, list]] = []
    monkeypatch.setattr(samsung, "wake_on_lan",
                        lambda mac, targets: sent.append((mac, targets)) or len(targets))
    clear_all()
    display._health_cache.clear()
    yield sent
    clear_all()


async def _client(env, role="admin"):
    email, user_id = await env.add_user(role)
    c = await env.client(email)
    c.headers["Host"] = "192.168.30.7"
    c.headers["X-Forwarded-Proto"] = "https"
    return c, email, user_id


def _token(url: str) -> str:
    assert "#token=" in url and "?" not in url  # a fragment: never sent to a server
    return url.split("#token=", 1)[1]


async def _page(env, token: str, *, expect: int = 200):
    """The TV page: posts the link from the fragment once, then lives on the cookie."""
    page = httpx.AsyncClient(transport=env.transport, base_url="https://192.168.30.7")
    env.clients.append(page)
    r = await page.post("/api/tv-display/session", json={"token": token})
    assert r.status_code == expect, r.text
    return page


async def _paired(c, tv) -> dict:
    r = await c.post("/api/tv/screens", json={"host": HOST})
    assert r.status_code == 200, r.text
    tv_id = r.json()["tvs"][0]["id"]
    r = await c.post(f"/api/tv/screens/{tv_id}/pair")
    assert r.status_code == 200, r.text
    assert r.json()["tvs"][0]["pairing"]["state"] == "waiting"
    for _ in range(100):
        area = (await c.get("/api/tv")).json()
        if area["tvs"][0]["pairing"]["state"] != "waiting":
            break
        await asyncio.sleep(0.05)
    return area["tvs"][0]


async def test_add_pair_and_send_wakes_the_tv_and_opens_the_link(env, tv, caplog):
    caplog.set_level(logging.DEBUG)
    c, _email, _user_id = await _client(env)
    area = (await c.get("/api/tv")).json()
    assert area["tvs"] == [] and area["link"] == {"active": False, "created_at": None,
                                                  "url": None, "reason": None}
    assert [s["key"] for s in area["screens"]] == ["flow", "campaign", "health"]
    assert area["display"] == {"screen": "campaign", "rotation": False, "interval_s": 30,
                               "version": 0, "updated_at": area["display"]["updated_at"],
                               "portal_url": None}
    assert area["sound"] == {"enabled": False, "volume": 40, "quiet_start": "22:00",
                             "quiet_end": "08:00", "tz": "Europe/Rome",
                             "quiet_now": area["sound"]["quiet_now"]}
    assert area["portal_url_detected"] == "https://192.168.30.7"
    assert area["can_discover"] is True and area["suggested_subnet"] == "192.168.30.0/24"

    t = await _paired(c, tv)
    assert t["name"] == '85" Crystal UHD' and t["model"] == "UE85DU7190UXZT"
    assert t["paired"] is True and t["pairing"]["state"] == "ok"
    assert tv.connections[-1]["token"] is None and tv.connections[-1]["name"] == "SMTrading"

    tv.power = "standby"
    r = await c.post(f"/api/tv/screens/{t['id']}/send", json={"screen": "health"})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True, body
    assert body["powered_on"] is True and body["confirmed"] is True
    assert body["app_id"] == "org.tizen.browser"
    assert body["url"].startswith("https://192.168.30.7/tv#token=tv-display.")
    assert tv.keys == ["KEY_POWER"] and tv.power == "on"
    assert tv.launches[-1]["metaTag"] == body["url"]
    area = (await c.get("/api/tv")).json()
    assert area["display"]["screen"] == "health" and area["link"]["url"] == body["url"]
    assert area["tvs"][0]["last_ok"] is True

    # the token never in a response, the audit, the activity log or a log
    token = next(iter(tv.tokens))
    texts = [r.text, (await c.get("/api/tv")).text,
             (await c.get("/api/tv/admin/screens")).text]
    async with env.sm() as s:
        texts += [str(a.params) for a in await s.scalars(select(McpAuditLog))]
        texts += [str(a.summary) for a in await s.scalars(select(UserActivity))]
        row = await s.scalar(select(TvScreen))
        assert token.encode() not in row.token_encrypted
    texts.append(caplog.text)
    assert not [t for t in texts if token in t]

    # the «Fiume dei flussi»: Database → Schermo TV, and the node's health
    ev = (await c.get("/api/flow/events")).json()["events"]
    [e] = [e for e in ev if e["kind"] == "tv"]
    assert e["path"] == ["db", "tv"] and e["hops"] == ["schermata Salute e avvisi"]
    assert e["outcome"] == "ok"
    nodes = {n["key"]: n for n in (await c.get("/api/flow/map")).json()["nodes"]}
    assert nodes["tv"]["label"] == "Schermo TV"
    assert (await c.get("/api/flow/health")).json()["nodes"]["tv"] == {
        "state": "ok", "reason": "1 TV collegata"}


async def test_a_screen_change_or_rotation_never_wakes_the_tv(env, tv, _no_wol):
    c, _, _user_id = await _client(env)
    t = await _paired(c, tv)
    tv.power = "standby"
    before = len(tv.connections)
    r = await c.put("/api/tv/display", json={"screen": "flow", "rotation": True,
                                             "interval_s": 60})
    assert r.status_code == 200, r.text
    assert r.json()["display"]["rotation"] is True and r.json()["display"]["version"] == 1
    assert len(tv.connections) == before and tv.keys == [] and _no_wol == []
    assert tv.power == "standby"
    for bad in ({"interval_s": 10}, {"interval_s": 301}, {"screen": "nope"}):
        r = await c.put("/api/tv/display", json=bad)
        assert r.status_code == 400, bad
    async with env.sm() as s:
        [e] = list(await s.scalars(select(TvEvent)))
        assert (e.kind, e.screen, e.rotation, e.tv_id) == ("display", "flow", True, None)
    assert t["paired"]


async def test_tv_off_or_unreachable_answers_fast_with_the_fallback_url(env, tv, _no_wol,
                                                                       monkeypatch):
    c, _, _ = await _client(env)
    t = await _paired(c, tv)
    tv.power, tv.power_key_works = "standby", False
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    body = (await c.post(f"/api/tv/screens/{t['id']}/send", json={})).json()
    assert body["ok"] is False and "non si è accesa" in body["message"]
    assert body["url"].startswith("https://192.168.30.7/tv#token=")
    assert _no_wol and _no_wol[0][0] == "02:00:5e:00:00:01"  # tried, never sent
    assert loop.time() - t0 < 10
    area = (await c.get("/api/tv")).json()
    assert area["tvs"][0]["last_ok"] is False
    assert (await c.get("/api/flow/health")).json()["nodes"]["tv"]["state"] == "attenzione"

    # nothing answers: still a quick, clear answer
    await tv.stop()
    t0 = loop.time()
    body = (await c.post(f"/api/tv/screens/{t['id']}/send", json={})).json()
    assert body["ok"] is False and "non raggiungibile" in body["message"]
    assert body["url"] and loop.time() - t0 < 10
    assert (await c.get(f"/api/tv/screens/{t['id']}/power")).json() == {"power": "unreachable"}


async def test_no_https_portal_address_no_display_url(env, tv):
    c, _, _ = await _client(env)
    t = await _paired(c, tv)
    before = (len(tv.connections), len(tv.launches))
    # the portal opened from localhost: no address the TV can reach
    c.headers["Host"] = "localhost:3000"
    c.headers["X-Forwarded-Proto"] = "http"
    r = await c.post(f"/api/tv/screens/{t['id']}/send", json={})
    assert r.status_code == 400 and r.json()["details"]["field"] == "portal_url"
    assert r.json()["message"] == tvs.NO_PORTAL
    area = (await c.post("/api/tv/link/rotate")).json()
    assert area["link"]["active"] is True and area["link"]["url"] is None
    assert area["link"]["reason"] == tvs.NO_PORTAL
    # a plain http address: the Secure cookie would never be kept, so no URL either
    c.headers["Host"] = "192.168.30.7"
    r = await c.post(f"/api/tv/screens/{t['id']}/send", json={})
    assert r.status_code == 400 and r.json()["details"]["field"] == "portal_url"
    assert r.json()["message"] == tvs.NOT_HTTPS and "https" in tvs.NOT_HTTPS
    area = (await c.get("/api/tv")).json()
    assert area["link"]["url"] is None and area["link"]["reason"] == tvs.NOT_HTTPS
    # a configured http portal address is refused the same way; https works
    area = (await c.put("/api/tv/display", json={"portal_url": "http://192.168.30.7"})).json()
    assert area["link"]["url"] is None and area["link"]["reason"] == tvs.NOT_HTTPS
    area = (await c.put("/api/tv/display", json={"portal_url": "https://smt.lan"})).json()
    assert area["link"]["url"].startswith("https://smt.lan/tv#token=tv-display.")
    assert area["link"]["reason"] is None
    assert (len(tv.connections), len(tv.launches)) == before  # the TV never contacted


async def test_only_home_network_addresses(env, tv, monkeypatch):
    c, _, _ = await _client(env)
    for bad in ("8.8.8.8", "100.64.0.1", "0.0.0.1", "192.0.0.5", "198.18.0.1",
                "169.254.1.1", "255.255.255.255", "192.168.1.255", "192.168.1.0",
                "172.32.0.1", "224.0.0.1", "nonsense", ""):
        r = await c.post("/api/tv/screens", json={"host": bad})
        assert r.status_code == 400, bad
        assert r.json()["details"]["field"] == "host"
    monkeypatch.setattr(samsung, "ALLOW_LOOPBACK", False)  # the application's value
    assert (await c.post("/api/tv/screens", json={"host": HOST})).status_code == 400
    assert (await c.post("/api/tv/discover", json={"subnet": "127.0.0.1/32"})
            ).status_code == 400
    for good in ("10.0.0.7", "172.16.4.2", "172.31.255.254", "192.168.30.84"):
        assert tvs.clean_host(good) == good


async def test_a_bad_mac_is_a_saved_failure_not_a_500(env, tv, monkeypatch):
    monkeypatch.setattr(samsung, "wake_on_lan",
                        lambda mac, targets: len(samsung.magic_packet(mac)))  # never sent
    c, _, _ = await _client(env)
    t = await _paired(c, tv)
    async with env.sm() as s:
        await s.execute(TvScreen.__table__.update().values(mac="not-a-mac"))
        await s.commit()
    tv.power, tv.power_key_works = "standby", False
    r = await c.post(f"/api/tv/screens/{t['id']}/send", json={})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False and "MAC" in r.json()["message"]
    t = (await c.get("/api/tv")).json()["tvs"][0]
    assert t["last_ok"] is False and "MAC" in t["last_message"]


async def test_refused_pairing_and_pairing_again_replaces_the_token(env, tv):
    c, _, _ = await _client(env)
    tv.mode = "refuse"
    t = await _paired(c, tv)
    assert t["paired"] is False and t["pairing"]["state"] == "refused"
    assert "rifiutato" in t["pairing"]["message"]
    tv.mode = "accept"
    r = await c.post(f"/api/tv/screens/{t['id']}/pair")
    for _ in range(100):
        t = (await c.get("/api/tv")).json()["tvs"][0]
        if t["pairing"]["state"] != "waiting":
            break
        await asyncio.sleep(0.05)
    assert t["paired"] is True
    async with env.sm() as s:
        first = tvs.decrypt_token((await s.scalar(select(TvScreen))).token_encrypted)
    # the TV forgot the pairing: «Collega la TV» again gives and stores a new token
    tv.tokens.clear()
    await c.post(f"/api/tv/screens/{t['id']}/pair")
    for _ in range(100):
        t = (await c.get("/api/tv")).json()["tvs"][0]
        if t["pairing"]["state"] != "waiting":
            break
        await asyncio.sleep(0.05)
    async with env.sm() as s:
        second = tvs.decrypt_token((await s.scalar(select(TvScreen))).token_encrypted)
    assert first and second and first != second and second in tv.tokens
    assert tv.connections[-1]["token"] is None  # pairing never sends the old token
    assert r.status_code == 200


async def test_each_user_has_their_own_area(env, tv):
    admin, admin_email, _admin_id = await _client(env)
    viewer, viewer_email, _viewer_id = await _client(env, "viewer")
    a = await _paired(admin, tv)
    r = await viewer.post("/api/tv/screens", json={"host": HOST})
    assert r.status_code == 200, r.text
    area = r.json()
    v_tv = area["tvs"][0]
    assert [t["id"] for t in area["tvs"]] == [v_tv["id"]] and v_tv["id"] != a["id"]
    assert [s["key"] for s in area["screens"]] == ["campaign", "health"]
    assert area["can_discover"] is False and area["suggested_subnet"] is None
    # never another user's TV
    for method, path in (("post", f"/api/tv/screens/{a['id']}/pair"),
                         ("post", f"/api/tv/screens/{a['id']}/send"),
                         ("post", f"/api/tv/screens/{a['id']}/power-off"),
                         ("delete", f"/api/tv/screens/{a['id']}"),
                         ("get", f"/api/tv/screens/{a['id']}/power")):
        r = await viewer.request(method.upper(), path, json={} if method != "get" else None)
        assert r.status_code == 404, (path, r.text)
    # a viewer may not pick the river, nor search the LAN
    r = await viewer.put("/api/tv/display", json={"screen": "flow"})
    assert r.status_code == 400 and "amministratori" in r.json()["message"]
    assert (await viewer.post("/api/tv/discover", json={"subnet": "127.0.0.1/32"})
            ).status_code == 403
    assert (await viewer.get("/api/tv/admin/screens")).status_code == 403
    # a viewer pairs and sends to their own TV
    await viewer.post(f"/api/tv/screens/{v_tv['id']}/pair")
    for _ in range(100):
        v_tv = (await viewer.get("/api/tv")).json()["tvs"][0]
        if v_tv["pairing"]["state"] != "waiting":
            break
        await asyncio.sleep(0.05)
    sent = (await viewer.post(f"/api/tv/screens/{v_tv['id']}/send", json={})).json()
    admin_url = (await admin.post("/api/tv/link/rotate")).json()["link"]["url"]
    assert sent["ok"] is True and sent["url"] != admin_url
    viewer_url = sent["url"]

    items = (await admin.get("/api/tv/admin/screens")).json()["items"]
    assert {(i["user_email"], i["paired"]) for i in items} == {(admin_email, True),
                                                               (viewer_email, True)}
    assert set(items[0]) == {"id", "user_id", "user_email", "name", "host", "model", "paired",
                             "paired_at", "last_used_at", "last_ok", "revoked_at",
                             "revoked_by", "created_at"}
    # an admin revokes the viewer's TV: pairing forgotten, the viewer's link locked out
    page = await _page(env, _token(viewer_url))
    assert (await page.get("/api/tv-display/selection")).status_code == 200
    r = await admin.post(f"/api/tv/admin/screens/{v_tv['id']}/revoke")
    assert r.status_code == 200 and r.json()["paired"] is False
    assert r.json()["revoked_by"] == admin_email
    assert (await page.get("/api/tv-display/selection")).status_code == 401
    v = (await viewer.get("/api/tv")).json()
    assert v["tvs"][0]["paired"] is False and v["link"]["active"] is False
    assert (await admin.get("/api/tv")).json()["link"]["url"] == admin_url  # untouched


async def test_discovery_is_for_admins_and_finds_the_tv(env, tv):
    c, _, _ = await _client(env)
    r = await c.post("/api/tv/discover", json={"subnet": "127.0.0.1/32"})
    assert r.status_code == 200, r.text
    assert r.json() == {"subnet": "127.0.0.1/32", "devices": [
        {"host": HOST, "name": '85" Crystal UHD', "model": "UE85DU7190UXZT",
         "resolution": "3840x2160", "mac": "02:00:5e:00:00:01", "power": "on"}]}
    assert (await c.post("/api/tv/discover", json={"subnet": "10.0.0.0/8"})).status_code == 400
    c.headers["Host"] = "test"  # no network known: never a guess
    assert (await c.post("/api/tv/discover", json={})).status_code == 400


async def test_display_link_valid_rotated_revoked_wrong_scope(env, tv):
    c, _, user_id = await _client(env)
    url = (await c.post("/api/tv/link/rotate")).json()["link"]["url"]
    token = _token(url)
    bare = httpx.AsyncClient(transport=env.transport, base_url="https://192.168.30.7")
    env.clients.append(bare)
    # the query form opens nothing (a token in a URL would land in access logs)
    for path in ("selection", "campaign", "health", "flow"):
        r = await bare.get(f"/api/tv-display/{path}?token={token}")
        assert r.status_code == 401, path
        assert "set-cookie" not in r.headers
    # the session call sets the cookie: HttpOnly, Secure, SameSite=Strict, own path
    tvc = httpx.AsyncClient(transport=env.transport, base_url="https://192.168.30.7")
    env.clients.append(tvc)
    r = await tvc.post("/api/tv-display/session", json={"token": token})
    assert r.status_code == 200 and r.json() == {"ok": True}
    cookie = r.headers["set-cookie"].lower()
    for part in ("smt_tv=", "httponly", "secure", "samesite=strict",
                 "path=/api/tv-display"):
        assert part in cookie, part
    r = await tvc.get("/api/tv-display/selection")
    assert r.status_code == 200, r.text
    sel = r.json()
    assert set(sel) == {"screen", "rotation", "interval_s", "version", "updated_at",
                        "screens", "poll_s", "now", "sound", "sound_events", "sound_cursor"}
    assert sel["screens"] == ["flow", "campaign", "health"] and sel["poll_s"] == 4
    assert (await tvc.get("/api/tv-display/campaign")).status_code == 200
    # nothing else of the portal
    assert (await tvc.get("/api/portfolios")).status_code == 401
    assert (await tvc.get("/api/tv")).status_code == 401
    # wrong scope with the right link id, junk, or nothing: refused, no cookie
    async with env.sm() as s:
        d = await s.get(TvDisplay, user_id)
        secret = link.decrypt_secret(d.link_secret_encrypted)
    for bad in (link.sign("admin", d.link_id, secret), "x.y.z", ""):
        r = await bare.post("/api/tv-display/session", json={"token": bad})
        assert r.status_code == 401 and "set-cookie" not in r.headers, bad
        assert "link della TV" in r.json()["message"]
    assert (await bare.post("/api/tv-display/session", json={})).status_code == 400
    # rotation: the cookie holding the old link stops at the next request
    new_url = (await c.post("/api/tv/link/rotate")).json()["link"]["url"]
    assert new_url != url
    assert (await tvc.get("/api/tv-display/selection")).status_code == 401
    await _page(env, token, expect=401)
    page = await _page(env, _token(new_url))
    assert (await page.get("/api/tv-display/health")).status_code == 200
    # revocation: the cookie set before is refused at once
    assert (await c.post("/api/tv/link/revoke")).json()["link"]["active"] is False
    assert (await page.get("/api/tv-display/health")).status_code == 401
    # a disabled user's link opens nothing
    page = await _page(env, _token((await c.post("/api/tv/link/rotate")).json()["link"]["url"]))
    assert (await page.get("/api/tv-display/selection")).status_code == 200
    from smtrading.admin.models import AdminUser
    async with env.sm() as s:
        (await s.get(AdminUser, user_id)).disabled_at = datetime.now(UTC)
        await s.commit()
    assert (await page.get("/api/tv-display/selection")).status_code == 401


async def test_screens_data_and_what_a_viewer_sees(env, tv):
    admin, _, _ = await _client(env)
    viewer, _, _ = await _client(env, "viewer")
    a = await _page(env, _token((await admin.post("/api/tv/link/rotate")).json()["link"]["url"]))
    v = await _page(env, _token((await viewer.post("/api/tv/link/rotate")).json()["link"]["url"]))

    flow = (await a.get("/api/tv-display/flow")).json()
    assert set(flow) == {"screen", "now", "map", "summary", "health", "events", "cursor"}
    assert {n["key"] for n in flow["map"]["nodes"]} >= {"tv", "db", "mcp"}
    assert set(flow["summary"]) == {"links", "nodes", "head"}
    r = await v.get("/api/tv-display/flow")
    assert r.status_code == 403  # the river is for admins only, like its page

    camp = (await v.get("/api/tv-display/campaign")).json()
    assert set(camp) == {"screen", "now", "campaign", "equity", "goal", "portfolios",
                         "positions", "fills", "notes"}
    assert {"name", "day", "total_days", "state", "start", "end"} <= set(camp["campaign"])

    ah = (await a.get("/api/tv-display/health")).json()
    vh = (await v.get("/api/tv-display/health")).json()
    assert set(ah) == {"screen", "now", "status", "checked_at", "sections", "nodes", "alerts"}
    assert ah["nodes"] and vh["nodes"] == []
    assert set(ah["nodes"][0]) == {"key", "label", "kind", "state", "reason"}
    assert set(ah["sections"][0]) == {"key", "label", "status", "summary"}
    assert set(ah["alerts"]) == {"on", "open", "recent"}
    # the viewer's selection never offers the river, even if stored before
    async with env.sm() as s:
        await s.execute(TvDisplay.__table__.update().values(screen="flow"))
        await s.commit()
    sel = (await v.get("/api/tv-display/selection")).json()
    assert sel["screen"] == "campaign" and sel["screens"] == ["campaign", "health"]


async def _fill(sm, *, role=None, otype=OrderType.market, side=OrderSide.buy, at):
    async with sm() as s:
        sym = f"TV{uuid.uuid4().hex[:6].upper()}"
        s.add(Instrument(symbol=sym, name=sym, asset_class=AssetClass.us_equity,
                         market="US", currency="USD"))
        p = Portfolio(name=f"TV-{uuid.uuid4().hex[:8]}", in_totals=True)
        s.add(p)
        await s.flush()
        o = Order(portfolio_id=p.id, symbol=sym, side=side, type=otype, qty=D(1),
                  status=OrderStatus.filled, role=role, source="agent",
                  stop_price=D(90) if otype != OrderType.market else None)
        s.add(o)
        h = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        j = Journal(type=JournalType.TRADE, author_type="system", prev_hash=h,
                    hash=hashlib.sha256(h.encode()).hexdigest())
        s.add(j)
        await s.flush()
        f = Fill(order_id=o.id, portfolio_id=p.id, symbol=sym, side=side, qty=D(1),
                 price=D(100), fee_currency="USD", journal_id=j.id, created_at=at,
                 source="sim")
        s.add(f)
        await s.commit()
        return f.id


async def test_sounds_settings_and_events_with_stable_ids(env, tv):
    c, _, _ = await _client(env)
    r = await c.put("/api/tv/display", json={"sound_enabled": True, "sound_volume": 70,
                                             "quiet_start": "23:30", "quiet_end": "07:00"})
    assert r.status_code == 200, r.text
    assert {k: r.json()["sound"][k] for k in ("enabled", "volume", "quiet_start",
                                               "quiet_end")} == {
        "enabled": True, "volume": 70, "quiet_start": "23:30", "quiet_end": "07:00"}
    for bad in ({"sound_volume": 101}, {"quiet_start": "25:00"}):
        assert (await c.put("/api/tv/display", json=bad)).status_code == 400, bad
    bare = await _page(env, _token((await c.post("/api/tv/link/rotate")).json()["link"]["url"]))
    first = (await bare.get("/api/tv-display/selection")).json()
    assert first["sound_events"] == [] and first["sound"]["enabled"] is True
    cursor = first["sound_cursor"]
    now = datetime.now(UTC)
    buy = await _fill(env.sm, at=now)
    sell = await _fill(env.sm, side=OrderSide.sell, at=now)
    stop = await _fill(env.sm, role="stop_loss", otype=OrderType.stop, side=OrderSide.sell,
                       at=now)
    target = await _fill(env.sm, role="take_profit", otype=OrderType.limit,
                         side=OrderSide.sell, at=now)
    sel = (await bare.get("/api/tv-display/selection",
                          params={"after": cursor})).json()
    kinds = {e["id"]: e["kind"] for e in sel["sound_events"] if e["id"].startswith("fill:")}
    # what was already red at the first look is the baseline: no sound for it
    assert not [e for e in sel["sound_events"] if e["kind"] == "health_bad"]
    assert kinds == {f"fill:{buy}": "fill_buy", f"fill:{sell}": "fill_sell",
                     f"fill:{stop}": "stop", f"fill:{target}": "target"}
    assert set(sel["sound_events"][0]) == {"id", "kind", "side", "symbol", "at"}
    # sounds off: no events
    await c.put("/api/tv/display", json={"sound_enabled": False})
    sel = (await bare.get("/api/tv-display/selection",
                          params={"after": cursor})).json()
    assert sel["sound_events"] == []


def test_health_incidents_once_per_incident_and_back_to_ok():
    t0 = datetime(2026, 11, 28, 10, tzinfo=UTC)
    state: dict = {}
    red = {"sections": {"backup": {"status": "problema", "label": "Backup"}}}
    ok = {"sections": {"backup": {"status": "ok"}}}
    nodes = {"broker:alpaca_paper": {"state": "errore", "label": "x"}}
    # a viewer's first poll: the red sections are the baseline, silently
    assert sounds.step_incidents(state, sounds.red_keys(red, None), t0, {"section"})
    assert state["history"] == [] and state["scopes"] == ["section"]
    # an admin's first poll later: the red nodes are the nodes' own baseline, silently
    t1 = t0 + timedelta(minutes=1)
    assert sounds.step_incidents(state, sounds.red_keys(red, nodes), t1, {"section", "node"})
    assert state["history"] == [] and sorted(state["scopes"]) == ["node", "section"]
    assert not sounds.step_incidents(state, sounds.red_keys(red, nodes),
                                     t1 + timedelta(minutes=1), {"section", "node"})
    # back to ok (from a viewer's poll: the node incident is not closed by it)
    t2 = t0 + timedelta(minutes=3)
    assert sounds.step_incidents(state, sounds.red_keys(ok, None), t2, {"section"})
    # a new incident on each scope, then the node back to ok
    t3 = t0 + timedelta(minutes=4)
    red2 = {"sections": {"mcp": {"status": "problema", "label": "MCP"}}}
    n2 = {"px": {"state": "errore", "label": "Prezzi"}}
    sounds.step_incidents(state, sounds.red_keys(red2, n2), t3, {"section", "node"})
    t4 = t0 + timedelta(minutes=5)
    sounds.step_incidents(state, sounds.red_keys(red2, {}), t4, {"section", "node"})
    ids = [h["id"] for h in state["history"]]
    assert ids == [f"health:section:backup:{t0.isoformat()}:ok",
                   f"health:section:mcp:{t3.isoformat()}:bad",
                   f"health:node:px:{t3.isoformat()}:bad",
                   f"health:node:broker:alpaca_paper:{t1.isoformat()}:ok",
                   f"health:node:px:{t3.isoformat()}:ok"]
    ev = sounds.health_events(state, t0, t0 + timedelta(hours=1), admin=False)
    assert [e["kind"] for e in ev] == ["health_ok", "health_bad"]  # no node for a viewer
    assert [e["id"] for e in ev] == ids[:2]
    assert sounds.is_quiet("22:00", "08:00", datetime(2026, 11, 28, 22, 30, tzinfo=UTC),
                           "UTC")
    assert not sounds.is_quiet("22:00", "08:00", datetime(2026, 11, 28, 12, tzinfo=UTC), "UTC")
    assert sounds.is_quiet("13:00", "14:00", datetime(2026, 11, 28, 13, 5, tzinfo=UTC), "UTC")
