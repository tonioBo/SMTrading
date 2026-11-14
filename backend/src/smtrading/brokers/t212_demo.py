from typing import ClassVar

from smtrading.brokers.registry import register
from smtrading.brokers.t212_core import (  # noqa: F401  (re-exported)
    _STATUS,
    _T212Protocol,
    instrument_cache,
    resolve_ticker,
)

DEMO_URL = "https://demo.trading212.com"

# The demo environment's instrument cache (aliases kept for the plan-6 tests).
_cache = instrument_cache(DEMO_URL).data
_reverse = instrument_cache(DEMO_URL).reverse


@register
class T212DemoAdapter(_T212Protocol):
    kind: ClassVar[str] = "t212_demo"
    BASE_URL: ClassVar[str] = DEMO_URL
