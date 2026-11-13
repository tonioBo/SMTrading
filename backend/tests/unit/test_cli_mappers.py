"""The CLI works in a fresh process: every model is registered (broker_links FK)."""

import subprocess
import sys

CODE = (
    "import smtrading.cli\n"
    "from sqlalchemy.orm import configure_mappers\n"
    "from smtrading.common.db import Base\n"
    "configure_mappers()\n"
    "assert 'broker_links' in Base.metadata.tables\n"
    "from sqlalchemy.schema import CreateTable\n"
    "from smtrading.agents.models import Agent\n"
    "CreateTable(Agent.__table__)\n"
    "print('ok')\n"
)


def test_cli_import_registers_all_models() -> None:
    out = subprocess.run([sys.executable, "-c", CODE], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"
