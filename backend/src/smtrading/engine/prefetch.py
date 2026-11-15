"""What to prefetch into a MemoQuoteSource before a call posts its first journal (I2).

The chain lock (taken by post_journal) is held until commit, so every quote and fx rate
used after the first journal of a transaction must already be in the memo: the
valuation of the portfolio (held symbols, every account currency -> EUR), and the
pre-trade check / execute_fill of further orders on `symbols` (instrument currency
<-> fee rule currencies, base and cash currencies -> instrument currency), and for a
portfolio sharing a real-money account the cash currencies of the other portfolios of
the link -> the account currency (their quotas, brokers.live.quotas).
"""

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smtrading.engine.quotesource import MemoQuoteSource
from smtrading.fees.resolver import load_rules
from smtrading.instruments.models import Instrument
from smtrading.ledger.models import Account, AccountBalance, AccountKind
from smtrading.portfolios.models import Portfolio

EUR = "EUR"
_MONEY_KINDS = (AccountKind.cash, AccountKind.collateral, AccountKind.margin_loan)


async def prefetch_portfolio(
    session: AsyncSession, memo: MemoQuoteSource, portfolio_id: UUID, *,
    symbols: Iterable[str] = (),
) -> None:
    """Best-effort prefetch for `portfolio_id` and orders on `symbols` (no-op if frozen)."""
    if memo.frozen:
        return
    rows = (await session.execute(
        select(Account.kind, Account.asset)
        .join(AccountBalance, AccountBalance.account_id == Account.id)
        .where(
            Account.portfolio_id == portfolio_id,
            (Account.kind.in_(_MONEY_KINDS))
            | ((Account.kind == AccountKind.position) & (AccountBalance.balance != 0)),
        )
    )).all()
    held = {asset for kind, asset in rows if kind == AccountKind.position}
    money = {asset for kind, asset in rows if kind != AccountKind.position}
    traded = set(symbols)
    all_symbols = held | traded
    inst_ccy = dict((await session.execute(
        select(Instrument.symbol, Instrument.currency)
        .where(Instrument.symbol.in_(sorted(all_symbols)))
    )).all()) if all_symbols else {}
    base, link_id = (await session.execute(
        select(Portfolio.base_currency, Portfolio.broker_link_id)
        .where(Portfolio.id == portfolio_id))).first() or (None, None)
    rule_ccys = {r.currency for r in await load_rules(session)}
    order_ccys = {inst_ccy[s] for s in traded if s in inst_ccy}
    sources = money | ({base} if base else set())

    pairs: set[tuple[str, str]] = {
        (c, EUR) for c in money | set(inst_ccy.values()) | rule_ccys | sources
    }
    for i in order_ccys:
        pairs |= {(i, r) for r in rule_ccys} | {(r, i) for r in rule_ccys}
        pairs |= {(c, i) for c in sources}
    if link_id is not None:  # real money: the other portfolios' quotas on the account
        from smtrading.brokers.live import LIVE_KINDS
        from smtrading.brokers.live.quotas import quota_pairs
        from smtrading.brokers.models import BrokerLink

        kind = await session.scalar(select(BrokerLink.kind).where(BrokerLink.id == link_id))
        if kind in LIVE_KINDS:
            pairs |= await quota_pairs(session, link_id, exclude_pid=portfolio_id)
    await memo.prefetch(sorted(all_symbols), {(a, b) for a, b in pairs if a != b})
