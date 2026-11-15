"""Live hosts and paths guard: the real-money hosts, and the real eToro execution and
info prefixes (eToro shares one host between demo and real: its guard is the path),
appear only as class constants in brokers/live.

The patterns are built from LIVE_HOSTS and LIVE_PATHS: this file never spells a live host
or a real path itself.

Inside brokers/live (excluded from the repository scan) the real paths are written only
by `etoro_live.py` (the ClassVars) and, as regex forms (`.../execution/(demo/)?orders`),
by `fake.py`: the in-memory FakeLiveBroker, used only by tests and, when the
`live_fake_broker` setting is explicitly on, by e2e (`gate._default_inner`).
`test_real_paths_inside_brokers_live_only_in_adapter_and_fake` keeps it that way.
"""

import re
import socket
import subprocess
from pathlib import Path

import pytest

from smtrading.brokers.live import LIVE_HOSTS, LIVE_PATHS

REPO_ROOT = Path(__file__).resolve().parents[3]
EXCLUDED_PREFIXES = ("docs/", "backend/src/smtrading/brokers/live/")
BROKERS_LIVE = "backend/src/smtrading/brokers/live/"
# the only files of brokers/live allowed to write a real eToro path (see the docstring)
REAL_PATH_FILES = {f"{BROKERS_LIVE}etoro_live.py", f"{BROKERS_LIVE}fake.py"}


def _patterns() -> list[re.Pattern[str]]:
    # (?<![\w-]): `paper-api.<alpaca host>` is the paper host, not a live one
    return [re.compile(rf"(?<![\w-]){re.escape(h)}", re.IGNORECASE) for h in sorted(LIVE_HOSTS)]


def _path_patterns() -> list[re.Pattern[str]]:
    """One per real prefix, with or without the leading slash, never matching the demo
    variant (the same prefix followed by a `/demo` segment)."""
    return [re.compile(rf"{re.escape(p.lstrip('/'))}(?!/demo\b)", re.IGNORECASE)
            for p in sorted(LIVE_PATHS)]


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True,
                         capture_output=True).stdout
    return [p for p in out.decode().split("\0") if p]


def scan(root: Path, paths: list[str], patterns: list[re.Pattern[str]] | None = None,
         *, excluded: tuple[str, ...] = EXCLUDED_PREFIXES) -> list[str]:
    """`path:line` of every live-host occurrence (or of `patterns`) outside the allowed
    places."""
    patterns = _patterns() if patterns is None else patterns
    hits: list[str] = []
    for rel in paths:
        if excluded and rel.startswith(excluded):
            continue
        path = root / rel
        if not path.is_file():  # deleted in the working tree
            continue
        data = path.read_bytes()
        if b"\0" in data:  # binary
            continue
        text = data.decode("utf-8", "replace")
        for n, line in enumerate(text.splitlines(), 1):
            if any(p.search(line) for p in patterns):
                hits.append(f"{rel}:{n}")
    return hits


def test_live_hosts_only_in_brokers_live():
    assert len(LIVE_HOSTS) == 2
    assert scan(REPO_ROOT, tracked_files(REPO_ROOT)) == []


def test_guard_detects_a_planted_host(tmp_path):
    alpaca = next(h for h in LIVE_HOSTS if "alpaca" in h)
    t212 = next(h for h in LIVE_HOSTS if "alpaca" not in h)
    (tmp_path / "planted.py").write_text(f'URL = "https://{alpaca}/v2"\n')
    (tmp_path / "other.ts").write_text(f"const u = '{t212}'\n")
    (tmp_path / "paper.py").write_text(f'URL = "https://paper-{alpaca}"\n')  # not live
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "spec.md").write_text(alpaca)  # docs are excluded
    paths = [*tracked_files(REPO_ROOT)[:5], "planted.py", "other.ts", "paper.py",
             "docs/spec.md"]
    hits = scan(tmp_path, paths)
    assert hits == ["planted.py:1", "other.ts:1"]


def test_socket_guard_blocks_live_host():
    for host in LIVE_HOSTS:
        with pytest.raises(RuntimeError, match="host live nei test"):
            socket.getaddrinfo(host, 443)
        with pytest.raises(RuntimeError, match="host live nei test"):
            socket.create_connection((host.upper() + ".", 443), timeout=0.1)


def test_live_paths_only_in_brokers_live():
    assert len(LIVE_PATHS) == 4
    assert scan(REPO_ROOT, tracked_files(REPO_ROOT), _path_patterns()) == []


def test_guard_detects_planted_real_path(tmp_path):
    from smtrading.brokers.etoro_demo import EtoroDemoAdapter as Demo

    real = sorted(LIVE_PATHS)
    (tmp_path / "planted.py").write_text(f'URL = "{real[0]}/orders"\n')
    (tmp_path / "other.ts").write_text(f"const p = '{real[1].lstrip('/')}/pnl'\n")
    # a regex form of a real path (as the fake writes them) is caught too
    exec_v2 = next(p for p in real if p.startswith("/api/v2/") and "execution" in p)
    (tmp_path / "regex.py").write_text(f'R = r"^{exec_v2}/(demo/)?orders$"\n')
    demo = "\n".join(f'X = "{getattr(Demo, a)}/orders"'
                     for a in ("EXEC_V1", "EXEC_V2", "INFO_V1", "INFO_V2"))
    (tmp_path / "demo.py").write_text(demo + "\n")  # the demo variants: never hits
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "spec.md").write_text(real[0])  # docs are excluded
    paths = ["planted.py", "other.ts", "regex.py", "demo.py", "docs/spec.md"]
    assert scan(tmp_path, paths, _path_patterns()) == [
        "planted.py:1", "other.ts:1", "regex.py:1"]


def test_real_paths_inside_brokers_live_only_in_adapter_and_fake():
    """brokers/live is excluded from the scan above: there, only the real adapter and the
    test-only fake write a real path, and the fake is reached only through the explicit
    `live_fake_broker` setting."""
    files = [p for p in tracked_files(REPO_ROOT) if p.startswith(BROKERS_LIVE)]
    hits = scan(REPO_ROOT, files, _path_patterns(), excluded=())
    assert {h.rsplit(":", 1)[0] for h in hits} <= REAL_PATH_FILES
    assert any(h.startswith(f"{BROKERS_LIVE}etoro_live.py:") for h in hits)
    fake = (REPO_ROOT / BROKERS_LIVE / "fake.py").read_text().splitlines()
    for hit in hits:
        rel, line = hit.rsplit(":", 1)
        if rel.endswith("fake.py"):  # only as regex forms of the routes it answers
            assert "re.compile(" in fake[int(line) - 1], hit
    imports = re.compile(r"brokers\.live\.fake\b|brokers\.live import [^\n]*\bfake\b")
    users = [p for p in tracked_files(REPO_ROOT)
             if p.startswith("backend/src/") and p.endswith(".py")
             and imports.search((REPO_ROOT / p).read_text())]
    assert users == [f"{BROKERS_LIVE}gate.py"]  # the only importer of the fake in src
    gate = (REPO_ROOT / BROKERS_LIVE / "gate.py").read_text()
    assert "live_fake_broker" in gate and "fake_broker().transport()" in gate
