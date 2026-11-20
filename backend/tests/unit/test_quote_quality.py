"""Quote quality guard and provider quotas: the pure parts (plan 25)."""

from datetime import UTC, datetime
from decimal import Decimal as D

import httpx
import pytest

from smtrading.admin.system_health import price_quality_section, quotas_section
from smtrading.marketdata import quality, quota
from smtrading.marketdata.quality import Contest, Context, Seen, problems
from smtrading.marketdata.types import Quote
from smtrading.valuation.service import MAX_MARK_SPREAD

NOW = 1_800_000_000.0
TS = datetime.fromtimestamp(NOW, UTC)


def q(last=None, bid=None, ask=None, provider="a", symbol="NVDA"):
    return Quote(symbol, None if bid is None else D(bid), None if ask is None else D(ask),
                 None if last is None else D(last), TS, provider)


def test_one_spread_constant_for_valuation_and_guard():
    assert MAX_MARK_SPREAD is quality.MAX_SPREAD == D("0.01")


def test_price_of_tight_mid_wide_last_and_sides():
    assert quality.price_of(q("100", "99.9", "100.1")) == D("100.0")
    nvda = q("176.5", "145.6", "178.29")  # after hours this week: the mid lies
    assert quality.is_wide(nvda) and quality.price_of(nvda) == D("176.5")
    assert quality.price_of(q(None, "145.6", "178.29")) == (D("145.6") + D("178.29")) / 2
    assert quality.price_of(q(None, "5", None)) == D(5)
    assert quality.price_of(q()) is None


@pytest.mark.parametrize("cls,price,jump", [
    ("us_equity", "114", False), ("us_equity", "116", True), ("us_equity", "84", True),
    ("crypto", "124", False), ("crypto", "126", True),
    ("forex", "104.9", False), ("forex", "105.1", True),
])
def test_jump_threshold_by_asset_class(cls, price, jump):
    ctx = Context(ref=Seen(D(100), NOW - 60))
    assert (quality.JUMP in problems(q(price), cls, ctx, NOW)) is jump


def test_jump_only_within_the_window():
    old = Context(ref=Seen(D(100), NOW - quality.JUMP_WINDOW_S - 1))
    assert problems(q("150"), "us_equity", old, NOW) == []


def test_disagree_with_another_recent_provider_not_with_itself():
    ctx = Context(others=[Seen(D(100), NOW - 10, "a"), Seen(D(100), NOW - 10, "b")])
    assert problems(q("104", provider="a"), "us_equity", ctx, NOW) == [quality.DISAGREE]
    mine = Context(others=[Seen(D(100), NOW - 10, "a")])
    assert problems(q("104", provider="a"), "us_equity", mine, NOW) == []
    stale = Context(others=[Seen(D(100), NOW - quality.DISAGREE_WINDOW_S - 1, "b")])
    assert problems(q("104"), "us_equity", stale, NOW) == []
    assert problems(q("102.9"), "us_equity", Context(others=[Seen(D(100), NOW, "b")]),
                    NOW) == []


def test_no_context_no_problem():
    assert problems(q("100"), "us_equity", None, NOW) == []
    assert problems(q(), "us_equity", Context(ref=Seen(D(1), NOW)), NOW) == []


def test_contest_first_candidate_confirmed_by_the_next():
    c = Contest([(q("130", provider="a"), [quality.JUMP])])
    winner, losers = c.offer(q("131", provider="b"), [quality.JUMP])
    assert winner.provider == "a" and losers == []


def test_contest_a_clean_candidate_replaces_the_outlier():
    first = q("130", provider="a")
    c = Contest([(first, [quality.JUMP])])
    winner, losers = c.offer(q("100.5", provider="b"), [])
    assert winner.provider == "b" and losers == [(first, [quality.JUMP])]


def test_contest_without_consensus_ends_suspect():
    c = Contest([(q("130", provider="a"), [quality.JUMP])])
    assert c.offer(q("70", provider="b"), [quality.JUMP]) is None
    s = c.suspect()
    assert s.provider == "a" and s.suspect == quality.JUMP


# -- quotas -----------------------------------------------------------------------------

def test_usage_demoted_at_90_percent_and_on_429():
    b = quota.Budget(8, None, 800)
    assert quota.Usage(0, 0, 719).demoted(b) is None
    assert quota.Usage(0, 0, 720).demoted(b) == quota.DAY
    assert quota.Usage(0, 1800, 0).demoted(quota.Budget(60, 2000, None)) == quota.HOUR
    assert quota.Usage(0, 0, 0, TS).demoted(b) == quota.QUOTA_429
    assert quota.Usage(0, 10**6, 10**6).demoted(quota.Budget(60, None, None)) is None


def test_reset_times_utc():
    now = datetime(2026, 11, 20, 23, 59, 30, tzinfo=UTC)
    assert quota.next_reset(quota.DAY, now) == datetime(2026, 11, 21, tzinfo=UTC)
    assert quota.next_reset(quota.HOUR, now) == datetime(2026, 11, 21, tzinfo=UTC)
    assert quota.next_reset(quota.MINUTE, now) == datetime(2026, 11, 21, tzinfo=UTC)
    assert quota.period_of(quota.Budget(8, None, 800)) == quota.DAY
    assert quota.period_of(quota.Budget(60, 2000, None)) == quota.HOUR
    assert quota.period_of(quota.Budget(60, None, None)) == quota.MINUTE


def test_quota_errors_detected():
    resp = httpx.Response(429, request=httpx.Request("GET", "https://x"))
    assert quota.is_quota_error(httpx.HTTPStatusError("x", request=resp.request,
                                                      response=resp), "")
    assert quota.is_quota_error(RuntimeError(), "RuntimeError: twelvedata: HTTP 429")
    assert quota.is_quota_error(RuntimeError(), "RuntimeError: twelvedata error 429: out")
    assert not quota.is_quota_error(RuntimeError(), "RuntimeError: HTTP 4290 or 1429")


def test_budget_defaults_and_overrides():
    from smtrading.marketdata.providers.twelvedata import TwelveDataProvider
    from smtrading.marketdata.providers.yahoo import YahooProvider

    assert quota.budget_of(TwelveDataProvider, None, None) == quota.Budget(8, None, 800)
    assert quota.budget_of(YahooProvider, None, None) == quota.Budget(60, 2000, None)
    assert quota.budget_of(TwelveDataProvider, 5, {"per_day": 0}) == quota.Budget(5, None, None)
    assert quota.budget_of(TwelveDataProvider, None, {"per_day": 500, "per_hour": 100}) \
        == quota.Budget(8, 100, 500)


def test_usage_dto():
    d = quota.usage_dto(quota.Budget(8, None, 800), quota.Usage(3, 50, 760), TS)
    assert d["pct_day"] == 95.0 and d["demoted"] and d["demoted_reason"] == quota.DAY
    assert d["pct_minute"] == 37.5 and d["pct_hour"] is None
    assert d["demoted_until"] == d["resets_at"]


# -- «Salute del sistema» ---------------------------------------------------------------

def test_price_quality_section():
    suspects = [(NOW - 100, "alpaca", "NVDA", "jump"), (NOW - 7200, "alpaca", "NVDA", "jump"),
                (NOW - 50, "yahoo", "ENI.MI", "disagree")]
    events = {("alpaca", "NVDA", "jump"): 2, ("alpaca", "NVDA", "wide_spread"): 7,
              ("yahoo", "ENI.MI", "disagree"): 1}
    s = price_quality_section(events, suspects, {}, NOW)
    assert s["status"] == "ok" and s["suspects_1h"] == 2 and s["suspects_24h"] == 3
    assert s["top_symbols"][0] == {"symbol": "NVDA", "count": 2}
    assert s["by_reason"][0] == {"reason": "wide_spread", "label": "spread ampio", "count": 7}
    late = price_quality_section(events, suspects,
                                 {"NVDA": NOW - quality.SUSPECT_ALERT_AFTER_S - 60}, NOW)
    assert late["status"] == "attenzione"
    assert late["reasons"] == ["NVDA: prezzo non confermato da 16 min"]
    empty = price_quality_section({}, [], {}, NOW)
    assert empty["summary"] == "Nessuna quotazione sospetta nelle 24 ore"


def test_quotas_section():
    ok = quota.usage_dto(quota.Budget(60, None, None), quota.Usage(1, 2, 3), TS)
    blocked = quota.usage_dto(quota.Budget(8, None, 800), quota.Usage(0, 0, 10, TS), TS)
    s = quotas_section([{"name": "yahoo", "quota": ok}, {"name": "twelvedata", "quota": blocked}])
    assert s["status"] == "attenzione"
    assert s["reasons"] == [("twelvedata: ha risposto 429 (quota esaurita), usato per ultimo "
                            "fino al reset")]
    assert quotas_section([{"name": "yahoo", "quota": ok}])["status"] == "ok"
