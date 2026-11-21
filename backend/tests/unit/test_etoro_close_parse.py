from decimal import Decimal

from smtrading.brokers.etoro_core import close_fill

# Close order read from the eToro demo API on 21/11/2026 (BTC, order 395843503).
REAL = {"orderID": 395843503, "statusID": 3, "instrumentID": 100000,
        "requestOccurred": "2026-11-21T17:07:57.667Z", "proceeds": "89.11",
        "positions": [{"positionID": 3648637494, "occurred": "2026-11-21T17:07:57.793Z",
                       "rate": "60010.0", "units": "0.0015", "conversionRate": "1.0",
                       "amount": "90.07"}]}


def test_real_close_order_gives_units_and_rate():
    assert close_fill(REAL) == (Decimal("0.0015"), Decimal("60010.0"))


def test_several_positions_give_the_units_weighted_rate():
    data = {"statusID": 3, "positions": [{"units": "1", "rate": "10"}, {"units": "3", "rate": "20"}]}
    assert close_fill(data) == (Decimal(4), Decimal("17.5"))


def test_older_top_level_fields_still_read():
    assert close_fill({"statusID": 3, "unitsDeducted": "2", "closeRate": "5"}) == (Decimal(2), Decimal(5))


def test_no_positions_yet_gives_none():
    assert close_fill({"statusID": 1, "positions": []}) == (None, None)
