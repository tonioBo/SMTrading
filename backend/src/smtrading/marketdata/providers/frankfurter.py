import json
import re
from datetime import UTC, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from smtrading.instruments.models import AssetClass
from smtrading.marketdata.provider import DataProvider, ProviderUnsupported
from smtrading.marketdata.registry import register
from smtrading.marketdata.types import Capability, Quote

BASE_URL = "https://api.frankfurter.dev/v1"
_ECB_TZ = ZoneInfo("Europe/Berlin")
_ECB_REFERENCE_TIME = time(16, 0)
_PAIR_RE = re.compile(r"^([A-Z]{3})/([A-Z]{3})$")


@register
class FrankfurterProvider(DataProvider):
    name = "frankfurter"
    capabilities = frozenset({Capability.fx, Capability.quote})
    asset_classes = frozenset({AssetClass.forex})
    requires_key = False
    default_rate_limit_per_min = 60

    def _default_symbol(self, symbol: str, asset_class: AssetClass) -> str:
        if asset_class != AssetClass.forex or not re.fullmatch(r"[A-Z]{6}", symbol):
            raise self._unsupported(f"symbol {symbol}")
        return f"{symbol[:3]}/{symbol[3:]}"

    async def _latest(self, base: str, quote: str) -> tuple[Decimal, datetime]:
        resp = await self.http.get(
            f"{BASE_URL}/latest", params={"base": base, "symbols": quote}
        )
        resp.raise_for_status()
        data = json.loads(resp.text, parse_float=Decimal)
        try:
            rate = Decimal(str(data["rates"][quote]))
            day = datetime.strptime(data["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError, TypeError) as exc:
            raise ProviderUnsupported(f"frankfurter: no rate for {base}/{quote}") from exc
        ts = datetime.combine(day, _ECB_REFERENCE_TIME, tzinfo=_ECB_TZ).astimezone(UTC)
        return rate, ts

    async def get_fx_rate(self, base: str, quote: str) -> Decimal:
        rate, _ = await self._latest(base, quote)
        return rate

    async def get_quotes(self, items: list[tuple[str, str]]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for canonical, provider_symbol in items:
            m = _PAIR_RE.match(provider_symbol)
            if not m:
                raise self._unsupported(f"symbol {provider_symbol}")
            rate, ts = await self._latest(m.group(1), m.group(2))
            out[canonical] = Quote(
                symbol=canonical, bid=None, ask=None, last=rate, ts=ts, provider=self.name
            )
        return out

    async def health_check(self) -> None:
        await self._latest("EUR", "USD")
