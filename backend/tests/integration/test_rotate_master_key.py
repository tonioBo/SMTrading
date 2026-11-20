"""Master key rotation (smtrading.devtools.rotate_master_key) on a fresh migrated database:
every encrypted field, wrong old key, rollback on a single failure, dry run, no secret in
the output or the logs."""

import asyncio
import base64
import logging
import uuid

import pyotp
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from smtrading.admin import auth
from smtrading.admin.models import AdminUser
from smtrading.agents.models import McpAuditLog
from smtrading.brokers.live.account import LIVE_AAD
from smtrading.brokers.models import BrokerLink
from smtrading.brokers.service import get_link_config
from smtrading.common.config import get_settings
from smtrading.common.crypto import encrypt_json
from smtrading.common.db import get_sessionmaker, make_engine
from smtrading.connectors.models import Connector
from smtrading.connectors.service import get_connector_config
from smtrading.devtools import rotate_master_key as rot
from smtrading.paperclip import config as paperclip
from smtrading.risk.models import AppSetting
from smtrading.telegram import config as telegram

OLD = "weak-old"  # like production's: short
NEW = "Zq8vN3xK7pR2mT9wL4sB6yH1cF5gJ0dE-aU_oI"
OTHER = "a-key-that-encrypted-nothing-here"

SECRETS = {
    "connector": "CONNECTOR-SECRET-VALUE-123",
    "paper": "PAPER-BROKER-SECRET-456",
    "live": "LIVE-BROKER-SECRET-789",
    "telegram": "123456789:AAH-telegram-bot-token-secret-value",
    "paperclip": "pcp_board_SECRET_board_key_value_000",
}
TOTP_A, TOTP_B = pyotp.random_base32(), pyotp.random_base32()

# the five fields of the round trip, with the TOTP of two admins
EXPECTED_COUNTS = {rot.F_CONNECTORS: 1, rot.F_BROKERS: 2, rot.F_TOTP: 2, rot.F_TELEGRAM: 1,
                   rot.F_PAPERCLIP: 1}


@pytest.fixture(autouse=True)
def _env(monkeypatch, fresh_db, redis_url):
    monkeypatch.setenv("SMT_DATABASE_URL", fresh_db)
    monkeypatch.setenv("SMT_REDIS_URL", redis_url)
    monkeypatch.setenv("SMT_MASTER_KEY", OLD)
    monkeypatch.setenv("SMT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("SMT_ADMIN_PASSWORD", "test-password")
    monkeypatch.delenv(rot.NEW_KEY_ENV, raising=False)
    get_settings.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture
async def sm(fresh_db):
    engine = make_engine(fresh_db)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _b64(blob: bytes) -> str:
    return base64.b64encode(blob).decode("ascii")


async def _seed(sm, *, bad_field: str | None = None) -> dict[str, uuid.UUID]:
    """One value per encrypted field (two broker links, two TOTP admins) under OLD;
    `bad_field` is encrypted under OTHER instead."""
    def key(f: str) -> str:
        return OTHER if f == bad_field else OLD

    ids = {"paper": uuid.uuid4(), "live": uuid.uuid4(), "admin_a": uuid.uuid4(),
           "admin_b": uuid.uuid4()}
    async with sm() as s, s.begin():
        s.add(Connector(name="alpaca", enabled=False, config_encrypted=encrypt_json(
            {"key_id": "K", "secret_key": SECRETS["connector"]}, key(rot.F_CONNECTORS))))
        s.add(Connector(name="yahoo", enabled=True, config_encrypted=None))
        s.add(BrokerLink(id=ids["paper"], name="Paper", kind="alpaca_paper",
                         config_encrypted=encrypt_json(
                             {"key_id": "PK", "secret_key": SECRETS["paper"]}, OLD)))
        s.add(BrokerLink(id=ids["live"], name="Reale", kind="t212_live", live_state="configured",
                         config_encrypted=encrypt_json(
                             {"api_key": "AK", "api_secret": SECRETS["live"]},
                             key(rot.F_BROKERS), aad=LIVE_AAD)))
        s.add(BrokerLink(name="Vuoto", kind="t212_demo", config_encrypted=None))
        s.add(AdminUser(id=ids["admin_a"], email="a@example.com", password_hash="x",
                        totp_enabled=True, totp_secret_encrypted=encrypt_json(
                            {"secret": TOTP_A}, key(rot.F_TOTP))))
        s.add(AdminUser(id=ids["admin_b"], email="b@example.com", password_hash="x",
                        totp_enabled=True,
                        totp_secret_encrypted=encrypt_json({"secret": TOTP_B}, OLD)))
        s.add(AdminUser(email="c@example.com", password_hash="x"))  # no 2FA
        s.add(AppSetting(key=telegram.SETTINGS_KEY, value={
            "token_enc": _b64(encrypt_json({"bot_token": SECRETS["telegram"]},
                                           key(rot.F_TELEGRAM))),
            "chat_id": "-100123", "enabled": True, "paused": False}))
        s.add(AppSetting(key=paperclip.SETTINGS_KEY, value={
            "base_url": "http://paperclip:3100", "company_id": "c1", "enabled": True,
            "board_key_enc": _b64(encrypt_json({"board_key": SECRETS["paperclip"]},
                                               key(rot.F_PAPERCLIP), aad=paperclip.AAD))}))
    return ids


async def _snapshot(sm) -> dict:
    async with sm() as s:
        rows = {}
        for sql in ("select name, config_encrypted from connectors",
                    "select id::text, config_encrypted from broker_links",
                    "select id::text, totp_secret_encrypted from admin_users",
                    "select key, value::text from app_settings"):
            rows[sql] = sorted((await s.execute(text(sql))).all(), key=lambda r: r[0])
        return rows


async def _audit_rows(sm) -> list[McpAuditLog]:
    async with sm() as s:
        return list(await s.scalars(
            select(McpAuditLog).where(McpAuditLog.method == rot.AUDIT_METHOD)))


async def _reads_with_current_key(sm, ids) -> dict:
    """What the application itself reads with the current SMT_MASTER_KEY."""
    async with sm() as s:
        a = await s.get(AdminUser, ids["admin_a"])
        b = await s.get(AdminUser, ids["admin_b"])
        return {
            "connector": (await get_connector_config(s, "alpaca"))["secret_key"],
            "paper": (await get_link_config(s, ids["paper"]))["secret_key"],
            "live": (await get_link_config(s, ids["live"]))["api_secret"],
            "totp": (auth._totp_secret(a), auth._totp_secret(b)),
            "telegram": (await telegram.load_config(s)).bot_token,
            "paperclip": (await paperclip.load_config(s)).board_key,
        }


async def test_apply_round_trips_every_encrypted_field(sm, monkeypatch):
    ids = await _seed(sm)
    code, lines = await rot.run(sm, OLD, NEW, apply=True)
    assert code == 0, lines
    for name, n in EXPECTED_COUNTS.items():
        assert f"{name}: {n}" in lines
    assert "totale: 7, verificati con la nuova chiave: 7" in lines

    # the application, restarted with the new key, reads every value (2FA included)
    monkeypatch.setenv("SMT_MASTER_KEY", NEW)
    get_settings.cache_clear()
    got = await _reads_with_current_key(sm, ids)
    assert got == {"connector": SECRETS["connector"], "paper": SECRETS["paper"],
                   "live": SECRETS["live"], "totp": (TOTP_A, TOTP_B),
                   "telegram": SECRETS["telegram"], "paperclip": SECRETS["paperclip"]}
    async with sm() as s:  # a 2FA login with the rotated secret still verifies
        a = await s.get(AdminUser, ids["admin_a"])
        assert auth._totp_ok(auth._totp_secret(a), pyotp.TOTP(TOTP_A).now())
        cfg = await s.scalar(select(AppSetting.value).where(
            AppSetting.key == telegram.SETTINGS_KEY))
        assert cfg["chat_id"] == "-100123" and cfg["enabled"] is True  # other keys kept

    # the old key reads nothing any more
    monkeypatch.setenv("SMT_MASTER_KEY", OLD)
    get_settings.cache_clear()
    async with sm() as s:
        assert (await telegram.load_config(s)).unreadable
        assert (await paperclip.load_config(s)).unreadable

    [row] = await _audit_rows(sm)
    assert row.outcome == "ok"
    assert row.params["counts"] == EXPECTED_COUNTS and row.params["verified"] == 7
    assert row.params["failures"] == [] and row.params["by"] == "cli"


async def test_dry_run_leaves_database_unchanged(sm):
    await _seed(sm)
    before = await _snapshot(sm)
    code, lines = await rot.run(sm, OLD, NEW, apply=False)
    assert code == 0
    assert "totale: 7, verificati con la nuova chiave: 7" in lines
    assert any("dry-run" in line for line in lines)
    assert await _snapshot(sm) == before
    assert await _audit_rows(sm) == []


async def test_wrong_old_key_is_refused(sm):
    await _seed(sm)
    before = await _snapshot(sm)
    code, lines = await rot.run(sm, OTHER, NEW, apply=True)
    assert code == 1
    refused = [line for line in lines if line.startswith("non decifrabile")]
    assert len(refused) == 7  # every value listed, by field and id
    assert await _snapshot(sm) == before
    [row] = await _audit_rows(sm)
    assert row.outcome == "refused" and len(row.params["failures"]) == 7


@pytest.mark.parametrize("bad_field", rot.ENCRYPTED_FIELDS)
async def test_one_unreadable_value_rolls_everything_back(sm, bad_field):
    ids = await _seed(sm, bad_field=bad_field)
    before = await _snapshot(sm)
    code, lines = await rot.run(sm, OLD, NEW, apply=True)
    assert code == 1
    expected_id = {rot.F_CONNECTORS: "alpaca", rot.F_BROKERS: str(ids["live"]),
                   rot.F_TOTP: str(ids["admin_a"]), rot.F_TELEGRAM: "telegram",
                   rot.F_PAPERCLIP: "paperclip"}[bad_field]
    assert f"non decifrabile con la chiave attuale: {bad_field} id={expected_id}" in lines
    assert sum(line.startswith("non decifrabile") for line in lines) == 1
    assert await _snapshot(sm) == before  # the six good values were not rotated either
    [row] = await _audit_rows(sm)
    assert row.outcome == "refused"
    assert row.params["failures"] == [{"field": bad_field, "id": expected_id,
                                       "reason": "old key"}]


async def test_a_value_that_does_not_verify_rolls_back(sm, monkeypatch):
    await _seed(sm)
    before = await _snapshot(sm)
    real = rot.encrypt_json

    def broken(data, key, *, aad=None):  # the TOTP of one admin is written with a wrong key
        return real(data, OTHER if data == {"secret": TOTP_B} else key, aad=aad)

    monkeypatch.setattr(rot, "encrypt_json", broken)
    code, lines = await rot.run(sm, OLD, NEW, apply=True)
    assert code == 1
    assert any(line.startswith("verifica con la nuova chiave non riuscita: "
                               f"{rot.F_TOTP}") for line in lines)
    assert await _snapshot(sm) == before


async def test_weak_or_equal_new_key_is_refused_before_the_database(sm):
    await _seed(sm)
    for bad in ("", "short", "a" * 40, "abcdefghijklmnopqrstuvwxyzabcdefgh", OLD):
        with pytest.raises(rot.UsageError):
            await rot.run(sm, OLD, bad, apply=True)
    with pytest.raises(rot.UsageError):
        await rot.run(sm, NEW, NEW, apply=True)


async def test_cli_output_and_logs_never_contain_a_secret(sm, monkeypatch, capsys, caplog):
    await _seed(sm)
    before = await _snapshot(sm)
    blobs = [v for rows in before.values() for _, v in rows if isinstance(v, bytes)]
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(rot.NEW_KEY_ENV, NEW)
    assert await asyncio.to_thread(rot.main, ["--dry-run"]) == 0
    assert await asyncio.to_thread(rot.main, []) == 0  # dry run is the default
    assert await _snapshot(sm) == before
    assert await asyncio.to_thread(rot.main, ["--apply"]) == 0
    monkeypatch.setenv(rot.NEW_KEY_ENV, NEW + "x")  # OLD no longer decrypts anything
    assert await asyncio.to_thread(rot.main, ["--apply"]) == 1
    out = capsys.readouterr()
    text_seen = out.out + out.err + caplog.text + "".join(
        str(r.params) for r in await _audit_rows(sm))
    assert "applicato" in out.out and "non decifrabile" in out.out
    for secret in (OLD, NEW, *SECRETS.values(), TOTP_A, TOTP_B,
                   *(_b64(b) for b in blobs), *(b.hex() for b in blobs)):
        assert secret not in text_seen


async def test_missing_new_key_exits_2(sm, capsys):
    assert await asyncio.to_thread(rot.main, ["--apply"]) == 2
    assert rot.NEW_KEY_ENV in capsys.readouterr().err
