"""The automatic-rule models (pure): parameter bounds, entry triggers and brakes.

A model's `entry` gets the market state of the symbol (closed one-minute bars and the
session VWAP), the current price and the validated parameters; it returns the trigger
values (stored in `auto_trades.trigger_values`) or None. Long entries only."""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from smtrading.autorules.indicators import MinuteSeries
from smtrading.common.errors import ErrorCode, SMTError

D = Decimal
Kind = Literal["dec", "int", "choice", "opt_dec"]


@dataclass(frozen=True)
class Bound:
    lo: Decimal | int | None
    hi: Decimal | int | None
    default: Decimal | int | str | None
    kind: Kind
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketState:
    series: MinuteSeries
    vwap: Decimal | None
    session_start: datetime | None = None  # start of the VWAP's session


@dataclass(frozen=True)
class ModelSpec:
    name: str
    params: dict[str, Bound]
    warmup: Callable[[dict], int]
    entry: Callable[[MarketState, Decimal, dict], dict | None]


COMMON_PARAMS: dict[str, Bound] = {
    "stop_pct": Bound(D("0.05"), D(5), D("0.3"), "dec"),
    "target_pct": Bound(D("0.05"), D(10), D("0.5"), "dec"),
    "max_hold_s": Bound(10, 3600, 300, "int"),
    "no_entry_first_min": Bound(0, 60, 5, "int"),
    "no_entry_last_min": Bound(0, 120, 15, "int"),
    "cooldown_s": Bound(0, 3600, 60, "int"),
    "entry": Bound(None, None, "market", "choice", ("market", "limit")),
    "limit_offset_pct": Bound(D(0), D("0.5"), D("0.05"), "dec"),
    "entry_timeout_s": Bound(1, 60, 5, "int"),
    "max_age_s": Bound(D("0.5"), D(10), D(2), "dec"),
    "max_spread_pct": Bound(D("0.01"), D(1), D("0.10"), "dec"),
}


def _breakout(state: MarketState, price: Decimal, p: dict) -> dict | None:
    n = p["lookback_min"]
    high = state.series.highest(n)
    if high is None or not price > high:
        return None
    last = state.series.closed(1)[-1]
    avg = state.series.avg_volume(n)
    mult = p["volume_mult"]
    if mult is not None and (avg is None or avg <= 0 or not last.volume >= mult * avg):
        return None
    return {"high": high, "volume": last.volume, "avg_volume": avg}


def _mean_reversion(state: MarketState, price: Decimal, p: dict) -> dict | None:
    ms = state.series.mean_std(p["lookback_min"])
    if ms is None:
        return None
    mean, std = ms
    if std <= 0 or not price <= mean - p["z"] * std:
        return None
    return {"mean": mean, "std": std, "z_now": (price - mean) / std}


def _vwap_pullback(state: MarketState, price: Decimal, p: dict) -> dict | None:
    vwap = state.vwap
    n = p["min_above_min"]
    if vwap is None or vwap <= 0 or state.session_start is None:
        return None
    bars = state.series.closed(n)
    # only this session's bars count: the VWAP knows nothing of earlier ones
    if len(bars) < n or any(b.ts < state.session_start for b in bars):
        return None
    if not all(b.close > vwap for b in bars):
        return None
    if not vwap <= price <= vwap * (1 + p["pullback_pct"] / 100):
        return None
    return {"vwap": vwap, "distance_pct": (price - vwap) / vwap * 100}


MODELS: dict[str, ModelSpec] = {
    "breakout": ModelSpec(
        "breakout",
        {"lookback_min": Bound(5, 240, 20, "int"),
         "volume_mult": Bound(D(1), D(10), None, "opt_dec")},
        lambda p: p["lookback_min"],
        _breakout,
    ),
    "mean_reversion": ModelSpec(
        "mean_reversion",
        {"lookback_min": Bound(10, 240, 30, "int"), "z": Bound(D(1), D(5), D(2), "dec")},
        lambda p: p["lookback_min"],
        _mean_reversion,
    ),
    "vwap_pullback": ModelSpec(
        "vwap_pullback",
        {"pullback_pct": Bound(D("0.01"), D(2), D("0.1"), "dec"),
         "min_above_min": Bound(1, 60, 5, "int")},
        lambda p: p["min_above_min"],  # plus the session's bars for the VWAP
        _vwap_pullback,
    ),
}


# --- validation ---------------------------------------------------------------------

def _it(v: Decimal | int) -> str:
    return format(v, "f").replace(".", ",") if isinstance(v, Decimal) else str(v)


def _invalid(message: str, key: str) -> SMTError:
    return SMTError(ErrorCode.INVALID_ORDER, message, {"param": key})


def _to_decimal(v: object) -> Decimal | None:
    """Decimal, int or numeric string (JSON-stored decimals); None for anything else."""
    if isinstance(v, bool):
        return None
    if isinstance(v, Decimal | int):
        d = D(v)
    elif isinstance(v, str):
        try:
            d = D(v.strip())
        except InvalidOperation:
            return None
    else:
        return None
    return d if d.is_finite() else None


def _coerce(label: str, k: str, b: Bound, v: object) -> Decimal | int | str | None:
    if b.kind == "choice":
        if v not in b.choices:
            raise _invalid(f"{label} {k}: valori ammessi {', '.join(b.choices)}", k)
        return v  # type: ignore[return-value]
    if b.kind == "opt_dec" and v is None:
        return None
    if b.kind == "int":
        if isinstance(v, bool) or not isinstance(v, int):
            raise _invalid(f"{label} {k}: atteso un numero intero", k)
        val: Decimal | int = v
    else:
        d = _to_decimal(v)
        if d is None:
            raise _invalid(f"{label} {k}: atteso un numero", k)
        val = d
    if (b.lo is not None and val < b.lo) or (b.hi is not None and val > b.hi):
        raise _invalid(f"{label} {k} fuori dai limiti {_it(b.lo)}–{_it(b.hi)}", k)
    return val


LIMIT_OFFSET_MESSAGE = ("parametro limit_offset_pct: con l'entrata limit deve essere minore "
                        "di target_pct")


def validate_params(model: str, params: dict) -> dict:
    """The rule's parameters with the defaults filled in; unknown keys, wrong types and
    out-of-bounds values → INVALID_ORDER. A `limit` entry's offset must stay below the
    target (else the limit would sit at or above the take-profit)."""
    spec = MODELS.get(model)
    if spec is None:
        raise SMTError(ErrorCode.INVALID_ORDER, f"modello sconosciuto: {model}", {"model": model})
    bounds = {**COMMON_PARAMS, **spec.params}
    for k in params:
        if k not in bounds:
            raise _invalid(f"parametro sconosciuto: {k}", k)
    out: dict = {}
    for k, b in bounds.items():
        out[k] = _coerce("parametro", k, b, params[k]) if k in params else b.default
    if out["entry"] == "limit" and out["limit_offset_pct"] >= out["target_pct"]:
        raise _invalid(LIMIT_OFFSET_MESSAGE, "limit_offset_pct")
    return out


def model_catalog() -> list[dict]:
    """The models with every parameter's bounds, for the portal."""
    out = []
    for spec in MODELS.values():
        params = [
            {"name": k, "kind": b.kind, "lo": b.lo, "hi": b.hi, "default": b.default,
             "choices": list(b.choices), "common": common}
            for common, group in ((False, spec.params), (True, COMMON_PARAMS))
            for k, b in group.items()
        ]
        out.append({"name": spec.name, "params": params})
    return out


# --- brakes -------------------------------------------------------------------------

@dataclass(frozen=True)
class Brakes:
    max_trades_hour: int | None
    max_trades_day: int | None
    max_loss_day: Decimal | None  # in the portfolio's currency
    max_loss_day_pct: Decimal | None
    max_consecutive_losses: int | None
    max_open_positions: int | None


# the spec gives no numbers: the plan's choices
RULE_BRAKE_DEFAULTS = Brakes(20, 100, None, D(1), 3, 1)
PORTFOLIO_BRAKE_DEFAULTS = Brakes(60, 300, None, D(2), 5, 5)

_BRAKE_BOUNDS: dict[str, Bound] = {
    "max_trades_hour": Bound(1, 120, None, "int"),
    "max_trades_day": Bound(1, 1000, None, "int"),
    "max_loss_day": Bound(None, None, None, "opt_dec"),  # > 0
    "max_loss_day_pct": Bound(D(0), D(50), None, "opt_dec"),  # (0, 50]
    "max_consecutive_losses": Bound(1, 20, None, "int"),
    "max_open_positions": Bound(1, 30, None, "int"),
}


def validate_brakes(d: dict, *, level: Literal["rule", "portfolio"]) -> Brakes:
    """The brakes of a rule or a portfolio: missing keys take the level's default, None
    switches a brake off; a rule holds at most one position."""
    defaults = RULE_BRAKE_DEFAULTS if level == "rule" else PORTFOLIO_BRAKE_DEFAULTS
    for k in d:
        if k not in _BRAKE_BOUNDS:
            raise _invalid(f"freno sconosciuto: {k}", k)
    values: dict = {}
    for k, b in _BRAKE_BOUNDS.items():
        if k not in d:
            values[k] = getattr(defaults, k)
            continue
        v = d[k]
        if v is None:
            values[k] = None
            continue
        val = _coerce("freno", k, b, v)
        if k in ("max_loss_day", "max_loss_day_pct") and not val > 0:  # type: ignore[operator]
            raise _invalid(f"freno {k}: deve essere maggiore di 0", k)
        values[k] = val
    if level == "rule":
        values["max_open_positions"] = 1
    return Brakes(**values)
