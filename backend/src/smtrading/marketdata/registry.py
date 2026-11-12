from smtrading.marketdata.provider import DataProvider

PROVIDERS: dict[str, type[DataProvider]] = {}


def register[T: type[DataProvider]](cls: T) -> T:
    PROVIDERS[cls.name] = cls
    return cls
