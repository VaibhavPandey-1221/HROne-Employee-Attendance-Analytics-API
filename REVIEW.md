# REVIEW.md — defects found in the starter `app/main.py`

Reviewed against `openapi.yaml` (R1–R10) and `DATA_MODEL.md`, before rewriting the file. Row references describe the starter code; each defect has a regression test in `tests/` unless noted.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | grace period checked on *floored* minutes (`> 10` after `// 60`) | punch at 09:40:01 → late 0; R2 requires 10 | compare whole seconds (`> 600`) before flooring; boundary tests |
| 2 | `compute_late_minutes` | no truncation to whole seconds (R1) | 09:40:01.9 treated differently than 09:40:01 | truncate instants to whole seconds at ingress |
| 3 | `compute_late_minutes` | wrong for overnight shifts after midnight | 00:30 punch-in on 22:00 shift → 0; must be 150 | attendance date per R1 (roll back before shift_end), measure from that day's shift start |
| 4 | `compute_work_hours` | used `round()` (banker's rounding) | 2.005 h → 2.0; R4 requires half-up → 2.01 | `Decimal` ROUND_HALF_UP on exact integer seconds / 3600 |
| 5 | `compute_work_hours` | sub-second inputs fed into the calculation | unstable rounding; R1 truncation missing | all instants truncated to whole seconds before any math |
| 6 | `compute_overtime_minutes` | mixed naive and aware datetimes | punch-out → `TypeError` → 500 | all internal times are aware UTC; ms↔datetime helpers |
| 7 | `compute_overtime_minutes` | used the *same day's* shift_end for overnight shifts | 21:55→06:40 gave 1480; must be 40 (R3) | shift end instant moves to next day when `end <= start` |
| 8 | `compute_overtime_minutes` | no 30-minute minimum | 5 min past shift end → 5; must be 0 | count only if ≥ 1800 s, then floor minutes |
| 9 | `now()` / `fromtimestamp` use | naive machine-local time | records dated in local time; R1 IST rules break | single `now_utc()` (aware, truncated); IST used only for R1 |
| 10 | `GET /health` | never contacted MongoDB | returns 200 while DB is down; contract wants 503 | ping via admin command; 503 when unreachable (verified) |
| 11 | `create_employee` | check-then-insert; no unique index | concurrent creates: both got 201 | unique index on `emp_code`; `DuplicateKeyError` → 409 (race test) |
| 12 | `create_employee` | no validation (emp_code, email, lengths, `HH:MM`, `shift_start == shift_end`) | garbage accepted | pydantic patterns/limits; equal shifts → 422 |
| 13 | `create_employee` | `created_at` naive, serialized as ISO string | contract wants epoch-ms integer | stored aware UTC; serialized via `dt_to_ms` |
| 14 | `list_employees` | `skip = page * page_size` | page 1 skipped an entire page | `skip = (page - 1) * page_size` |
| 15 | `list_employees` | `total` ignored the department filter | pagination totals wrong when filtering | `count_documents` on the same filter |
| 16 | `list_employees` | no `emp_code` sort; unbounded `page`/`page_size` | unstable order; page_size=10000 accepted | sort `emp_code` asc; `Query(ge=1/le=100)` |
| 17 | `punch_in` | unknown employee → `KeyError` → 500 | should be 404 | employee lookup first → 404 |
| 18 | `punch_in` | status not validated (ABSENT/LEAVE/garbage accepted) | contract allows only PRESENT/WFH/ON_DUTY | `Literal[...]` → 422 for others |
| 19 | `punch_in` | check-then-insert race | two simultaneous punch-ins both 201 | unique `(emp_code, date)`; `DuplicateKeyError` → 409 (race test) |
| 20 | `punch_in` | date taken from local wall clock; no IST/overnight rule | wrong record dates (R1) | `attendance_date_for` (IST + overnight rollback) |
| 21 | `punch_in` | `punched_at` unvalidated / untruncated | seconds, floats, strings, out-of-range accepted | `StrictInt` in [10^11, 4102444800000]; truncate to seconds |
| 22 | punch responses | leaked `"id"`; ISO datetime strings | contract forbids ids; wants epoch ms | `record_to_api` (no `_id`, ms integers) |
| 23 | `list_attendance` | loaded the whole collection into memory and sorted in Python | violates the 100k-row requirement | DB sort `{date:-1, emp_code:1}` + `skip`/`limit`; `count_documents` |
| 24 | `list_attendance` | sort had no `emp_code` tiebreaker | unstable order for same-day rows | added `emp_code` asc |
| 25 | `list_attendance` | no `date_from > date_to` check; status/page unvalidated | nonsense ranges/statuses accepted | explicit 422s; typed query params |
| 26 | module level | no indexes created at startup | duplicate punch-ins; COLLSCAN in `/admin/explain` | idempotent `ensure_indexes()` in lifespan (see DECISIONS.md) |
| 27 | module level | 10 of 11 endpoints missing | contract not implemented | all endpoints implemented per `openapi.yaml` + R1–R10 |

## Considered and **not** a defect

- `int(... .total_seconds()) // 60` for minutes-since-start is the right shape; only the grace *comparison* was wrong (rows 1–2).
- String date comparisons (`"2026-07-06" <= date <= ...`): correct because dates are zero-padded `YYYY-MM-DD` strings; kept (and used for fast range matches on an indexed field).
- `tz_aware=True` on the Mongo client: correct; aware UTC datetimes round-trip safely.
- `load_dotenv()` without `override`: correct — real environment variables must win over `.env`.
- The `localhost` default for `MONGO_URI`: harmless fallback, kept.
- ABSENT/LEAVE exist in `DATA_MODEL.md` as stored statuses: the model is fine; the punch-in endpoint simply must not accept them (row 18).
- `sample_seed.py`, `sample_data/*.json`: consistent with the data model; unchanged.
