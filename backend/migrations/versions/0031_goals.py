"""Goals with horizons and scopes (plan 14): the `goals` table, and the weekly goal
setting (`app_settings["weekly_goal"]` = {"on", "pct"}, absent = on at +2%) moved into
it as a recurring weekly goal on the total named «settimanale» (active = on), so the
weekly goal keeps working with no action. The setting row is removed.

Downgrade: the setting is written back from the earliest recurring weekly minimum goal
on the total ({"on": its active, "pct": its target}; none = off at +2%), then the table
is dropped (the other goals are lost: older code knows only the weekly one).

Revision ID: 0031
"""
import json
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

SETTINGS_KEY = "weekly_goal"
DEFAULT_PCT = Decimal("2.0")
# the migrated goal has applied all along: its first period is the first week of 2026
FIRST_MONDAY = date(2026, 1, 5)


def _pct(raw) -> Decimal:
    value = raw.get("pct", str(DEFAULT_PCT)) if isinstance(raw, dict) else str(DEFAULT_PCT)
    if isinstance(value, bool) or not isinstance(value, str | int):
        return DEFAULT_PCT
    try:
        pct = Decimal(str(value).strip())
    except InvalidOperation:
        return DEFAULT_PCT
    if not pct.is_finite() or not 0 < pct <= 100 or pct != pct.quantize(Decimal("0.01")):
        return DEFAULT_PCT
    return pct


def upgrade() -> None:
    op.create_table(
        "goals",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("horizon", sa.String(16), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date()),
        sa.Column("recurring", sa.Boolean(), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("portfolio_id", sa.UUID(), sa.ForeignKey("portfolios.id")),
        sa.Column("target_pct", sa.Numeric(9, 2), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "horizon in ('week', 'month', 'quarter', 'year', 'custom')",
            name="ck_goals_horizon"),
        sa.CheckConstraint("scope in ('total', 'portfolio')", name="ck_goals_scope"),
        sa.CheckConstraint("kind in ('min', 'cap')", name="ck_goals_kind"),
        sa.CheckConstraint("(scope = 'portfolio') = (portfolio_id IS NOT NULL)",
                           name="ck_goals_portfolio"),
        sa.CheckConstraint("(horizon = 'custom') = (end_date IS NOT NULL)",
                           name="ck_goals_end"),
        sa.CheckConstraint("end_date IS NULL OR end_date >= start_date",
                           name="ck_goals_dates"),
        sa.CheckConstraint("NOT (recurring AND horizon = 'custom')",
                           name="ck_goals_recurring"),
        sa.CheckConstraint("target_pct > 0", name="ck_goals_target"),
    )
    bind = op.get_bind()
    raw = bind.execute(sa.text("SELECT value FROM app_settings WHERE key = :k"),
                       {"k": SETTINGS_KEY}).scalar()
    on = bool(raw.get("on", True)) if isinstance(raw, dict) else True
    bind.execute(sa.text(
        "INSERT INTO goals (id, name, horizon, start_date, recurring, scope, target_pct,"
        " kind, active) VALUES (:id, 'settimanale', 'week', :start, true, 'total', :pct,"
        " 'min', :on)"),
        {"id": uuid.uuid4(), "start": FIRST_MONDAY, "pct": _pct(raw), "on": on})
    bind.execute(sa.text("DELETE FROM app_settings WHERE key = :k"), {"k": SETTINGS_KEY})


def downgrade() -> None:
    bind = op.get_bind()
    row = bind.execute(sa.text(
        "SELECT active, target_pct FROM goals WHERE horizon = 'week' AND recurring"
        " AND scope = 'total' AND kind = 'min' ORDER BY created_at, id LIMIT 1")).first()
    value = ({"on": bool(row[0]), "pct": str(row[1])} if row is not None
             else {"on": False, "pct": str(DEFAULT_PCT)})
    bind.execute(sa.text(
        "INSERT INTO app_settings (key, value) VALUES (:k, CAST(:v AS jsonb))"
        " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"),
        {"k": SETTINGS_KEY, "v": json.dumps(value)})
    op.drop_table("goals")
