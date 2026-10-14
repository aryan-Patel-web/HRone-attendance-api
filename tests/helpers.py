from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def ms(y, mo, d, h=0, mi=0, s=0, ms_=0) -> int:
    """IST wall-clock -> epoch milliseconds."""
    dt = datetime(y, mo, d, h, mi, s, tzinfo=IST)
    return int(dt.timestamp()) * 1000 + ms_




def make_employee(api, code="EMP0001", **kw):
    body = {
        "emp_code": code,
        "name": "Test " + code,
        "email": f"{code.lower()}@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05",
        **kw,
    }
    r = api.post("/employees", json=body)
    assert r.status_code == 201, r.text
    return r.json()
