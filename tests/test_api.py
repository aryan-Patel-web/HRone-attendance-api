"""End-to-end tests of employees / attendance endpoints over real HTTP, including the race conditions."""
from concurrent.futures import ThreadPoolExecutor

import httpx

from tests.helpers import make_employee, ms


# ------------------------------------------------------------------ employees
def test_create_employee_and_duplicates(api):
    e = make_employee(api)
    assert e["shift_start"] == "09:30" and isinstance(e["created_at"], int) and "_id" not in e
    assert api.post("/employees", json={**e, "created_at": 1}).status_code == 409


def test_create_employee_validation(api):
    base = {"emp_code": "EMP0001", "name": "A", "email": "a@b.co", "department": "X", "joined_on": "2026-01-05"}
    for bad in [
        {**base, "emp_code": "E1"}, {**base, "email": "nope"}, {**base, "joined_on": "2026-02-30"},
        {**base, "shift_start": "9:30"}, {**base, "shift_start": "10:00", "shift_end": "10:00"},
        {**base, "name": "   "}, {k: v for k, v in base.items() if k != "department"},
    ]:
        assert api.post("/employees", json=bad).status_code == 422, bad


def test_concurrent_duplicate_employee_creates(base_url, api):
    body = {"emp_code": "EMP0042", "name": "Race", "email": "r@x.io", "department": "X", "joined_on": "2026-01-05"}

    def go(_):
        with httpx.Client(base_url=base_url) as c:
            return c.post("/employees", json=body).status_code

    with ThreadPoolExecutor(16) as ex:
        codes = list(ex.map(go, range(16)))
    assert sorted(codes) == [201] + [409] * 15


def test_list_employees_pagination_filter_and_order(api):
    for i, dept in enumerate(["A", "B", "A", "B", "A"], start=1):
        make_employee(api, f"EMP{i:04d}", department=dept)
    r = api.get("/employees", params={"page": 1, "page_size": 2}).json()
    assert [x["emp_code"] for x in r["items"]] == ["EMP0001", "EMP0002"] and r["total"] == 5
    r = api.get("/employees", params={"department": "A", "page": 2, "page_size": 2}).json()
    assert [x["emp_code"] for x in r["items"]] == ["EMP0005"] and r["total"] == 3
    assert api.get("/employees", params={"page": 0}).status_code == 422
    assert api.get("/employees", params={"page_size": 101}).status_code == 422


# ------------------------------------------------------------------ punch-in
def test_punch_in_rules(api):
    make_employee(api)
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 40, 1, 900)})
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["date"] == "2026-07-06" and j["late_minutes"] == 10 and j["status"] == "PRESENT"
    assert j["punch_in"] == ms(2026, 7, 6, 9, 40, 1)  # truncated to whole seconds
    assert j["punch_out"] is None and j["work_hours"] is None and j["history"] == [] and "id" not in j


def test_punch_in_errors(api):
    make_employee(api)
    p = "/attendance/punch-in"
    assert api.post(p, json={"emp_code": "EMP9999"}).status_code == 404
    for bad in [
        {"punched_at": 1783312500}, {"punched_at": 1783312500000.5}, {"punched_at": "1783312500000"},
        {"punched_at": True}, {"punched_at": 99_999_999_999}, {"punched_at": 4_102_444_800_001},
        {"status": "ABSENT"}, {"status": "LEAVE"}, {"status": "BOGUS"},
    ]:
        assert api.post(p, json={"emp_code": "EMP0001", **bad}).status_code == 422, bad
    assert api.post(p, json={}).status_code == 422
    ok = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)}
    assert api.post(p, json=ok).status_code == 201
    assert api.post(p, json=ok).status_code == 409


def test_punch_in_overnight_shift_date(api):
    make_employee(api, "EMP0005", shift_start="22:00", shift_end="06:00")
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 2, 30)})
    assert r.json()["date"] == "2026-07-06"  # belongs to the shift that started the previous evening
    assert r.json()["late_minutes"] == 4 * 60 + 30


def test_concurrent_punch_ins_single_winner(base_url, api):
    make_employee(api)
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)}

    def go(_):
        with httpx.Client(base_url=base_url) as c:
            return c.post("/attendance/punch-in", json=body).status_code

    with ThreadPoolExecutor(20) as ex:
        codes = list(ex.map(go, range(20)))
    assert sorted(codes) == [201] + [409] * 19


# ------------------------------------------------------------------ punch-out
def test_punch_out_computes_everything(api):
    make_employee(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 10, 5)})
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 19, 10)})
    assert r.status_code == 200, r.text
    j = r.json()
    assert (j["work_hours"], j["late_minutes"], j["overtime_minutes"], j["half_day"]) == (9.08, 35, 40, False)
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 20)}).status_code == 409


def test_punch_out_across_midnight_and_limits(api):
    make_employee(api, "EMP0005", shift_start="22:00", shift_end="06:00")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 6, 21, 55)})
    # too early / not after punch-in / > 24h
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 6, 21, 55)}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 21, 56)}).status_code == 422
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 6, 40)})
    j = r.json()
    assert r.status_code == 200 and j["date"] == "2026-07-06"
    assert (j["work_hours"], j["overtime_minutes"]) == (8.75, 40)


def test_punch_out_half_day_and_404s(api):
    make_employee(api)
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404  # no punch-in
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP7777"}).status_code == 404  # no employee
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 30), "status": "WFH"})
    j = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 13, 0)}).json()
    assert j["work_hours"] == 3.5 and j["half_day"] is True and j["status"] == "WFH"


def test_concurrent_punch_outs_single_winner(base_url, api):
    make_employee(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 30)})
    body = {"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 30)}

    def go(_):
        with httpx.Client(base_url=base_url) as c:
            return c.post("/attendance/punch-out", json=body).status_code

    with ThreadPoolExecutor(20) as ex:
        codes = list(ex.map(go, range(20)))
    assert sorted(codes) == [200] + [409] * 19


# ------------------------------------------------------------------ list attendance
def test_list_attendance_sort_filter_paging(api, db):
    for c in ("EMP0001", "EMP0002"):
        make_employee(api, c)
        for day in (6, 7, 8):
            api.post("/attendance/punch-in", json={"emp_code": c, "punched_at": ms(2026, 7, day, 9, 30)})
    r = api.get("/attendance", params={"page_size": 4}).json()
    assert r["total"] == 6
    assert [(x["date"], x["emp_code"]) for x in r["items"]] == [
        ("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0002"), ("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0002")]
    r = api.get("/attendance", params={"emp_code": "EMP0002", "date_from": "2026-07-07", "date_to": "2026-07-07"}).json()
    assert r["total"] == 1 and r["items"][0]["emp_code"] == "EMP0002"
    assert api.get("/attendance", params={"date_from": "2026-07-09", "date_to": "2026-07-01"}).status_code == 422
    assert api.get("/attendance", params={"date_from": "2026-7-9"}).status_code == 422
    assert api.get("/attendance", params={"status": "NOPE"}).status_code == 422


def test_legacy_document_is_defaulted(api, db):
    from datetime import datetime, timezone
    db.attendance_logs.insert_one({
        "emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
        "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=timezone.utc), "work_hours": 8.98, "late_minutes": 0,
        "overtime_minutes": 0})
    it = api.get("/attendance").json()["items"][0]
    assert it["history"] == [] and it["half_day"] is False and isinstance(it["punch_in"], int)


# ------------------------------------------------------------------ regularization (PATCH)
def _open_day(api):
    make_employee(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 10, 5)})
    api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 18, 30)})


def test_patch_recomputes_and_appends_history(api):
    _open_day(api)
    p = "/attendance/EMP0001/2026-07-07"
    r = api.patch(p, json={"punch_in": ms(2026, 7, 7, 9, 28), "reason": "biometric glitch", "regularized_by": "hr.admin",
                           "work_hours": 99})  # derived fields from the client are ignored
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["late_minutes"] == 0 and j["work_hours"] == 9.03
    h = j["history"]
    assert len(h) == 1 and h[0]["by"] == "hr.admin"
    assert set(h[0]["changes"]) == {"punch_in", "late_minutes", "work_hours"}  # only what actually changed
    assert h[0]["changes"]["punch_in"] == {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)}
    # second correction appends, never overwrites
    r = api.patch(p, json={"status": "WFH", "reason": "worked remotely", "regularized_by": "hr2"})
    assert [x["by"] for x in r.json()["history"]] == ["hr.admin", "hr2"]


def test_patch_rules(api):
    _open_day(api)
    p = "/attendance/EMP0001/2026-07-07"
    meta = {"reason": "because", "regularized_by": "hr"}
    assert api.patch(p, json={**meta}).status_code == 422  # changes nothing
    assert api.patch(p, json={"status": "PRESENT", **meta}).status_code == 422  # same value
    assert api.patch(p, json={"status": "ABSENT", "punch_in": ms(2026, 7, 7, 9), **meta}).status_code == 422
    assert api.patch(p, json={"punch_in": ms(2026, 7, 8, 9), **meta}).status_code == 422  # other attendance date
    assert api.patch(p, json={"punch_out": ms(2026, 7, 7, 9), **meta}).status_code == 422  # before punch-in
    assert api.patch(p, json={"punch_out": ms(2026, 7, 8, 12), **meta}).status_code == 422  # > 24h
    assert api.patch(p, json={"reason": "x", "regularized_by": "hr", "status": "LEAVE"}).status_code == 422
    assert api.patch("/attendance/EMP0001/2026-07-09", json={"status": "LEAVE", **meta}).status_code == 404
    assert api.patch("/attendance/EMP0404/2026-07-07", json={"status": "LEAVE", **meta}).status_code == 404
    assert api.patch("/attendance/EMP0001/2026-13-07", json={"status": "LEAVE", **meta}).status_code == 422
    r = api.patch(p, json={"status": "LEAVE", **meta})
    j = r.json()
    assert r.status_code == 200 and j["punch_in"] is None and j["punch_out"] is None and j["work_hours"] is None
    assert (j["late_minutes"], j["overtime_minutes"], j["half_day"]) == (0, 0, False)
    # overtime was already 0 (punch-out at 18:30), so it is NOT listed: only fields that really changed
    assert set(j["history"][0]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "late_minutes"}
    # back to a presence status needs a punch_in
    assert api.patch(p, json={"status": "PRESENT", **meta}).status_code == 422
    assert api.patch(p, json={"status": "PRESENT", "punch_in": ms(2026, 7, 7, 9, 30), **meta}).status_code == 200


def test_concurrent_patches_keep_every_history_entry(base_url, api):
    _open_day(api)
    n = 8

    def go(i):
        with httpx.Client(base_url=base_url) as c:
            # every request moves punch_out by a different amount, so each one is a real change
            r = c.patch("/attendance/EMP0001/2026-07-07",
                        json={"punch_out": ms(2026, 7, 7, 19, i), "reason": f"fix number {i}", "regularized_by": "hr"})
            return r.status_code

    with ThreadPoolExecutor(n) as ex:
        codes = list(ex.map(go, range(1, n + 1)))
    final = api.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]
    assert all(c in (200, 409) for c in codes)
    assert len(final["history"]) == codes.count(200)  # nothing was silently lost
    assert codes.count(200) >= 1


def test_health_and_explain_validation(api):
    assert api.get("/health").json() == {"status": "ok"}
    assert api.get("/admin/explain/nonsense").status_code == 422
    assert api.get("/admin/explain/employee_monthly").status_code == 422
