"""Provider quotas (plan 25): how much of each data provider's allowance is used.

Budgets per provider: per minute (the connector's `rate_limit_per_min`, enforced by the
token bucket `rl:<provider>`), per hour and per day (`app_settings["provider_quotas"]`:
{provider: {"per_hour": n, "per_day": n}}, 0 = none, absent = the provider's default,
`DataProvider.default_hourly_budget` / `default_daily_budget`). The defaults are the
published free plans, to be checked against each account («da verificare»).

Every outbound call the router admits (a token taken from `rl:<provider>`) is counted
here, by its cost, in Redis counters per minute, hour and UTC day (with TTL). A provider
at DEMOTE_AT of its hourly or daily budget is demoted: the router tries it after every
other provider (still used when it is the only one that answers) until the period resets
(the hour, or 00:00 UTC: Twelve Data's daily reset). A 429 answer demotes it at once for
the rest of its period (the day with a daily budget, else the hour with an hourly one,
else the minute). Redis errors fail open: counting never blocks a call.
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.risk.models import AppSetting

log = logging.getLogger(__name__)

SETTINGS_KEY = "provider_quotas"
DEMOTE_AT = 0.9
MAX_BUDGET = 100_000_000
DAY, HOUR, MINUTE = "day", "hour", "minute"
QUOTA_429 = "429"
NOTE = "da verificare: i valori predefiniti sono quelli dei piani gratuiti pubblicati"

_429 = re.compile(r"(?<!\d)429(?!\d)")


def _s(v) -> str:
    return v.decode() if isinstance(v, bytes) else v


@dataclass(frozen=True)
class Budget:
    per_min: int | None
    per_hour: int | None
    per_day: int | None


@dataclass(frozen=True)
class Usage:
    minute: int
    hour: int
    day: int
    blocked_until: datetime | None = None  # a 429 demotion

    def demoted(self, budget: Budget) -> str | None:
        """Why the provider is demoted (`429`, `day`, `hour`), or None."""
        if self.blocked_until is not None:
            return QUOTA_429
        if budget.per_day and self.day >= DEMOTE_AT * budget.per_day:
            return DAY
        if budget.per_hour and self.hour >= DEMOTE_AT * budget.per_hour:
            return HOUR
        return None


def _key(provider: str, unit: str, now: datetime) -> str:
    fmt = {DAY: "%Y%m%d", HOUR: "%Y%m%d%H", MINUTE: "%Y%m%d%H%M"}[unit]
    return f"quota:{provider}:{unit[0]}:{now.astimezone(UTC).strftime(fmt)}"


def _block_key(provider: str) -> str:
    return f"quota:{provider}:block"


def next_reset(unit: str, now: datetime) -> datetime:
    now = now.astimezone(UTC)
    if unit == DAY:
        return datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    if unit == HOUR:
        return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return now.replace(second=0, microsecond=0) + timedelta(minutes=1)


def period_of(budget: Budget) -> str:
    return DAY if budget.per_day else HOUR if budget.per_hour else MINUTE


def is_quota_error(exc: BaseException, text: str) -> bool:
    """A 429 (too many requests / quota exhausted), from httpx or a provider's message."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        return True
    return bool(_429.search(text))


async def count(redis, provider: str, cost: int, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    try:
        async with redis.pipeline(transaction=False) as pipe:
            for unit, ttl in ((MINUTE, 120), (HOUR, 7200), (DAY, 2 * 86400)):
                k = _key(provider, unit, now)
                pipe.incrby(k, cost)
                pipe.expire(k, ttl)
            await pipe.execute()
    except Exception:
        log.warning("quota counter of %s not updated", provider, exc_info=True)


async def block(redis, provider: str, budget: Budget, now: datetime | None = None
                ) -> datetime | None:
    """A 429: demoted for the rest of its period. Returns until when."""
    now = now or datetime.now(UTC)
    until = next_reset(period_of(budget), now)
    ms = max(int((until - now).total_seconds() * 1000), 1000)
    try:
        await redis.set(_block_key(provider), until.isoformat(), px=ms)
    except Exception:
        log.warning("quota block of %s not stored", provider, exc_info=True)
        return None
    log.warning("provider %s answered 429: demoted until %s", provider, until.isoformat())
    return until


async def unblock(redis, provider: str) -> None:
    await redis.delete(_block_key(provider))


async def usage(redis, providers: list[str], now: datetime | None = None) -> dict[str, Usage]:
    now = now or datetime.now(UTC)
    if not providers:
        return {}
    async with redis.pipeline(transaction=False) as pipe:
        for p in providers:
            for unit in (MINUTE, HOUR, DAY):
                pipe.get(_key(p, unit, now))
            pipe.get(_block_key(p))
        raw = await pipe.execute()
    out = {}
    for i, p in enumerate(providers):
        m, h, d, b = raw[4 * i:4 * i + 4]
        until = None
        if b is not None:
            try:
                until = datetime.fromisoformat(_s(b))
            except ValueError:
                until = next_reset(MINUTE, now)
        out[p] = Usage(int(_s(m) or 0) if m else 0, int(_s(h) or 0) if h else 0,
                       int(_s(d) or 0) if d else 0, until)
    return out


async def load_overrides(session: AsyncSession) -> dict[str, dict]:
    value = await session.scalar(select(AppSetting.value).where(AppSetting.key == SETTINGS_KEY))
    return value if isinstance(value, dict) else {}


async def save_override(session: AsyncSession, provider: str, changes: dict[str, int | None]
                        ) -> None:
    """Set `per_hour` / `per_day` of a provider (None: back to the default, 0: none)."""
    all_ = dict(await load_overrides(session))
    mine = dict(all_.get(provider) or {})
    for k, v in changes.items():
        if v is None:
            mine.pop(k, None)
        else:
            mine[k] = int(v)
    if mine:
        all_[provider] = mine
    else:
        all_.pop(provider, None)
    stmt = insert(AppSetting).values(key=SETTINGS_KEY, value=all_)
    await session.execute(stmt.on_conflict_do_update(index_elements=["key"],
                                                     set_={"value": all_}))
    await session.flush()


def _pick(override: dict, key: str, default: int | None) -> int | None:
    if key not in override or override[key] is None:
        return default
    v = int(override[key])
    return v if v > 0 else None


def budget_of(cls, rate_limit_per_min: int | None, override: dict | None) -> Budget:
    """The effective budget of a provider class (defaults, then the admin's values)."""
    o = override or {}
    per_min = rate_limit_per_min if rate_limit_per_min is not None else getattr(
        cls, "default_rate_limit_per_min", None)
    return Budget(per_min,
                  _pick(o, "per_hour", getattr(cls, "default_hourly_budget", None)),
                  _pick(o, "per_day", getattr(cls, "default_daily_budget", None)))


def usage_dto(budget: Budget, u: Usage | None, now: datetime) -> dict:
    """The quota block of a connector («Connettori», «Salute del sistema»)."""
    u = u or Usage(0, 0, 0)
    reason = u.demoted(budget)

    def pct(used: int, total: int | None) -> float | None:
        return round(100 * used / total, 1) if total else None

    return {
        "per_min": budget.per_min, "per_hour": budget.per_hour, "per_day": budget.per_day,
        "used_minute": u.minute, "used_hour": u.hour, "used_day": u.day,
        "pct_minute": pct(u.minute, budget.per_min), "pct_hour": pct(u.hour, budget.per_hour),
        "pct_day": pct(u.day, budget.per_day),
        "demoted": reason is not None, "demoted_reason": reason,
        "demoted_until": (u.blocked_until.isoformat() if u.blocked_until
                          else next_reset(reason, now).isoformat() if reason else None),
        "resets_at": next_reset(DAY, now).isoformat(),
    }
