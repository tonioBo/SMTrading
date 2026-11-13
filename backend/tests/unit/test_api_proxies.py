from smtrading.api.proxies import attached_subnets, forwarded_allow_ips

ROUTES = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
eth0\t00000000\t01001AAC\t0003\t0\t0\t0\t00000000\t0\t0\t0
eth0\t00001AAC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0
eth1\t0014A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0
eth1\t0014A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0
"""


def test_attached_subnets_skips_default_and_gateway_routes():
    assert attached_subnets(ROUTES) == ["172.26.0.0/16", "192.168.20.0/24"]


def test_override_wins(monkeypatch):
    monkeypatch.setenv("SMT_FORWARDED_ALLOW_IPS", "10.0.0.0/8")
    assert forwarded_allow_ips() == "10.0.0.0/8"


def test_no_route_table_falls_back_to_loopback(monkeypatch):
    from smtrading.api import proxies

    monkeypatch.delenv("SMT_FORWARDED_ALLOW_IPS", raising=False)
    monkeypatch.setattr(proxies, "ROUTE_FILE", "/nonexistent/route")
    assert forwarded_allow_ips() == "127.0.0.1"
