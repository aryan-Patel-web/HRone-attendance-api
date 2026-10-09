# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (duplicate-safe create, joins); `(department, emp_code)` for department lists and the trend; `(joined_on, department)` for headcount. `attendance_logs`: unique `(emp_code, date)` (one record per day, month joins); `(date desc, emp_code)` for the list sort and the leaderboard scan; `(status, date desc, emp_code)`; `(emp_code, punch_in desc)` for punch-out. Rejected: an index on `late_minutes`; the date range already narrows the scan and it would slow every write.

2. **Punch-in race.** Both requests compute the same date and call `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept only one; the other gets `DuplicateKeyError`, which I turn into 409. There is no check-then-insert.

3. **Ties.** `$rank` gives tied employees the same rank and skips the next one. I filter `rank <= limit` after ranking, so a tie at the cutoff returns everyone in it, ordered by `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (`joined_on` on or before month end) and joins their logs with `$lookup`, so someone with zero logs still produces a row and is counted.

5. **100x the data.** I would stop scanning raw logs for analytics: keep a daily per-employee rollup, updated on every punch and correction, and store `department` on it.
