from smtrading.brokers.etoro_core import _buy_unleveraged_real, _eligibility_items

# Response captured from the eToro demo API on 21/11/2026 (BTC, instrument 100000).
REAL = {"currency": "usd", "eligibilities": [{
    "instrumentId": 100000, "allowOpenPosition": True, "allowPartialClosePosition": True,
    "leverageConfigs": [
        {"settlementType": "real", "direction": "long", "leverageValues": [1]},
        {"settlementType": "cfd", "direction": "long", "leverageValues": [1, 2]},
    ]}]}


def test_real_eligibility_shape_is_parsed():
    items = _eligibility_items(REAL)
    assert [i["instrumentId"] for i in items] == [100000]
    assert _buy_unleveraged_real(items[0]) is True
    assert items[0]["allowPartialClosePosition"] is True
