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
    ["update journals set reason='x'", "delete from journal_lines", "truncate journals cascade"],
)
async def test_ledger_tables_immutable(db_session, stmt, posted_journal):
    with pytest.raises(DBAPIError, match="LEDGER_IMMUTABLE"):
        await db_session.execute(text(stmt))


async def test_migration_matches_models(migrated_db):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from smtrading.common.db import Base, make_engine

    for mod in ("ledger", "instruments", "portfolios", "fees"):
        importlib.import_module(f"smtrading.{mod}.models")

    engine = make_engine(migrated_db)
    async with engine.connect() as conn:
        diff = await conn.run_sync(
            lambda c: compare_metadata(MigrationContext.configure(c), Base.metadata)
        )
    await engine.dispose()
    assert diff == []
