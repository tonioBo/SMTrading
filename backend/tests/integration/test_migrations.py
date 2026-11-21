import importlib
import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


async def test_tables_exist(db_session):
    names = {
        r[0]
        for r in (
            await db_session.execute(
                text("select tablename from pg_tables where schemaname='public'")
            )
        ).all()
    }
    assert {
        "accounts", "journals", "journal_lines", "account_balances",
        "instruments", "portfolios", "fee_rules",
    } <= names


@pytest.mark.parametrize(
    "stmt",
    [
        "update journals set reason='x'", "delete from journal_lines", "truncate journals cascade",
        "update accounts set asset='X'", "delete from accounts", "truncate accounts cascade",
    ],
)
async def test_ledger_tables_immutable(db_session, stmt, posted_journal):
    with pytest.raises(DBAPIError, match="LEDGER_IMMUTABLE"):
        await db_session.execute(text(stmt))


async def test_migration_matches_models(migrated_db):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from smtrading.common.db import Base, make_engine

    importlib.import_module("smtrading.models")

    engine = make_engine(migrated_db)
    async with engine.connect() as conn:
        diff = await conn.run_sync(
            lambda c: compare_metadata(MigrationContext.configure(c), Base.metadata)
        )
    await engine.dispose()
    assert diff == []


async def test_prev_hash_unique(db_session, posted_journal):
    with pytest.raises(DBAPIError, match="uq_journals_prev_hash"):
        await db_session.execute(
            text(
                "insert into journals (id, type, author_type, prev_hash, hash) values"
                " (gen_random_uuid(), 'DEPOSIT', 'system', :ph, :h)"
            ),
            {"ph": "0" * 64, "h": "b" * 64},
        )


_ENGINE_TABLES = (
    "orders", "fills", "positions", "risk_profiles", "interest_rates", "system_state",
    "app_settings", "equity_snapshots", "job_runs", "notifications",
)


async def test_engine_tables_present(db_session):
    got = set(
        (
            await db_session.execute(
                text("select table_name from information_schema.tables where table_schema='public'")
            )
        ).scalars()
    )
    assert set(_ENGINE_TABLES) <= got


async def test_order_qty_xor_notional(db_session, portfolio):
    from smtrading.seed import run_seed

    await run_seed(db_session)
    ins = (
        "insert into orders (id, portfolio_id, symbol, side, type, tif, qty, notional, filled_qty,"
        " status, source) values (gen_random_uuid(), :p, 'AAPL', 'buy', 'market', 'day', {q}, {n},"
        " 0, 'new', 'agent')"
    )
    for q, n in (("1", "1"), ("null", "null")):
        with pytest.raises(DBAPIError, match="ck_orders_qty_xor_notional"):
            async with db_session.begin_nested():
                await db_session.execute(text(ins.format(q=q, n=n)), {"p": portfolio.id})
    await db_session.execute(text(ins.format(q="1", n="null")), {"p": portfolio.id})


async def test_broker_watch_backtest_tables_present(db_session):
    got = set(
        (
            await db_session.execute(
                text("select table_name from information_schema.tables where table_schema='public'")
            )
        ).scalars()
    )
    assert {
        "broker_links", "reconciliations", "watchlists", "watchlist_items", "alerts", "backtests",
    } <= got


async def test_open_order_indexes_include_partially_filled(db_session):
    defs = (
        await db_session.execute(
            text(
                "select indexdef from pg_indexes where indexname in"
                " ('ix_orders_open_expires_at', 'ix_orders_open_created_at')"
            )
        )
    ).scalars().all()
    assert len(defs) == 2
    assert all("partially_filled" in d for d in defs)


async def test_risk_profile_description_column(db_session):
    row = (
        await db_session.execute(
            text(
                "select data_type, is_nullable from information_schema.columns"
                " where table_name='risk_profiles' and column_name='description'"
            )
        )
    ).one()
    assert tuple(row) == ("text", "YES")


def _alembic(url: str, *args: str) -> None:
    """The alembic command API on `url` (env.py runs its own event loop: call in a thread)."""
    from argparse import Namespace
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    backend = Path(__file__).resolve().parents[2]
    cfg = Config(str(backend / "alembic.ini"), cmd_opts=Namespace(x=[f"url={url}"]))
    cfg.set_main_option("script_location", str(backend / "migrations"))
    getattr(command, args[0])(cfg, *args[1:])


async def test_downgrade_to_0008_and_back(fresh_db):
    """M16: downgrades map the values older code cannot load, then upgrade restores head."""
    import asyncio

    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from smtrading.common.db import Base, make_engine
    from smtrading.seed import run_seed

    importlib.import_module("smtrading.models")
    engine = make_engine(fresh_db)
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(engine) as s:
        await run_seed(s)
        await s.commit()
        pid = await s.scalar(text("select id from portfolios where name = 'Demo'"))
        await s.execute(text(
            "insert into orders (id, portfolio_id, symbol, side, type, qty, status, filled_qty,"
            " tif, reduce_only, source, created_at, updated_at) values (gen_random_uuid(), :p,"
            " 'AAPL', 'buy', 'market', 10, 'partially_filled', 4, 'day', false, 'admin', now(),"
            " now())"
        ), {"p": pid})
        await s.commit()
    await engine.dispose()

    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0008")
    engine = make_engine(fresh_db)
    async with engine.connect() as conn:
        statuses = set((await conn.execute(text("select distinct status from orders"))).scalars())
        assert "partially_filled" not in statuses
        paris = await conn.scalar(text(
            "select count(*) from instruments where market = 'EURONEXT_PARIS' and active"))
        assert paris == 0
    await engine.dispose()

    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")
    engine = make_engine(fresh_db)
    async with engine.connect() as conn:
        diff = await conn.run_sync(
            lambda c: compare_metadata(MigrationContext.configure(c), Base.metadata)
        )
    await engine.dispose()
    assert diff == []


async def test_downgrade_0016_clears_dangling_agent_and_restores_fk(fresh_db):
    """0016 downgrade: an activity naming an agent that does not exist gets agent_id NULL,
    then the foreign key is restored; upgrade drops it again."""
    import asyncio
    import uuid

    from smtrading.common.db import make_engine

    engine = make_engine(fresh_db)
    async with engine.begin() as conn:
        dangling = await conn.scalar(text(
            "insert into activity (type, level, agent_id, summary, data)"
            " values ('test.event', 'info', :a, 'x', '{}') returning id"
        ), {"a": uuid.uuid4()})
    await engine.dispose()

    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0015")
    engine = make_engine(fresh_db)
    fk = text("select count(*) from pg_constraint where conname = 'activity_agent_id_fkey'")
    async with engine.connect() as conn:
        assert await conn.scalar(
            text("select agent_id from activity where id = :i"), {"i": dangling}) is None
        assert await conn.scalar(fk) == 1
    await engine.dispose()

    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")
    engine = make_engine(fresh_db)
    async with engine.connect() as conn:
        assert await conn.scalar(fk) == 0
    await engine.dispose()


async def test_decision_outcome_check(db_session):
    from smtrading.agents.service import create_agent

    agent, _ = await create_agent(db_session, "outcome-check")
    ins = (
        "insert into decisions (agent_id, kind, summary, details, symbols, outcome)"
        " values (:a, 'other', 'x', '{}', '[]', :o)"
    )
    with pytest.raises(DBAPIError, match="ck_decisions_outcome"):
        async with db_session.begin_nested():
            await db_session.execute(text(ins), {"a": agent.id, "o": "ok"})
    for o in ("approvata", "respinta", "sospesa", "modificata", "informativa", None):
        await db_session.execute(text(ins), {"a": agent.id, "o": o})


async def test_0028_etoro_connector_keeps_admin_order(fresh_db):
    """0028 (data only): the `etoro` connector row, off, and `etoro` appended to the four
    existing quote/bars priority rows of US stocks and crypto, after the admin's order;
    the downgrade removes both."""
    import asyncio

    from smtrading.common.db import make_engine

    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0027")
    engine = make_engine(fresh_db)
    rows = {
        ("us_equity", "quote"): ["yahoo", "alpaca"],  # reordered by the admin
        ("us_equity", "bars"): ["yahoo"],
        ("crypto", "quote"): ["etoro", "kraken"],  # already has it: unchanged
        ("us_equity", "news"): ["yahoo"],  # not an eToro capability
    }  # (crypto, bars) has no row: the seed writes it later, from DEFAULT_PRIORITIES
    async with engine.begin() as conn:
        assert await conn.scalar(text("select count(*) from connectors where name = 'etoro'")) == 0
        for (ac, cap), providers in rows.items():
            await conn.execute(text(
                "insert into provider_priorities (asset_class, capability, providers)"
                " values (:a, :c, cast(:p as jsonb))"), {"a": ac, "c": cap, "p": json.dumps(providers)})
    await engine.dispose()

    async def priorities() -> dict:
        engine = make_engine(fresh_db)
        async with engine.connect() as conn:
            got = {(r[0], r[1]): r[2] for r in await conn.execute(
                text("select asset_class, capability, providers from provider_priorities"))}
            etoro = (await conn.execute(text(
                "select enabled, health_status, config_encrypted from connectors"
                " where name = 'etoro'"))).all()
        await engine.dispose()
        return got, etoro

    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")
    got, etoro = await priorities()
    assert got == {
        ("us_equity", "quote"): ["yahoo", "alpaca", "etoro"],
        ("us_equity", "bars"): ["yahoo", "etoro"],
        ("crypto", "quote"): ["etoro", "kraken"],
        ("us_equity", "news"): ["yahoo"],
    }
    assert etoro == [(False, "unknown", None)]

    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0027")
    got, etoro = await priorities()
    assert got == {
        ("us_equity", "quote"): ["yahoo", "alpaca"],
        ("us_equity", "bars"): ["yahoo"],
        ("crypto", "quote"): ["kraken"],
        ("us_equity", "news"): ["yahoo"],
    }
    assert etoro == []
    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")


async def test_0029_rssnews_connector_first_in_crypto_news(fresh_db):
    """0029 (data only): the `rssnews` connector row, on, and `rssnews` put first in the
    existing crypto news priority row (other rows untouched); the downgrade removes both."""
    import asyncio

    from smtrading.common.db import make_engine

    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0028")
    engine = make_engine(fresh_db)
    rows = {
        ("crypto", "news"): ["yahoo"],
        ("crypto", "quote"): ["kraken", "yahoo"],
        ("us_equity", "news"): ["finnhub", "yahoo"],
    }
    async with engine.begin() as conn:
        await conn.execute(text("delete from provider_priorities"))
        assert await conn.scalar(text("select count(*) from connectors where name = 'rssnews'")) == 0
        for (ac, cap), providers in rows.items():
            await conn.execute(text(
                "insert into provider_priorities (asset_class, capability, providers)"
                " values (:a, :c, cast(:p as jsonb))"), {"a": ac, "c": cap, "p": json.dumps(providers)})
    await engine.dispose()

    async def state() -> tuple[dict, list]:
        engine = make_engine(fresh_db)
        async with engine.connect() as conn:
            got = {(r[0], r[1]): r[2] for r in await conn.execute(
                text("select asset_class, capability, providers from provider_priorities"))}
            row = (await conn.execute(text(
                "select enabled, health_status, config_encrypted from connectors"
                " where name = 'rssnews'"))).all()
        await engine.dispose()
        return got, row

    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")
    got, row = await state()
    assert got == {**rows, ("crypto", "news"): ["rssnews", "yahoo"]}
    assert row == [(True, "unknown", None)]

    # idempotent on a row that already lists it (e.g. re-run after a manual fix)
    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0028")
    engine = make_engine(fresh_db)
    async with engine.begin() as conn:
        await conn.execute(text(
            "update provider_priorities set providers = cast(:p as jsonb)"
            " where asset_class = 'crypto' and capability = 'news'"),
            {"p": json.dumps(["yahoo", "rssnews"])})
    await engine.dispose()
    got, row = await state()
    assert got[("crypto", "news")] == ["yahoo", "rssnews"] and row == []
    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")
    got, _ = await state()
    assert got[("crypto", "news")] == ["yahoo", "rssnews"]

    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0028")
    got, row = await state()
    assert got == {**rows, ("crypto", "news"): ["yahoo"]}
    assert row == []
    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")


async def test_0030_fee_rule_asset_class_reversible(fresh_db):
    """0030: the nullable fee_rules.asset_class; existing rules keep null (every class);
    the downgrade drops it."""
    import asyncio

    from smtrading.common.db import make_engine

    async def columns() -> set[str]:
        engine = make_engine(fresh_db)
        async with engine.connect() as conn:
            got = {r[0] for r in await conn.execute(text(
                "select column_name from information_schema.columns"
                " where table_name = 'fee_rules'"))}
        await engine.dispose()
        return got

    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0029")
    assert "asset_class" not in await columns()
    engine = make_engine(fresh_db)
    async with engine.begin() as conn:
        await conn.execute(text(
            "insert into fee_rules (id, name, scope, side, currency, fixed, pct, fx_pct,"
            " priority, active) values (gen_random_uuid(), 'Vecchia', 'global', 'both', 'EUR',"
            " 0, 0, 0, 0, true)"))
    await engine.dispose()
    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")
    assert "asset_class" in await columns()
    engine = make_engine(fresh_db)
    async with engine.connect() as conn:
        assert await conn.scalar(text(
            "select asset_class from fee_rules where name = 'Vecchia'")) is None
    await engine.dispose()
    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0029")
    assert "asset_class" not in await columns()
    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")


async def test_0030_downgrade_deactivates_asset_class_rules(fresh_db):
    """0030 downgrade: a rule limited to one asset class would cover every class once the
    column is gone, so it is deactivated first; the other rules are untouched."""
    import asyncio

    from smtrading.common.db import make_engine

    engine = make_engine(fresh_db)
    ins = text(
        "insert into fee_rules (id, name, scope, scope_ref, asset_class, side, currency,"
        " fixed, pct, fx_pct, priority, active) values (gen_random_uuid(), :n, 'portfolio',"
        " 'p1', :ac, 'both', 'EUR', 1, 0, 0, 0, true)")
    async with engine.begin() as conn:
        await conn.execute(ins, {"n": "zz-crypto", "ac": "crypto"})
        await conn.execute(ins, {"n": "zz-all", "ac": None})
    await engine.dispose()
    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0029")
    engine = make_engine(fresh_db)
    async with engine.connect() as conn:
        got = dict((await conn.execute(text(
            "select name, active from fee_rules where name like 'zz-%'"))).all())
    await engine.dispose()
    assert got == {"zz-crypto": False, "zz-all": True}
    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")


async def test_0031_goals_migrate_the_weekly_goal_setting(fresh_db):
    """0031: the weekly goal setting becomes a recurring weekly goal on the total (absent
    = on at +2%, off = inactive), the setting row goes; the downgrade writes it back from
    that goal and drops the table."""
    import asyncio

    from smtrading.common.db import make_engine

    async def query(sql: str, **params):
        engine = make_engine(fresh_db)
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            rows = result.all() if result.returns_rows else []
        await engine.dispose()
        return rows

    goal_cols = ("select name, horizon, start_date::text, end_date, recurring, scope,"
                 " portfolio_id, target_pct::text, kind, active from goals")
    # fresh database: no setting at all → on at +2%
    assert await query(goal_cols) == [
        ("settimanale", "week", "2026-01-05", None, True, "total", None, "2.00", "min", True)]
    assert await query("select key from app_settings where key = 'weekly_goal'") == []

    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0030")
    got = await query("select value from app_settings where key = 'weekly_goal'")
    assert got == [({"on": True, "pct": "2.00"},)]
    assert await query("select to_regclass('goals')") == [(None,)]

    await query("update app_settings set value = cast(:v as jsonb) where key = 'weekly_goal'",
                v=json.dumps({"on": False, "pct": "1.5"}))
    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")
    assert [(r[0], r[7], r[9]) for r in await query(goal_cols)] == [
        ("settimanale", "1.50", False)]
    assert await query("select key from app_settings where key = 'weekly_goal'") == []

    # a malformed stored percent falls back to +2%; other goals are dropped on downgrade
    await query("insert into goals (id, name, horizon, start_date, end_date, recurring,"
                " scope, target_pct, kind, active) values (gen_random_uuid(), 'mese',"
                " 'custom', '2026-11-01', '2026-11-30', false, 'total', 5, 'min', true)")
    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0030")
    assert await query("select value from app_settings where key = 'weekly_goal'") == [
        ({"on": False, "pct": "1.50"},)]
    await query("update app_settings set value = cast(:v as jsonb) where key = 'weekly_goal'",
                v=json.dumps({"pct": "abc"}))
    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")
    assert [(r[7], r[9]) for r in await query(goal_cols)] == [("2.00", True)]


async def test_0031_goal_checks(db_session):
    """The goals table refuses inconsistent rows."""
    bad = [
        "(gen_random_uuid(), 'x', 'week', '2026-11-16', null, false, 'portfolio', null, 2, 'min', true)",
        "(gen_random_uuid(), 'x', 'custom', '2026-11-16', null, false, 'total', null, 2, 'min', true)",
        "(gen_random_uuid(), 'x', 'week', '2026-11-16', '2026-11-20', false, 'total', null, 2, 'min', true)",
        "(gen_random_uuid(), 'x', 'custom', '2026-11-16', '2026-11-01', false, 'total', null, 2, 'min', true)",
        "(gen_random_uuid(), 'x', 'custom', '2026-11-16', '2026-11-30', true, 'total', null, 2, 'min', true)",
        "(gen_random_uuid(), 'x', 'day', '2026-11-16', null, false, 'total', null, 2, 'min', true)",
        "(gen_random_uuid(), 'x', 'week', '2026-11-16', null, false, 'total', null, 0, 'min', true)",
        "(gen_random_uuid(), 'x', 'week', '2026-11-16', null, false, 'total', null, 2, 'max', true)",
    ]
    for values in bad:
        nested = await db_session.begin_nested()
        with pytest.raises(DBAPIError, match="ck_goals_"):
            await db_session.execute(text(
                "insert into goals (id, name, horizon, start_date, end_date, recurring, scope,"
                f" portfolio_id, target_pct, kind, active) values {values}"))
        await nested.rollback()


async def test_0032_goal_kinds_round_trip(fresh_db):
    """0032: max_loss and beat_index (benchmark required there only, margin 0 allowed),
    goal_alerts and benchmark_snapshots; the downgrade deletes the goals of the new kinds
    (with their alerts) and keeps the others."""
    import asyncio

    from smtrading.common.db import make_engine

    async def query(sql: str, **params):
        engine = make_engine(fresh_db)
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            rows = result.all() if result.returns_rows else []
        await engine.dispose()
        return rows

    insert = ("insert into goals (id, name, horizon, start_date, recurring, scope,"
              " target_pct, kind, benchmark, active) values (gen_random_uuid(), :n, 'week',"
              " '2026-11-16', false, 'total', :t, :k, :b, true)")
    await query(insert, n="rischio", t=5, k="max_loss", b=None)
    await query(insert, n="indice", t=0, k="beat_index", b="VWCE.DE")
    for t, k, b in ((0, "max_loss", None), (0, "beat_index", None), (0, "min", "SPY"),
                    (1, "beat_index", "DAX"), (-1, "beat_index", "SPY")):
        with pytest.raises(DBAPIError, match="ck_goals_"):
            await query(insert, n="x", t=t, k=k, b=b)
    await query("insert into goal_alerts (goal_id, period_start, status, kind, target_pct)"
                " select id, '2026-11-16', 'superato', kind, target_pct from goals"
                " where name = 'rischio'")
    await query("insert into benchmark_snapshots (symbol, ts, price, currency, fx_eur,"
                " price_eur) values ('SPY', now(), 600, 'USD', 0.9, 540)")

    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0031")
    assert await query("select name, kind from goals order by name") == [
        ("settimanale", "min")]
    assert await query("select to_regclass('goal_alerts'), to_regclass('benchmark_snapshots')"
                       ) == [(None, None)]
    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")
    await query(insert, n="indice", t=0, k="beat_index", b="SPY")
    assert await query("select name, kind, benchmark from goals order by name") == [
        ("indice", "beat_index", "SPY"), ("settimanale", "min", None)]


async def test_0033_order_valid_until_round_trip(fresh_db):
    """0033: orders.valid_until with its open-orders partial index; reversible."""
    import asyncio

    from smtrading.common.db import make_engine

    async def query(sql: str):
        engine = make_engine(fresh_db)
        async with engine.begin() as conn:
            rows = (await conn.execute(text(sql))).all()
        await engine.dispose()
        return rows

    column = ("select data_type from information_schema.columns"
              " where table_name = 'orders' and column_name = 'valid_until'")
    index = "select to_regclass('ix_orders_open_valid_until')::text"
    assert await query(column) == [("timestamp with time zone",)]
    assert await query(index) == [("ix_orders_open_valid_until",)]
    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0032")
    assert await query(column) == [] and await query(index) == [(None,)]
    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")
    assert await query(index) == [("ix_orders_open_valid_until",)]


async def test_0034_decision_lessons_round_trip(fresh_db):
    """0034: the decision kinds lesson and team_rule with ix_decisions_kind; the downgrade
    keeps their rows as other (details.former_kind) and the upgrade allows them again."""
    import asyncio

    from smtrading.common.db import make_engine

    async def query(sql: str, **params):
        engine = make_engine(fresh_db)
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            rows = result.all() if result.returns_rows else []
        await engine.dispose()
        return rows

    agent = ("insert into agents (id, name, token_prefix, token_hash)"
             " values (gen_random_uuid(), 'lessons', 'lessons0', 'x') returning id")
    insert = ("insert into decisions (agent_id, kind, summary, details, symbols)"
              " values (:a, :k, 'x', '{\"rule\": \"r\"}', '[]')")
    index = "select to_regclass('ix_decisions_kind')::text"
    (agent_id,), = await query(agent)
    for k in ("lesson", "team_rule", "veto"):
        await query(insert, a=agent_id, k=k)
    assert await query(index) == [("ix_decisions_kind",)]
    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0033")
    assert await query(index) == [(None,)]
    assert await query("select kind, details from decisions order by id") == [
        ("other", {"rule": "r", "former_kind": "lesson"}),
        ("other", {"rule": "r", "former_kind": "team_rule"}),
        ("veto", {"rule": "r"})]
    with pytest.raises(DBAPIError, match="ck_decisions_kind"):
        await query(insert, a=agent_id, k="lesson")
    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")
    assert await query(index) == [("ix_decisions_kind",)]
    await query(insert, a=agent_id, k="team_rule")


async def test_0035_plan_close_round_trip(fresh_db):
    """0035: goals.closes_positions / plan_since and plan_closures; reversible."""
    import asyncio

    from smtrading.common.db import make_engine

    async def query(sql: str):
        engine = make_engine(fresh_db)
        async with engine.begin() as conn:
            rows = (await conn.execute(text(sql))).all()
        await engine.dispose()
        return rows

    column = ("select column_default from information_schema.columns"
              " where table_name = 'goals' and column_name = 'closes_positions'")
    table = "select to_regclass('plan_closures')::text"
    assert await query(column) == [("false",)]
    assert await query(table) == [("plan_closures",)]
    # the weekly goal on the total becomes a piano from the migration on
    assert await query("select name, closes_positions, plan_since is not null from goals") == [
        ("settimanale", True, True)]
    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0034")
    assert await query(column) == [] and await query(table) == [(None,)]
    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")
    assert await query(table) == [("plan_closures",)]


async def test_0036_portfolio_in_totals_round_trip(fresh_db):
    """0036: portfolios.in_totals, true by default; the downgrade drops it."""
    import asyncio

    from smtrading.common.db import make_engine

    async def query(sql: str):
        engine = make_engine(fresh_db)
        async with engine.begin() as conn:
            result = await conn.execute(text(sql))
            rows = result.all() if result.returns_rows else []
        await engine.dispose()
        return rows

    column = ("select data_type, column_default from information_schema.columns"
              " where table_name = 'portfolios' and column_name = 'in_totals'")
    await query("insert into portfolios (id, name, base_currency, leverage,"
                " maintenance_margin_pct, warning_margin_pct, slippage_bps, status, timezone)"
                " values (gen_random_uuid(), 'tot', 'EUR', 1, 50, 75, 0, 'active',"
                " 'Europe/Rome')")
    assert await query(column) == [("boolean", "true")]
    assert await query("select in_totals from portfolios where name = 'tot'") == [(True,)]
    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0035")
    assert await query(column) == []
    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")
    assert await query("select in_totals from portfolios where name = 'tot'") == [(True,)]


async def test_0037_reconciliation_checks_round_trip(fresh_db):
    """0037: reconciliation_checks (plan 21); reversible."""
    import asyncio

    from smtrading.common.db import make_engine

    async def query(sql: str):
        engine = make_engine(fresh_db)
        async with engine.begin() as conn:
            rows = (await conn.execute(text(sql))).all()
        await engine.dispose()
        return rows

    table = "select to_regclass('reconciliation_checks')::text"
    assert await query(table) == [("reconciliation_checks",)]
    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0036")
    assert await query(table) == [(None,)]
    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")
    assert await query(table) == [("reconciliation_checks",)]


async def test_0038_campaigns_round_trip(fresh_db):
    """0038: campaigns, campaign_portfolios, campaign_goals; reversible (one step back)."""
    import asyncio

    from smtrading.common.db import make_engine

    async def query(sql: str):
        engine = make_engine(fresh_db)
        async with engine.begin() as conn:
            rows = (await conn.execute(text(sql))).all()
        await engine.dispose()
        return rows

    tables = ("select to_regclass('campaigns')::text, to_regclass('campaign_portfolios')::text,"
              " to_regclass('campaign_goals')::text")
    assert await query(tables) == [("campaigns", "campaign_portfolios", "campaign_goals")]
    with pytest.raises(Exception, match="ck_campaigns_status"):
        await query("insert into campaigns (id, name, start_date, end_date, status)"
                    " values (gen_random_uuid(), 'x', '2027-01-01', '2027-02-01', 'altro')")
    with pytest.raises(Exception, match="ck_campaigns_dates"):
        await query("insert into campaigns (id, name, start_date, end_date, status)"
                    " values (gen_random_uuid(), 'x', '2027-02-01', '2027-01-01', 'attiva')")
    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0037")
    assert await query(tables) == [(None, None, None)]
    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")
    assert await query(tables) == [("campaigns", "campaign_portfolios", "campaign_goals")]


async def test_0039_instrument_metadata_round_trip(fresh_db):
    """0039: sector / industry / indices / created_at; rows that existed before keep a
    NULL created_at (never «aggiunti di recente»), new ones get now(); downgrade drops."""
    import asyncio

    from smtrading.common.db import make_engine

    async def columns() -> set[str]:
        engine = make_engine(fresh_db)
        async with engine.connect() as conn:
            got = {r[0] for r in await conn.execute(text(
                "select column_name from information_schema.columns"
                " where table_name = 'instruments'"))}
        await engine.dispose()
        return got

    new = {"sector", "industry", "indices", "created_at"}
    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0038")
    assert not new & await columns()
    engine = make_engine(fresh_db)
    async with engine.begin() as conn:
        await conn.execute(text(
            "insert into instruments (symbol, name, asset_class, market, currency, lot_size,"
            " tick_size, shortable, active, provider_symbols, source) values ('OLD1', 'Old',"
            " 'us_equity', 'NYSE', 'USD', 1, 0.01, false, true, '{}', 'manual')"))
    await engine.dispose()
    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")
    assert new <= await columns()
    engine = make_engine(fresh_db)
    async with engine.begin() as conn:
        await conn.execute(text(
            "insert into instruments (symbol, name, asset_class, market, currency, lot_size,"
            " tick_size, shortable, active, provider_symbols, source, indices) values ('NEW1',"
            " 'New', 'us_equity', 'NYSE', 'USD', 1, 0.01, false, true, '{}', 'manual',"
            " '{SP500}')"))
        rows = dict((await conn.execute(text(
            "select symbol, created_at is not null from instruments"
            " where symbol in ('OLD1', 'NEW1')"))).all())
        assert rows == {"OLD1": False, "NEW1": True}
        assert await conn.scalar(text(
            "select symbol from instruments where indices @> '{SP500}'")) == "NEW1"
        await conn.execute(text("delete from instruments where symbol in ('OLD1', 'NEW1')"))
    await engine.dispose()
    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0038")
    assert not new & await columns()
    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")


async def test_0041_research_runs_round_trip(fresh_db):
    """0041: research_runs; reversible (one step back)."""
    import asyncio

    from smtrading.common.db import make_engine

    async def table() -> str | None:
        engine = make_engine(fresh_db)
        async with engine.connect() as conn:
            got = await conn.scalar(text("select to_regclass('research_runs')::text"))
        await engine.dispose()
        return got

    assert await table() == "research_runs"
    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0040")
    assert await table() is None
    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")
    assert await table() == "research_runs"


async def test_0043_markets_v2_round_trip(fresh_db):
    """0043: portal_favorites, portal_price_alerts, market_screen_rows; reversible."""
    import asyncio

    from smtrading.common.db import make_engine

    names = ("portal_favorites", "portal_price_alerts", "market_screen_rows")

    async def tables() -> set[str]:
        engine = make_engine(fresh_db)
        async with engine.connect() as conn:
            got = {n for n in names
                   if await conn.scalar(text(f"select to_regclass('{n}')::text")) == n}
        await engine.dispose()
        return got

    assert await tables() == set(names)
    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0042")
    assert await tables() == set()
    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")
    assert await tables() == set(names)


async def test_0044_eu_etoro_and_yahoo_coin_ids(fresh_db):
    """0044 (data only): `etoro` appended to the EU quote/bars priority rows (after the
    admin's order, never twice) and the Yahoo id merged into the provider symbols of four
    recent coins (an admin's own Yahoo override wins); the downgrade takes both back."""
    import asyncio

    from smtrading.common.db import make_engine

    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0043")
    engine = make_engine(fresh_db)
    async with engine.begin() as conn:
        await conn.execute(text("delete from provider_priorities"))
        for (ac, cap), providers in {
            ("eu_equity", "quote"): ["yahoo", "twelvedata", "finnhub"],
            ("eu_equity", "bars"): ["yahoo", "etoro"],  # already there: unchanged
            ("eu_equity", "news"): ["yahoo"],
        }.items():
            await conn.execute(text(
                "insert into provider_priorities (asset_class, capability, providers)"
                " values (:a, :c, cast(:p as jsonb))"), {"a": ac, "c": cap, "p": json.dumps(providers)})
        for sym, ps in {"HYPE-USD": {"coingecko": "hyperliquid:usd"},
                        "CC-USD": {"yahoo": "MINE-USD"}}.items():
            await conn.execute(text(
                "insert into instruments (symbol, name, asset_class, market, currency,"
                " lot_size, tick_size, shortable, active, provider_symbols, source)"
                " values (:s, :s, 'crypto', 'CRYPTO', 'USD', 0.00000001,"
                " 0.00000001, false, true, cast(:p as jsonb), 'catalog')"
                " on conflict (symbol) do update set provider_symbols = excluded.provider_symbols"),
                {"s": sym, "p": json.dumps(ps)})
    await engine.dispose()

    async def state() -> tuple[dict, dict]:
        engine = make_engine(fresh_db)
        async with engine.connect() as conn:
            prio = {(r[0], r[1]): r[2] for r in await conn.execute(
                text("select asset_class, capability, providers from provider_priorities"))}
            syms = {r[0]: r[1] for r in await conn.execute(text(
                "select symbol, provider_symbols from instruments"
                " where symbol in ('HYPE-USD', 'CC-USD')"))}
        await engine.dispose()
        return prio, syms

    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")
    prio, syms = await state()
    assert prio == {
        ("eu_equity", "quote"): ["yahoo", "twelvedata", "finnhub", "etoro"],
        ("eu_equity", "bars"): ["yahoo", "etoro"],
        ("eu_equity", "news"): ["yahoo"],
    }
    assert syms == {"HYPE-USD": {"coingecko": "hyperliquid:usd", "yahoo": "HYPE32196-USD"},
                    "CC-USD": {"yahoo": "MINE-USD"}}

    await asyncio.to_thread(_alembic, fresh_db, "downgrade", "0043")
    prio, syms = await state()
    assert prio[("eu_equity", "quote")] == ["yahoo", "twelvedata", "finnhub"]
    assert syms == {"HYPE-USD": {"coingecko": "hyperliquid:usd"}, "CC-USD": {"yahoo": "MINE-USD"}}
    await asyncio.to_thread(_alembic, fresh_db, "upgrade", "head")
