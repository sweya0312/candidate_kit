# Starter defects found

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Uses naive subtraction and ignores the shift's IST calendar date/overnight attribution. | Punches around UTC midnight or overnight shifts get the wrong lateness. | Compute the shift start in IST for the attendance day, then compare instants. |
| 2 | `compute_late_minutes` | Grace boundary uses truncated integer minutes, so 09:40:01 can be treated as 10 and not late. | Check 09:40:00 versus 09:40:01 for a 09:30 shift. | Compare elapsed seconds strictly to 600, then floor total elapsed minutes. |
| 3 | `compute_work_hours` | Python float `round` uses ties-to-even rather than required decimal half-up. | A duration exactly at a half-cent-hour boundary rounds unexpectedly. | Use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | Builds a naive shift-end datetime with timezone metadata and fails to move overnight shift end to the next day. | Punch out after midnight for a 22:00-06:00 shift produces incorrect overtime. | Build an aware IST shift end on the proper calendar date, then compare UTC instants. |
| 5 | `create_employee` | Duplicate check followed by insert races under concurrent requests. | Send parallel creates with the same `emp_code`; duplicate rows or 500s can occur. | Enforce a unique database index and map `DuplicateKeyError` to 409. |
| 6 | `create_employee` | Returns BSON datetime for `created_at` rather than epoch milliseconds. | Response violates the instant convention / schema. | Convert all response instants to epoch milliseconds. |
| 7 | `create_employee` / `EmployeeIn` | Missing format, date and shift validation; equal shift endpoints accepted. | Invalid code, date, time or identical shift times enter storage. | Add Pydantic validation and reject equal shift times. |
| 8 | `list_employees` | `skip = page * page_size` skips the first page; `total` ignores the department filter. | Page 1 omits the first 20 employees and filtered totals remain global. | Use `(page - 1) * page_size` and count the filtered query. |
| 9 | `list_employees` | No page bounds, no deterministic sort. | Invalid page sizes and unstable page membership. | Validate pagination and sort by `emp_code`. |
| 10 | `punch_in` | Does not return 404 for an unknown employee and dereferences `emp` anyway. | Unknown `emp_code` triggers a server error. | Check employee existence first. |
| 11 | `punch_in` | Converts milliseconds using local host time and does not truncate to seconds. | Results vary by host timezone and fractional seconds leak into storage. | Parse epoch milliseconds as UTC and truncate before calculations/storage. |
| 12 | `punch_in` | Check-then-insert is not concurrency safe and there is no unique (`emp_code`,`date`) index. | Parallel punch-ins can create duplicates. | Add a compound unique index and map duplicate-key conflicts to 409. |
| 13 | `punch_in` | Does not validate presence status, and accepts seconds/float/string timestamps through loose Python handling. | `ABSENT` punch-ins or malformed times get stored / crash. | Constrain status and timestamp types/range at the request boundary. |
| 14 | `list_attendance` | Loads and sorts the whole collection in Python. | Slow/high memory use on 100k logs. | Filter, sort, skip, limit and count in MongoDB. |
| 15 | `list_attendance` | Pagination logic, input validation, date validation and ordering are incomplete. | Invalid ranges and pages behave inconsistently. | Validate ISO dates and bounds; sort date descending then employee ascending. |
| 16 | `list_attendance` | Returns Mongo `_id` as a string `id`, contrary to the natural-key contract; misses epoch conversion/defaults. | Response leaks an internal ID and timestamps are datetimes. | Remove `_id`; serialize instants and legacy defaults consistently. |
| 17 | Application setup / all endpoints | No startup indexes and health always returns 200 without checking MongoDB. | Duplicate races and false-ready status. | Create idempotent indexes at startup and ping MongoDB in health. |

## Reviewed and retained

The starter's intended natural employee key (`emp_code`), default `PRESENT` status, and initial attendance field values (`punch_out`/`work_hours` null, overtime 0, half-day false, empty history) match the contract. These are preserved while fixing validation, time handling and concurrency.
