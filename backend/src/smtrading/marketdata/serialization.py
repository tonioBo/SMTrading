import json
import types
import typing
from dataclasses import fields, is_dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from smtrading.marketdata.types import Bar, Quote


def _encode(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _encode(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.astimezone(UTC).isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, (list, tuple)):
        return [_encode(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _encode(v) for k, v in obj.items()}
    return obj


def dumps(obj: Any) -> str:
    return json.dumps(_encode(obj), sort_keys=True)


def _decode(value: Any, tp: Any) -> Any:
    origin = typing.get_origin(tp)
    if origin is typing.Union or origin is types.UnionType:
        if value is None:
            return None
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        return _decode(value, args[0])
    if value is None:
        return None
    if origin is Literal:
        return value
    if origin is tuple:
        return tuple(_decode(v, typing.get_args(tp)[0]) for v in value)
    if origin is list:
        return [_decode(v, typing.get_args(tp)[0]) for v in value]
    if origin is dict or tp is dict:
        return dict(value)
    if tp is Decimal:
        return Decimal(value)
    if tp is datetime:
        dt = datetime.fromisoformat(value)
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    if tp is date:
        return date.fromisoformat(value)
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(value)
    return value


def loads[T](s: str, cls: type[T]) -> T:
    raw = json.loads(s)
    hints = typing.get_type_hints(cls)
    return cls(**{f.name: _decode(raw[f.name], hints[f.name]) for f in fields(cls) if f.name in raw})  # type: ignore[call-arg]


def loads_quote(s: str) -> Quote:
    return loads(s, Quote)


def loads_bars(s: str) -> list[Bar]:
    return [loads(json.dumps(item), Bar) for item in json.loads(s)]
