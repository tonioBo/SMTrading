from smtrading.brokers.base import BrokerAdapter
from smtrading.common.errors import ErrorCode, SMTError

ADAPTERS: dict[str, type[BrokerAdapter]] = {}


def register(cls: type[BrokerAdapter]) -> type[BrokerAdapter]:
    ADAPTERS[cls.kind] = cls
    return cls


def adapter_class(kind: str) -> type[BrokerAdapter]:
    _load()
    try:
        return ADAPTERS[kind]
    except KeyError:
        raise SMTError(ErrorCode.INVALID_ORDER, f"unknown broker kind {kind}") from None


def _load() -> None:
    from smtrading.brokers import alpaca_paper, t212_demo  # noqa: F401  (registers on import)
    from smtrading.brokers.live import alpaca_live, t212_live  # noqa: F401
