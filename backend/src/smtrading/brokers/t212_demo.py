from typing import ClassVar

from smtrading.brokers.registry import register
from smtrading.brokers.t212_core import (  # noqa: F401  (re-exported)
    _STATUS,
    _cache,
    _reverse,
    _T212Protocol,
    resolve_ticker,
)

DEMO_URL = "https://demo.trading212.com"


@register
class T212DemoAdapter(_T212Protocol):
    kind: ClassVar[str] = "t212_demo"
    BASE_URL: ClassVar[str] = DEMO_URL
