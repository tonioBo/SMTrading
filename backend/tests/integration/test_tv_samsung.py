"""The Samsung TV client (`smtrading.tv.samsung`), the LAN discovery and the display link,
against a fake TV on 127.0.0.1 (never the real TV)."""

import asyncio
import logging
import socket

import httpx
import pytest
from fake_samsung_tv import FakeSamsungTv

from smtrading.common.config import get_settings
from smtrading.tv import discovery, link, samsung


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    monkeypatch.setenv("SMT_DATABASE_URL", "postgresql+asyncpg://x@localhost/x")
    monkeypatch.setenv("SMT_REDIS_URL", "redis://localhost:1/0")
    monkeypatch.setenv("SMT_MASTER_KEY", "test-master-key")
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    get_settings.cache_clear()


@pytest.fixture
async def tv(monkeypatch):
    monkeypatch.setattr(samsung, "ALLOW_LOOPBACK", True)
    fake = await FakeSamsungTv().start()
    fake.point(monkeypatch)
    yield fake
    await fake.stop()


async def test_device_info_reads_the_rest_description(tv):
    dev = await samsung.device_info("127.0.0.1")
    assert dev == {"name": '85" Crystal UHD', "model": "UE85DU7190UXZT",
                   "resolution": "3840x2160", "mac": "02:00:5e:00:00:01", "power": "on",
                   "token_auth": True}
    tv.power = "standby"
    assert (await samsung.device_info("127.0.0.1"))["power"] == "standby"
    tv.samsung = False
    assert await samsung.device_info("127.0.0.1") is None


async def test_device_info_unreachable_is_a_clear_error(monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(samsung, "REST_PORT", port)
    with pytest.raises(samsung.TvError) as e:
        await samsung.device_info("127.0.0.1", timeout=0.5)
    assert e.value.kind == "unreachable" and "non raggiungibile" in e.value.message


async def test_pairing_returns_the_token_over_tls(tv):
    token = await samsung.pair("127.0.0.1", wait_s=2)
    assert token == "fake-tv-token-0001-abcdef"
    conn = tv.connections[-1]
    assert conn == {"name": "SMTrading", "token": None,
                    "path": "/api/v2/channels/samsung.remote.control"}


async def test_pairing_refused_and_timeout(tv):
    tv.mode = "refuse"
    with pytest.raises(samsung.TvError) as e:
        await samsung.pair("127.0.0.1", wait_s=2)
    assert e.value.kind == "refused" and "rifiutato" in e.value.message
    tv.mode = "silent"
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(samsung.TvError) as e:
        await samsung.pair("127.0.0.1", wait_s=0.5)
    assert e.value.kind == "timeout" and loop.time() - t0 < 3


async def test_power_key_and_browser_launch_with_the_token(tv):
    token = await samsung.pair("127.0.0.1", wait_s=2)
    tv.power = "standby"
    async with samsung.open_session("127.0.0.1", token) as s:
        await s.key("KEY_POWER")
        await asyncio.sleep(0.1)
        assert s.token == token
    assert tv.keys == ["KEY_POWER"] and tv.power == "on"
    assert tv.connections[-1]["token"] == token
    # the first browser id unknown on this model: the second one opens
    tv.launch_replies = {"org.tizen.browser": 404, "com.samsung.tv.browser": 200}
    async with samsung.open_session("127.0.0.1", token) as s:
        app, confirmed = await s.launch_browser("https://portal.lan/tv?token=abc")
    assert (app, confirmed) == ("com.samsung.tv.browser", True)
    assert tv.launches == [
        {"appId": "3202010022079", "metaTag": "https://portal.lan/tv?token=abc",
         "action_type": "NATIVE_LAUNCH"},
        {"appId": "org.tizen.browser", "metaTag": "https://portal.lan/tv?token=abc",
         "action_type": "NATIVE_LAUNCH"},
        {"appId": "com.samsung.tv.browser", "metaTag": "https://portal.lan/tv?token=abc",
         "action_type": "NATIVE_LAUNCH"}]


async def test_launch_unanswered_is_not_confirmed_and_all_refused_is_an_error(tv, monkeypatch):
    monkeypatch.setattr(samsung, "LAUNCH_ACK_S", 0.3)
    token = await samsung.pair("127.0.0.1", wait_s=2)
    tv.launch_replies = {"org.tizen.browser": None}
    async with samsung.open_session("127.0.0.1", token) as s:
        assert await s.launch_browser("http://x/tv", ("org.tizen.browser",)) == (
            "org.tizen.browser", False)
    tv.launch_replies = {}
    async with samsung.open_session("127.0.0.1", token) as s:
        with pytest.raises(samsung.TvError) as e:
            await s.launch_browser("http://x/tv")
    assert e.value.kind == "launch" and "QR" in e.value.message


async def test_the_browser_is_found_by_rest_and_opened_by_rest(tv):
    # the 2024 model: only «Browser» 3202010022079 answers; the Tizen ids are 404
    tv.installed = {"3202010022079"}
    assert await samsung.app_installed("127.0.0.1", "3202010022079") is True
    assert await samsung.app_installed("127.0.0.1", "org.tizen.browser") is False
    assert await samsung.find_browser("127.0.0.1") == "3202010022079"
    assert tv.app_probes[-1] == "3202010022079"
    assert await samsung.open_app_rest("127.0.0.1", "3202010022079") is True
    assert tv.rest_launches == [{"appId": "3202010022079", "body": None}]
    # an older model: the Tizen browser only
    tv.installed, tv.app_probes = {"org.tizen.browser"}, []
    assert await samsung.find_browser("127.0.0.1") == "org.tizen.browser"
    assert tv.app_probes == ["3202010022079", "org.tizen.browser"]
    assert await samsung.open_app_rest("127.0.0.1", "com.samsung.tv.browser") is False
    tv.installed = set()
    assert await samsung.find_browser("127.0.0.1") is None
    await tv.stop()
    with pytest.raises(samsung.TvError) as e:
        await samsung.find_browser("127.0.0.1")
    assert e.value.kind == "unreachable"


async def test_a_forgotten_token_asks_again_without_hanging(tv, monkeypatch):
    monkeypatch.setattr(samsung, "CONNECT_TIMEOUT_S", 0.5)
    tv.mode = "silent"
    with pytest.raises(samsung.TvError) as e:
        async with samsung.open_session("127.0.0.1", "old-token-forgotten", wait_s=0.5):
            pass
    assert e.value.kind == "timeout" and "Collega la TV" in e.value.message


async def test_the_token_never_reaches_a_log(tv, caplog):
    caplog.set_level(logging.DEBUG)
    token = await samsung.pair("127.0.0.1", wait_s=2)
    async with samsung.open_session("127.0.0.1", token) as s:
        await s.key("KEY_HOME")
    from smtrading.common.logsetup import redact_log_text
    text = "\n".join(redact_log_text(r.getMessage()) for r in caplog.records)
    assert token not in text
    assert redact_log_text(f"opening wss://tv/x?token={token}").count(token) == 0


def test_magic_packet_and_wake_on_lan():
    pkt = samsung.magic_packet("02:00:5e:00:00:01")
    assert len(pkt) == 102 and pkt[:6] == b"\xff" * 6
    assert pkt[6:12] == bytes.fromhex("02005e000001") and pkt[-6:] == pkt[6:12]
    for bad in ("nope", "zz:zz:zz:zz:zz:zz", None):
        with pytest.raises(samsung.TvError) as e:
            samsung.magic_packet(bad)
        assert e.value.kind == "mac"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rx:
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(2)
        assert samsung.wake_on_lan("02-00-5E-00-00-01", [rx.getsockname()]) == 1
        assert rx.recv(200) == pkt


# -- discovery -----------------------------------------------------------------------


def _fake_lan(devices: dict[str, dict]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        dev = devices.get(request.url.host)
        if dev is None:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=dev)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_huge_answer_is_not_a_tv_and_the_search_is_bounded():
    big = {"type": "Samsung SmartTV", "device": {"type": "Samsung SmartTV",
                                                 "name": "x" * (samsung.BODY_MAX + 10)}}
    lan = _fake_lan({"192.168.5.2": big})
    async with lan:
        assert await samsung.device_info("192.168.5.2", client=lan) is None

    async def slow(request):
        await asyncio.sleep(30)

    hang = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    async with hang:
        found = await discovery.discover(["192.168.5.9"], client=hang, timeout=60,
                                         timeout_s=0.5)
    assert found == [] and loop.time() - t0 < 3


async def test_discovery_finds_only_samsung_tvs():
    tvjson = FakeSamsungTv(power="standby").device_json()
    lan = _fake_lan({"192.168.5.84": tvjson, "192.168.5.10": {"type": "Router"},
                     "192.168.5.3": FakeSamsungTv().device_json()})
    hosts = discovery.scan_hosts(discovery.parse_subnet("192.168.5.0/24"))
    assert len(hosts) == 254
    async with lan:
        found = await discovery.discover(hosts, client=lan)
    assert [d["host"] for d in found] == ["192.168.5.3", "192.168.5.84"]
    assert found[1] == {"host": "192.168.5.84", "name": '85" Crystal UHD',
                        "model": "UE85DU7190UXZT", "resolution": "3840x2160",
                        "mac": "02:00:5e:00:00:01", "power": "standby"}


def test_subnets(monkeypatch):
    for bad in ("10.0.0.0/16",  # too big
                "8.8.8.0/24", "100.64.0.0/24", "192.0.0.0/24", "198.18.0.0/24",
                "169.254.1.0/24", "127.0.0.0/24", "0.0.0.0/24", "nonsense"):
        with pytest.raises(discovery.SubnetError):
            discovery.parse_subnet(bad)
    for good in ("10.1.2.0/24", "172.16.5.0/24", "172.31.0.0/24", "192.168.30.0/24"):
        assert discovery.parse_subnet(good)
    with pytest.raises(discovery.SubnetError):
        discovery.parse_subnet("172.32.0.0/24")
    monkeypatch.setattr(samsung, "ALLOW_LOOPBACK", True)  # the tests' fake TV only
    assert discovery.scan_hosts(discovery.parse_subnet("127.0.0.1/32")) == ["127.0.0.1"]
    assert str(discovery.default_subnet("192.168.30.7")) == "192.168.30.0/24"
    assert str(discovery.default_subnet("192.168.30.7:3000")) == "192.168.30.0/24"


# -- display link --------------------------------------------------------------------


def test_display_link_valid_revoked_and_wrong_scope():
    link_id, blob, secret = link.new_link()
    assert secret.encode() not in blob
    token = link.token_for(link_id, blob)
    assert token.startswith(f"tv-display.{link_id}.") and link.verify(token, link_id, blob)
    assert link.link_id_of(token) == link_id
    # another link (rotation): the old token no longer opens anything
    newer_id, newer_blob, _ = link.new_link()
    assert not link.verify(token, newer_id, newer_blob)
    assert not link.verify(token, None, None)
    # same secret, another scope: refused
    other = link.sign("admin", link_id, secret)
    assert not link.verify(other, link_id, blob) and link.link_id_of(other) is None
    assert not link.verify(other.replace("admin", "tv-display", 1), link_id, blob)
    # tampered or junk
    assert not link.verify(token[:-2] + "xx", link_id, blob)
    assert not link.verify("", link_id, blob) and not link.verify("a.b", link_id, blob)
    assert not link.verify("tv-display." + "x" * 300 + ".y", link_id, blob)
