"""Live hosts guard: the real-money hosts appear only as class constants in brokers/live.

The patterns are built from LIVE_HOSTS: this file never spells a live host itself.
"""

import re
import socket
import subprocess
from pathlib import Path

import pytest

from smtrading.brokers.live import LIVE_HOSTS

REPO_ROOT = Path(__file__).resolve().parents[3]
EXCLUDED_PREFIXES = ("docs/", "backend/src/smtrading/brokers/live/")


def _patterns() -> list[re.Pattern[str]]:
    # (?<![\w-]): `paper-api.<alpaca host>` is the paper host, not a live one
    return [re.compile(rf"(?<![\w-]){re.escape(h)}", re.IGNORECASE) for h in sorted(LIVE_HOSTS)]


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True,
                         capture_output=True).stdout
    return [p for p in out.decode().split("\0") if p]


def scan(root: Path, paths: list[str]) -> list[str]:
    """`path:line` of every live-host occurrence outside the allowed places."""
    patterns = _patterns()
    hits: list[str] = []
    for rel in paths:
        if rel.startswith(EXCLUDED_PREFIXES):
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
