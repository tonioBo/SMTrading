import re
import uuid
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
    service.rejected_tokens.clear()
    yield
    service.token_cache.clear()
    service.rejected_tokens.clear()


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
    secret = token.split("_", 2)[2]  # the secret itself may contain "_"
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

        def hash(self, *a):  # pragma: no cover
            raise AssertionError

    monkeypatch.setattr(service, "_hasher", Spy())
    assert (await service.authenticate(db_session, token)).id == agent.id
    assert calls == []


async def test_strict_token_parse(db_session):
    _, token = await service.create_agent(db_session, "bot")
    for bad in (token + "\n", "\n" + token, token + " ", None, 123, b"x"):
        with pytest.raises(SMTError) as e:
            await service.authenticate(db_session, bad)
        assert e.value.code == ErrorCode.FORBIDDEN


async def test_argon2_runs_off_event_loop(db_session, monkeypatch):
    import threading

    main = threading.get_ident()
    seen = []
    real = service._hasher

    class Spy:
        def hash(self, s):
            seen.append(threading.get_ident())
            return real.hash(s)

        def verify(self, h, s):
            seen.append(threading.get_ident())
            return real.verify(h, s)

    monkeypatch.setattr(service, "_hasher", Spy())
    agent, token = await service.create_agent(db_session, "bot")
    await service.authenticate(db_session, token)
    await service.rotate_token(db_session, agent.id)
    with pytest.raises(SMTError):
        await service.authenticate(db_session, "smt_zzzzzzzz_" + "A" * 40)
    assert len(seen) == 4 and main not in seen


async def test_assign_missing_agent_or_portfolio_not_found(db_session, portfolio):
    import uuid

    agent, _ = await service.create_agent(db_session, "bot")
    for a, p in ((agent.id, uuid.uuid4()), (uuid.uuid4(), portfolio.id)):
        with pytest.raises(SMTError) as e:
            await service.assign_portfolio(db_session, a, p, "read")
        assert e.value.code == ErrorCode.NOT_FOUND


async def _committed_agent(db_url: str, name: str = "bot"):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from smtrading.common.db import make_engine

    engine = make_engine(db_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        agent, token = await service.create_agent(s, name)
        await s.commit()
    return engine, sm, agent, token


@pytest.mark.parametrize("change", ["rotate", "deactivate"])
async def test_old_token_recached_before_commit_is_dropped_after_commit(fresh_db, change):
    engine, sm, agent, old = await _committed_agent(fresh_db)
    try:
        async with sm() as writer:
            if change == "rotate":
                await service.rotate_token(writer, agent.id)
            else:
                await service.set_agent_active(writer, agent.id, False)
            # a concurrent request still sees the committed row and re-caches the old token
            async with sm() as reader:
                assert (await service.authenticate(reader, old)).id == agent.id
                await reader.rollback()  # its last_seen_at update would wait on the writer
            assert service.token_cache.get(old) == agent.id
            await writer.commit()
        assert service.token_cache.get(old) is None
        async with sm() as reader:
            with pytest.raises(SMTError) as e:
                await service.authenticate(reader, old)
            assert e.value.code == ErrorCode.FORBIDDEN
    finally:
        await engine.dispose()


async def test_pending_invalidation_cleared_on_rollback(fresh_db):
    engine, sm, agent, old = await _committed_agent(fresh_db)
    try:
        async with sm() as writer:
            await service.rotate_token(writer, agent.id)
            assert writer.info["smt_invalidate_agents"] == {agent.id}
            await writer.rollback()
            assert "smt_invalidate_agents" not in writer.info
            # rolled back: the old token is still the valid one
            assert (await service.authenticate(writer, old)).id == agent.id
            await writer.commit()
        assert service.token_cache.get(old) == agent.id
    finally:
        await engine.dispose()


async def test_savepoint_release_does_not_flush_pending_invalidation(fresh_db):
    engine, sm, agent, old = await _committed_agent(fresh_db)
    try:
        async with sm() as writer:
            await service.rotate_token(writer, agent.id)
            async with writer.begin_nested():
                pass
            assert writer.info["smt_invalidate_agents"] == {agent.id}
            service.token_cache.put(old, agent.id)  # re-cached concurrently
            await writer.commit()
        assert service.token_cache.get(old) is None
    finally:
        await engine.dispose()


def test_rejected_token_cache_ttl_and_bound():
    now = [0.0]
    cache = service.RejectedTokenCache(ttl=60, max_entries=3, clock=lambda: now[0])
    for t in ("a", "b", "c"):
        cache.add(t)
    assert "a" in cache and len(cache) == 3
    cache.add("d")  # full, nothing expired: the oldest goes
    assert len(cache) == 3 and "a" not in cache and "d" in cache
    now[0] = 61
    assert "b" not in cache
    cache.add("e")
    now[0] = 62
    cache.add("f")
    cache.add("g")  # full: expired entries are swept first
    assert len(cache) == 3 and {"e", "f", "g"} == {t for t in "defg" if t in cache}


async def test_bad_token_skips_argon2_while_rejected(db_session, monkeypatch):
    real = service._hasher
    calls = []

    class Spy:
        def verify(self, h, s):
            calls.append(s)
            return real.verify(h, s)

    agent, token = await service.create_agent(db_session, "bot")
    monkeypatch.setattr(service, "_hasher", Spy())
    bad = token[:13] + ("B" if token[13] != "B" else "C") + token[14:]
    for _ in range(3):
        with pytest.raises(SMTError):
            await service.authenticate(db_session, bad)
    assert len(calls) == 1
    assert (await service.authenticate(db_session, token)).id == agent.id
    assert len(calls) == 2


async def test_argon2_verify_concurrency_is_bounded(db_session, monkeypatch):
    import asyncio
    import threading
    import time

    lock = threading.Lock()
    active = [0, 0]  # current, peak

    class Slow:
        def verify(self, h, s):
            with lock:
                active[0] += 1
                active[1] = max(active[1], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            raise service.VerificationError("no")

    monkeypatch.setattr(service, "_hasher", Slow())
    tokens = [f"smt_{i:08d}_" + "A" * 40 for i in range(10)]

    async def attempt(t):
        with pytest.raises(SMTError):
            await service.authenticate(db_session, t)

    # one AsyncSession cannot run queries concurrently: authenticate with fresh lookups
    async def no_agent(*a, **k):
        return None

    monkeypatch.setattr(db_session, "scalar", no_agent)
    await asyncio.gather(*(attempt(t) for t in tokens))
    assert active[1] == 4


async def test_can_notify_defaults_false_and_toggles(db_session):
    agent, _ = await service.create_agent(db_session, "Notifier")
    assert agent.can_notify is False
    assert (await service.set_agent_can_notify(db_session, agent.id, True)).can_notify is True
    with pytest.raises(SMTError) as exc:
        await service.set_agent_can_notify(db_session, uuid.uuid4(), True)
    assert exc.value.code == ErrorCode.NOT_FOUND


async def test_decision_kind_check_constraint(db_session):
    from sqlalchemy.exc import IntegrityError

    from smtrading.activity.models import Decision

    agent, _ = await service.create_agent(db_session, "Decider")
    db_session.add(Decision(agent_id=agent.id, kind="foo", summary="x"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
