"""Pure cost functions over Paperclip heartbeat runs (amounts in USD, as in Paperclip)."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from zoneinfo import ZoneInfo

Bucket = Literal["api", "subscription", "other"]
# the run's date: the first of these fields present
DATE_FIELDS = ("startedAt", "createdAt", "finishedAt")


@dataclass
class CostRow:
    api_usd: Decimal = Decimal(0)
    subscription_usd: Decimal = Decimal(0)
    other_usd: Decimal = Decimal(0)
    runs: int = 0


def _amount(value: Any) -> Decimal | None:
    """A number ≥ 0 (numbers or numeric texts; booleans are not numbers), else None."""
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() and d >= 0 else None


def billed_usd(usage: dict) -> Decimal | None:
    """`cacheAdjustedCostUsd` when it is a number ≥ 0, else `costUsd` when ≥ 0, else None."""
    if not isinstance(usage, dict):
        return None
    for key in ("cacheAdjustedCostUsd", "costUsd"):
        amount = _amount(usage.get(key))
        if amount is not None:
            return amount
    return None


def bucket(billing_type: str | None) -> Bucket:
    if billing_type == "metered_api":
        return "api"
    if billing_type in ("subscription_included", "subscription_overage"):
        return "subscription"
    return "other"


def run_time(run: dict) -> datetime | None:
    for key in DATE_FIELDS:
        value = run.get(key)
        if isinstance(value, str) and value:
            try:
                ts = datetime.fromisoformat(value)
            except ValueError:
                continue
            return ts if ts.tzinfo else None
    return None


def daily_costs(runs: list[dict], tz: str, *, truncated: bool = True
                ) -> dict[tuple[date, str], CostRow]:
    """Runs grouped by local day (`tz`) and agent id. With `truncated` (the answer was
    capped by the request limit) the day of the oldest run received is dropped: older runs
    of that day were not received, so it is incomplete. Runs without an agent or a date are
    ignored."""
    zone = ZoneInfo(tz)
    out: dict[tuple[date, str], CostRow] = {}
    oldest: datetime | None = None
    for run in runs:
        if not isinstance(run, dict):
            continue
        agent, ts = run.get("agentId"), run_time(run)
        if not agent or ts is None:
            continue
        oldest = ts if oldest is None or ts < oldest else oldest
        usage = run.get("usageJson") if isinstance(run.get("usageJson"), dict) else {}
        row = out.setdefault((ts.astimezone(zone).date(), str(agent)), CostRow())
        row.runs += 1
        amount = billed_usd(usage)
        if amount is not None:
            field = f"{bucket(usage.get('billingType'))}_usd"
            setattr(row, field, getattr(row, field) + amount)
    if truncated and oldest is not None:
        first_day = oldest.astimezone(zone).date()
        out = {k: v for k, v in out.items() if k[0] != first_day}
    return out
