from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from smtrading.telegram.quiet import is_quiet, next_quiet_end

TZ = "Europe/Rome"
ROME = ZoneInfo(TZ)


def _local(*args, fold=0):
    return datetime(*args, tzinfo=ROME).replace(fold=fold)


@pytest.mark.parametrize("day", [(2026, 11, 14), (2026, 10, 25), (2026, 3, 29), (2026, 10, 26)])
@pytest.mark.parametrize(("hms", "quiet"), [
    ((22, 59, 59), False),
    ((23, 0, 0), True),
    ((0, 0, 0), True),
    ((6, 59, 59), True),
    ((7, 0, 0), False),
    ((12, 0, 0), False),
])
def test_quiet_boundaries(day, hms, quiet):
    now = _local(*day, *hms).astimezone(UTC)
    assert is_quiet(now, TZ) is quiet


def test_quiet_during_repeated_dst_hour():
    # 2026-10-25 02:30 happens twice in Rome; both are night
    for fold in (0, 1):
        assert is_quiet(_local(2026, 10, 25, 2, 30, fold=fold).astimezone(UTC), TZ)


def test_next_quiet_end_across_dst():
    # end of DST: night of 24-25 Oct (CEST 23:00 -> CET 07:00)
    now = _local(2026, 10, 24, 23, 0).astimezone(UTC)
    assert now == datetime(2026, 10, 24, 21, 0, tzinfo=UTC)
    assert next_quiet_end(now, TZ) == datetime(2026, 10, 25, 6, 0, tzinfo=UTC)
    # start of DST: night of 28-29 Mar (CET 23:00 -> CEST 07:00)
    now = _local(2026, 3, 28, 23, 30).astimezone(UTC)
    assert next_quiet_end(now, TZ) == datetime(2026, 3, 29, 5, 0, tzinfo=UTC)
    # after midnight: same day 07:00
    now = _local(2026, 11, 14, 3, 0).astimezone(UTC)
    assert next_quiet_end(now, TZ) == datetime(2026, 11, 14, 6, 0, tzinfo=UTC)
    # at 07:00 exactly the next one is tomorrow
    now = _local(2026, 11, 14, 7, 0).astimezone(UTC)
    assert next_quiet_end(now, TZ) == datetime(2026, 11, 15, 6, 0, tzinfo=UTC)
