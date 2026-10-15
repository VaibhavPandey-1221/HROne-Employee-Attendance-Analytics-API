# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the HROne assignment: employee records, attendance punch-in/out with the R1–R10 business rules, four analytics endpoints (real aggregation pipelines), and an `/admin/explain` endpoint that shows the MongoDB plan for each query. All application code lives in the single file `app/main.py`.

## Requirements

- Python 3.11+ (developed on 3.14)
- MongoDB **6.0+** (Atlas M0 works; the app creates its indexes at startup, idempotently)

## Setup

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # then set MONGO_URI / MONGO_DB (URL-encode special characters in the password)
python sample_seed.py                                 # optional: loads the nine sample documents
uvicorn app.main:app --port 8000
```

Interactive docs: `http://localhost:8000/docs`. `GET /health` returns 200 when MongoDB answers a ping, 503 otherwise.

## Endpoints

`POST/GET /employees` · `POST /attendance/punch-in` · `POST /attendance/punch-out` · `GET /attendance` · `PATCH /attendance/{emp_code}/{date}` · `GET /analytics/employees/{emp_code}/monthly` · `GET /analytics/departments/summary` · `GET /analytics/leaderboard/late` · `GET /analytics/departments/{department}/trend` · `GET /admin/explain/{endpoint}`

Notes: instants in requests/responses are epoch milliseconds (integers); a record's `date` is the IST calendar date of `punch_in` (overnight shifts roll back before the shift end); instants are truncated to whole seconds; all derived fields (late, overtime, work hours, half-day) are recomputed server-side — clients may send them but they are ignored. `PATCH` appends exactly one `history` entry per real change and returns 409 for concurrent edits.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests
```

The suite runs against a dedicated test database (`<MONGO_DB>_test`) and never touches your main database. It needs a reachable MongoDB; `tests/test_explain_indexes.py` seeds ~20k records and asserts that every `/admin/explain` payload uses an index (IXSCAN) with no collection scan.

## Assumptions & limitations

- No authentication/authorization is specified or implemented; `regularized_by` is free-text audit information.
- Application code lives in the single `app/main.py` (assignment layout) and uses synchronous PyMongo - FastAPI runs these sync endpoints in its worker thread pool.
- Tests need a reachable MongoDB 6.0+; `tests/test_explain_indexes.py` briefly seeds ~20k records into `<MONGO_DB>_test` and cleans up after the session.
- `/admin/explain` returns the raw server explain document; exact plan shapes vary by MongoDB version, so the tests assert IXSCAN / no COLLSCAN rather than a fixed plan.

## Notes for reviewers

- `REVIEW.md` lists the starter defects and fixes; `DECISIONS.md` covers the indexes, the punch-in race, tie ranking, zero-log headcount, and scaling.
- Indexes created at startup: `employees(emp_code unique)`, `employees(department, joined_on)`, `employees(joined_on)`, `attendance_logs(emp_code, date unique)`, `attendance_logs(date desc, emp_code)`, `attendance_logs(emp_code, punch_in desc)`.
- No secrets are committed: `.env` is git-ignored; `.env.example` holds placeholders only.
