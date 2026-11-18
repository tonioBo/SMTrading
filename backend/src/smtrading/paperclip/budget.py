"""Monthly agent-cost budget: month-to-date spend, daily average, projection to month end
and per-agent share, from `agent_cost_days` (local days, USD).

The spend counted against the budget is what Paperclip bills: «a consumo API» plus «altro»
(credits and the like). Runs «incluse nell'abbonamento» are not billed (Paperclip counts
them 0 against its own budget), so they are left out.

The budget is the Paperclip company's `budgetMonthlyCents` (read by the reader, stored in
the reader state) when it is > 0, else `monthly_budget_usd` of the Paperclip settings,
else none (no percentages, no alerts).

Alerts (Telegram, category `system`): once per month per threshold, deduplicated in the
reader state (`budget_alerted`, an app setting: it survives a restart):
- `spent`: month-to-date spend ≥ 80 % of the budget;
- `projection`: projection > budget, only from the 3rd day of the month (one expensive
  first day would project far over the budget and use up the month's alert)."""

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.paperclip.costs import _amount
from smtrading.paperclip.models import AgentCostDay

SPENT_ALERT_PCT = Decimal(80)
PROJECTION_MIN_DAYS = 3
CENT = Decimal("0.01")


@dataclass(frozen=True)
class Budget:
    usd: Decimal | None
    source: str | None  # "paperclip" | "settings" | None


def resolve_budget(company_budget_cents: Any, settings_usd: Any) -> Budget:
    cents = _amount(company_budget_cents)
    if cents is not None and cents > 0:
        return Budget((cents / 100).quantize(CENT), "paperclip")
    usd = _amount(settings_usd)
    if usd is not None and usd > 0:
        return Budget(usd, "settings")
    return Budget(None, None)


def month_key(today: date) -> str:
    return f"{today.year:04d}-{today.month:02d}"


async def month_spend(session: AsyncSession, today: date) -> list[tuple[str, str | None, Decimal]]:
    """(paperclip agent id, name, billed USD) from the 1st of `today`'s month to `today`."""
    rows = (await session.execute(
        select(AgentCostDay.paperclip_agent_id, func.max(AgentCostDay.paperclip_agent_name),
               func.sum(AgentCostDay.api_usd + AgentCostDay.other_usd))
        .where(AgentCostDay.day >= today.replace(day=1), AgentCostDay.day <= today)
        .group_by(AgentCostDay.paperclip_agent_id)
        .order_by(AgentCostDay.paperclip_agent_id)
    )).all()
    return [(pid, name, Decimal(usd or 0)) for pid, name, usd in rows]


def pct(part: Decimal, whole: Decimal | None) -> Decimal | None:
    if not whole:
        return None
    return (part * 100 / whole).quantize(Decimal("0.1"), ROUND_HALF_UP)


def summary(today: date, spent: Decimal, budget: Budget) -> dict[str, Any]:
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    days = today.day  # today included
    daily = spent / days
    projected = daily * days_in_month
    return {
        "month": month_key(today), "from": today.replace(day=1), "to": today,
        "days_elapsed": days, "days_in_month": days_in_month,
        "spent_usd": spent.quantize(CENT, ROUND_HALF_UP),
        "daily_avg_usd": daily.quantize(CENT, ROUND_HALF_UP),
        "projected_usd": projected.quantize(CENT, ROUND_HALF_UP),
        "budget_usd": budget.usd, "budget_source": budget.source,
        "spent_pct": pct(spent, budget.usd),
        "projected_pct": pct(projected, budget.usd),
    }


def _usd(v: Decimal) -> str:
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def due_alerts(s: dict[str, Any], alerted: Any) -> list[tuple[str, str]]:
    """(threshold, text) still to send this month."""
    budget = s["budget_usd"]
    if budget is None:
        return []
    done = alerted.get(s["month"], []) if isinstance(alerted, dict) else []
    out = []
    if "spent" not in done and s["spent_pct"] >= SPENT_ALERT_PCT:
        out.append(("spent", (f"🟠 Costi agenti: spesi {_usd(s['spent_usd'])} $ su "
                             f"{_usd(budget)} $ del budget mensile ({s['spent_pct']:.0f}%).")))
    if ("projection" not in done and s["days_elapsed"] >= PROJECTION_MIN_DAYS
            and s["projected_usd"] > budget):
        out.append(("projection", (f"🟠 Costi agenti: a questo ritmo "
                                  f"({_usd(s['daily_avg_usd'])} $ al giorno) il mese "
                                  f"chiude a {_usd(s['projected_usd'])} $, oltre il budget "
                                  f"di {_usd(budget)} $.")))
    return out


def mark_alerted(alerted: Any, month: str, threshold: str) -> dict[str, list[str]]:
    """Only the current month is kept."""
    done = alerted.get(month, []) if isinstance(alerted, dict) else []
    return {month: sorted({*done, threshold})}
