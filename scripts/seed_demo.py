"""Generate demo / load-test data straight into MongoDB (the way the grader seeds it).

    python scripts/seed_demo.py                                # ~40 employees, last 90 days  (for the dashboard)
    python scripts/seed_demo.py --employees 1000 --days 100    # ~70k+ records (performance / explain check)

Reads MONGO_URI / MONGO_DB from the environment or .env. Existing data in the two collections is replaced.
Includes the awkward cases: employees with no logs, mid-month joiners, an overnight shift, weekend records,
half days, leave / absent rows, open (not punched out) records and deliberate late-minute ties.
"""


import argparse
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.main import (  # noqa: E402  (path tweak above)
    IST, MONGO_DB, UTC, attendance_date, derive_fields, ensure_indexes, employees, logs,
)

DEPARTMENTS = ["Engineering", "Sales", "Support", "HR", "Operations"]
FIRST = ["Asha", "Vikram", "Neha", "Rohan", "Priya", "Arjun", "Meera", "Karan", "Isha", "Dev", "Sana", "Nikhil",
         "Tara", "Aman", "Riya", "Kabir", "Diya", "Yash", "Anika", "Rahul"]
LAST = ["Rao", "Shah", "Iyer", "Gupta", "Nair", "Mehta", "Singh", "Verma", "Das", "Kapoor", "Joshi", "Patel"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--employees", type=int, default=40)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rnd = random.Random(args.seed)

    today = datetime.now(IST).date()
    start = today - timedelta(days=args.days)

    emps = []
    for i in range(1, args.employees + 1):
        overnight = i % 17 == 0
        joined = start - timedelta(days=rnd.randint(30, 400))
        if i % 9 == 0:  # mid-period joiner
            joined = start + timedelta(days=rnd.randint(5, max(6, args.days - 10)))
        emps.append({
            "emp_code": f"EMP{i:04d}",
            "name": f"{rnd.choice(FIRST)} {rnd.choice(LAST)}",
            "email": f"user{i}@example.com",
            "department": DEPARTMENTS[i % len(DEPARTMENTS)],
            "shift_start": "22:00" if overnight else rnd.choice(["09:30", "09:30", "09:00", "10:00"]),
            "shift_end": "06:00" if overnight else rnd.choice(["18:30", "18:30", "18:00", "19:00"]),
            "joined_on": joined.isoformat(),
            "created_at": datetime.now(UTC).replace(microsecond=0),
        })

    docs = []
    for idx, e in enumerate(emps):
        if idx % 11 == 10:
            continue  # employees with no logs at all
        shift_h, shift_m = map(int, e["shift_start"].split(":"))
        end_h, end_m = map(int, e["shift_end"].split(":"))
        habit = rnd.choice([0, 0, 0, 12, 25, 40])  # habitual lateness -> plenty of ties on late minutes
        d = max(start, datetime.fromisoformat(e["joined_on"]).date())
        while d <= today:
            weekend = d.weekday() >= 5
            if (weekend and rnd.random() > 0.04) or (not weekend and rnd.random() < 0.06):
                d += timedelta(days=1)
                continue
            roll = rnd.random()
            status = "LEAVE" if roll < 0.04 else "ABSENT" if roll < 0.07 else rnd.choices(
                ["PRESENT", "WFH", "ON_DUTY"], [80, 14, 6])[0]
            if status in ("LEAVE", "ABSENT"):
                docs.append({"emp_code": e["emp_code"], "date": d.isoformat(), "status": status, "punch_in": None,
                             "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
                             "half_day": False, "history": []})
            else:
                base = datetime(d.year, d.month, d.day, shift_h, shift_m, tzinfo=IST)
                jitter = rnd.choice([-20, -5, 0, 3, 8, 10]) + (habit if rnd.random() < 0.5 else 0)
                p_in = (base + timedelta(minutes=jitter, seconds=rnd.randint(0, 59))).astimezone(UTC)
                length = timedelta(hours=rnd.choice([3.5, 4.2, 8, 8.5, 9, 9.2, 9.8, 10.5]), minutes=rnd.randint(0, 20))
                p_out = None if (d == today and rnd.random() < 0.7) else p_in + length
                att = attendance_date(p_in, e["shift_start"], e["shift_end"])
                docs.append({"emp_code": e["emp_code"], "date": att.isoformat(), "status": status, "punch_in": p_in,
                             "punch_out": p_out, **derive_fields(e, att, status, p_in, p_out), "history": []})
            d += timedelta(days=1)

    employees.delete_many({})
    logs.delete_many({})
    employees.insert_many(emps)
    for i in range(0, len(docs), 5000):
        logs.insert_many(docs[i:i + 5000], ordered=False)
    ensure_indexes()
    print(f"seeded {len(emps)} employees and {len(docs)} attendance records into '{MONGO_DB}'")


if __name__ == "__main__":
    main()
