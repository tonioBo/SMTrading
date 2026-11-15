import asyncio
import hashlib
import json
import logging
import re
import secrets
import string
import time
import uuid
import weakref
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from sqlalchemy import event, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from smtrading.agents.models import Agent, AgentPortfolio, McpAuditLog
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.portfolios.models import Portfolio, PortfolioStatus, live_portfolio_ids
from smtrading.useractivity.context import current_request_id

log = logging.getLogger("smtrading.agents")

_PREFIX_LEN = 8
_SECRET_LEN = 40
_PREFIX_ALPHABET = string.ascii_lowercase + string.digits
_TOKEN_RE = re.compile(r"smt_([a-z0-9]{8})_([A-Za-z0-9_-]{40})")
_LAST_SEEN_EVERY = timedelta(minutes=1)
_PERMISSIONS = ("read", "trade")
_MAX_CONCURRENT_VERIFY = 4

_hasher = PasswordHasher()  # argon2id defaults
_DUMMY_HASH = _hasher.hash("smt-dummy-secret")


def _invalid_token() -> SMTError:
    return SMTError(ErrorCode.FORBIDDEN, "invalid token")


def generate_token() -> tuple[str, str, str]:
    """Return (token, prefix, secret): smt_<8 [a-z0-9]>_<40 url-safe>."""
    prefix = "".join(secrets.choice(_PREFIX_ALPHABET) for _ in range(_PREFIX_LEN))
    secret = secrets.token_urlsafe(30)[:_SECRET_LEN]  # 30 bytes -> exactly 40 chars
    return f"smt_{prefix}_{secret}", prefix, secret


class TokenCache:
    """In-memory token -> agent id cache (key sha256(token)), expiring after `ttl` seconds."""

    def __init__(self, ttl: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl
        self._clock = clock
        self._entries: dict[str, tuple[uuid.UUID, float]] = {}

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def get(self, token: str) -> uuid.UUID | None:
        key = self._key(token)
        entry = self._entries.get(key)
        if entry is None:
            return None
        agent_id, expires_at = entry
        if self._clock() >= expires_at:
            del self._entries[key]
            return None
        return agent_id

    def put(self, token: str, agent_id: uuid.UUID) -> None:
        self._entries[self._key(token)] = (agent_id, self._clock() + self._ttl)

    def invalidate_agent(self, agent_id: uuid.UUID) -> None:
        for key in [k for k, (aid, _) in self._entries.items() if aid == agent_id]:
            del self._entries[key]

    def clear(self) -> None:
        self._entries.clear()


class RejectedTokenCache:
    """Bounded set of recently rejected tokens (key sha256(token)), each kept `ttl` seconds,
    so a client retrying a bad token does not cost an argon2 verify every time."""

    def __init__(
        self, ttl: float = 60.0, max_entries: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._ttl = ttl
        self._max = max_entries
        self._clock = clock
        self._entries: dict[str, float] = {}  # key -> expires_at, in insertion order

    def __contains__(self, token: str) -> bool:
        key = TokenCache._key(token)
        expires_at = self._entries.get(key)
        if expires_at is None:
            return False
        if self._clock() >= expires_at:
            del self._entries[key]
            return False
        return True

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, token: str) -> None:
        key = TokenCache._key(token)
        self._entries.pop(key, None)
        if len(self._entries) >= self._max:
            now = self._clock()
            for k in [k for k, exp in self._entries.items() if exp <= now]:
                del self._entries[k]
            while len(self._entries) >= self._max:  # still full: drop the oldest
                del self._entries[next(iter(self._entries))]
        self._entries[key] = self._clock() + self._ttl

    def clear(self) -> None:
        self._entries.clear()


token_cache = TokenCache()
rejected_tokens = RejectedTokenCache()
# one semaphore per event loop (tests and the API server run several loops)
_verify_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _verify_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _verify_slots.get(loop)
    if sem is None:
        sem = _verify_slots[loop] = asyncio.Semaphore(_MAX_CONCURRENT_VERIFY)
    return sem

_INVALIDATE_KEY = "smt_invalidate_agents"


def _invalidate_after_commit(session: AsyncSession, agent_id: uuid.UUID) -> None:
    """Drop the agent's cached tokens now and again once the transaction commits: a
    concurrent request may re-cache the old token from the still-committed row meanwhile."""
    token_cache.invalidate_agent(agent_id)
    session.info.setdefault(_INVALIDATE_KEY, set()).add(agent_id)


@event.listens_for(Session, "after_commit")
def _on_commit(session: Session) -> None:
    if session.in_nested_transaction():  # a savepoint release, not the real commit
        return
    for agent_id in session.info.pop(_INVALIDATE_KEY, ()):
        token_cache.invalidate_agent(agent_id)


@event.listens_for(Session, "after_rollback")
def _on_rollback(session: Session) -> None:
    if not session.in_nested_transaction():
        session.info.pop(_INVALIDATE_KEY, None)


async def _get_agent(session: AsyncSession, agent_id: uuid.UUID) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise SMTError(ErrorCode.NOT_FOUND, "agent not found")
    return agent


async def create_agent(session: AsyncSession, name: str) -> tuple[Agent, str]:
    if await session.scalar(select(Agent.id).where(Agent.name == name)) is not None:
        raise SMTError(ErrorCode.INVALID_ORDER, f"agent {name!r} already exists")
    for _ in range(5):  # prefix collisions are astronomically rare; retry anyway
        token, prefix, secret = generate_token()
        agent = Agent(
            name=name, token_prefix=prefix, token_hash=await asyncio.to_thread(_hasher.hash, secret)
        )
        try:
            async with session.begin_nested():
                session.add(agent)
                await session.flush()
        except IntegrityError:
            continue
        return agent, token
    raise SMTError(ErrorCode.INVALID_ORDER, "could not create agent")


async def rotate_token(session: AsyncSession, agent_id: uuid.UUID) -> str:
    """Issue a new token. This process's token cache is invalidated now and after the
    commit; the cache TTL bounds staleness in other processes."""
    agent = await _get_agent(session, agent_id)
    for _ in range(5):
        token, prefix, secret = generate_token()
        new_hash = await asyncio.to_thread(_hasher.hash, secret)
        try:
            async with session.begin_nested():
                agent.token_prefix = prefix
                agent.token_hash = new_hash
                await session.flush()
        except IntegrityError:
            continue
        _invalidate_after_commit(session, agent_id)
        return token
    raise SMTError(ErrorCode.INVALID_ORDER, "could not rotate token")


async def set_agent_active(session: AsyncSession, agent_id: uuid.UUID, active: bool) -> Agent:
    """This process's token cache is invalidated now and after the commit; the cache TTL
    bounds staleness in other processes."""
    agent = await _get_agent(session, agent_id)
    agent.active = active
    await session.flush()
    _invalidate_after_commit(session, agent_id)
    return agent


async def set_agent_can_notify(
    session: AsyncSession, agent_id: uuid.UUID, can_notify: bool
) -> Agent:
    agent = await _get_agent(session, agent_id)
    agent.can_notify = can_notify
    await session.flush()
    return agent


async def set_agent_can_halt(
    session: AsyncSession, agent_id: uuid.UUID, can_halt: bool
) -> Agent:
    """The `halt` permission: the agent may set/clear trading halts (set_trading_halt)."""
    agent = await _get_agent(session, agent_id)
    agent.can_halt = can_halt
    await session.flush()
    return agent


def _touch(agent: Agent) -> None:
    now = datetime.now(UTC)
    if agent.last_seen_at is None or now - agent.last_seen_at >= _LAST_SEEN_EVERY:
        agent.last_seen_at = now


async def authenticate(session: AsyncSession, token: str) -> Agent:
    """Resolve a bearer token to an active agent, or raise FORBIDDEN 'invalid token'.

    At most 4 argon2 verifies run at once (per event loop); a token whose secret failed
    verification is refused without hashing for the next 60 s (`rejected_tokens`)."""
    if not isinstance(token, str) or _TOKEN_RE.fullmatch(token) is None:
        raise _invalid_token()
    cached_id = token_cache.get(token)
    if cached_id is not None:
        agent = await session.get(Agent, cached_id)
        if agent is not None and agent.active:
            _touch(agent)
            return agent
        token_cache.invalidate_agent(cached_id)

    if token in rejected_tokens:
        raise _invalid_token()
    prefix, secret = _TOKEN_RE.fullmatch(token).groups()  # type: ignore[union-attr]
    agent = await session.scalar(select(Agent).where(Agent.token_prefix == prefix))
    try:
        async with _verify_semaphore():
            await asyncio.to_thread(
                _hasher.verify, agent.token_hash if agent else _DUMMY_HASH, secret
            )
    except VerificationError:
        rejected_tokens.add(token)
        raise _invalid_token() from None
    if agent is None or not agent.active:
        raise _invalid_token()
    token_cache.put(token, agent.id)
    _touch(agent)
    return agent


def _check_permission(permission: str) -> None:
    if permission not in _PERMISSIONS:
        raise SMTError(ErrorCode.INVALID_ORDER, f"invalid permission {permission!r}")


async def assign_portfolio(
    session: AsyncSession, agent_id: uuid.UUID, portfolio_id: uuid.UUID, permission: str
) -> None:
    _check_permission(permission)
    if await session.get(Agent, agent_id) is None:
        raise SMTError(ErrorCode.NOT_FOUND, "agent not found")
    if await session.get(Portfolio, portfolio_id) is None:
        raise SMTError(ErrorCode.NOT_FOUND, "portfolio not found")
    stmt = insert(AgentPortfolio).values(
        agent_id=agent_id, portfolio_id=portfolio_id, permission=permission
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["agent_id", "portfolio_id"], set_={"permission": permission}
        )
    )


async def unassign_portfolio(
    session: AsyncSession, agent_id: uuid.UUID, portfolio_id: uuid.UUID
) -> None:
    link = await session.get(AgentPortfolio, (agent_id, portfolio_id))
    if link is not None:
        await session.delete(link)
        await session.flush()


async def get_permission(
    session: AsyncSession, agent_id: uuid.UUID, portfolio_id: uuid.UUID
) -> str | None:
    return await session.scalar(
        select(AgentPortfolio.permission).where(
            AgentPortfolio.agent_id == agent_id, AgentPortfolio.portfolio_id == portfolio_id
        )
    )


async def require_permission(
    session: AsyncSession,
    agent_id: uuid.UUID,
    portfolio_id: uuid.UUID,
    needed: Literal["read", "trade"],
) -> None:
    """`trade` implies `read`. Unassigned portfolio or insufficient permission -> FORBIDDEN.
    An archived portfolio does not exist for agents (Task 13) -> NOT_FOUND; the
    assignment itself is kept for when it is restored."""
    have = await get_permission(session, agent_id, portfolio_id)
    if have is not None and await session.scalar(
        select(Portfolio.status).where(Portfolio.id == portfolio_id)
    ) == PortfolioStatus.archived:
        raise SMTError(
            ErrorCode.NOT_FOUND, "portfolio not found", {"portfolio_id": str(portfolio_id)}
        )
    if have == "trade" or (have == "read" and needed == "read"):
        return
    raise SMTError(
        ErrorCode.FORBIDDEN,
        f"agent has no {needed} permission on this portfolio",
        {"portfolio_id": str(portfolio_id), "needed": needed},
    )


async def agent_portfolio_ids(session: AsyncSession, agent_id: uuid.UUID) -> list[uuid.UUID]:
    """The agent's portfolios, archived ones excluded."""
    rows = await session.scalars(
        select(AgentPortfolio.portfolio_id)
        .where(
            AgentPortfolio.agent_id == agent_id,
            AgentPortfolio.portfolio_id.in_(live_portfolio_ids()),
        )
        .order_by(AgentPortfolio.portfolio_id)
    )
    return list(rows)


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.astimezone(UTC).isoformat() if o.tzinfo else o.isoformat()
    if isinstance(o, Decimal | uuid.UUID):
        return str(o)
    if isinstance(o, set | frozenset | tuple):
        return list(o)
    return str(o)


def _json_safe(params: Any) -> Any:
    return json.loads(json.dumps(params, default=_json_default))


async def write_audit(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID | None,
    method: str,
    params: dict[str, Any],
    outcome: str,
    latency_ms: int,
) -> None:
    """Append an audit row. Never raises: failures are logged (inside a savepoint).
    An `admin:*` row written for a portal request carries its activity `request_id`
    (the link to its «Registro attività» row)."""
    if method.startswith("admin:") and (request_id := current_request_id()) is not None:
        params = {**params, "request_id": request_id}
    try:
        row = McpAuditLog(
            agent_id=agent_id,
            method=method,
            params=_json_safe(params),
            outcome=outcome,
            latency_ms=latency_ms,
        )
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except Exception:
        log.exception("audit log write failed (method=%s)", method)


async def list_audit(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID | None = None,
    method: str | None = None,
    outcome: str | None = None,
    since: datetime | None = None,
    limit: int = 200,
) -> list[McpAuditLog]:
    stmt = select(McpAuditLog).order_by(McpAuditLog.id.desc()).limit(limit)
    if agent_id is not None:
        stmt = stmt.where(McpAuditLog.agent_id == agent_id)
    if method is not None:
        stmt = stmt.where(McpAuditLog.method == method)
    if outcome is not None:
        stmt = stmt.where(McpAuditLog.outcome == outcome)
    if since is not None:
        stmt = stmt.where(McpAuditLog.created_at >= since)
    return list((await session.scalars(stmt)).all())
