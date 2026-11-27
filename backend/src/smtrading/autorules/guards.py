"""The guards before an automatic entry (pure) and the per-day count of the entries they
blocked (`autorules:blocked:{rule_id}:{yyyymmdd}`, a Redis hash reason -> n, for the
evening summary; no database row).

The order of `check_entry` is the order of the checks: the first that fails is the
reason. The exits never pass through them (a stale bar or quote never holds an exit).
A quote's age is judged from its own event time (`quote.ts`), so the time it
spent queued before reaching the scalper counts."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from smtrading.marketdata.types import Quote

STALE = "stale"
SPREAD = "spread"
CLOSED = "closed"
BUSY = "busy"
NO_STREAM = "no_stream"
BROKER_DOWN = "broker_down"
BRAKED = "braked"
SIZE = "size"  # the rule's amount buys less than one lot: no order
REJECTED = "rejected"  # a risk refusal of the rule is cooling down: no new attempt
INACTIVE = "inactive"  # switch off, rule no longer active or a newer version at send time
PRICE_MOVED = "price_moved"  # the placement quote too far from the trigger's: no order
BARS_STALE = "bars_stale"  # the runner's newest minute bar is too old: frozen indicators

# the runner's newest minute bar closed longer ago than this with the market open: its
# indicators are frozen (stream bars and the REST fallback both failing), no entry
BARS_STALE_AFTER = timedelta(minutes=3)
BAR_LENGTH = timedelta(minutes=1)
BROKER_SILENT_AFTER = timedelta(seconds=10)  # BrokerSync writes its heartbeat every pass
MAX_FUTURE_S = Decimal(1)  # a quote stamped further ahead (clock skew) is not trusted
BLOCKED_KEY = "autorules:blocked:{rule_id}:{day}"
# a risk refusal's cooldown (the executor's; cleared when the rule is activated or resumed)
REJECT_KEY = "autorules:rejected:{rule_id}"
BLOCKED_TTL_S = 3 * 24 * 3600


def _seconds(d: timedelta) -> Decimal:
    """A timedelta in seconds, exactly (no float)."""
    return Decimal((d.days * 86400 + d.seconds) * 1_000_000 + d.microseconds) / 1_000_000


def check_entry(*, quote: Quote, now: datetime, params: dict, market_open: bool,
                busy: bool, stream: str, broker_last_pass: datetime | None,
                last_bar: datetime | None) -> str | None:
    """The reason an entry must not be sent now, or None. `last_bar`: the start of the
    runner's newest minute bar (None: no bar at all)."""
    if not market_open:
        return CLOSED
    if stream != "stream":
        return NO_STREAM
    if last_bar is None or now - (last_bar + BAR_LENGTH) > BARS_STALE_AFTER:
        return BARS_STALE
    if broker_last_pass is None or now - broker_last_pass > BROKER_SILENT_AFTER:
        return BROKER_DOWN
    age = _seconds(now - quote.ts)
    if age > Decimal(params["max_age_s"]) or age < -MAX_FUTURE_S:
        return STALE
    bid, ask = quote.bid, quote.ask
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return SPREAD
    mid = (bid + ask) / 2
    if (ask - bid) / mid * 100 > Decimal(params["max_spread_pct"]):
        return SPREAD
    if quote.suspect is not None:
        return STALE
    if busy:
        return BUSY
    return None


def blocked_key(rule_id, now: datetime) -> str:
    return BLOCKED_KEY.format(rule_id=rule_id, day=f"{now.astimezone(UTC):%Y%m%d}")


async def count_blocked(redis, rule_id, now: datetime, reason: str) -> None:
    """HINCRBY of the day's hash (UTC day of `now`), TTL 3 days."""
    key = blocked_key(rule_id, now)
    async with redis.pipeline(transaction=False) as pipe:
        pipe.hincrby(key, reason, 1)
        pipe.expire(key, BLOCKED_TTL_S)
        await pipe.execute()
