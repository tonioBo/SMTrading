import uuid
from typing import Annotated

from pydantic import BaseModel, Field

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
