"""«Campagne» (plan 21): the guided wizard creates a campaign with new and existing
portfolios, paper/demo links and goals in one transaction (a real-money link refused,
everything rolled back on any error); the close wizard shows what is open, archives
through the archive rules and concludes; the campaign report downloads as a PDF; viewers
read, admins write."""

# ruff: noqa: F811  (the `env` fixture of test_admin_users, imported then requested)

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from test_admin_users import _audit, env  # noqa: F401

from smtrading.brokers.models import BrokerLink
from smtrading.campaigns.models import Campaign, CampaignGoal, CampaignPortfolio
from smtrading.common.config import get_settings
from smtrading.common.crypto import encrypt_json
from smtrading.events import discard_post_commit
from smtrading.goals.models import Goal
from smtrading.portfolios.models import Portfolio, PortfolioStatus
from smtrading.portfolios.service import create_portfolio, get_cash_balances

D = Decimal


def _today() -> date:
    return datetime.now(UTC).astimezone(ZoneInfo(get_settings().tz)).date()


async def _admin(env):
    email, _ = await env.add_user("admin")
    return await env.client(email)


async def _portfolio(sm, name: str) -> uuid.UUID:
    async with sm() as s:
        p = await create_portfolio(s, name=name, initial_cash=D(5000))
        discard_post_commit(s)
        await s.commit()
        return p.id


async def _link(sm, name: str, kind: str = "alpaca_paper", live_state=None) -> uuid.UUID:
    async with sm() as s:
        link = BrokerLink(name=name, kind=kind, live_state=live_state,
                          config_encrypted=encrypt_json({}, get_settings().master_key))
        s.add(link)
        await s.commit()
        return link.id


async def _count(sm, model) -> int:
    async with sm() as s:
        return await s.scalar(select(func.count()).select_from(model))


async def _drop_total_plan(c) -> None:
    """The migrated weekly piano covers every portfolio: off, so campaign goals may be
    pianos."""
    for g in (await c.get("/api/goals")).json():
        if g["closes_positions"] and g["active"]:
            assert (await c.delete(f"/api/goals/{g['id']}")).status_code == 200


def _body(start: date, end: date, portfolios: list[dict], goals: list[dict] | None = None,
          **over) -> dict:
    return {"name": "Prova novembre", "start_date": start.isoformat(),
            "end_date": end.isoformat(), "notes": "prima campagna", "portfolios": portfolios,
            "goals": goals or [], **over}


def _goal(key: str, start: date, end: date, **over) -> dict:
    return {"portfolio_key": key, "name": "campagna", "horizon": "custom",
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "recurring": False, "target_pct": "3", "kind": "min", **over}


async def test_templates_follow_the_goals_rules(env):
    c = await _admin(env)
    start, end = _today(), _today() + timedelta(days=30)
    existing = await _portfolio(env.sm, "Esistente T")
    r = await c.post("/api/campaigns/templates", json={
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "portfolios": [{"key": "new-1", "name": "Nuovo T"},
                       {"key": "ex", "portfolio_id": str(existing)}]})
    assert r.status_code == 200, r.text
    t = {x["key"]: x for x in r.json()}
    own = t["campaign_min:new-1"]
    assert own["portfolio_key"] == "new-1" and own["recommended"] is True
    assert own["body"] | {"closes_positions": None} == {
        "name": "campagna", "horizon": "custom", "start_date": start.isoformat(),
        "end_date": end.isoformat(), "recurring": False, "scope": "portfolio",
        "portfolio_id": None, "target_pct": "8", "kind": "min", "benchmark": None,
        "active": True, "closes_positions": None}
    # the migrated weekly piano on the total covers every portfolio: no second piano
    assert own["body"]["closes_positions"] is False
    assert t["campaign_min:ex"]["body"]["portfolio_id"] == str(existing)
    assert t["campaign_risk:ex"]["body"]["kind"] == "max_loss"
    assert t["monthly_portfolio:ex"]["body"]["start_date"] == start.isoformat()
    await _drop_total_plan(c)
    r = await c.post("/api/campaigns/templates", json={
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "portfolios": [{"key": "new-1", "name": "Nuovo T"}]})
    t = {x["key"]: x for x in r.json()}
    assert t["campaign_min:new-1"]["body"]["closes_positions"] is True
    assert t["campaign_risk:new-1"]["body"]["closes_positions"] is False


async def test_create_with_new_and_existing_portfolios(env):
    c = await _admin(env)
    await _drop_total_plan(c)
    start, end = _today(), _today() + timedelta(days=20)
    existing = await _portfolio(env.sm, "Esistente")
    link = await _link(env.sm, "Alpaca prova")
    r = await c.post("/api/campaigns", json=_body(start, end, [
        {"key": "n1", "name": "Campagna Alfa", "initial_cash": "10000",
         "base_currency": "usd", "broker_link_id": str(link)},
        {"key": "ex", "portfolio_id": str(existing)},
    ], [_goal("n1", start, end), _goal("ex", start, end, name="rischio campagna",
                                       kind="max_loss", target_pct="5")]))
    assert r.status_code == 200, r.text
    camp = r.json()
    assert (camp["name"], camp["status"], camp["start_date"], camp["end_date"],
            camp["notes"]) == ("Prova novembre", "attiva", start.isoformat(),
                               end.isoformat(), "prima campagna")
    ports = {p["name"]: p for p in camp["portfolios"]}
    assert ports["Campagna Alfa"]["created"] is True
    assert ports["Campagna Alfa"]["base_currency"] == "USD"
    assert ports["Campagna Alfa"]["broker_link_name"] == "Alpaca prova"
    assert ports["Esistente"]["created"] is False
    goals = {g["portfolio_name"]: g for g in camp["goals"]}
    assert goals["Campagna Alfa"]["end_date"] == end.isoformat()
    assert goals["Campagna Alfa"]["closes_positions"] is True  # the goals' default
    assert goals["Esistente"]["kind"] == "max_loss"
    assert goals["Esistente"]["closes_positions"] is True  # its own portfolio's piano
    new_id = ports["Campagna Alfa"]["id"]
    detail = (await c.get(f"/api/portfolios/{new_id}")).json()
    assert detail["broker_link_id"] == str(link)
    async with env.sm() as s:
        assert await get_cash_balances(s, uuid.UUID(new_id)) == {"USD": D(10000)}
    rows = await _audit(env.sm, "admin:campaign.create")
    assert len(rows) == 1 and rows[0].params["name"] == "Prova novembre"
    assert [p["created"] for p in rows[0].params["portfolios"]] == [True, False]
    listed = (await c.get("/api/campaigns")).json()
    assert [x["id"] for x in listed] == [camp["id"]]
    # an existing portfolio in an open campaign cannot join another one
    r = await c.post("/api/campaigns", json=_body(
        start, end, [{"key": "ex", "portfolio_id": str(existing)}], name="Seconda"))
    assert r.status_code == 400 and "già nella campagna aperta" in r.json()["message"]


async def test_real_money_links_are_refused_and_nothing_is_left(env):
    c = await _admin(env)
    start, end = _today(), _today() + timedelta(days=10)
    live = await _link(env.sm, "Reale", kind="alpaca_live", live_state="armed")
    before = await _count(env.sm, Portfolio)
    r = await c.post("/api/campaigns", json=_body(start, end, [
        {"key": "n1", "name": "Mai creato", "initial_cash": "1000",
         "broker_link_id": str(live)}]))
    assert r.status_code == 403, r.text
    assert "denaro reale" in r.json()["message"]
    assert await _count(env.sm, Portfolio) == before
    assert await _count(env.sm, Campaign) == 0
    # a portfolio already on a real-money link cannot join either
    pid = await _portfolio(env.sm, "Su conto reale")
    async with env.sm() as s:
        (await s.get(Portfolio, pid)).broker_link_id = live
        await s.commit()
    r = await c.post("/api/campaigns", json=_body(start, end, [
        {"key": "ex", "portfolio_id": str(pid)}]))
    assert r.status_code == 403 and "denaro reale" in r.json()["message"]


async def test_a_failing_goal_rolls_everything_back(env):
    c = await _admin(env)
    start, end = _today(), _today() + timedelta(days=10)
    existing = await _portfolio(env.sm, "Resta libero")
    link = await _link(env.sm, "Demo")
    portfolios, goals = await _count(env.sm, Portfolio), await _count(env.sm, Goal)
    r = await c.post("/api/campaigns", json=_body(start, end, [
        {"key": "n1", "name": "Nuovo annullato", "initial_cash": "1000"},
        {"key": "ex", "portfolio_id": str(existing), "broker_link_id": str(link)},
    ], [_goal("n1", start, end), _goal("ex", start, end, target_pct="abc")]))
    assert r.status_code == 400, r.text
    assert "obiettivo «campagna» (Resta libero)" in r.json()["message"]
    assert r.json()["details"]["goal"] == 1
    assert await _count(env.sm, Portfolio) == portfolios
    assert await _count(env.sm, Goal) == goals
    assert await _count(env.sm, Campaign) == 0
    assert await _count(env.sm, CampaignPortfolio) == 0
    async with env.sm() as s:
        assert (await s.get(Portfolio, existing)).broker_link_id is None
    assert await _audit(env.sm, "admin:campaign.") == []
    # header checks
    for body, field in (
        (_body(start, start - timedelta(days=1), []), "end_date"),
        (_body(start, end, []), "portfolios"),
        (_body(start, end, [{"key": "a", "name": "Zero", "initial_cash": "0"}]),
         "initial_cash"),
        (_body(start, end, [{"key": "a", "portfolio_id": str(existing)}],
               goals=[_goal("zz", start, end)]), "portfolio_key"),
    ):
        r = await c.post("/api/campaigns", json=body)
        assert r.status_code == 400 and r.json()["details"]["field"] == field, r.text


async def test_patch_draft_and_viewer(env):
    c = await _admin(env)
    start, end = _today(), _today() + timedelta(days=10)
    r = await c.post("/api/campaigns", json=_body(start, end, [
        {"key": "n1", "name": "Bozza P", "initial_cash": "1000"}], status="bozza"))
    assert r.status_code == 200 and r.json()["status"] == "bozza"
    cid = r.json()["id"]
    r = await c.patch(f"/api/campaigns/{cid}", json={"status": "conclusa"})
    assert r.status_code == 400
    r = await c.patch(f"/api/campaigns/{cid}", json={"status": "attiva", "notes": " via "})
    assert r.status_code == 200 and (r.json()["status"], r.json()["notes"]) == ("attiva",
                                                                                "via")
    email, _ = await env.add_user("viewer")
    v = await env.client(email)
    assert (await v.get("/api/campaigns")).status_code == 200
    assert (await v.get(f"/api/campaigns/{cid}")).status_code == 200
    r = await v.post("/api/campaigns", json=_body(start, end, []))
    assert r.status_code == 403
    r = await v.get(f"/api/campaigns/{cid}/report.pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert r.headers["content-type"] == "application/pdf"
    assert 'filename="resoconto-prova-novembre-' in r.headers["content-disposition"]


async def test_close_wizard(env):
    c = await _admin(env)
    start, end = _today(), _today() + timedelta(days=10)
    link = await _link(env.sm, "Paper chiusura")
    r = await c.post("/api/campaigns", json=_body(start, end, [
        {"key": "a", "name": "Da archiviare", "initial_cash": "1000"},
        {"key": "b", "name": "Collegato", "initial_cash": "1000",
         "broker_link_id": str(link)}]))
    camp = r.json()
    ids = {p["name"]: p["id"] for p in camp["portfolios"]}
    r = await c.get(f"/api/campaigns/{camp['id']}/close-check")
    assert r.status_code == 200, r.text
    check = {p["name"]: p for p in r.json()["portfolios"]}
    assert check["Da archiviare"]["can_archive"] is True
    assert check["Collegato"]["can_archive"] is False
    assert check["Collegato"]["archive_blocker"] == "Scollega prima il broker"
    assert check["Collegato"]["broker_link_name"] == "Paper chiusura"
    assert r.json()["open_orders"] == 0 and r.json()["plans"] == []
    async with env.sm() as s:  # the dry run archived nothing
        assert (await s.get(Portfolio, uuid.UUID(ids["Da archiviare"]))).status \
            == PortfolioStatus.active
    assert await _audit(env.sm, "admin:portfolio.archive") == []
    # a refused archive stops the whole close
    r = await c.post(f"/api/campaigns/{camp['id']}/close",
                     json={"archive_portfolio_ids": list(ids.values())})
    assert r.status_code == 400 and r.json()["message"].startswith("«Collegato»")
    async with env.sm() as s:
        assert (await s.get(Campaign, uuid.UUID(camp["id"]))).status == "attiva"
        assert (await s.get(Portfolio, uuid.UUID(ids["Da archiviare"]))).status \
            == PortfolioStatus.active
    r = await c.post(f"/api/campaigns/{camp['id']}/close",
                     json={"archive_portfolio_ids": [ids["Da archiviare"]],
                           "notes": "fine prova"})
    assert r.status_code == 200, r.text
    done = r.json()
    assert (done["status"], done["notes"]) == ("conclusa", "fine prova")
    assert done["closed_by"].startswith("admin-")
    assert {p["name"]: p["status"] for p in done["portfolios"]} == {
        "Da archiviare": "archived", "Collegato": "active"}
    assert len(await _audit(env.sm, "admin:campaign.close")) == 1
    assert len(await _audit(env.sm, "admin:portfolio.archive")) == 1
    r = await c.post(f"/api/campaigns/{camp['id']}/close", json={})
    assert r.status_code == 400 and "già conclusa" in r.json()["message"]
    # the report of a concluded campaign keeps its archived portfolio
    r = await c.get(f"/api/campaigns/{camp['id']}/report.pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    async with env.sm() as s:
        assert await s.scalar(select(func.count()).select_from(CampaignGoal)) == 0


async def test_period_report_api(env):
    pid = await _portfolio(env.sm, "Per resoconto")
    email, _ = await env.add_user("viewer")
    v = await env.client(email)
    start, end = _today() - timedelta(days=7), _today()
    q = f"from={start.isoformat()}&to={end.isoformat()}&portfolio_ids={pid}"
    r = await v.get(f"/api/reports/period?{q}")
    assert r.status_code == 200, r.text
    data = r.json()
    assert (data["title"], data["from"], data["to"]) == (
        "Resoconto del periodo", start.isoformat(), end.isoformat())
    assert [p["name"] for p in data["portfolios"]] == ["Per resoconto"]
    r = await v.get(f"/api/reports/period.pdf?{q}")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert r.headers["content-disposition"] == (
        f'attachment; filename="resoconto-{start.isoformat()}-{end.isoformat()}.pdf"')
    r = await v.get("/api/reports/period?from=2027-02-01&to=2027-01-01")
    assert r.status_code == 400 and r.json()["details"]["field"] == "to"
    r = await v.get("/api/reports/period?from=ieri")
    assert r.status_code == 400 and r.json()["details"]["field"] == "from"
    r = await v.get(f"/api/reports/period?portfolio_ids={uuid.uuid4()}")
    assert r.status_code == 404
