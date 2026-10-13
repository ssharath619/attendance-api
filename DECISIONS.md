# DECISIONS.md

1. **Indexes.** On `employees`: `emp_code` (unique, so no duplicate codes), `(department, joined_on)` for department filters and headcount, and `joined_on` for summaries without a department. On `attendance_logs`: `(emp_code, date)` unique, so one record per day and fast per-employee queries, and `(date -1, emp_code)` for the list sort and the leaderboard date range. I rejected indexes on `status` and `late_minutes`: status has only five values, and the date range already narrows the leaderboard.

2. **Punch-in race.** Both requests find the employee and both call `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept only one of them. The other raises `DuplicateKeyError`, which I catch and return as 409. So one gets 201 and the other 409.

3. **Ties.** I rank with `$rank`, so tied people share a rank and the next rank is skipped. `limit` is applied after ranking (`rank <= limit`), so both tied people at the cutoff are returned, even if that is more than `limit` rows.

4. **Headcount.** The summary starts from `employees`, not from the logs. A `$lookup` with `preserveNullAndEmptyArrays` keeps people who have no logs, and headcount counts employees who joined on or before the month end.

5. **100x data.** I would pre-compute daily totals per department, so analytics would not scan every raw log.
