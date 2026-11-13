import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from smtrading.agents import service
from smtrading.agents.models import Agent
from smtrading.common.errors import ErrorCode, SMTError

TOKEN_RE = re.compile(r"^smt_[a-z0-9]{8}_[A-Za-z0-9_-]{40}$")


@pytest.fixture(autouse=True)
def _clean_cache():
    service.token_cache.clear()
    yield
    service.token_cache.clear()


async def test_generate_token_format():
    token, prefix, secret = service.generate_token()
    assert TOKEN_RE.match(token)
    assert token == f"smt_{prefix}_{secret}"
    assert len(prefix) == 8 and len(secret) == 40


async def test_create_and_authenticate(db_session):
    agent, token = await service.create_agent(db_session, "bot")
    assert TOKEN_RE.match(token)
    got = await service.authenticate(db_session, token)
    assert got.id == agent.id


async def test_token_not_stored_in_clear(db_session):
    agent, token = await service.create_agent(db_session, "bot")
    secret = token.rsplit("_", 1)[1]
    assert secret not in agent.token_hash
    assert token not in agent.token_hash
    assert agent.token_hash.startswith("$argon2id$")
    assert agent.token_prefix == token.split("_")[1]


async def test_duplicate_name_invalid_order(db_session):
    await service.create_agent(db_session, "bot")
    with pytest.raises(SMTError) as e:
        await service.create_agent(db_session, "bot")
    assert e.value.code == ErrorCode.INVALID_ORDER
    # session still usable
    await service.create_agent(db_session, "bot2")


@pytest.mark.parametrize("bad", ["", "garbage", "smt_short_x", "smt_abcdefgh_" + "x" * 39])
async def test_malformed_token_forbidden(db_session, bad):
    with pytest.raises(SMTError) as e:
        await service.authenticate(db_session, bad)
    assert e.value.code == ErrorCode.FORBIDDEN
    assert e.value.message == "invalid token"


async def test_wrong_secret_and_unknown_prefix_forbidden(db_session):
    _, token = await service.create_agent(db_session, "bot")
    prefix = token.split("_")[1]
    wrong_secret = f"smt_{prefix}_" + "A" * 40
    unknown = "smt_zzzzzzzz_" + "A" * 40
    for bad in (wrong_secret, unknown):
        with pytest.raises(SMTError) as e:
            await service.authenticate(db_session, bad)
        assert e.value.code == ErrorCode.FORBIDDEN


async def test_inactive_agent_forbidden(db_session):
    agent, token = await service.create_agent(db_session, "bot")
    await service.set_agent_active(db_session, agent.id, False)
    with pytest.raises(SMTError) as e:
        await service.authenticate(db_session, token)
    assert e.value.code == ErrorCode.FORBIDDEN
    await service.set_agent_active(db_session, agent.id, True)
    assert (await service.authenticate(db_session, token)).id == agent.id


async def test_rotate_invalidates_old_token(db_session):
    agent, old = await service.create_agent(db_session, "bot")
    service.token_cache.put(old, agent.id)
    new = await service.rotate_token(db_session, agent.id)
    assert new != old
    assert service.token_cache.get(old) is None
    with pytest.raises(SMTError):
        await service.authenticate(db_session, old)
    assert (await service.authenticate(db_session, new)).id == agent.id


async def test_set_active_false_invalidates_cache(db_session):
    agent, token = await service.create_agent(db_session, "bot")
    service.token_cache.put(token, agent.id)
    await service.set_agent_active(db_session, agent.id, False)
    assert service.token_cache.get(token) is None


async def test_unknown_agent_not_found(db_session):
    import uuid

    with pytest.raises(SMTError) as e:
        await service.set_agent_active(db_session, uuid.uuid4(), False)
    assert e.value.code == ErrorCode.NOT_FOUND


async def test_last_seen_updated_at_most_once_a_minute(db_session):
    agent, token = await service.create_agent(db_session, "bot")
    assert agent.last_seen_at is None
    await service.authenticate(db_session, token)
    first = agent.last_seen_at
    assert first is not None
    await service.authenticate(db_session, token)
    assert agent.last_seen_at == first
    agent.last_seen_at = datetime.now(UTC) - timedelta(seconds=61)
    stale = agent.last_seen_at
    await service.authenticate(db_session, token)
    assert agent.last_seen_at > stale


async def test_permissions(db_session, portfolio):
    agent, _ = await service.create_agent(db_session, "bot")
    assert await service.get_permission(db_session, agent.id, portfolio.id) is None
    for needed in ("read", "trade"):
        with pytest.raises(SMTError) as e:
            await service.require_permission(db_session, agent.id, portfolio.id, needed)
        assert e.value.code == ErrorCode.FORBIDDEN

    await service.assign_portfolio(db_session, agent.id, portfolio.id, "read")
    await service.require_permission(db_session, agent.id, portfolio.id, "read")
    with pytest.raises(SMTError) as e:
        await service.require_permission(db_session, agent.id, portfolio.id, "trade")
    assert e.value.code == ErrorCode.FORBIDDEN

    await service.assign_portfolio(db_session, agent.id, portfolio.id, "trade")  # upsert
    assert await service.get_permission(db_session, agent.id, portfolio.id) == "trade"
    await service.require_permission(db_session, agent.id, portfolio.id, "read")
    await service.require_permission(db_session, agent.id, portfolio.id, "trade")
    assert await service.agent_portfolio_ids(db_session, agent.id) == [portfolio.id]

    await service.unassign_portfolio(db_session, agent.id, portfolio.id)
    assert await service.agent_portfolio_ids(db_session, agent.id) == []


async def test_assign_invalid_permission(db_session, portfolio):
    agent, _ = await service.create_agent(db_session, "bot")
    with pytest.raises(SMTError) as e:
        await service.assign_portfolio(db_session, agent.id, portfolio.id, "admin")
    assert e.value.code == ErrorCode.INVALID_ORDER


async def test_audit_write_and_list(db_session):
    agent, _ = await service.create_agent(db_session, "bot")
    await service.write_audit(
        db_session,
        agent_id=agent.id,
        method="place_order",
        params={"qty": Decimal("1.5"), "at": datetime(2026, 1, 1, tzinfo=UTC), "id": agent.id},
        outcome="ok",
        latency_ms=12,
    )
    await service.write_audit(
        db_session, agent_id=None, method="get_quote", params={}, outcome="FORBIDDEN", latency_ms=1
    )
    rows = await service.list_audit(db_session, agent_id=agent.id)
    assert len(rows) == 1
    assert rows[0].params == {"qty": "1.5", "at": "2026-01-01T00:00:00+00:00", "id": str(agent.id)}
    assert [r.method for r in await service.list_audit(db_session, outcome="FORBIDDEN")] == ["get_quote"]
    assert len(await service.list_audit(db_session, method="place_order")) == 1
    future = datetime.now(UTC) + timedelta(hours=1)
    assert await service.list_audit(db_session, since=future) == []
    assert len(await service.list_audit(db_session, limit=1)) == 1


async def test_write_audit_never_raises(db_session):
    class Bad:
        pass

    # unserialisable params -> coerced, not raised
    await service.write_audit(
        db_session, agent_id=None, method="m", params={"x": Bad()}, outcome="ok", latency_ms=1
    )

    class Broken:
        async def execute(self, *a, **k):
            raise RuntimeError("db down")

        def begin_nested(self):
            raise RuntimeError("db down")

        def add(self, *a):
            raise RuntimeError("db down")

    await service.write_audit(
        Broken(), agent_id=None, method="m", params={}, outcome="ok", latency_ms=1
    )
    # the real session is still healthy
    assert await db_session.scalar(select(Agent.id).limit(1)) is None


async def test_write_audit_bad_agent_id_does_not_poison_session(db_session):
    import uuid

    await service.write_audit(
        db_session, agent_id=uuid.uuid4(), method="m", params={}, outcome="ok", latency_ms=1
    )
    assert await db_session.scalar(select(Agent.id).limit(1)) is None


def test_token_cache_ttl_and_invalidate():
    now = [1000.0]
    cache = service.TokenCache(ttl=60, clock=lambda: now[0])
    import uuid

    a, b = uuid.uuid4(), uuid.uuid4()
    cache.put("t1", a)
    cache.put("t2", a)
    cache.put("t3", b)
    assert cache.get("t1") == a
    now[0] += 59
    assert cache.get("t1") == a
    now[0] += 2
    assert cache.get("t1") is None
    cache.put("t1", a)
    cache.invalidate_agent(a)
    assert cache.get("t1") is None and cache.get("t2") is None
    assert cache.get("t3") is None  # expired
    cache.put("t3", b)
    cache.clear()
    assert cache.get("t3") is None


async def test_authenticate_uses_cache_for_hash(db_session, monkeypatch):
    agent, token = await service.create_agent(db_session, "bot")
    await service.authenticate(db_session, token)
    calls = []

    class Spy:
        def verify(self, *a):
            calls.append(a)

    monkeypatch.setattr(service, "_hasher", Spy())
    assert (await service.authenticate(db_session, token)).id == agent.id
    assert calls == []
