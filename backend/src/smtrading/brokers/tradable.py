"""What a broker lets SMTrading do with one instrument (plan 44): the value the adapters'
`tradability` answer, cached per (broker link, symbol) by `brokers.tradability`.

- `tradable`: SMTrading can open a position there (true), cannot (false), not known
  (None: never read, or the broker did not answer);
- `long_unleveraged`: a buy without leverage (what SMTrading sends) is accepted;
- `short`: a short sale can be opened; `fractional`: fractional quantities;
  `partial_close`: part of a position can be sold (eToro: per instrument);
- `code`: why it is not tradable (None when it is), `reason` its neutral Italian text,
  the same for every broker (agents read it: no broker name, no account kind).
"""

import json
from dataclasses import asdict, dataclass

REASONS: dict[str, str] = {
    "not_found": "non disponibile presso il broker",
    "asset_class": "non disponibile presso il broker",
    "leverage_only": "solo con leva presso il broker",
    "tax_form": "richiede un modulo fiscale presso il broker",
    "not_tradable": "non negoziabile al momento presso il broker",
    "unknown": "verifica presso il broker non riuscita: riprova più tardi",
    "not_in_catalog": "strumento non trovato",
}


@dataclass(frozen=True)
class Tradability:
    tradable: bool | None
    long_unleveraged: bool | None = None
    short: bool | None = None
    fractional: bool | None = None
    partial_close: bool | None = None
    code: str | None = None

    @property
    def reason(self) -> str | None:
        return REASONS.get(self.code) if self.code else None

    @property
    def buy_refused(self) -> bool:
        """The broker is known to refuse an opening buy (not found, leverage only...)."""
        return self.tradable is False or self.long_unleveraged is False

    def dto(self) -> dict:
        return {"tradable": self.tradable, "long_unleveraged": self.long_unleveraged,
                "short": self.short, "fractional": self.fractional,
                "partial_close": self.partial_close, "reason": self.reason}

    def dumps(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def loads(cls, raw: str | bytes) -> "Tradability | None":
        try:
            data = json.loads(raw)
            return cls(**{k: data.get(k) for k in
                          ("tradable", "long_unleveraged", "short", "fractional",
                           "partial_close", "code")})
        except (ValueError, TypeError, AttributeError):
            return None


def unknown() -> Tradability:
    return Tradability(None, code="unknown")


def not_found(code: str = "not_found") -> Tradability:
    return Tradability(False, False, False, None, None, code)


def buyable(*, short: bool | None = False, fractional: bool | None = None,
            partial_close: bool | None = True) -> Tradability:
    return Tradability(True, True, short, fractional, partial_close, None)
