import importlib

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
