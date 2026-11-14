"""Trading 212 live (real money). Built only on a LiveClient: every request goes through
the live gate. The URL is this class constant, never configuration. Placement is not
idempotent (no client reference): an unknown outcome is looked up, never resent.

Docs: https://docs.trading212.com/api (OpenAPI https://docs.trading212.com/_bundle/api.yaml).
"""

from typing import ClassVar

from smtrading.brokers.live import require_live_construction
from smtrading.brokers.registry import register
from smtrading.brokers.t212_core import _T212Protocol
from smtrading.common.errors import ErrorCode, SMTError
from smtrading.engine.models import OrderType

DEFAULT_ORDER_TYPES = ("market", "limit", "stop", "stop_limit")


@register
class T212LiveAdapter(_T212Protocol):
    """Config: `api_key`, `api_secret`, `order_types` (default all four T212 types; a
    list or a comma-separated text). Quantity orders only."""

    kind: ClassVar[str] = "t212_live"
    live: ClassVar[bool] = True
    idempotent_place: ClassVar[bool] = False
    BASE_URL: ClassVar[str] = "https://live.trading212.com"

    def __init__(self, config: dict, http) -> None:
        require_live_construction(type(self), config, http)
        super().__init__(config, http)
        self.supported_types = self._order_types(config.get("order_types"))

    @classmethod
    def _order_types(cls, value: object) -> frozenset[OrderType]:
        if value is None:
            value = DEFAULT_ORDER_TYPES
        names = value.split(",") if isinstance(value, str) else value
        if not isinstance(names, list | tuple):
            raise SMTError(ErrorCode.INVALID_ORDER, "order_types deve essere un elenco")
        out: set[OrderType] = set()
        for name in names:
            key = str(name).strip().lower()
            if key not in DEFAULT_ORDER_TYPES:
                raise SMTError(ErrorCode.INVALID_ORDER,
                               f"tipo d'ordine non ammesso per Trading 212: {key}")
            out.add(OrderType(key))
        if not out:
            raise SMTError(ErrorCode.INVALID_ORDER, "order_types non può essere vuoto")
        return frozenset(out) & cls.supported_types
