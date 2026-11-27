"""SMT_AUTO_RULES_STREAM_RESERVE: a bad value falls back to 10 with a warning, never an
import error of the stream manager."""
import logging
import os
import subprocess
import sys

from smtrading.marketdata import streaming

DEFAULT = streaming.DEFAULT_AUTO_RULES_RESERVE


def test_reserve_parsed(caplog):
    assert DEFAULT == 10
    assert streaming.auto_rules_reserve(None) == 10
    assert streaming.auto_rules_reserve("7") == 7
    assert streaming.auto_rules_reserve(" 0 ") == 0
    assert caplog.records == []


def test_bad_reserve_falls_back_with_a_warning(caplog):
    caplog.set_level(logging.WARNING, logger="smtrading.marketdata.streaming")
    for raw in ("dieci", "", "-3", "2.5"):
        caplog.clear()
        assert streaming.auto_rules_reserve(raw) == 10
        [record] = caplog.records
        assert record.levelno == logging.WARNING
        assert "SMT_AUTO_RULES_STREAM_RESERVE" in record.getMessage()


def test_import_survives_a_bad_value():
    env = {**os.environ, "SMT_AUTO_RULES_STREAM_RESERVE": "dieci"}
    code = "from smtrading.marketdata import streaming as s; print(s.AUTO_RULES_RESERVE)"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, timeout=60, check=False)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "10"
