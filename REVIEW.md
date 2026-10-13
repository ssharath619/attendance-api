# REVIEW.md

Defects found in the starter `app/main.py` and how each one was fixed.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `list_employees`, skip calculation | `skip = page * page_size`, so page 1 skips the first page of results | `GET /employees?page=1` does not return the first employee | `skip = (page - 1) * page_size` |
| 2 | `list_employees`, total | `total` was counted without the department filter | `GET /employees?department=QA` shows `total` for all employees, not just QA | `count_documents(q)` with the same filter `q` as the query |
| 3 | `list_employees`, ordering | No sort, so pages can repeat or miss rows | The same employee shows on two pages, or disappears, between calls | `.sort("emp_code", 1)` |
| 4 | `/health` | Always returned `ok`, even when MongoDB was down | Stop the database or use a wrong `MONGO_URI`: `/health` still says ok | Runs `ping` on MongoDB; returns 503 if it fails |
| 5 | `create_employee` | Check-then-insert: two parallel requests both pass the check and both insert | Two simultaneous POSTs with the same `emp_code` give two 201s | Unique index on `emp_code`; `DuplicateKeyError` is caught and returned as 409 |
| 6 | `punch_in`, date and time | Used naive server-local `datetime.now()` and server-local dates | Punching at 23:30 IST from a UTC server gets the wrong `date`; stored times are ambiguous | Store UTC instants (`tz_aware=True`); `date` is the IST date of `punch_in`, overnight shifts handled (R1); times truncated to seconds |
| 7 | `punch_in`, unknown employee | Crashes with a 500 when the employee does not exist | `POST /attendance/punch-in` for `EMP9999` returns 500 | 404 "employee not found" |
| 8 | `punch_in`, input validation | `status` and `punched_at` were not validated (any string, float, bool, negative number) | Send `status: "XYZ"` or `punched_at: "abc"` | `status` is a `Literal` (PRESENT, WFH, ON_DUTY); `punched_at` is a `StrictInt` inside an epoch-ms range; otherwise 422 |
| 9 | `punch_in`, duplicate record | Check-then-insert race: two parallel punch-ins create two records for one day | Fire two identical punch-ins at once: two 201s | Unique compound index `(emp_code, date)`; `DuplicateKeyError` becomes 409 |
| 10 | `compute_late_minutes` | Did not follow R2 (10 minute grace is strict: late only above 10:00; minutes counted from `shift_start`, floored) | 09:40:00 must be 0, 09:40:01 must be 10 | Rewritten: seconds since `shift_start`; if more than 600 then `seconds // 60`, else 0 |
| 11 | `compute_work_hours` | Float `round()` is banker's rounding, not half-up (R4) | A 9648 second shift must give 2.68, the starter gave 2.67 | `Decimal` with `ROUND_HALF_UP` to 2 decimals |
| 12 | `compute_overtime` | Did not follow R3 (whole minutes after `shift_end`, only if at least 30; overnight shift ends next day) | Out at 19:10 on a 18:30 shift must give 40, out at 18:50 must give 0 | Rewritten: floor minutes after the shift end (next day for overnight shifts), 0 if under 30 |
| 13 | Response of `punch_in` / `punch_out` / list | Leaked Mongo `id`/`_id`, returned datetimes instead of epoch milliseconds, missing defaults for old records | Response has `_id` or ISO strings instead of the contract fields | `record_to_api` builds the exact API shape (epoch ms, defaults for missing fields) |
| 14 | `punch_out` (and `punch_in`) timestamps | `punched_at` could be before `punch_in` or more than 24 hours later; no atomic guard | Punch out before punch in gives negative work hours; two punch-outs both succeed | 422 for invalid range; `find_one_and_update` with `punch_out: None` in the filter, so the loser gets 409 |
| 15 | `EmployeeIn` model | No validation of `emp_code`, email, shift times, `joined_on` | POST with `emp_code: "x"`, `shift_start: "25:99"`, `shift_start == shift_end`, `joined_on: "2026-02-31"` was accepted | Pydantic patterns/limits, a real-date check and a shifts-must-differ check |
| 16 | `create_employee`, `created_at` | Naive local `datetime.now()` | Time differs by server time zone | UTC, truncated to seconds, returned as epoch ms |
| 17 | `list_employees`, paging params | `page` / `page_size` not validated | `page=0`, `page_size=-5` or `page_size=100000` worked or crashed | `page >= 1`, `1 <= page_size <= 100` (R10) |
| 18 | `list_attendance`, performance | Loaded all documents and sorted/sliced in Python | Slow and memory heavy with 100k records | Filter, sort, skip and limit are done by MongoDB, backed by indexes |
| 19 | `list_attendance`, validation and output | No validation of dates/status, `date_from > date_to` accepted, `_id` leaked | `date_from=abc` gives 500, `status=XYZ` accepted | `date` and `Status` typed query parameters (422), explicit `date_from > date_to` check, `record_to_api` output |
| 20 | Whole app, indexes | No indexes at all | Every query is a collection scan (`COLLSCAN` in explain) | `create_indexes()` on startup: `employees.emp_code` (unique), `employees(department, joined_on)`, `employees.joined_on`, `attendance_logs(emp_code, date)` (unique), `attendance_logs(date -1, emp_code)` |

## Looked at and decided it is not a defect

- `load_dotenv()` does not override variables that are already set in the real environment. That is the wanted behaviour, because the grader's `MONGO_URI` wins over a local `.env`.
- Old records with no `late_minutes`, `overtime_minutes`, `half_day` or `history` field: the data model says missing means 0 / false / empty, so `record_to_api` defaults them instead of treating them as errors.
