"""Automatic backups, pure parts: names, sha256 sidecars, retention (30 daily + 12
monthly), BACKUP_DIRS with spaces, the config bundle without secrets, the schedule and
the «Backup» section of «Salute del sistema»."""

import hashlib
import subprocess
import tarfile
from datetime import UTC, datetime, time, timedelta

import pytest

from smtrading.admin.system_health import backup_section
from smtrading.backup import files
from smtrading.backup.service import (
    BACKUP_AT,
    RESTORE_AT,
    RESTORE_WEEKDAY,
    is_due,
    last_occurrence,
)

TZ = "Europe/Rome"


# -- names ------------------------------------------------------------------------------

def test_names_use_the_local_time():
    now = datetime(2026, 11, 21, 2, 30, tzinfo=UTC)  # 03:30 in Rome (CET)
    st = files.stamp(now, TZ)
    assert st == "20261121-0330"
    assert files.dump_name(st) == "smtrading-20261121-0330.dump"
    assert files.config_name(st) == "smtrading-20261121-0330-config.tar.gz"
    assert files.sidecar_name("smtrading-20261121-0330.dump") == \
        "smtrading-20261121-0330.dump.sha256"
    summer = datetime(2026, 7, 1, 1, 30, tzinfo=UTC)  # CEST
    assert files.stamp(summer, TZ) == "20260701-0330"


@pytest.mark.parametrize("name,ok", [
    ("smtrading-20261121-0330.dump", True),
    ("smtrading-20261121-0330.dump.sha256", True),
    ("smtrading-20261121-0330-config.tar.gz", True),
    ("smtrading-20261121-0330-config.tar.gz.sha256", True),
    ("smtrading-2026-11-21.sql.gz", False),
    ("smtrading-20261121-0330.dump.partial", False),
    ("smtrading-20261341-0330.dump", False),  # not a date
    ("other-20261121-0330.dump", False),
    ("notes.txt", False),
])
def test_parse_stamp(name, ok):
    assert (files.parse_stamp(name) is not None) is ok


# -- sha256 -----------------------------------------------------------------------------

def test_sha256_sidecar_is_sha256sum_compatible(tmp_path):
    p = tmp_path / "smtrading-20261121-0330.dump"
    p.write_bytes(b"PGDMP" + b"x" * 3_000_000)
    digest = files.sha256_file(p)
    assert digest == hashlib.sha256(p.read_bytes()).hexdigest()
    side = files.write_sidecar(p, digest)
    assert side.name == "smtrading-20261121-0330.dump.sha256"
    assert side.read_text() == f"{digest}  {p.name}\n"
    assert files.read_sidecar(p) == digest and files.verify(p)
    try:
        out = subprocess.run(["sha256sum", "-c", side.name], cwd=tmp_path,
                             capture_output=True, text=True, check=False)
    except FileNotFoundError:
        pass
    else:
        assert out.returncode == 0, out.stdout + out.stderr
    p.write_bytes(b"changed")
    assert not files.verify(p)
    side.write_text("not a digest\n")
    assert files.read_sidecar(p) is None and not files.verify(p)


# -- retention --------------------------------------------------------------------------

def _daily(start: datetime, days: int, hour=3, minute=30) -> list[datetime]:
    return [(start + timedelta(days=i)).replace(hour=hour, minute=minute) for i in range(days)]


def test_retention_keeps_30_daily_and_12_monthly():
    stamps = _daily(datetime(2025, 1, 1), 500)  # 2025-01-01 .. 2026-05-15
    keep = files.select_keep(stamps)
    newest = max(stamps)
    daily = {s for s in keep if s > newest - timedelta(days=30)}
    assert len(daily) == 30 and newest in keep
    monthly = sorted(s for s in keep if s.day == 1)
    # the 1st of the 12 most recent months (2025-06 .. 2026-05)
    assert [(s.year, s.month) for s in monthly] == \
        [(2025, m) for m in range(6, 13)] + [(2026, m) for m in range(1, 6)]
    assert len(keep) == 30 + 12 - 1  # 2026-05-01 is both


def test_retention_newest_of_the_day_and_first_of_the_month():
    stamps = [datetime(2026, 11, 20, 3, 30), datetime(2026, 11, 20, 15, 2),  # manual run
              datetime(2026, 10, 2, 3, 30), datetime(2026, 10, 5, 3, 30)]  # no backup on the 1st
    keep = files.select_keep(stamps, daily=1, monthly=2)
    assert keep == {datetime(2026, 11, 20, 15, 2),  # newest of the newest day
                    datetime(2026, 11, 20, 3, 30),  # first of November
                    datetime(2026, 10, 2, 3, 30)}  # first of October


def test_retention_counts_days_with_a_backup_not_calendar_days():
    """A service stopped for months never lets the retention delete every backup."""
    old = _daily(datetime(2026, 1, 1), 40)
    keep = files.select_keep(old, daily=30, monthly=0)
    assert len(keep) == 30


def test_retention_plan_keeps_sets_whole_and_ignores_other_files():
    names = []
    for st in ("20261101-0330", "20261119-0330", "20261120-0330"):
        names += [f"smtrading-{st}.dump", f"smtrading-{st}.dump.sha256",
                  f"smtrading-{st}-config.tar.gz", f"smtrading-{st}-config.tar.gz.sha256"]
    names += ["notes.txt", "smtrading-2026-11-12.sql.gz",
              "smtrading-20261001-0330-config.tar.gz"]  # an orphan bundle: no dump
    kept, deleted = files.retention_plan(names, daily=1, monthly=1)
    assert set(kept) == {n for n in names if "20261120" in n or "20261101" in n}
    assert set(deleted) == {n for n in names if "20261119" in n} | {
        "smtrading-20261001-0330-config.tar.gz"}


def test_apply_retention_deletes_only_backup_files(tmp_path):
    for st in ("20261118-0330", "20261119-0330", "20261120-0330"):
        (tmp_path / f"smtrading-{st}.dump").write_bytes(b"d")
        (tmp_path / f"smtrading-{st}.dump.sha256").write_text("x")
    (tmp_path / "smtrading-20261117-0330.dump.partial").write_bytes(b"left over")
    (tmp_path / "mine.txt").write_text("keep me")
    deleted = files.apply_retention(tmp_path, daily=2, monthly=0)
    assert deleted == ["smtrading-20261117-0330.dump.partial", "smtrading-20261118-0330.dump",
                       "smtrading-20261118-0330.dump.sha256"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "mine.txt", "smtrading-20261119-0330.dump", "smtrading-20261119-0330.dump.sha256",
        "smtrading-20261120-0330.dump", "smtrading-20261120-0330.dump.sha256"]


# -- destinations -----------------------------------------------------------------------

ONEDRIVE = "/mnt/d/onedrive/OneDrive - SantoMagno/Founders - Founders/Backups/SMTrading"


def test_backup_dirs_with_spaces():
    raw = f" /mnt/d/Backups/SMTrading ;{ONEDRIVE};; /mnt/d/Backups/SMTrading "
    assert [str(p) for p in files.parse_dirs(raw)] == ["/mnt/d/Backups/SMTrading", ONEDRIVE]
    assert files.parse_dirs("") == [] and files.parse_dirs(None) == []


def test_inventory_in_a_folder_with_spaces(tmp_path):
    d = tmp_path / "OneDrive - SantoMagno" / "Founders - Founders" / "Backups"
    d.mkdir(parents=True)
    p = d / "smtrading-20261120-0330.dump"
    p.write_bytes(b"dump")
    files.write_sidecar(p, files.sha256_file(p))
    (d / "smtrading-20261120-0330-config.tar.gz").write_bytes(b"tar")
    (d / "smtrading-20261119-0330.dump").write_bytes(b"older, no sidecar")
    rows = files.inventory(d)
    assert [(r["name"], r["config"]) for r in rows] == [
        ("smtrading-20261120-0330.dump", True), ("smtrading-20261119-0330.dump", False)]
    assert rows[0]["sha256"] == files.sha256_file(p) and rows[1]["sha256"] is None


# -- config bundle ----------------------------------------------------------------------

@pytest.mark.parametrize("name,secret", [
    (".env", True), (".env.local", True), (".env.example", False), ("master.key", True),
    ("server.pem", True), ("id_ed25519", True), ("telegram_token.txt", True),
    ("secrets.json", True), ("docker-compose.yml", False), ("AGENTS.md", False),
])
def test_is_secret(name, secret):
    assert files.is_secret(name) is secret


def test_config_bundle_never_holds_secrets(tmp_path):
    agents = tmp_path / "agent-instructions"
    (agents / "ceo").mkdir(parents=True)
    (agents / "ceo" / "AGENTS.md").write_text("# CEO")
    (agents / ".env").write_text("SMT_MASTER_KEY=leak")
    cfg = tmp_path / "restore config"
    cfg.mkdir()
    (cfg / "docker-compose.yml").write_text("services: {}")
    (cfg / ".env.example").write_text("SMT_MASTER_KEY=")
    (cfg / ".env").write_text("SMT_MASTER_KEY=leak")
    (cfg / "web.key").write_text("leak")
    (cfg / "link.yml").symlink_to(cfg / ".env")
    target = tmp_path / "bundle.tar.gz"
    members = files.build_config_bundle(
        target, {"agent-instructions": agents, "config": cfg, "missing": tmp_path / "nope"},
        "notes")
    with tarfile.open(target) as tar:
        names = sorted(m.name for m in tar.getmembers() if m.isfile())
        assert tar.extractfile("RESTORE.txt").read() == b"notes"
        blob = b"".join(tar.extractfile(n).read() for n in names)
    assert names == sorted(members) == ["RESTORE.txt", "agent-instructions/ceo/AGENTS.md",
                                        "config/.env.example", "config/docker-compose.yml"]
    assert b"leak" not in blob


# -- schedule ---------------------------------------------------------------------------

def _rome(y, mo, d, h, mi):
    from zoneinfo import ZoneInfo

    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(TZ)).astimezone(UTC)


def test_last_occurrence_daily_and_weekly():
    now = _rome(2026, 11, 20, 3, 29)  # a Friday, before 03:30
    assert last_occurrence(now, TZ, BACKUP_AT) == _rome(2026, 11, 19, 3, 30)
    assert last_occurrence(_rome(2026, 11, 20, 3, 30), TZ, BACKUP_AT) == \
        _rome(2026, 11, 20, 3, 30)
    assert last_occurrence(now, TZ, RESTORE_AT, RESTORE_WEEKDAY) == _rome(2026, 11, 15, 4, 0)
    sunday = _rome(2026, 11, 22, 4, 0)
    assert last_occurrence(sunday, TZ, RESTORE_AT, RESTORE_WEEKDAY) == sunday


def test_is_due_once_per_occurrence_and_catches_up():
    now = _rome(2026, 11, 20, 10, 0)
    due, occ = is_due(None, now, TZ, BACKUP_AT)
    assert due and occ == _rome(2026, 11, 20, 3, 30).isoformat()  # first start: at once
    assert is_due(occ, now, TZ, BACKUP_AT) == (False, occ)
    later = _rome(2026, 11, 22, 9, 0)  # the PC was off two nights: one run
    due, occ2 = is_due(occ, later, TZ, BACKUP_AT)
    assert due and occ2 == _rome(2026, 11, 22, 3, 30).isoformat()


def test_schedule_across_dst():
    # 2026-03-29: Rome jumps from 02:00 to 03:00; 03:30 exists (CEST, 01:30 UTC)
    assert last_occurrence(_rome(2026, 3, 29, 12, 0), TZ, time(3, 30)) == \
        datetime(2026, 3, 29, 1, 30, tzinfo=UTC)


# -- «Salute del sistema» ---------------------------------------------------------------

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def _iso(delta: timedelta) -> str:
    return (NOW - delta).isoformat()


def _state(ok_age=timedelta(hours=8), last_status="ok", restore_status="ok",
           restore_age=timedelta(days=5)):
    last_at = _iso(ok_age if last_status == "ok" else timedelta(hours=1))
    return {
        "last_ok_at": _iso(ok_age) if ok_age is not None else None, "last_ok_name": "x.dump",
        "last_backup": {"status": last_status, "started_at": last_at, "error":
                        None if last_status == "ok" else "pg_dump uscito con 1"},
        "last_restore_test": None if restore_status is None else {
            "status": restore_status, "finished_at": _iso(restore_age),
            "error": None if restore_status == "ok" else "nessuna riga in fills"},
        "destinations": [{"dir": "/backups/local", "files": [{}, {}], "error": None}],
    }


def test_backup_section_ok():
    s = backup_section(_state(), NOW)
    assert s["status"] == "ok" and s["reasons"] == []
    assert s["summary"].startswith("Ultimo backup 8,0 h fa")
    assert s["destinations"] == [{"dir": "/backups/local", "files": 2, "error": None}]


def test_backup_section_old_backup_is_a_problem():
    s = backup_section(_state(ok_age=timedelta(hours=27)), NOW)
    assert s["status"] == "problema" and "ultimo backup riuscito 27,0 h fa" in s["reasons"]
    s = backup_section(_state(ok_age=timedelta(hours=25)), NOW)
    assert s["status"] == "ok"
    s = backup_section({}, NOW)
    assert s["status"] == "problema"
    assert s["reasons"][0] == "nessun backup automatico riuscito registrato"


def test_backup_section_failed_run_and_restore_test_are_warnings():
    s = backup_section(_state(last_status="non riuscito"), NOW)
    assert s["status"] == "attenzione"
    assert "ultimo backup non riuscito: pg_dump uscito con 1" in s["reasons"]
    s = backup_section(_state(restore_status="non riuscito"), NOW)
    assert s["status"] == "attenzione"
    assert "prova di ripristino non riuscita: nessuna riga in fills" in s["reasons"]
    s = backup_section(_state(restore_age=timedelta(days=9)), NOW)
    assert s["status"] == "attenzione" and s["reasons"] == ["ultima prova di ripristino 216,0 h fa"]
    s = backup_section(_state(restore_status=None), NOW)
    assert s["status"] == "attenzione" and s["reasons"] == ["prova di ripristino mai eseguita"]
    # a failed run older than the last ok one is history, not a warning
    st = _state()
    st["last_backup"] = {"status": "non riuscito", "started_at": _iso(timedelta(days=2))}
    assert backup_section(st, NOW)["status"] == "ok"


# -- the restore test never touches the live database -----------------------------------

@pytest.mark.parametrize("dbname", ["smtrading", "postgres", "smtrading_restore_test2"])
async def test_pg_runner_refuses_any_other_database(dbname):
    from smtrading.backup.runner import BackupError, PgRunner

    runner = PgRunner("postgresql+asyncpg://u:p@db:5432/smtrading")
    for call in (runner.restore(files.Path("x.dump"), dbname), runner.inspect(dbname),
                 runner.drop(dbname)):
        with pytest.raises(BackupError, match="database di prova non ammesso"):
            await call


def test_pg_args_keep_the_password_off_the_argv():
    from sqlalchemy.engine import make_url

    from smtrading.backup.runner import pg_args

    argv, env = pg_args(make_url("postgresql+asyncpg://u:p%40ss@db:5433/smtrading"))
    assert argv == ["--no-password", "-h", "db", "-p", "5433", "-U", "u"]
    assert env["PGPASSWORD"] == "p@ss" and "p@ss" not in " ".join(argv)
