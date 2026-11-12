from enum import StrEnum


class ErrorCode(StrEnum):
    INSUFFICIENT_BUYING_POWER = "INSUFFICIENT_BUYING_POWER"
    MARKET_CLOSED_QUEUED = "MARKET_CLOSED_QUEUED"
    SYMBOL_NOT_ALLOWED = "SYMBOL_NOT_ALLOWED"
    SYMBOL_NOT_FOUND = "SYMBOL_NOT_FOUND"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    REDUCE_ONLY = "REDUCE_ONLY"
    PORTFOLIO_LOCKED = "PORTFOLIO_LOCKED"
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


class SMTError(Exception):
    def __init__(self, code: ErrorCode, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {"code": str(self.code), "message": self.message, "details": self.details}
