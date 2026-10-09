"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is loaded for convenience; real environment variables win)

Everything lives in this one file, laid out top to bottom as:

    1. Configuration & database handles
    2. Small helpers          (time conversion, half-up rounding, error helper)
    3. Business rules R1-R5   (pure functions - no database access, easy to unit test)
    4. Request models         (pydantic validation -> 422)
    5. Serializers            (Mongo document -> API JSON, datetimes -> epoch milliseconds)
    6. Indexes                (created idempotently at startup)
    7. Query / pipeline builders  (shared by the real endpoints AND the explain endpoint)
    8. Endpoints              (system, employees, attendance, analytics, admin/explain)
"""
import calendar
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time as Time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

log = logging.getLogger("attendance")

# =========================================================================== #
# 1. Configuration & database handles
# =========================================================================== #
load_dotenv()  # override=False (default) -> variables already in the environment take priority

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: PyMongo hands back timezone-aware (UTC) datetimes instead of naive ones, so we never
# mix naive and aware values in arithmetic. The short timeout keeps /health from hanging when Mongo is down.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000)
db = client[MONGO_DB]
EMPLOYEES = "employees"
LOGS = "attendance_logs"
employees = db[EMPLOYEES]
logs = db[LOGS]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")  # R6
NON_PRESENCE_STATUSES = ("ABSENT", "LEAVE")
GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_BELOW = Decimal("4.50")  # R5
MAX_SHIFT_LENGTH = timedelta(hours=24)  # punch-out / regularization limit
MAX_TREND_DAYS = 92
MIN_EPOCH_MS = 100_000_000_000  # anything smaller "looks like seconds"
MAX_EPOCH_MS = 4_102_444_800_000  # 2100-01-01

PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


# =========================================================================== #
# 2. Helpers
# =========================================================================== #
def to_ms(dt: Optional[datetime]) -> Optional[int]:
    """BSON datetime -> integer epoch milliseconds (exact integer maths, no float)."""
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: treat naive values as UTC
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def now_utc_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def now_utc_millis() -> datetime:
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)  # BSON stores milliseconds


def round_half_up(value: Any, places: int) -> Decimal:
    """Round-half-up (what the business rules ask for). Python's round() and Mongo's $round are banker's."""
    q = Decimal(1).scaleb(-places)
    return Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP)


def mongo_half_up(expr: Any, places: int) -> dict:
    """The same half-up rounding as a Mongo expression. `expr` must evaluate to a decimal128."""
    scale = 10**places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, scale]}, Decimal128("0.5")]}}, scale]}


def dec(expr: Any) -> dict:
    return {"$toDecimal": expr}


def num(x: Any) -> Optional[float]:
    """Mongo number (maybe Decimal128) -> plain float for JSON."""
    if x is None:
        return None
    if isinstance(x, Decimal128):
        return float(x.to_decimal())
    return float(x)


def unprocessable(msg: str, *loc: str) -> None:
    """Raise a 422 shaped like FastAPI's own validation errors."""
    raise HTTPException(
        status_code=422,
        detail=[{"loc": list(loc) or ["body"], "msg": msg, "type": "value_error"}],
    )


def hhmm(s: str) -> Time:
    h, m = s.split(":")
    return Time(int(h), int(m))


# =========================================================================== #
# 3. Business rules (pure functions)
# =========================================================================== #
def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded "HH:MM" strings compare correctly


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> Date:
    """R1: IST calendar date of the punch-in; for an overnight shift a punch-in earlier than shift_end
    belongs to the shift that started the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < hhmm(shift_end):
        day -= timedelta(days=1)
    return day


def _ist(day: Date, clock: str) -> datetime:
    return datetime.combine(day, hhmm(clock), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, att_date: Date, shift_start: str) -> int:
    """R2: late only when strictly more than 10:00 after shift_start; then floor minutes since shift_start."""
    seconds = int((punch_in - _ist(att_date, shift_start)).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime_minutes(punch_out: datetime, att_date: Date, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes between shift_end and punch_out, counted only if >= 30. An overnight shift ends the
    next calendar day."""
    end_day = att_date + timedelta(days=1) if is_overnight(shift_start, shift_end) else att_date
    seconds = int((punch_out - _ist(end_day, shift_end)).total_seconds())
    minutes = seconds // 60  # floor (also for negative values, which are < 30 anyway)
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded half-up to 2 decimals. Integer seconds + Decimal -> no float artefacts."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float(round_half_up(Decimal(seconds) / Decimal(3600), 2))


def derive_fields(
    emp: dict, att_date: Date, status: str, punch_in: Optional[datetime], punch_out: Optional[datetime]
) -> dict:
    """The four server-computed fields, for a record in its final state (used by punch-in, punch-out, PATCH)."""
    if status not in PRESENCE_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, att_date, emp["shift_start"])
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": compute_overtime_minutes(punch_out, att_date, emp["shift_start"], emp["shift_end"]),
        "half_day": Decimal(str(hours)) < HALF_DAY_BELOW,  # R5 uses the *rounded* hours
    }


# =========================================================================== #
# 4. Request models & validated types
# =========================================================================== #
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")


def _check_date(v: str) -> str:
    if not _DATE_RE.fullmatch(v):
        raise ValueError("must be a date in YYYY-MM-DD format")
    try:
        Date.fromisoformat(v)
    except ValueError:
        raise ValueError("not a real calendar date")
    return v


def _check_month(v: str) -> str:
    if not _MONTH_RE.fullmatch(v) or int(v[:4]) < 1:
        raise ValueError("must be a month in YYYY-MM format")
    return v


DateStr = Annotated[str, AfterValidator(_check_date)]
MonthStr = Annotated[str, AfterValidator(_check_month)]
# strict=True -> floats, strings and booleans are rejected, not coerced (R: "rejected with 422")
EpochMs = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
TimeStr = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: TimeStr = "09:30"
    shift_end: TimeStr = "18:30"
    joined_on: DateStr

    @field_validator("name", "department")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1, max_length=50)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1, max_length=50)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # Derived fields (work_hours, ...) are not declared, so anything a client sends for them is ignored.
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


# =========================================================================== #
# 5. Serializers (Mongo document -> API JSON)
# =========================================================================== #
def serialize_employee(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "name": d.get("name"),
        "email": d.get("email"),
        "department": d.get("department"),
        "shift_start": d.get("shift_start"),
        "shift_end": d.get("shift_end"),
        "joined_on": d.get("joined_on"),
        "created_at": to_ms(d.get("created_at")),
    }


def _serialize_change_value(field: str, value: Any) -> Any:
    return to_ms(value) if field in ("punch_in", "punch_out") else value


def serialize_history_entry(h: dict) -> dict:
    changes = {
        field: {
            "from": _serialize_change_value(field, c.get("from")),
            "to": _serialize_change_value(field, c.get("to")),
        }
        for field, c in (h.get("changes") or {}).items()
    }
    return {"at": to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"), "changes": changes}


def serialize_record(d: dict) -> dict:
    """Defaults cover legacy documents that lack history / half_day / the derived numbers."""
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": to_ms(d.get("punch_in")),
        "punch_out": to_ms(d.get("punch_out")),
        "work_hours": num(d.get("work_hours")),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day", False)),
        "history": [serialize_history_entry(h) for h in (d.get("history") or [])],
    }


# =========================================================================== #
# 6. Indexes (idempotent; called at startup and lazily from /health)
# =========================================================================== #
# (collection, keys, options)
INDEXES = [
    # employees ---------------------------------------------------------------------------------------------
    # unique business key -> duplicate-safe POST /employees, lookups in $lookup, GET /employees sort order
    (employees, [("emp_code", 1)], {"name": "uq_emp_code", "unique": True}),
    # department filter + emp_code ordering (GET /employees?department=, trend, 404 check for unknown dept)
    (employees, [("department", 1), ("emp_code", 1)], {"name": "ix_dept_emp"}),
    # headcount: joined_on <= end of period (department summary), optionally narrowed by department
    (employees, [("joined_on", 1), ("department", 1)], {"name": "ix_joined_dept"}),
    # attendance_logs ---------------------------------------------------------------------------------------
    # one record per employee per day (race-safe punch-in) + per-employee month ranges ($lookup in analytics)
    (logs, [("emp_code", 1), ("date", 1)], {"name": "uq_emp_date", "unique": True}),
    # GET /attendance default sort (date desc, emp_code asc) with a date-range filter; leaderboard month scan
    (logs, [("date", -1), ("emp_code", 1)], {"name": "ix_date_emp"}),
    # GET /attendance?status=... keeps the same sort order without an in-memory sort
    (logs, [("status", 1), ("date", -1), ("emp_code", 1)], {"name": "ix_status_date_emp"}),
    # punch-out: "most recent record of this employee whose punch_in <= punched_at"
    (logs, [("emp_code", 1), ("punch_in", -1)], {"name": "ix_emp_punch_in"}),
]
_index_lock = threading.Lock()
_indexes_ready = False


def ensure_indexes() -> None:
    global _indexes_ready
    with _index_lock:
        if _indexes_ready:
            return
        for coll, keys, opts in INDEXES:
            try:
                coll.create_index(keys, **opts)
            except OperationFailure as exc:
                # The only realistic failure: a unique index over data that already has duplicates. Do not take
                # the whole service down - fall back to a plain index so queries stay fast.
                if not opts.get("unique"):
                    raise
                log.error("could not create unique index %s (%s); creating a non-unique one", opts["name"], exc)
                coll.create_index(keys, name=opts["name"] + "_nonunique")
        _indexes_ready = True


# =========================================================================== #
# 7. Query / pipeline builders - used by the endpoints AND by /admin/explain
# =========================================================================== #
def month_bounds(month: str) -> tuple:
    """-> (first_day 'YYYY-MM-DD', last_day 'YYYY-MM-DD', number_of_days)"""
    year, mon = int(month[:4]), int(month[5:])
    n = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{n:02d}", n


def weekday_dates(month: str) -> list:
    """All Monday-Friday dates of the month as 'YYYY-MM-DD' strings (R7 - no holiday calendar)."""
    first, _, n = month_bounds(month)
    start = Date.fromisoformat(first)
    return [d.isoformat() for d in (start + timedelta(days=i) for i in range(n)) if d.weekday() < 5]


# expression fragments evaluated per attendance record -------------------------------------------------------
IS_PRESENT = {"$in": ["$status", list(PRESENCE_STATUSES)]}
LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$cond": [{"$gt": [LATE_MIN, 0]}, 1, 0]}
# a present record is worth 1 day, a half day 0.5 (R5, R6); stored half_day may be missing on legacy rows
PRESENT_VALUE = {
    "$cond": [IS_PRESENT, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]
}
HAS_WORK_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}


def build_attendance_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
    page: int,
    page_size: int,
) -> dict:
    if date_from and date_to and date_from > date_to:
        unprocessable("date_from must not be after date_to", "query", "date_from")
    flt: dict = {}
    if emp_code is not None:
        flt["emp_code"] = emp_code
    if date_from or date_to:
        flt["date"] = {}
        if date_from:
            flt["date"]["$gte"] = date_from
        if date_to:
            flt["date"]["$lte"] = date_to
    if status is not None:
        flt["status"] = status
    return {
        "filter": flt,
        "projection": {"_id": 0},
        "sort": [("date", -1), ("emp_code", 1)],
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }


def build_employee_monthly(emp_code: str, month: str) -> list:
    """Starts at the employee (unique-index lookup), joins that employee's month of logs through the
    (emp_code, date) index and aggregates them inside the join, then derives working_days / attendance_pct."""
    first, last, _ = month_bounds(month)
    weekdays = weekday_dates(month)
    month_stats = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                # present_days: only Mon-Fri records count, a half day is 0.5 (R5-R7)
                "present_days": {"$sum": {"$cond": [{"$in": ["$date", weekdays]}, PRESENT_VALUE, 0]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": IS_LATE},
                "total_late_minutes": {"$sum": LATE_MIN},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]
    return [
        {"$match": {"emp_code": emp_code}},
        {
            "$lookup": {
                "from": LOGS,
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": month_stats,
                "as": "s",
            }
        },
        {"$set": {"s": {"$ifNull": [{"$first": "$s"}, {}]}}},
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                # working days = Mon-Fri of the month on/after joined_on (R7); the weekday list comes from Python
                "working_days": {
                    "$size": {"$filter": {"input": weekdays, "as": "d", "cond": {"$gte": ["$$d", "$joined_on"]}}}
                },
                "present_days": {"$ifNull": ["$s.present_days", 0]},
                "leave_days": {"$ifNull": ["$s.leave_days", 0]},
                "late_count": {"$ifNull": ["$s.late_count", 0]},
                "total_late_minutes": {"$ifNull": ["$s.total_late_minutes", 0]},
                "total_overtime_minutes": {"$ifNull": ["$s.total_overtime_minutes", 0]},
            }
        },
        {
            "$set": {
                "attendance_pct": {
                    "$cond": [
                        {"$gt": ["$working_days", 0]},
                        mongo_half_up(
                            {"$divide": [{"$multiply": [dec("$present_days"), 100]}, dec("$working_days")]}, 2
                        ),
                        None,
                    ]
                }
            }
        },
    ]


def build_department_summary(month: str, department: Optional[str]) -> list:
    """Starts at *employees* (so people with zero logs still count) and joins their month of logs."""
    first, last, _ = month_bounds(month)
    weekdays = weekday_dates(month)
    match: dict = {"joined_on": {"$lte": last}}  # R9: joined on/before the end of the month
    if department is not None:
        match["department"] = department
    per_employee = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": {"$cond": [{"$in": ["$date", weekdays]}, PRESENT_VALUE, 0]}},
                # sum + count of work_hours (decimal128) so the department mean is over *records*
                "wh_sum": {"$sum": {"$cond": [HAS_WORK_HOURS, dec("$work_hours"), 0]}},
                "wh_cnt": {"$sum": {"$cond": [HAS_WORK_HOURS, 1, 0]}},
                "late_count": {"$sum": IS_LATE},
                "total_late_minutes": {"$sum": LATE_MIN},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }
        },
    ]
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": LOGS,
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": per_employee,
                "as": "s",
            }
        },
        # an employee without logs gets an empty stats object; $sum below ignores the missing fields
        {"$set": {"s": {"$ifNull": [{"$first": "$s"}, {}]}}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$s.present_days"},
                "wh_sum": {"$sum": "$s.wh_sum"},
                "wh_cnt": {"$sum": "$s.wh_cnt"},
                "late_count": {"$sum": "$s.late_count"},
                "total_late_minutes": {"$sum": "$s.total_late_minutes"},
                "leave_count": {"$sum": "$s.leave_count"},
                "on_duty_count": {"$sum": "$s.on_duty_count"},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$wh_cnt", 0]},
                        mongo_half_up({"$divide": ["$wh_sum", dec("$wh_cnt")]}, 2),
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]


def build_late_leaderboard(month: str, limit: int, department: Optional[str]) -> list:
    first, last, _ = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": EMPLOYEES, "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of employees that do not exist are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit is applied AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$e.name",
                "department": "$e.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def check_trend_range(date_from: str, date_to: str) -> int:
    d0, d1 = Date.fromisoformat(date_from), Date.fromisoformat(date_to)
    if d1 < d0:
        unprocessable("'to' must not be before 'from'", "query", "to")
    n = (d1 - d0).days + 1
    if n > MAX_TREND_DAYS:
        unprocessable(f"range must not exceed {MAX_TREND_DAYS} days", "query", "to")
    return n


def build_department_trend(department: str, date_from: str, date_to: str) -> list:
    n = check_trend_range(date_from, date_to)
    d0 = Date.fromisoformat(date_from)
    start_dt = datetime(d0.year, d0.month, d0.day, tzinfo=UTC)
    return [
        {"$match": {"department": department}},
        {
            "$facet": {
                # employees per join date -> headcount(day) = sum of counts with joined_on <= day (R9)
                "hc": [{"$group": {"_id": "$joined_on", "n": {"$sum": 1}}}],
                # logs of this department's employees, joined through the (emp_code, date) index, per day
                "logs": [
                    {
                        "$lookup": {
                            "from": LOGS,
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "pipeline": [
                                {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
                                {"$project": {"_id": 0, "date": 1, "present": PRESENT_VALUE, "late": IS_LATE}},
                            ],
                            "as": "l",
                        }
                    },
                    {"$unwind": "$l"},
                    {
                        "$group": {
                            "_id": "$l.date",
                            "present_count": {"$sum": "$l.present"},
                            "late_count": {"$sum": "$l.late"},
                        }
                    },
                ],
            }
        },
        {"$match": {"hc.0": {"$exists": True}}},  # no employees in this department -> no row -> 404
        {
            # gap filling: one row per calendar day of the range, whether or not any log exists
            "$project": {
                "_id": 0,
                "rows": {
                    "$map": {
                        "input": {"$range": [0, n]},
                        "as": "i",
                        "in": {
                            "$let": {
                                "vars": {"dt": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$$i"}}},
                                "in": {
                                    "$let": {
                                        "vars": {"ds": {"$dateToString": {"format": "%Y-%m-%d", "date": "$$dt"}}},
                                        "in": {
                                            "date": "$$ds",
                                            "wd": {"$in": [{"$dayOfWeek": "$$dt"}, [2, 3, 4, 5, 6]]},
                                            "headcount": {
                                                "$sum": {
                                                    "$map": {
                                                        "input": {
                                                            "$filter": {
                                                                "input": "$hc",
                                                                "as": "h",
                                                                "cond": {"$lte": ["$$h._id", "$$ds"]},
                                                            }
                                                        },
                                                        "as": "h",
                                                        "in": "$$h.n",
                                                    }
                                                }
                                            },
                                            "log": {
                                                "$first": {
                                                    "$filter": {
                                                        "input": "$logs",
                                                        "as": "l",
                                                        "cond": {"$eq": ["$$l._id", "$$ds"]},
                                                    }
                                                }
                                            },
                                        },
                                    }
                                },
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$rows"},
        {"$replaceRoot": {"newRoot": "$rows"}},
        {
            "$set": {
                "present_count": {"$ifNull": ["$log.present_count", 0]},
                "late_count": {"$ifNull": ["$log.late_count", 0]},
            }
        },
        {
            "$set": {
                # only working days with headcount > 0 get a rate; a working day without logs gets 0.0
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$wd", {"$gt": ["$headcount", 0]}]},
                        mongo_half_up({"$divide": [dec("$present_count"), dec("$headcount")]}, 4),
                        None,
                    ]
                }
            }
        },
        {
            # current row + up to 6 preceding rows *inside the requested range*; $avg ignores null rates
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {"mov": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": "$wd",
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {"$cond": [{"$eq": ["$mov", None]}, None, mongo_half_up("$mov", 4)]},
            }
        },
        {"$sort": {"date": 1}},
    ]


def run_pipeline(collection, pipeline: list) -> list:
    return list(collection.aggregate(pipeline, allowDiskUse=True))


# =========================================================================== #
# 8. Application & endpoints
# =========================================================================== #
@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # Mongo not reachable yet: /health will retry the index creation later
        log.error("index creation at startup failed: %s", exc)
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    description="Punch in/out with business rules, audited manual corrections, and MongoDB aggregation analytics.",
    lifespan=lifespan,
)

# Only needed for the optional React dashboard in ./frontend when it talks to the API directly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    log.exception("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable or query failed"})


def get_employee_or_404(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


def resolve_instant(punched_at: Optional[int]) -> datetime:
    """Request instant -> aware UTC datetime truncated to whole seconds (R1). Default: now."""
    return from_ms(punched_at) if punched_at is not None else now_utc_seconds()


# ----------------------------------------------------------------------------- system
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        ensure_indexes()  # no-op once done; covers "Mongo was down at startup"
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


# ----------------------------------------------------------------------------- employees
@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc_millis()
    try:
        employees.insert_one(doc)  # the unique index on emp_code is the guard, not a find-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    flt = {} if department is None else {"department": department}
    cursor = employees.find(flt, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [serialize_employee(d) for d in cursor],
        "total": employees.count_documents(flt),  # the filtered set, not the whole collection
        "page": page,
        "page_size": page_size,
    }


# ----------------------------------------------------------------------------- attendance
@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = resolve_instant(body.punched_at)
    att_date = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(emp, att_date, body.status, ts, None),
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date): exactly one of N parallel requests wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = resolve_instant(body.punched_at)
    for _ in range(3):
        # most recent record whose punch_in <= punched_at (works across midnight: it is found by instant, not date)
        rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)])
        if rec is None:
            raise HTTPException(404, "no punch-in found for this employee")
        if rec.get("punch_out") is not None:
            raise HTTPException(409, "already punched out")
        started = rec["punch_in"]
        if ts <= started:
            unprocessable("punched_at must be after the punch-in", "body", "punched_at")
        if ts - started > MAX_SHIFT_LENGTH:
            unprocessable("punched_at is more than 24 hours after the punch-in", "body", "punched_at")
        derived = derive_fields(emp, Date.fromisoformat(rec["date"]), rec["status"], started, ts)
        # Atomic compare-and-set: only the request that still sees punch_out == null can win (exactly one 200).
        updated = logs.find_one_and_update(
            {"_id": rec["_id"], "punch_out": None, "punch_in": started, "status": rec["status"]},
            {"$set": {"punch_out": ts, **derived}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return serialize_record(updated)
        # nothing matched: someone punched out (-> 409 on the next read) or corrected the record (-> recompute)
    raise HTTPException(409, "record was modified concurrently, please retry")


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    q = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
    cursor = logs.find(**q)  # sorted, skipped and limited by the database - never loaded into memory
    return {
        "items": [serialize_record(d) for d in cursor],
        "total": logs.count_documents(q["filter"]),
        "page": page,
        "page_size": page_size,
    }


def plan_regularization(emp: dict, rec: dict, body: RegularizeIn) -> tuple:
    """Validate the correction and compute (new_values, changes). Raises 422 on rule violations."""
    new_status = body.status or rec["status"]
    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")

    if new_status in NON_PRESENCE_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            unprocessable(f"status {new_status} clears the punch times; do not send punch_in/punch_out", "body")
        new_in = new_out = None
    else:
        new_in = from_ms(body.punch_in) if body.punch_in is not None else old_in
        new_out = from_ms(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            unprocessable(f"status {new_status} requires a punch_in", "body", "punch_in")
        if body.punch_in is not None:
            if attendance_date(new_in, emp["shift_start"], emp["shift_end"]).isoformat() != rec["date"]:
                unprocessable("punch_in must stay on the record's attendance date", "body", "punch_in")
        if new_out is not None and (body.punch_in is not None or body.punch_out is not None):
            if new_out <= new_in:
                unprocessable("punch_out must be after punch_in", "body", "punch_out")
            if new_out - new_in > MAX_SHIFT_LENGTH:
                unprocessable("punch_out must be within 24 hours of punch_in", "body", "punch_out")

    new_vals = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **derive_fields(emp, Date.fromisoformat(rec["date"]), new_status, new_in, new_out),
    }
    old_vals = {
        "status": rec["status"],
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": num(rec.get("work_hours")),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": old_vals[k], "to": v} for k, v in new_vals.items() if old_vals[k] != v}
    if not changes:
        unprocessable("this request changes nothing", "body")
    return new_vals, changes


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(emp_code: str, date: DateStr, body: RegularizeIn):
    emp = get_employee_or_404(emp_code)
    for _ in range(5):
        rec = logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no attendance record for this employee and date")
        new_vals, changes = plan_regularization(emp, rec, body)
        entry = {"at": now_utc_millis(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

        # Optimistic concurrency: the update only applies if the record is exactly as we read it. history is
        # append-only, so its length acts as a version number.
        version: dict = {"_id": rec["_id"], "status": rec["status"], "punch_in": rec.get("punch_in"),
                         "punch_out": rec.get("punch_out")}
        if isinstance(rec.get("history"), list):
            version["history"] = {"$size": len(rec["history"])}
        else:
            version["history"] = {"$exists": False}

        updated = logs.find_one_and_update(
            version,
            {"$set": new_vals, "$push": {"history": entry}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return serialize_record(updated)
        # lost the race: re-read and re-plan against the newer state, so no history entry is ever overwritten
    raise HTTPException(409, "record is being modified concurrently, please retry")


# ----------------------------------------------------------------------------- analytics
@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str, month: MonthStr):
    rows = run_pipeline(employees, build_employee_monthly(emp_code, month))
    if not rows:
        raise HTTPException(404, "employee not found")
    r = rows[0]
    return {
        "emp_code": r["emp_code"],
        "month": month,
        "working_days": int(r["working_days"]),
        "present_days": num(r["present_days"]),
        "leave_days": int(r["leave_days"]),
        "late_count": int(r["late_count"]),
        "total_late_minutes": int(r["total_late_minutes"]),
        "total_overtime_minutes": int(r["total_overtime_minutes"]),
        "attendance_pct": num(r.get("attendance_pct")),
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(month: MonthStr, department: Optional[str] = None):
    rows = run_pipeline(employees, build_department_summary(month, department))
    return {
        "month": month,
        "items": [
            {
                "department": r["department"],
                "headcount": int(r["headcount"]),
                "present_days": num(r["present_days"]),
                "avg_work_hours": num(r["avg_work_hours"]),
                "late_count": int(r["late_count"]),
                "total_late_minutes": int(r["total_late_minutes"]),
                "leave_count": int(r["leave_count"]),
                "on_duty_count": int(r["on_duty_count"]),
            }
            for r in rows
        ],
    }


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: MonthStr,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    rows = run_pipeline(logs, build_late_leaderboard(month, limit, department))
    return {
        "month": month,
        "items": [
            {
                "rank": int(r["rank"]),
                "emp_code": r["emp_code"],
                "name": r["name"],
                "department": r["department"],
                "total_late_minutes": int(r["total_late_minutes"]),
                "late_count": int(r["late_count"]),
            }
            for r in rows
        ],
    }


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    date_from: Annotated[DateStr, Query(alias="from")],
    date_to: Annotated[DateStr, Query(alias="to")],
):
    rows = run_pipeline(employees, build_department_trend(department, date_from, date_to))
    if not rows:
        raise HTTPException(404, "department not found")
    return {
        "department": department,
        "items": [
            {
                "date": r["date"],
                "is_working_day": bool(r["is_working_day"]),
                "headcount": int(r["headcount"]),
                "present_count": num(r["present_count"]),
                "late_count": int(r["late_count"]),
                "attendance_rate": num(r["attendance_rate"]),
                "moving_avg_7d": num(r["moving_avg_7d"]),
            }
            for r in rows
        ],
    }


# ----------------------------------------------------------------------------- admin / explain
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _require(**params: Any) -> None:
    for name, value in params.items():
        if value is None:
            unprocessable(f"query parameter '{name}' is required for this endpoint", "query", name)


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[MonthStr] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[DateStr], Query(alias="from")] = None,
    to: Annotated[Optional[DateStr], Query()] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    """Runs explain('executionStats') on the very query/pipeline the real endpoint builds."""
    if endpoint == "attendance_list":
        q = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
        collection = LOGS
        command = {
            "find": LOGS,
            "filter": q["filter"],
            "projection": q["projection"],
            "sort": dict(q["sort"]),
            "skip": q["skip"],
            "limit": q["limit"],
        }
    else:
        if endpoint == "employee_monthly":
            _require(emp_code=emp_code, month=month)
            collection, pipeline = EMPLOYEES, build_employee_monthly(emp_code, month)
        elif endpoint == "department_summary":
            _require(month=month)
            collection, pipeline = EMPLOYEES, build_department_summary(month, department)
        elif endpoint == "late_leaderboard":
            _require(month=month)
            collection, pipeline = LOGS, build_late_leaderboard(month, limit, department)
        else:  # department_trend
            _require(department=department, **{"from": from_, "to": to})
            collection, pipeline = EMPLOYEES, build_department_trend(department, from_, to)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}

    raw = db.command({"explain": command, "verbosity": "executionStats"})
    # bson -> plain JSON (dates, decimals, int64 become JSON-safe)
    explain = json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": collection, "explain": explain}
