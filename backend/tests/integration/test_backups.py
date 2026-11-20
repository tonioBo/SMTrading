"""Automatic backups against a real database, with a fake runner (no real pg_dump): the
run record in `app_settings`, the destinations (one with spaces, one created at run
time), the Telegram alert of a failed run, the restore test, the schedule,
`GET /api/backups`, `POST /api/backups/run` and the «Backup» section of
«Salute del sistema»."""
# ruff: noqa: F811  (the `admin` fixture of test_admin_telegram, imported then requested)

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from test_admin_telegram import admin  # noqa: F401  (the fixture)

from smtrading.admin import routes_system_health
from smtrading.admin.auth import hash_password
from smtrading.admin.models import AdminUser
from smtrading.agents.models import McpAuditLog
from smtrading.backup import files, service
from smtrading.backup.runner import RESTORE_DB, BackupError
from smtrading.telegram.models import TelegramOutbox

BACKEND_DIR = Path(__file__).resolve().parents[2]
INI = BACKEND_DIR / "alembic.ini"
NOW = datetime(2026, 11, 21, 2, 30, tzinfo=UTC)  # 03:30 in Rome
PAYLOAD = b"PGDMP-fake-" + b"x" * 100_000


class FakeRunner:
    def __init__(self, *, dump_error: str | None = None, versions: list[str] | None = None,
                 counts: dict[str, int] | None = None) -> None:
        self.dump_error, self.versions = dump_error, versions
        self.counts = counts if counts is not None else {"portfolios": 2, "orders": 5, "fills": 3}
        self.dumps: list[Path] = []
        self.restored: list[tuple[str, str]] = []
        self.dropped: list[str] = []

    async def dump(self, target: Path) -> None:
        if self.dump_error:
            raise BackupError(self.dump_error)
        self.dumps.append(target)
        target.write_bytes(PAYLOAD)

    async def restore(self, dump: Path, dbname: str) -> None:
        assert dump.read_bytes() == PAYLOAD
        self.restored.append((dump.name, dbname))

    async def inspect(self, dbname: str) -> dict:
        heads = service.alembic_heads(INI)
        return {"alembic_versions": self.versions if self.versions is not None else heads,
                "counts": self.counts}

    async def drop(self, dbname: str) -> None:
        self.dropped.append(dbname)


@pytest.fixture(autouse=True)
def _no_cache():
    routes_system_health.clear_cache()
    yield
    routes_system_health.clear_cache()


@pytest.fixture
def dests(tmp_path):
    local = tmp_path / "Backups" / "SMTrading"  # missing: created at run time
    onedrive = (tmp_path / "onedrive" / "OneDrive - SantoMagno" / "Founders - Founders"
                / "Backups" / "SMTrading")
    onedrive.parent.mkdir(parents=True)
    return local, onedrive


def _cfg(tmp_path, dirs) -> service.BackupConfig:
    agents = tmp_path / "agent-instructions" / "ceo"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "AGENTS.md").write_text("# CEO")
    conf = tmp_path / "restore-config"
    conf.mkdir(exist_ok=True)
    (conf / "docker-compose.yml").write_text("services: {}")
    (conf / ".env").write_text("SMT_MASTER_KEY=must-not-be-copied")
    raw = ";".join(str(d) for d in dirs)  # as in the compose file: `;` separated
    return service.BackupConfig(
        dirs=files.parse_dirs(raw), tz="Europe/Rome", alembic_ini=INI,
        bundle_sources={"agent-instructions": tmp_path / "agent-instructions",
                        "config": conf})


async def _outbox(sm) -> list[TelegramOutbox]:
    async with sm() as s:
        return list(await s.scalars(select(TelegramOutbox).order_by(TelegramOutbox.id)))


async def _health_backup(admin) -> dict:
    routes_system_health.clear_cache()  # cached 30 s
    r = await admin.client.get("/api/system-health")
    assert r.status_code == 200, r.text
    return r.json()["sections"]["backup"]


async def test_backup_to_two_destinations_and_restore_test(admin, tmp_path, dests):
    local, onedrive = dests
    cfg = _cfg(tmp_path, [local, onedrive])
    runner = FakeRunner()
    section = await _health_backup(admin)
    assert section["status"] == "problema" and section["label"] == "Backup"

    result = await service.run_backup(admin.sm, runner, cfg, now=NOW)
    assert result["status"] == "ok", result
    assert result["name"] == "smtrading-20261121-0330.dump"
    assert result["size_bytes"] == len(PAYLOAD) and result["error"] is None
    for d in (local, onedrive):
        names = sorted(p.name for p in d.iterdir())
        assert names == ["smtrading-20261121-0330-config.tar.gz",
                         "smtrading-20261121-0330-config.tar.gz.sha256",
                         "smtrading-20261121-0330.dump", "smtrading-20261121-0330.dump.sha256"]
        assert files.verify(d / "smtrading-20261121-0330.dump")
        assert files.verify(d / "smtrading-20261121-0330-config.tar.gz")
        import tarfile

        with tarfile.open(d / "smtrading-20261121-0330-config.tar.gz") as tar:
            members = sorted(m.name for m in tar.getmembers() if m.isfile())
        assert members == ["RESTORE.txt", "agent-instructions/ceo/AGENTS.md",
                           "config/docker-compose.yml"]
    assert await _outbox(admin.sm) == []  # no alert for an ok run

    r = await admin.client.get("/api/backups")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["last_ok_name"] == "smtrading-20261121-0330.dump"
    assert body["last_backup"]["status"] == "ok"
    assert [d["dir"] for d in body["destinations"]] == [str(local), str(onedrive)]
    assert body["destinations"][1]["files"][0]["sha256"] == result["sha256"]
    assert body["schedule"]["backup_at"] == "03:30"
    assert body["schedule"]["restore_test_database"] == RESTORE_DB
    rows = (await admin.client.get("/api/system/backups")).json()
    assert [(x["name"], x["size_bytes"]) for x in rows] == [
        ("smtrading-20261121-0330.dump", len(PAYLOAD))]

    section = await _health_backup(admin)
    assert section["status"] == "attenzione"  # the backup is fresh, no restore test yet
    assert section["reasons"] == ["prova di ripristino mai eseguita"]

    test = await service.run_restore_test(admin.sm, runner, cfg, now=NOW)
    assert test["status"] == "ok", test
    assert runner.restored == [("smtrading-20261121-0330.dump", RESTORE_DB)]
    assert runner.dropped == [RESTORE_DB, RESTORE_DB]  # a leftover first, then the test db
    assert test["checks"]["counts"] == {"portfolios": 2, "orders": 5, "fills": 3}
    section = await _health_backup(admin)
    assert section["status"] == "ok", section
    assert section["last_restore_test"]["status"] == "ok"
    history = (await admin.client.get("/api/backups")).json()["history"]
    assert [h["kind"] for h in history] == ["restore_test", "backup"]


async def test_failed_backup_is_recorded_and_alerted(admin, tmp_path, dests):
    cfg = _cfg(tmp_path, list(dests))
    result = await service.run_backup(
        admin.sm, FakeRunner(dump_error="pg_dump uscito con 1: connection refused"), cfg,
        now=NOW)
    assert result["status"] == "non riuscito"
    assert result["error"] == "pg_dump uscito con 1: connection refused"
    for d in dests:  # no partial file is left behind
        assert [p.name for p in d.iterdir()] == []
    [msg] = await _outbox(admin.sm)
    assert (msg.category, msg.level, msg.source) == ("system", "warning", "system")
    assert msg.text == "💾 Backup non riuscito: pg_dump uscito con 1: connection refused"
    section = await _health_backup(admin)
    assert section["status"] == "problema"
    assert "ultimo backup non riuscito: pg_dump uscito con 1: connection refused" in \
        section["reasons"]


async def test_partial_backup_when_a_destination_is_unusable(admin, tmp_path, dests):
    local, _ = dests
    blocker = tmp_path / "a file"
    blocker.write_text("not a folder")
    cfg = _cfg(tmp_path, [local, blocker / "SMTrading"])
    result = await service.run_backup(admin.sm, FakeRunner(), cfg, now=NOW)
    assert result["status"] == "parziale"
    assert [d["ok"] for d in result["destinations"]] == [True, False]
    assert files.verify(local / "smtrading-20261121-0330.dump")
    [msg] = await _outbox(admin.sm)
    assert msg.text.startswith("💾 Backup parziale: smtrading-20261121-0330.dump non copiato in ")
    async with admin.sm() as s:
        state = await service.load_state(s)
    assert state["last_ok_at"] is None  # only a backup in every destination counts as ok


async def test_retention_runs_in_every_destination(admin, tmp_path, dests):
    cfg = _cfg(tmp_path, list(dests))
    cfg.daily, cfg.monthly = 2, 0
    for d in dests:
        d.mkdir(parents=True, exist_ok=True)
        for st in ("20261118-0330", "20261119-0330", "20261120-0330"):
            (d / f"smtrading-{st}.dump").write_bytes(b"old")
        (d / "keep-me.txt").write_text("not ours")
    result = await service.run_backup(admin.sm, FakeRunner(), cfg, now=NOW)
    assert result["status"] == "ok"
    for d, row in zip(dests, result["destinations"], strict=True):
        assert sorted(p.name for p in d.iterdir() if p.name.endswith((".dump", ".txt"))) == [
            "keep-me.txt", "smtrading-20261120-0330.dump", "smtrading-20261121-0330.dump"]
        assert "smtrading-20261118-0330.dump" in row["deleted"]


async def test_restore_test_failures(admin, tmp_path, dests):
    cfg = _cfg(tmp_path, list(dests))
    runner = FakeRunner()
    empty = await service.run_restore_test(admin.sm, runner, cfg, now=NOW)
    assert empty["status"] == "non riuscito" and empty["error"] == "nessun backup da provare"
    assert runner.dropped == []  # nothing was created

    await service.run_backup(admin.sm, runner, cfg, now=NOW)
    bad = FakeRunner(versions=["0001"], counts={"portfolios": 1, "orders": 0, "fills": 0})
    result = await service.run_restore_test(admin.sm, bad, cfg, now=NOW)
    assert result["status"] == "non riuscito"
    assert result["error"].startswith("versione dello schema 0001 invece di ")
    assert result["error"].endswith("; nessuna riga in orders; nessuna riga in fills")
    assert bad.dropped == [RESTORE_DB, RESTORE_DB]  # dropped even when the test fails

    for d in dests:  # a corrupted newest dump: refused before any restore
        (d / "smtrading-20261121-0330.dump").write_bytes(b"corrupted")
    other = FakeRunner()
    result = await service.run_restore_test(admin.sm, other, cfg, now=NOW)
    assert result["error"] == "smtrading-20261121-0330.dump: impronta sha256 mancante o diversa"
    assert other.restored == []
    texts = [m.text for m in await _outbox(admin.sm)]
    assert texts[-1] == ("💾 Prova di ripristino del backup non riuscita: "
                         "smtrading-20261121-0330.dump: impronta sha256 mancante o diversa")
    section = await _health_backup(admin)
    assert section["status"] == "attenzione"
    assert section["reasons"] == [("prova di ripristino non riuscita: "
                                   "smtrading-20261121-0330.dump: impronta sha256 mancante "
                                   "o diversa")]


async def test_schedule_runs_once_per_occurrence(admin, tmp_path, dests):
    cfg = _cfg(tmp_path, list(dests))
    runner = FakeRunner()
    # first start (nothing recorded): the missed backup and restore test run at once
    assert await service.tick(admin.sm, runner, cfg, now=NOW) == ["backup", "restore_test"]
    assert await service.tick(admin.sm, runner, cfg, now=NOW) == []
    async with admin.sm() as s:
        state = await service.load_state(s)
    assert state["last_backup"]["trigger"] == "programmato"
    assert state["scheduled_backup_for"] == NOW.isoformat()
    next_night = datetime(2026, 11, 22, 2, 30, tzinfo=UTC)  # Sunday 03:30: backup only
    assert await service.tick(admin.sm, runner, cfg, now=next_night) == ["backup"]
    sunday_4 = datetime(2026, 11, 22, 3, 0, tzinfo=UTC)
    assert await service.tick(admin.sm, runner, cfg, now=sunday_4) == ["restore_test"]


async def _viewer(admin) -> httpx.AsyncClient:
    email = "viewer-backups@example.com"
    async with admin.sm() as s:
        s.add(AdminUser(email=email, password_hash=await hash_password("pw-viewer-1"),
                        role="viewer"))
        await s.commit()
    c = httpx.AsyncClient(transport=admin.client._transport, base_url="http://test")
    r = await c.post("/api/auth/login", json={"email": email, "password": "pw-viewer-1"})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = c.cookies["smt_csrf"]
    return c


async def test_run_endpoint_queues_a_request_served_by_the_service(admin, tmp_path, dests):
    cfg = _cfg(tmp_path, list(dests))
    runner = FakeRunner()
    await service.tick(admin.sm, runner, cfg, now=NOW)  # the scheduled runs are done
    viewer = await _viewer(admin)
    try:
        assert (await viewer.get("/api/backups")).status_code == 200
        assert (await viewer.post("/api/backups/run", json={})).status_code == 403
    finally:
        await viewer.aclose()

    r = await admin.client.post("/api/backups/run")
    assert r.status_code == 202, r.text
    req = r.json()["request"]
    assert req["by"] == admin.email and req["restore_test"] is False
    again = await admin.client.post("/api/backups/run", json={"restore_test": True})
    assert again.json()["request"]["requested_at"] == req["requested_at"]  # one request
    assert again.json()["request"]["restore_test"] is True
    assert (await admin.client.get("/api/backups")).json()["pending_request"]["by"] == admin.email
    async with admin.sm() as s:
        audit = list(await s.scalars(select(McpAuditLog.method)
                                     .where(McpAuditLog.method == "admin:backup.run")))
    assert len(audit) == 2

    later = datetime(2026, 11, 21, 14, 5, tzinfo=UTC)
    assert await service.tick(admin.sm, runner, cfg, now=later) == ["backup", "restore_test"]
    body = (await admin.client.get("/api/backups")).json()
    assert body["pending_request"] is None
    assert body["last_backup"]["trigger"] == "manuale"
    assert body["last_backup"]["name"] == "smtrading-20261121-1505.dump"
    assert body["last_restore_test"]["name"] == "smtrading-20261121-1505.dump"
    assert await service.tick(admin.sm, runner, cfg, now=later) == []
