"""The `goals` table (migration 0031).

- `horizon` week | month | quarter | year: the calendar period (Monday–Sunday, month,
  quarter, year, local time) containing `start_date`; with `recurring` the period rolls
  over by itself (the current one contains today). `custom`: from `start_date` to
  `end_date` included, never recurring.
- `scope` total (the live portfolios together) | portfolio (`portfolio_id`).
- `kind` min (the change to reach at least) | cap (a ceiling not to exceed) | max_loss
  (a loss not to reach: `target_pct` is the limit, e.g. 5 = at most −5%) | beat_index
  (beat `benchmark` by `target_pct` points, 0 allowed) (migration 0032).
- `benchmark` SPY | QQQ | VWCE.DE for beat_index, null otherwise.
- `target_pct`: the change in percent (2 decimals at most).
- `closes_positions` (migration 0035): the goal is a «piano»: the positions open at the end
  of its period are closed (`goals.plans`); `plan_since` the moment it last became one
  (only the periods ending after it are handled). At most one active piano covers a
  portfolio.

`goal_alerts`: the last status seen by the worker per goal and period (the status-change
alerts, `goals.alerts`), with the Paperclip issues opened for it. `benchmark_snapshots`:
the benchmark prices in EUR sampled by the worker (`goals.benchmark`). `plan_closures`: the
positions of a piano's ended period and how they are closed (`goals.plans`).
Goals are never deleted: DELETE sets `active` false."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from smtrading.common.db import Base

HORIZONS = ("week", "month", "quarter", "year", "custom")
SCOPES = ("total", "portfolio")
KINDS = ("min", "cap", "max_loss", "beat_index")
BENCHMARKS = ("SPY", "QQQ", "VWCE.DE")
MAX_NAME_LEN = 80


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class Goal(Base):
    __tablename__ = "goals"
    __table_args__ = (
        CheckConstraint(f"horizon in ({_in(HORIZONS)})", name="ck_goals_horizon"),
        CheckConstraint(f"scope in ({_in(SCOPES)})", name="ck_goals_scope"),
        CheckConstraint(f"kind in ({_in(KINDS)})", name="ck_goals_kind"),
        CheckConstraint("(scope = 'portfolio') = (portfolio_id IS NOT NULL)",
                        name="ck_goals_portfolio"),
        CheckConstraint("(horizon = 'custom') = (end_date IS NOT NULL)", name="ck_goals_end"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="ck_goals_dates"),
        CheckConstraint("NOT (recurring AND horizon = 'custom')", name="ck_goals_recurring"),
        CheckConstraint(f"(kind = 'beat_index') = (benchmark IS NOT NULL)"
                        f" AND (benchmark IS NULL OR benchmark in ({_in(BENCHMARKS)}))",
                        name="ck_goals_benchmark"),
        CheckConstraint("target_pct > 0 OR (kind = 'beat_index' AND target_pct >= 0)",
                        name="ck_goals_target"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(MAX_NAME_LEN))
    horizon: Mapped[str] = mapped_column(String(16))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    recurring: Mapped[bool] = mapped_column(Boolean, default=False)
    scope: Mapped[str] = mapped_column(String(16))
    portfolio_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("portfolios.id"))
    target_pct: Mapped[Decimal] = mapped_column(Numeric(9, 2))
    kind: Mapped[str] = mapped_column(String(16), default="min")
    benchmark: Mapped[str | None] = mapped_column(String(16))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    closes_positions: Mapped[bool] = mapped_column(Boolean, default=False,
                                                   server_default=false())
    plan_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class GoalAlert(Base):
    """The last status of a goal's period seen by the alerts job (`goals.alerts`):
    `alerted` the warning status announced and not recovered yet, `near_sent` the
    «vicino al limite» of a max_loss goal announced (once per period), `sent` the local
    date each event was last sent ({event: "YYYY-MM-DD"}), `issues` the Paperclip
    issues of the period per key ({"ident", "attempts" (refused answers), "retries" and
    "retry_at" (outages), "uncertain", "title", "description", "assignee",
    "priority"})."""

    __tablename__ = "goal_alerts"

    goal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("goals.id"), primary_key=True)
    period_start: Mapped[date] = mapped_column(Date, primary_key=True)
    status: Mapped[str] = mapped_column(String(24))
    kind: Mapped[str] = mapped_column(String(16))
    target_pct: Mapped[Decimal] = mapped_column(Numeric(9, 2))
    alerted: Mapped[str | None] = mapped_column(String(24))
    near_sent: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    sent: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    issues: Mapped[dict] = mapped_column(JSONB, default=dict,
                                         server_default=text("'{}'::jsonb"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BenchmarkSnapshot(Base):
    """A benchmark price at `ts`, with the FX to EUR of that moment (a backfilled daily
    close carries the ECB rate of its day)."""

    __tablename__ = "benchmark_snapshots"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    currency: Mapped[str] = mapped_column(String(16))
    fx_eur: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    price_eur: Mapped[Decimal] = mapped_column(Numeric(20, 8))


PLAN_STATES = ("pending", "grace", "closing", "closed")


class PlanClosure(Base):
    """A position open at the end of a piano's period (`goals.plans`): `pending` until
    its market's first session after the end, `grace` (in loss then) until `deadline`,
    `closing` while SMTrading's close order (`order_id`) is open, `closed` once flat
    (`closed_by` system | agent). `opened_at` and `qty` are the position's at the end; a
    position reopened since (another `opened_at`) is not this one any more."""

    __tablename__ = "plan_closures"
    __table_args__ = (
        UniqueConstraint("goal_id", "period_start", "portfolio_id", "symbol",
                         name="uq_plan_closures_position"),
        CheckConstraint(f"state in ({_in(PLAN_STATES)})", name="ck_plan_closures_state"),
        Index("ix_plan_closures_open", "portfolio_id", "symbol",
              postgresql_where=text("state <> 'closed'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    goal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("goals.id"))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    portfolio_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("portfolios.id"))
    symbol: Mapped[str] = mapped_column(String(64))
    qty: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16), default="pending")
    grace_days: Mapped[int] = mapped_column()
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    closed_by: Mapped[str | None] = mapped_column(String(16))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
