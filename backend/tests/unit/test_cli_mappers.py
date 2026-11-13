"""CLI and seed work in a fresh process: every model is registered (broker_links FK).

Without `smtrading.models`, flushing a Portfolio in such a process fails with
NoReferencedTableError (portfolios.broker_link_id -> broker_links).
"""

import subprocess
import sys

import pytest

CODE = """
import {module}
from smtrading.portfolios.models import Portfolio
fk = next(iter(Portfolio.__table__.c.broker_link_id.foreign_keys))
assert fk.column.table.name == "broker_links"
print("ok")
"""


@pytest.mark.parametrize("module", ["smtrading.cli", "smtrading.seed"])
def test_entrypoints_register_all_models(module: str) -> None:
    out = subprocess.run(
        [sys.executable, "-c", CODE.format(module=module)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"
