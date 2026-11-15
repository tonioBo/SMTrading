from enum import StrEnum


class ErrorCode(StrEnum):
    INSUFFICIENT_BUYING_POWER = "INSUFFICIENT_BUYING_POWER"
    MARKET_CLOSED_QUEUED = "MARKET_CLOSED_QUEUED"
    SYMBOL_NOT_ALLOWED = "SYMBOL_NOT_ALLOWED"
    SYMBOL_NOT_FOUND = "SYMBOL_NOT_FOUND"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    TRADING_HALTED = "TRADING_HALTED"
    REDUCE_ONLY = "REDUCE_ONLY"
    PORTFOLIO_LOCKED = "PORTFOLIO_LOCKED"
    PORTFOLIO_ARCHIVED = "PORTFOLIO_ARCHIVED"
    STALE_QUOTE = "STALE_QUOTE"
    RISK_LIMIT_EXCEEDED = "RISK_LIMIT_EXCEEDED"
    SHORT_NOT_ALLOWED = "SHORT_NOT_ALLOWED"
    INVALID_ORDER = "INVALID_ORDER"
    NOT_FOUND = "NOT_FOUND"
    FORBIDDEN = "FORBIDDEN"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    INVALID_AMOUNT = "INVALID_AMOUNT"
    LEDGER_UNBALANCED = "LEDGER_UNBALANCED"
    LEDGER_INVALID = "LEDGER_INVALID"
    ALREADY_REVERSED = "ALREADY_REVERSED"
    INVALID_REVERSAL = "INVALID_REVERSAL"
    FEE_RULE_NOT_FOUND = "FEE_RULE_NOT_FOUND"
    LIVE_NOT_ARMED = "LIVE_NOT_ARMED"
    LIVE_DRY_RUN = "LIVE_DRY_RUN"
    LIVE_CASH_LIMIT = "LIVE_CASH_LIMIT"
    VERSION_CONFLICT = "VERSION_CONFLICT"  # a stale base version (admin editors, HTTP 409)
    PASSWORD_CHANGE_REQUIRED = "PASSWORD_CHANGE_REQUIRED"  # temporary password (HTTP 403)


class SMTError(Exception):
    """`persisted` is True when the error reports a refusal already written to the session
    (a rejected order): the caller should commit to keep it."""

    def __init__(
        self, code: ErrorCode, message: str, details: dict | None = None, *,
        persisted: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.persisted = persisted

    def to_dict(self) -> dict:
        return {"code": str(self.code), "message": self.message, "details": self.details}
