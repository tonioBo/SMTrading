import uuid
from typing import Annotated

from pydantic import BaseModel, Field, StrictBool, StrictInt

ShortStr = Annotated[str, Field(max_length=256)]
TotpCode = Annotated[str, Field(max_length=16)]


class LoginIn(BaseModel):
    email: ShortStr
    password: ShortStr
    totp: TotpCode | None = None


class TotpCodeIn(BaseModel):
    code: TotpCode


class PasswordChangeIn(BaseModel):
    current: ShortStr
    new: ShortStr


class AdminOut(BaseModel):
    email: str
    totp_enabled: bool
    role: str
    must_change_password: bool


class UserCreateIn(BaseModel):
    email: ShortStr
    role: ShortStr
    totp: TotpCode | None = None  # the actor's code, needed to create an admin


class UserRoleIn(BaseModel):
    role: ShortStr
    totp: TotpCode | None = None  # the actor's code, needed to promote to admin


class StepUpIn(BaseModel):
    totp: TotpCode | None = None  # the actor's code, needed to reset an admin's password


class TotpSetupOut(BaseModel):
    secret: str
    otpauth_uri: str


# --- portfolios ----------------------------------------------------------------------
# Money and ratios travel as strings (ints accepted); they are parsed with
# mcp.dto.parse_decimal, never through float.
Num = str | int


class PortfolioCreateIn(BaseModel):
    name: str
    initial_cash: Num
    base_currency: str = "EUR"
    leverage: Num = 1
    slippage_bps: int = 0
    timezone: str = "Europe/Rome"
    risk_profile_id: uuid.UUID | None = None
    in_totals: StrictBool = True


class PortfolioPatchIn(BaseModel):
    """Only the fields present in the request are changed (`model_fields_set`)."""

    name: str | None = None
    leverage: Num | None = None
    maintenance_margin_pct: Num | None = None
    warning_margin_pct: Num | None = None
    slippage_bps: int | None = None
    risk_profile_id: uuid.UUID | None = None
    timezone: str | None = None
    status: str | None = None
    # false: left out of the total (goals on the total, dashboard totals)
    in_totals: StrictBool | None = None


class CashMoveIn(BaseModel):
    amount: Num
    currency: str | None = None  # default: the portfolio base currency
    reason: str | None = None


class ReverseIn(BaseModel):
    reason: str = ""


class ResetIn(BaseModel):
    new_cash: Num
    currency: str = "EUR"
    reason: str = ""


# --- agents, fees, risk, settings ------------------------------------------------------


class AgentCreateIn(BaseModel):
    name: str


class AgentPatchIn(BaseModel):
    """Only the fields present in the request are changed (`model_fields_set`)."""

    active: bool | None = None
    can_notify: bool | None = None
    can_halt: bool | None = None


class AgentPortfolioIn(BaseModel):
    portfolio_id: uuid.UUID
    permission: str


class FeeTierIn(BaseModel):
    up_to: Num | None = None
    pct: Num


class FeeRuleCreateIn(BaseModel):
    name: str
    scope: str = "global"
    scope_ref: str | None = None
    asset_class: str | None = None  # portfolio scope only (null: every class)
    side: str = "both"
    currency: str = "EUR"
    fixed: Num = 0
    pct: Num = 0
    min_fee: Num | None = None
    max_fee: Num | None = None
    tiers: list[FeeTierIn] | None = None
    fx_pct: Num = 0
    priority: int = 0
    active: bool = True


class FeeRulePatchIn(BaseModel):
    """Only the fields present in the request are changed (`model_fields_set`)."""

    name: str | None = None
    scope: str | None = None
    scope_ref: str | None = None
    asset_class: str | None = None
    side: str | None = None
    currency: str | None = None
    fixed: Num | None = None
    pct: Num | None = None
    min_fee: Num | None = None
    max_fee: Num | None = None
    tiers: list[FeeTierIn] | None = None
    fx_pct: Num | None = None
    priority: int | None = None
    active: bool | None = None


class FeeSimulateIn(BaseModel):
    portfolio_id: uuid.UUID
    symbol: str
    side: str
    qty: Num | None = None
    notional: Num | None = None


class FeeRuleSimulateIn(BaseModel):
    """The rule that wins for (portfolio, asset class, side) and its fees on `notional`,
    expressed in the instrument `currency`."""

    portfolio_id: uuid.UUID
    asset_class: str
    side: str
    notional: Num
    currency: str


class FeePresetApplyIn(BaseModel):
    """The broker kind whose fee preset becomes the portfolio's fee rule."""

    broker_kind: str


class RiskProfileCreateIn(BaseModel):
    name: str
    max_order_notional_eur: Num | None = None
    max_position_pct: Num | None = None
    max_daily_loss_pct: Num | None = None
    max_daily_loss_eur: Num | None = None
    allowed_symbols: list[str] = []
    allowed_asset_classes: list[str] = []
    allowed_markets: list[str] = []
    short_allowed: bool = True
    description: str | None = None
    max_live_orders_per_hour: StrictInt | None = None  # R8.1 fuse; None: the default (30)


class RiskProfilePatchIn(BaseModel):
    """Only the fields present in the request are changed (`model_fields_set`)."""

    name: str | None = None
    max_order_notional_eur: Num | None = None
    max_position_pct: Num | None = None
    max_daily_loss_pct: Num | None = None
    max_daily_loss_eur: Num | None = None
    allowed_symbols: list[str] | None = None
    allowed_asset_classes: list[str] | None = None
    allowed_markets: list[str] | None = None
    short_allowed: bool | None = None
    description: str | None = None
    max_live_orders_per_hour: StrictInt | None = None


class InterestRateIn(BaseModel):
    kind: str
    key: str
    annual_pct: Num


class WeeklyGoalIn(BaseModel):
    on: StrictBool | None = None
    pct: Num | None = None


class PreopenCheckIn(BaseModel):
    on: StrictBool | None = None
    at: str | None = None  # HH:MM, local time


class HandoffWatchdogIn(BaseModel):
    on: StrictBool | None = None


class AppSettingsIn(BaseModel):
    """Per asset class maps and the default timezone; absent fields are left unchanged,
    a present map is merged class by class (and so are `weekly_goal`, `preopen_check`,
    `handoff_watchdog`)."""

    stale_quote_s: dict[str, StrictInt] | None = None
    default_spread_bps: dict[str, StrictInt] | None = None
    timezone: str | None = None
    mcp_hide_mode: StrictBool | None = None
    weekly_goal: WeeklyGoalIn | None = None
    preopen_check: PreopenCheckIn | None = None
    handoff_watchdog: HandoffWatchdogIn | None = None


class KillSwitchIn(BaseModel):
    on: bool
    cancel_open: bool = False
