"""Pure business-rule tests (R1-R5). No database needed."""
from datetime import date, datetime, timezone

import pytest

from app.main import (
    attendance_date,
    compute_late_minutes,
    compute_overtime_minutes,
    compute_work_hours,
    derive_fields,
    round_half_up,
)
from tests.helpers import IST


def t(h, m, s=0, day=6):  # IST instant on 2026-07-<day>
    return datetime(2026, 7, day, h, m, s, tzinfo=IST).astimezone(timezone.utc)


D = date(2026, 7, 6)


@pytest.mark.parametrize(
    "punch, expected",
    [((9, 29, 0), 0), ((9, 40, 0), 0), ((9, 40, 1), 10), ((10, 15, 59), 45), ((9, 0, 0), 0), ((10, 5, 0), 35)],
)
def test_late_rule_r2(punch, expected):
    assert compute_late_minutes(t(*punch), D, "09:30") == expected


@pytest.mark.parametrize("out, expected", [((18, 59, 59), 0), ((19, 0, 0), 30), ((19, 10, 0), 40), ((18, 30, 0), 0)])
def test_overtime_rule_r3(out, expected):
    assert compute_overtime_minutes(t(*out), D, "09:30", "18:30") == expected


def test_overtime_overnight_shift_ends_next_day():
    out = datetime(2026, 7, 7, 6, 40, tzinfo=IST).astimezone(timezone.utc)
    assert compute_overtime_minutes(out, D, "22:00", "06:00") == 40


def test_work_hours_half_up_not_bankers():
    # 8.125 h exactly: Python round() gives 8.12, the rule says 8.13
    pi = t(9, 0)
    po = datetime.fromtimestamp(pi.timestamp() + 29250, tz=timezone.utc)
    assert round(29250 / 3600, 2) == 8.12
    assert compute_work_hours(pi, po) == 8.13
    assert round_half_up("0.03125", 4) == round_half_up("0.0313", 4)


def test_half_day_uses_rounded_hours():
    emp = {"shift_start": "09:30", "shift_end": "18:30"}
    pi = t(9, 30)
    # 4.495 h -> rounds to 4.50 -> NOT a half day ; 4.494 h -> 4.49 -> half day
    po_a = datetime.fromtimestamp(pi.timestamp() + 16182, tz=timezone.utc)  # 4.495
    po_b = datetime.fromtimestamp(pi.timestamp() + 16178, tz=timezone.utc)  # 4.4939
    assert derive_fields(emp, D, "PRESENT", pi, po_a)["half_day"] is False
    assert derive_fields(emp, D, "PRESENT", pi, po_b)["half_day"] is True


def test_attendance_date_overnight():
    # 22:00-06:00 shift: 02:30 belongs to the previous day's shift, 21:55 / 07:00 to their own day
    assert attendance_date(t(2, 30, day=7), "22:00", "06:00") == date(2026, 7, 6)
    assert attendance_date(t(21, 55, day=6), "22:00", "06:00") == date(2026, 7, 6)
    assert attendance_date(t(7, 0, day=7), "22:00", "06:00") == date(2026, 7, 7)
    assert attendance_date(t(2, 30, day=7), "09:30", "18:30") == date(2026, 7, 7)


def test_absent_and_leave_have_zero_derived_fields():
    emp = {"shift_start": "09:30", "shift_end": "18:30"}
    assert derive_fields(emp, D, "LEAVE", None, None) == {
        "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
