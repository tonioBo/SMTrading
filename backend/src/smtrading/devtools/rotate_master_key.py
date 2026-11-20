"""Rotation of the encryption master key (`SMT_MASTER_KEY`), run inside the api container.

    python -m smtrading.devtools.rotate_master_key --generate
    SMT_NEW_MASTER_KEY=... python -m smtrading.devtools.rotate_master_key [--dry-run]
    SMT_NEW_MASTER_KEY=... python -m smtrading.devtools.rotate_master_key --apply

The old key is the current `SMT_MASTER_KEY` (the settings), the new one is read only from
the environment variable `SMT_NEW_MASTER_KEY`: never from argv. `--generate` prints a new
random key (and nothing else) once, without touching the database: the only secret this
tool ever prints, and it is the new key, never the old one.

Every value encrypted with the master key (`ENCRYPTED_FIELDS`) is decrypted with the old
key, encrypted again with the new one (same AAD context), read back from the database and
decrypted with the new key, all in ONE transaction with the rows locked:
- `--dry-run` (the default): the counts per field, then ROLLBACK (nothing is written,
  not even an audit row);
- `--apply`: COMMIT only if every value round-trips, with an audit row
  (`admin:master_key.rotate`, counts only); if any value cannot be decrypted with the old
  key nothing is changed, the field and row id of each one are listed and a `refused`
  audit row is written.

The output, the logs and the audit rows carry counts, field names and row ids only: never
a key, a key fingerprint, a decrypted value or an encrypted blob.

The key is used for nothing else: portal sessions are random ids in Redis (not signed),
the CSRF token is random, passwords are argon2 hashes without pepper, agent tokens are
not derived from it. After a rotation nobody is logged out and passwords keep working;
TOTP (2FA) keeps working because the TOTP secrets are rotated too.
"""

import argparse
import asyncio
import base64
import binascii
import os
import re
import secrets
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import smtrading.models  # noqa: F401  (complete mapper registry)
from smtrading.admin.models import AdminUser
from smtrading.agents.service import write_audit
from smtrading.brokers.live import LIVE_KINDS
from smtrading.brokers.live.account import LIVE_AAD
from smtrading.brokers.models import BrokerLink
from smtrading.common.config import get_settings
from smtrading.common.crypto import decrypt_json, encrypt_json
from smtrading.common.errors import SMTError
from smtrading.common.logsetup import configure_logging, register_secret
from smtrading.connectors.models import Connector
from smtrading.paperclip.config import AAD as PAPERCLIP_AAD
from smtrading.paperclip.config import SETTINGS_KEY as PAPERCLIP_KEY
from smtrading.risk.models import AppSetting
from smtrading.telegram.config import SETTINGS_KEY as TELEGRAM_KEY

NEW_KEY_ENV = "SMT_NEW_MASTER_KEY"
MIN_KEY_LEN = 32
MIN_DISTINCT_CHARS = 12
AUDIT_METHOD = "admin:master_key.rotate"

F_CONNECTORS = "connectors.config_encrypted"
F_BROKERS = "broker_links.config_encrypted"
F_TOTP = "admin_users.totp_secret_encrypted"
F_TELEGRAM = "app_settings.telegram.token_enc"
F_PAPERCLIP = "app_settings.paperclip.board_key_enc"
# every value encrypted with SMT_MASTER_KEY, in report order
ENCRYPTED_FIELDS = (F_CONNECTORS, F_BROKERS, F_TOTP, F_TELEGRAM, F_PAPERCLIP)


class UsageError(Exception):
    """A refusal before any database access (exit code 2). Messages carry no secret."""


def generate_key() -> str:
    """64 URL-safe characters from 48 random bytes (384 bits)."""
    return secrets.token_urlsafe(48)


def key_problem(new_key: str | None, old_key: str | None = None) -> str | None:
    """Why `new_key` is refused (None = acceptable). Never quotes the key."""
    if not new_key:
        return f"{NEW_KEY_ENV} mancante o vuota"
    if len(new_key) < MIN_KEY_LEN:
        return f"nuova chiave troppo corta: servono almeno {MIN_KEY_LEN} caratteri"
    if re.search(r"\s", new_key) or not new_key.isprintable():
        return "nuova chiave non valida: niente spazi, a capo o caratteri di controllo"
    if len(set(new_key)) < MIN_DISTINCT_CHARS:
        return (f"nuova chiave poco casuale: servono almeno {MIN_DISTINCT_CHARS} "
                "caratteri diversi")
    classes = sum((any(c.islower() for c in new_key), any(c.isupper() for c in new_key),
                   any(c.isdigit() for c in new_key),
                   any(not c.isalnum() for c in new_key)))
    if classes < 2:
        return ("nuova chiave poco casuale: servono almeno due tipi di caratteri "
                "(minuscole, maiuscole, cifre, simboli); usa --generate")
    if old_key is not None and new_key == old_key:
        return "la nuova chiave è uguale a quella attuale"
    return None


@dataclass
class Failure:
    field: str
    row_id: str
    reason: str  # "old key" (cannot be decrypted with the old key) | "verify"


@dataclass
class Report:
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(ENCRYPTED_FIELDS, 0))
    verified: int = 0
    failures: list[Failure] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def audit_params(self) -> dict[str, Any]:
        return {"counts": dict(self.counts), "total": self.total, "verified": self.verified,
                "failures": [{"field": f.field, "id": f.row_id, "reason": f.reason}
                             for f in self.failures],
                "by": "cli"}


class RotationRefused(Exception):
    def __init__(self, report: Report) -> None:
        super().__init__(f"{len(report.failures)} value(s) refused")
        self.report = report


class _DryRunDone(Exception):
    def __init__(self, report: Report) -> None:
        super().__init__("dry run")
        self.report = report


def _reencrypt(blob: bytes, old_key: str, new_key: str, aad: bytes | None) -> tuple[bytes, dict]:
    plain = decrypt_json(blob, old_key, aad=aad)
    return encrypt_json(plain, new_key, aad=aad), plain


# (field, row id, the encrypted value read back from the database, aad, expected plaintext)
_Check = tuple[str, str, Callable[[], Awaitable[bytes | None]], bytes | None, dict]


async def rotate(session: AsyncSession, old_key: str, new_key: str) -> Report:
    """Re-encrypts every value of `ENCRYPTED_FIELDS` in `session`'s transaction (rows
    locked FOR UPDATE), flushes and verifies each value read back from the database with
    the new key. Neither commits nor rolls back. Raises RotationRefused (after visiting
    every value, so the report lists them all) when any value cannot be decrypted with
    the old key or does not verify; the caller must then roll back."""
    report = Report()
    checks: list[_Check] = []

    def fail(field_name: str, row_id: str, reason: str = "old key") -> None:
        report.failures.append(Failure(field_name, row_id, reason))

    def col_reader(sql: str, row_id: Any) -> Callable[[], Awaitable[bytes | None]]:
        async def read() -> bytes | None:
            return await session.scalar(text(sql), {"id": row_id})
        return read

    for c in await session.scalars(
        select(Connector).where(Connector.config_encrypted.is_not(None))
        .order_by(Connector.name).with_for_update()
    ):
        try:
            c.config_encrypted, plain = _reencrypt(c.config_encrypted, old_key, new_key, None)
        except SMTError:
            fail(F_CONNECTORS, c.name)
            continue
        report.counts[F_CONNECTORS] += 1
        checks.append((F_CONNECTORS, c.name, col_reader(
            "select config_encrypted from connectors where name = :id", c.name), None, plain))

    for link in await session.scalars(
        select(BrokerLink).where(BrokerLink.config_encrypted.is_not(None))
        .order_by(BrokerLink.name).with_for_update()
    ):
        aad = LIVE_AAD if link.kind in LIVE_KINDS else None
        try:
            link.config_encrypted, plain = _reencrypt(link.config_encrypted, old_key, new_key,
                                                      aad)
        except SMTError:
            fail(F_BROKERS, str(link.id))
            continue
        report.counts[F_BROKERS] += 1
        checks.append((F_BROKERS, str(link.id), col_reader(
            "select config_encrypted from broker_links where id = :id", link.id), aad, plain))

    for user in await session.scalars(
        select(AdminUser).where(AdminUser.totp_secret_encrypted.is_not(None))
        .order_by(AdminUser.id).with_for_update()
    ):
        try:
            user.totp_secret_encrypted, plain = _reencrypt(user.totp_secret_encrypted, old_key,
                                                           new_key, None)
        except SMTError:
            fail(F_TOTP, str(user.id))
            continue
        report.counts[F_TOTP] += 1
        checks.append((F_TOTP, str(user.id), col_reader(
            "select totp_secret_encrypted from admin_users where id = :id", user.id),
            None, plain))

    for setting_key, json_key, field_name, aad in (
        (TELEGRAM_KEY, "token_enc", F_TELEGRAM, None),
        (PAPERCLIP_KEY, "board_key_enc", F_PAPERCLIP, PAPERCLIP_AAD),
    ):
        setting = await session.scalar(
            select(AppSetting).where(AppSetting.key == setting_key).with_for_update()
        )
        if setting is None or not isinstance(setting.value, dict):
            continue
        enc = setting.value.get(json_key)
        if not enc:
            continue
        try:
            blob = base64.b64decode(str(enc), validate=True)
            new_blob, plain = _reencrypt(blob, old_key, new_key, aad)
        except (SMTError, binascii.Error, ValueError):
            fail(field_name, setting_key)
            continue
        # a new dict: the JSONB column is not mutable-tracked
        setting.value = {**setting.value, json_key: base64.b64encode(new_blob).decode("ascii")}
        report.counts[field_name] += 1

        async def read_setting(k: str = setting_key, j: str = json_key) -> bytes | None:
            value = await session.scalar(
                text("select value from app_settings where key = :id"), {"id": k})
            return base64.b64decode(value[j], validate=True) if value else None
        checks.append((field_name, setting_key, read_setting, aad, plain))

    if report.failures:
        raise RotationRefused(report)

    await session.flush()
    for field_name, row_id, read, aad, plain in checks:
        try:
            stored = await read()
            ok = stored is not None and decrypt_json(stored, new_key, aad=aad) == plain
        except (SMTError, binascii.Error, ValueError, KeyError, TypeError):
            ok = False
        if ok:
            report.verified += 1
        else:
            fail(field_name, row_id, "verify")
    if report.failures:
        raise RotationRefused(report)
    return report


def _report_lines(report: Report) -> list[str]:
    lines = [f"{name}: {report.counts[name]}" for name in ENCRYPTED_FIELDS]
    lines.append(f"totale: {report.total}, verificati con la nuova chiave: {report.verified}")
    for f in report.failures:
        what = ("non decifrabile con la chiave attuale" if f.reason == "old key"
                else "verifica con la nuova chiave non riuscita")
        lines.append(f"{what}: {f.field} id={f.row_id}")
    return lines


async def run(sessionmaker: async_sessionmaker[AsyncSession], old_key: str, new_key: str,
              *, apply: bool) -> tuple[int, list[str]]:
    """(exit code, output lines). One transaction: committed only by `apply` with every
    value verified."""
    problem = key_problem(new_key, old_key)
    if problem:
        raise UsageError(problem)
    async with sessionmaker() as session:
        try:
            async with session.begin():
                report = await rotate(session, old_key, new_key)
                if not apply:
                    raise _DryRunDone(report)  # leaving the block rolls back
                await write_audit(session, agent_id=None, method=AUDIT_METHOD,
                                  params=report.audit_params(), outcome="ok", latency_ms=0)
        except _DryRunDone as done:
            return 0, [*_report_lines(done.report),
                       ("prova (dry-run): nessuna modifica salvata (rollback). "
                       "Per applicare: --apply")]
        except RotationRefused as exc:
            lines = [*_report_lines(exc.report),
                     ("rifiutato: nessuna modifica salvata (rollback). Reinserisci o elimina "
                     "le credenziali elencate, oppure controlla SMT_MASTER_KEY")]
            if apply:
                async with session.begin():
                    await write_audit(session, agent_id=None, method=AUDIT_METHOD,
                                      params=exc.report.audit_params(), outcome="refused",
                                      latency_ms=0)
            return 1, lines
    return 0, [*_report_lines(report),
               ("applicato: valori cifrati con la nuova chiave (commit). Ora aggiorna "
               "SMT_MASTER_KEY in .env e riavvia api, worker e backup")]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m smtrading.devtools.rotate_master_key",
        description=f"Cambio della chiave master: la nuova chiave si legge da {NEW_KEY_ENV}.",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--generate", action="store_true",
                      help="stampa una nuova chiave casuale (solo quella) e termina")
    mode.add_argument("--dry-run", action="store_true",
                      help="prova completa con rollback finale (predefinito)")
    mode.add_argument("--apply", action="store_true",
                      help="applica: commit solo se ogni valore supera la verifica")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.generate:
        print(generate_key())
        return 0
    configure_logging()
    try:
        settings = get_settings()
        old_key = settings.master_key
        new_key = os.environ.get(NEW_KEY_ENV, "")
        for value in (old_key, new_key):
            register_secret(value)  # masked should anything ever log them
        engine = create_async_engine(settings.database_url, hide_parameters=True)
        try:
            code, lines = asyncio.run(_run_and_dispose(engine, old_key, new_key, args.apply))
        except UsageError as exc:
            print(f"errore: {exc}", file=sys.stderr)
            return 2
    except Exception as exc:  # noqa: BLE001 - only the type: a message could quote data
        print(f"errore: {type(exc).__name__}; nessuna modifica salvata", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return code


async def _run_and_dispose(engine, old_key: str, new_key: str, apply: bool):
    try:
        return await run(async_sessionmaker(engine, expire_on_commit=False), old_key, new_key,
                         apply=apply)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
