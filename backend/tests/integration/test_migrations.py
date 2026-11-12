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

    for mod in ("ledger", "instruments", "portfolios", "fees", "connectors", "engine", "positions", "risk"):
        importlib.import_module(f"smtrading.{mod}.models")

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
