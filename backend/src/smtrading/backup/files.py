"""Pure file helpers of the automatic backups: names, sha256 sidecars, retention, the
destination list and the restore config bundle.

Names carry the local time of the run (SMT_TZ): `smtrading-YYYYMMDD-HHMM.dump`, its
sidecar `smtrading-YYYYMMDD-HHMM.dump.sha256` (the `sha256sum` format, so
`sha256sum -c` checks it), and the config bundle `smtrading-YYYYMMDD-HHMM-config.tar.gz`
(with its own sidecar). Only files matching these names are ever deleted."""

import hashlib
import io
import os
import re
import tarfile
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PREFIX = "smtrading-"
DUMP_SUFFIX = ".dump"
CONFIG_SUFFIX = "-config.tar.gz"
SIDECAR_SUFFIX = ".sha256"
PARTIAL_SUFFIX = ".partial"
STAMP_FORMAT = "%Y%m%d-%H%M"
NAME_RE = re.compile(r"^smtrading-(\d{8}-\d{4})(\.dump|-config\.tar\.gz)(\.sha256)?$")
PARTIAL_RE = re.compile(r"^smtrading-\d{8}-\d{4}.*\.partial$")
KEEP_DAILY = 30
KEEP_MONTHLY = 12
_CHUNK = 1024 * 1024


def stamp(now: datetime, tz: str) -> str:
    """The run's local time as YYYYMMDD-HHMM."""
    return now.astimezone(ZoneInfo(tz)).strftime(STAMP_FORMAT)


def dump_name(st: str) -> str:
    return f"{PREFIX}{st}{DUMP_SUFFIX}"


def config_name(st: str) -> str:
    return f"{PREFIX}{st}{CONFIG_SUFFIX}"


def sidecar_name(name: str) -> str:
    return name + SIDECAR_SUFFIX


def parse_stamp(name: str) -> datetime | None:
    """The (naive, local) time in a backup file name; None for any other file."""
    m = NAME_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), STAMP_FORMAT)
    except ValueError:
        return None


def parse_dirs(raw: str | None) -> list[Path]:
    """BACKUP_DIRS: paths separated by `;` (spaces inside a path are kept, the ones around
    it stripped), empty entries and duplicates dropped, order kept."""
    out: list[Path] = []
    for part in (raw or "").split(";"):
        part = part.strip()
        if part and Path(part) not in out:
            out.append(Path(part))
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def write_sidecar(path: Path, digest: str) -> Path:
    """`<name>.sha256` next to `path`, `sha256sum` format ("<hex>  <name>\\n")."""
    side = path.with_name(sidecar_name(path.name))
    tmp = side.with_name(side.name + PARTIAL_SUFFIX)
    tmp.write_text(f"{digest}  {path.name}\n", encoding="ascii")
    os.replace(tmp, side)
    return side


def read_sidecar(path: Path) -> str | None:
    """The digest recorded for `path` (None: no sidecar or not readable)."""
    try:
        text = path.with_name(sidecar_name(path.name)).read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    digest = text.split()[0] if text.split() else ""
    return digest.lower() if re.fullmatch(r"[0-9a-fA-F]{64}", digest) else None


def verify(path: Path) -> bool:
    expected = read_sidecar(path)
    return expected is not None and sha256_file(path) == expected


# -- retention --------------------------------------------------------------------------

def select_keep(stamps: Iterable[datetime], daily: int = KEEP_DAILY,
                monthly: int = KEEP_MONTHLY) -> set[datetime]:
    """The backups to keep: the newest of each of the `daily` most recent days that have a
    backup, plus the first backup of each of the `monthly` most recent months (normally
    the one of the 1st). Counting days with a backup (not calendar days) means a stopped
    service never lets the retention delete everything."""
    newest_of_day: dict[date, datetime] = {}
    first_of_month: dict[tuple[int, int], datetime] = {}
    for s in stamps:
        d = s.date()
        if d not in newest_of_day or s > newest_of_day[d]:
            newest_of_day[d] = s
        m = (s.year, s.month)
        if m not in first_of_month or s < first_of_month[m]:
            first_of_month[m] = s
    keep = {newest_of_day[d] for d in sorted(newest_of_day, reverse=True)[:daily]}
    keep |= {first_of_month[m] for m in sorted(first_of_month, reverse=True)[:monthly]}
    return keep


def retention_plan(names: Iterable[str], daily: int = KEEP_DAILY,
                   monthly: int = KEEP_MONTHLY) -> tuple[list[str], list[str]]:
    """(kept, deleted) backup file names of one destination. A set (dump, config bundle,
    their sidecars) shares one time stamp and is kept or deleted whole; a stamp counts
    only when its dump exists. Other files are in neither list."""
    by_stamp: dict[datetime, list[str]] = {}
    with_dump: set[datetime] = set()
    for n in names:
        st = parse_stamp(n)
        if st is None:
            continue
        by_stamp.setdefault(st, []).append(n)
        if n.endswith(DUMP_SUFFIX):
            with_dump.add(st)
    keep = select_keep(with_dump, daily, monthly)
    kept = sorted(n for st, ns in by_stamp.items() if st in keep for n in ns)
    deleted = sorted(n for st, ns in by_stamp.items() if st not in keep for n in ns)
    return kept, deleted


def apply_retention(directory: Path, daily: int = KEEP_DAILY,
                    monthly: int = KEEP_MONTHLY) -> list[str]:
    """Deletes what `retention_plan` drops (and leftover `.partial` files); returns the
    deleted names."""
    names = [p.name for p in directory.iterdir() if p.is_file()]
    _, deleted = retention_plan(names, daily, monthly)
    deleted += [n for n in names if PARTIAL_RE.match(n)]
    for n in deleted:
        (directory / n).unlink(missing_ok=True)
    return sorted(deleted)


def inventory(directory: Path) -> list[dict[str, Any]]:
    """The dumps of a destination, newest first: name, size, modification time, recorded
    sha256 and whether the config bundle is there."""
    rows = []
    for p in directory.iterdir():
        if not (p.is_file() and p.name.endswith(DUMP_SUFFIX) and parse_stamp(p.name)):
            continue
        st = p.stat()
        rows.append({
            "name": p.name, "size_bytes": st.st_size,
            "modified_at": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(),
            "sha256": read_sidecar(p),
            "config": (p.with_name(p.name[: -len(DUMP_SUFFIX)] + CONFIG_SUFFIX)).is_file(),
        })
    return sorted(rows, key=lambda r: r["name"], reverse=True)


# -- config bundle ----------------------------------------------------------------------

_SECRET_NAMES = re.compile(
    r"^(\.env(\..*)?|.*\.(key|pem|p12|pfx|crt|kdbx)|id_(rsa|ed25519|ecdsa|dsa).*|"
    r".*secret.*|.*credential.*|.*token.*)$", re.IGNORECASE)
_ALLOWED = {".env.example"}


def is_secret(name: str) -> bool:
    """A file name that may hold a secret: never put in a backup (`.env.example` is the
    one exception, it holds placeholders only)."""
    return name not in _ALLOWED and bool(_SECRET_NAMES.match(name))


def build_config_bundle(target: Path, sources: dict[str, Path], notes: str) -> list[str]:
    """A tar.gz at `target` with each existing source (file or folder) under its key
    (e.g. "config", "agent-instructions") and `RESTORE.txt` with `notes`. Secret-looking
    files and symlinks are skipped. Returns the archived member names."""
    members: list[str] = []

    def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if info.issym() or info.islnk() or not (info.isfile() or info.isdir()):
            return None
        if is_secret(Path(info.name).name):
            return None
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        if info.isfile():
            members.append(info.name)
        return info

    with tarfile.open(target, "w:gz") as tar:
        data = notes.encode()
        info = tarfile.TarInfo("RESTORE.txt")
        info.size = len(data)
        info.mtime = int(datetime.now(UTC).timestamp())
        tar.addfile(info, io.BytesIO(data))
        members.append("RESTORE.txt")
        for arc, src in sources.items():
            if src.is_dir():
                for p in sorted(src.rglob("*")):
                    if p.is_file() and not p.is_symlink() and not any(
                            is_secret(part) for part in p.relative_to(src).parts):
                        tar.add(p, arcname=f"{arc}/{p.relative_to(src).as_posix()}",
                                recursive=False, filter=keep)
            elif src.is_file() and not src.is_symlink():
                tar.add(src, arcname=f"{arc}/{src.name}" if arc else src.name,
                        recursive=False, filter=keep)
    return members
