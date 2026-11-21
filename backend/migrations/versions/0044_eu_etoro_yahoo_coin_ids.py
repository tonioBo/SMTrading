"""Market data fixes (data only):

- `etoro` appended to the existing EU stocks quote/bars priority rows, after the admin's
  order (the seed never touches existing rows): Twelve Data's and Finnhub's plans do not
  cover EU listings, so Yahoo was the only working EU source and a Yahoo glitch left the
  EU symbols without a price;
- the Yahoo id merged into the provider symbols of four recent coins Yahoo lists as
  `<TICKER><id>-USD` (without it every Yahoo call answered «possibly delisted»); an
  existing Yahoo override (the admin's) is kept.

Downgrade: `etoro` leaves the EU quote/bars rows, and the four Yahoo ids are removed where
they still are the ones written here.

Revision ID: 0044
"""
import json

from alembic import op
from sqlalchemy import text

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

_PAIRS = "('eu_equity', 'quote'), ('eu_equity', 'bars')"
YAHOO_IDS = {
    "ASTER-USD": "ASTER36341-USD",
    "CC-USD": "CC37263-USD",
    "HYPE-USD": "HYPE32196-USD",
    "PEARL-USD": "PEARL41876-USD",
}


def upgrade() -> None:
    op.execute(
        "UPDATE provider_priorities SET providers = providers || '[\"etoro\"]'::jsonb"
        f" WHERE (asset_class, capability) IN ({_PAIRS})"
        " AND NOT providers @> '[\"etoro\"]'::jsonb"
    )
    for symbol, yahoo in YAHOO_IDS.items():
        op.get_bind().execute(
            text(
                "UPDATE instruments SET provider_symbols ="
                " coalesce(provider_symbols, '{}'::jsonb) || cast(:p as jsonb)"
                " WHERE symbol = :s AND NOT coalesce(provider_symbols, '{}'::jsonb) ? 'yahoo'"
            ),
            {"s": symbol, "p": json.dumps({"yahoo": yahoo})},
        )


def downgrade() -> None:
    op.execute(
        "UPDATE provider_priorities SET providers = providers - 'etoro'"
        f" WHERE (asset_class, capability) IN ({_PAIRS})"
    )
    for symbol, yahoo in YAHOO_IDS.items():
        op.get_bind().execute(
            text(
                "UPDATE instruments SET provider_symbols = provider_symbols - 'yahoo'"
                " WHERE symbol = :s AND provider_symbols ->> 'yahoo' = :y"
            ),
            {"s": symbol, "y": yahoo},
        )
