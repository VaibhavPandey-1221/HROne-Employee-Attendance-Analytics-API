# DECISIONS.md

1. **Indexes.** Unique `emp_code` (employee identity + create race); unique `(emp_code, date)` (one record per employee-day + punch-in race); `(date desc, emp_code)` (attendance list sort/pagination); `(emp_code, punch_in desc)` (punch-out finds the open record); `(department, joined_on)` (department filters, trend headcount); `joined_on` (R9 headcount lookups). Rejected `{status: 1}`: low selectivity — date/emp_code indexes already filter, status is just a residual match.

2. **Punch-in race.** Both requests look up the employee, derive the IST attendance date, then insert. The unique `(emp_code, date)` index makes the second insert fail with `DuplicateKeyError`, mapped to 409. No check-then-insert, so exactly one 201 and one 409 — never two records.

3. **Ties.** Ranking uses `$setWindowFields` `$rank` sorted by total only, giving standard competition ranks (1, 2, 2, 4). `limit` is applied *after* ranking (`$match rank <= limit`), so everyone tied at the cutoff is returned (the list can exceed `limit`), ordered total desc, `emp_code` asc.

4. **Headcount.** The month's logs are grouped per employee, then `$unionWith` adds every employee whose `joined_on ≤ month end`. Merging by `emp_code` gives zero-log staff a row with zero stats; people joining later drop out before grouping by department.

5. **At 100x.** Replace live cross-collection scans with materialized rollups: a scheduled `$merge` pipeline storing per employee-month summaries and a department-day headcount series. Endpoints read small precomputed documents; raw logs stay for punch-in/out and the audit trail only.

6. **Trend gap-filling (`$densify`).** One row per calendar day via `$densify` with explicit bounds `[from, to + 1 day]`: the explicit upper bound is exclusive on all supported versions (6.0-8.0), so `to + 1` is what includes the last day (and keeps a single-day range non-empty on 8.0, where equal bounds produce an empty set). `attendance_rate` is kept as an integer scaled by 10000 (4 decimals half-up) so `moving_avg_7d` is the mean of the *reported* values over the current and up to 6 preceding rows, rounded half-up.

7. **Audit trail and concurrent corrections.** `PATCH` recomputes derived fields from the final state (R2-R5) and appends exactly one `history` entry - `by`, `reason`, UTC `at`, and a per-field `from`/`to` for only the fields that actually changed. The write is one `find_one_and_update` guarded by the exact `history` array and the fields it read: two concurrent corrections cannot silently drop an entry - the loser gets 409. Legacy documents without `history` match via `{"$exists": false}` and gain the field on their first correction.

8. **Timestamps and time zones.** Instants are stored as aware UTC datetimes; IST (UTC+5:30) is used only for R1 (`date`, shift boundaries). The API exchanges epoch milliseconds (integers), truncated to whole seconds on ingress (R1); an omitted `punched_at` uses the server clock. `date` stays a zero-padded `YYYY-MM-DD` string, so range filters and the unique `(emp_code, date)` index are lexicographic comparisons.

9. **Validation and errors.** Pydantic enforces shapes the contract can express: `StrictInt` epoch window (~1973-2100), `HH:MM` patterns, presence-only `Literal` for punch-in, `reason` 5-200 chars, `page_size` <= 100. Rule violations raise explicit 422s; unknown employee/record 404; duplicates and lost races 409 via unique indexes and atomic find-and-update - never check-then-insert.

10. **Testing.** pytest + Starlette `TestClient` against a real MongoDB, on a dedicated `<MONGO_DB>_test` database dropped around the session. Sample-data tests assert exact contract-derived values; rule tests cover the boundaries (10-minute grace, 30-minute overtime minimum, half-day 4.50, overnight rollover, 92-day range); `test_explain_indexes.py` seeds ~20k records and asserts IXSCAN with no COLLSCAN for every explained query. Aggregation behavior is never mocked - it runs on the server.
