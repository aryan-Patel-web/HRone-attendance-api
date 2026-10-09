"""Analytics tests. Data is inserted straight into MongoDB (like the grader's seed) and the API's aggregation
pipelines are compared with a deliberately naive, independent pure-Python reference implementation."""
import calendar
import random
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import pytest

PRESENT = {"PRESENT", "WFH", "ON_DUTY"}


def hu(x, places):
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def emp(code, dept, joined="2026-01-05", name=None):
    return {"emp_code": code, "name": name or f"Name {code}", "email": f"{code}@x.io", "department": dept,
            "shift_start": "09:30", "shift_end": "18:30", "joined_on": joined,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}


def log(code, day, status="PRESENT", late=0, ot=0, wh=8.0, half=False, legacy=False):
    d = {"emp_code": code, "date": day, "status": status, "late_minutes": late, "overtime_minutes": ot,
         "work_hours": wh if status in PRESENT else None, "half_day": half, "history": [],
         "punch_in": datetime.fromisoformat(day).replace(tzinfo=timezone.utc) + timedelta(hours=4)
         if status in PRESENT else None, "punch_out": None}
    if legacy:
        d.pop("half_day"), d.pop("history")
    return d


# --------------------------------------------------------------------------- reference implementation
def month_days(month):
    y, m = int(month[:4]), int(month[5:])
    return [date(y, m, d) for d in range(1, calendar.monthrange(y, m)[1] + 1)]


def ref_present(r):
    return 0.5 if r.get("half_day") else 1.0


def ref_monthly(emps, logs, code, month):
    e = next(x for x in emps if x["emp_code"] == code)
    days = month_days(month)
    wd = sum(1 for d in days if d.weekday() < 5 and d.isoformat() >= e["joined_on"])
    rs = [r for r in logs if r["emp_code"] == code and r["date"].startswith(month)]
    present = sum(ref_present(r) for r in rs if r["status"] in PRESENT and date.fromisoformat(r["date"]).weekday() < 5)
    return {
        "working_days": wd, "present_days": present, "leave_days": sum(r["status"] == "LEAVE" for r in rs),
        "late_count": sum(r["late_minutes"] > 0 for r in rs), "total_late_minutes": sum(r["late_minutes"] for r in rs),
        "total_overtime_minutes": sum(r["overtime_minutes"] for r in rs),
        "attendance_pct": hu(Decimal(str(present)) * 100 / Decimal(wd), 2) if wd else None,
    }


def ref_summary(emps, logs, month, department=None):
    last = month_days(month)[-1].isoformat()
    out = {}
    for e in emps:
        if e["joined_on"] > last or (department is not None and e["department"] != department):
            continue
        o = out.setdefault(e["department"], dict(headcount=0, present_days=0.0, whs=[], late_count=0,
                                                 total_late_minutes=0, leave_count=0, on_duty_count=0))
        o["headcount"] += 1
        for r in logs:
            if r["emp_code"] != e["emp_code"] or not r["date"].startswith(month):
                continue
            if r["status"] in PRESENT and date.fromisoformat(r["date"]).weekday() < 5:
                o["present_days"] += ref_present(r)
            if r["status"] in PRESENT and r["work_hours"] is not None:
                o["whs"].append(Decimal(str(r["work_hours"])))
            o["late_count"] += r["late_minutes"] > 0
            o["total_late_minutes"] += r["late_minutes"]
            o["leave_count"] += r["status"] == "LEAVE"
            o["on_duty_count"] += r["status"] == "ON_DUTY"
    items = []
    for dept in sorted(out):
        o = out[dept]
        whs = o.pop("whs")
        items.append({"department": dept, **o, "avg_work_hours": hu(sum(whs) / len(whs), 2) if whs else None})
    return items


def ref_leaderboard(emps, logs, month, limit, department=None):
    info = {e["emp_code"]: e for e in emps}
    tot = {}
    for r in logs:
        if r["date"].startswith(month) and r["late_minutes"] > 0 and r["emp_code"] in info:
            e = info[r["emp_code"]]
            if department is None or e["department"] == department:
                t = tot.setdefault(r["emp_code"], [0, 0])
                t[0] += r["late_minutes"]
                t[1] += 1
    rows = sorted(tot.items(), key=lambda kv: (-kv[1][0], kv[0]))
    items = []
    for i, (code, (mins, cnt)) in enumerate(rows):
        rank = 1 + sum(1 for _, (m2, _) in rows if m2 > mins)
        if rank <= limit:
            items.append({"rank": rank, "emp_code": code, "name": info[code]["name"],
                          "department": info[code]["department"], "total_late_minutes": mins, "late_count": cnt})
    return items


def ref_trend(emps, logs, dept, d0, d1):
    mine = [e for e in emps if e["department"] == dept]
    codes = {e["emp_code"] for e in mine}
    rows, rates = [], []
    day = date.fromisoformat(d0)
    while day <= date.fromisoformat(d1):
        ds = day.isoformat()
        hc = sum(e["joined_on"] <= ds for e in mine)
        rs = [r for r in logs if r["emp_code"] in codes and r["date"] == ds]
        present = sum(ref_present(r) for r in rs if r["status"] in PRESENT)
        working = day.weekday() < 5
        rate = hu(Decimal(str(present)) / Decimal(hc), 4) if working and hc > 0 else None
        rates.append(rate)
        window = [x for x in rates[-7:] if x is not None]
        rows.append({"date": ds, "is_working_day": working, "headcount": hc, "present_count": present,
                     "late_count": sum(r["late_minutes"] > 0 for r in rs), "attendance_rate": rate,
                     "moving_avg_7d": hu(sum(Decimal(str(x)) for x in window) / len(window), 4) if window else None})
        day += timedelta(days=1)
    return rows


# --------------------------------------------------------------------------- hand-made edge cases
@pytest.fixture()
def seeded(db, api):
    emps = [
        emp("EMP0001", "Eng"), emp("EMP0002", "Eng"), emp("EMP0003", "Eng", joined="2026-07-15"),
        emp("EMP0004", "Eng", joined="2026-08-01"),  # joins after July -> not in July headcount
        emp("EMP0005", "Sales"), emp("EMP0006", "Sales"),  # EMP0006 has no logs at all
        emp("EMP0007", "Ops", joined="2026-08-05"),  # whole department joins later -> omitted for July
    ]
    logs = [
        log("EMP0001", "2026-07-06", late=20, ot=30, wh=9.12), log("EMP0001", "2026-07-07", wh=3.5, half=True),
        log("EMP0001", "2026-07-11", late=15, ot=45, wh=6.0),  # Saturday: counts for late/ot, not present_days
        log("EMP0002", "2026-07-06", late=20, wh=8.0), log("EMP0002", "2026-07-08", status="LEAVE"),
        log("EMP0002", "2026-07-09", status="ON_DUTY", wh=None), log("EMP0002", "2026-07-10", status="ABSENT"),
        log("EMP0003", "2026-07-16", legacy=True, wh=8.13), log("EMP0003", "2026-07-17", late=35, wh=8.12),
        log("EMP0005", "2026-07-06", late=35, wh=8.125), log("EMP0005", "2026-07-07", late=20, wh=8.0),
        log("GHOST01", "2026-07-06", late=999),  # no such employee: ignored by the leaderboard
        log("EMP0001", "2026-06-30", late=500),  # other month: ignored
    ]
    db.employees.insert_many([dict(e) for e in emps])
    db.attendance_logs.insert_many([dict(l) for l in logs])
    return emps, logs


def test_monthly_hand_checked(api, seeded):
    j = api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert j == {"emp_code": "EMP0001", "month": "2026-07", "working_days": 23, "present_days": 1.5, "leave_days": 0,
                 "late_count": 2, "total_late_minutes": 35, "total_overtime_minutes": 75, "attendance_pct": 6.52}
    j = api.get("/analytics/employees/EMP0003/monthly", params={"month": "2026-07"}).json()
    assert j["working_days"] == 13 and j["present_days"] == 2.0  # counted from joined_on (Wed 15th)
    j = api.get("/analytics/employees/EMP0004/monthly", params={"month": "2026-07"}).json()
    assert j["working_days"] == 0 and j["attendance_pct"] is None
    assert api.get("/analytics/employees/NOPE/monthly", params={"month": "2026-07"}).status_code == 404
    assert api.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-7"}).status_code == 422


def test_department_summary_hand_checked(api, seeded):
    j = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()
    assert [i["department"] for i in j["items"]] == ["Eng", "Sales"]  # Ops omitted: headcount 0
    eng, sales = j["items"]
    assert eng["headcount"] == 3  # EMP0004 not joined yet; EMP0003 joined 15th counts
    assert sales["headcount"] == 2  # EMP0006 has zero logs but counts
    assert sales["avg_work_hours"] == 8.06  # (8.125 + 8.0) / 2 = 8.0625 -> 8.06 ; mean over records
    assert eng["on_duty_count"] == 1 and eng["leave_count"] == 1
    # records with a work_hours value: 9.12, 3.5, 6.0, 8.0, 8.13, 8.12 (the ON_DUTY row has none) -> 42.87 / 6 = 7.145
    assert eng["avg_work_hours"] == 7.15  # half-up (a banker's rounding would give 7.14)
    only = api.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Sales"}).json()
    assert [i["department"] for i in only["items"]] == ["Sales"]


def test_leaderboard_ties_and_limit(api, db):
    db.employees.insert_many([emp(f"EMP000{i}", "A" if i % 2 else "B") for i in range(1, 8)])
    mins = {1: 100, 2: 80, 3: 80, 4: 80, 5: 50, 6: 50, 7: 10}
    db.attendance_logs.insert_many([log(f"EMP000{i}", "2026-07-06", late=m) for i, m in mins.items()])
    items = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 3}).json()["items"]
    # ranks 1,2,2,2 -> the tie at the cutoff is returned in full, ordered by emp_code
    assert [(i["rank"], i["emp_code"]) for i in items] == [(1, "EMP0001"), (2, "EMP0002"), (2, "EMP0003"), (2, "EMP0004")]
    items = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 5}).json()["items"]
    assert [i["rank"] for i in items] == [1, 2, 2, 2, 5, 5]
    items = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": "B"}).json()["items"]
    assert [(i["rank"], i["emp_code"]) for i in items] == [(1, "EMP0002"), (1, "EMP0004"), (3, "EMP0006")]
    assert api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 0}).status_code == 422
    assert api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 51}).status_code == 422


def test_trend_gap_filling_and_moving_average(api, seeded):
    j = api.get("/analytics/departments/Eng/trend", params={"from": "2026-07-04", "to": "2026-07-17"}).json()
    assert len(j["items"]) == 14  # exactly one row per day, weekends and empty days included
    rows = {r["date"]: r for r in j["items"]}
    assert rows["2026-07-04"]["is_working_day"] is False and rows["2026-07-04"]["attendance_rate"] is None
    assert rows["2026-07-06"]["headcount"] == 2 and rows["2026-07-06"]["present_count"] == 2.0
    assert rows["2026-07-06"]["attendance_rate"] == 1.0 and rows["2026-07-06"]["late_count"] == 2
    assert rows["2026-07-07"]["present_count"] == 0.5 and rows["2026-07-07"]["attendance_rate"] == 0.25
    assert rows["2026-07-13"]["attendance_rate"] == 0.0  # working day with no records at all
    assert rows["2026-07-16"]["headcount"] == 3  # EMP0003 joined on the 15th
    assert rows["2026-07-04"]["moving_avg_7d"] is None  # window holds no non-null rate yet
    assert rows["2026-07-06"]["moving_avg_7d"] == 1.0
    assert api.get("/analytics/departments/Nope/trend", params={"from": "2026-07-04", "to": "2026-07-05"}).status_code == 404
    assert api.get("/analytics/departments/Eng/trend", params={"from": "2026-07-05", "to": "2026-07-04"}).status_code == 422
    r = api.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01", "to": "2026-10-01"})
    assert r.status_code == 422  # 93 days
    assert api.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01", "to": "2026-09-30"}).status_code == 200  # 92


# --------------------------------------------------------------------------- randomized cross-check
def random_dataset(seed):
    rnd = random.Random(seed)
    depts = ["Eng", "Sales", "Ops", "HR"]
    emps = []
    for i in range(1, 41):
        joined = rnd.choice(["2026-01-05", "2026-06-20", "2026-07-01", "2026-07-10", "2026-07-22", "2026-08-03"])
        emps.append(emp(f"EMP{i:04d}", rnd.choice(depts), joined=joined))
    logs = []
    for e in emps:
        if rnd.random() < 0.2:
            continue  # employees without any logs
        for d in month_days("2026-07"):
            if rnd.random() < 0.35:
                continue  # a missing day is not an ABSENT record
            status = rnd.choices(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"], [60, 10, 8, 8, 8])[0]
            half = rnd.random() < 0.12
            wh = round(rnd.uniform(3.0, 10.5), 2) if rnd.random() > 0.1 else None  # some open records
            late = rnd.choice([0, 0, 0, 11, 20, 20, 35, 35, 60])
            logs.append(log(e["emp_code"], d.isoformat(), status=status, late=late if status in PRESENT else 0,
                            ot=rnd.choice([0, 0, 30, 45]), wh=wh, half=half, legacy=rnd.random() < 0.05))
    logs += [log("GHOST99", "2026-07-08", late=77)]
    return emps, logs


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_cross_check(api, db, seed):
    emps, logs = random_dataset(seed)
    db.employees.insert_many([dict(e) for e in emps])
    db.attendance_logs.insert_many([dict(l) for l in logs])
    full = [{**l, "half_day": l.get("half_day", False)} for l in logs]  # legacy rows: missing == false

    for e in emps[:15]:
        got = api.get(f"/analytics/employees/{e['emp_code']}/monthly", params={"month": "2026-07"}).json()
        want = ref_monthly(emps, full, e["emp_code"], "2026-07")
        assert {k: got[k] for k in want} == want, e["emp_code"]

    got = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]
    assert got == ref_summary(emps, full, "2026-07")
    got = api.get("/analytics/departments/summary", params={"month": "2026-06"}).json()["items"]
    assert got == ref_summary(emps, full, "2026-06")

    for limit in (1, 3, 10, 50):
        for dept in (None, "Eng", "Sales"):
            params = {"month": "2026-07", "limit": limit, **({"department": dept} if dept else {})}
            got = api.get("/analytics/leaderboard/late", params=params).json()["items"]
            assert got == ref_leaderboard(emps, full, "2026-07", limit, dept), params

    for dept in ("Eng", "Ops"):
        got = api.get(f"/analytics/departments/{dept}/trend", params={"from": "2026-06-28", "to": "2026-08-10"}).json()
        assert got["items"] == ref_trend(emps, full, dept, "2026-06-28", "2026-08-10"), dept
