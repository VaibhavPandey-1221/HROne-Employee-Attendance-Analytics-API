"""`/admin/explain` must show index usage (IXSCAN, no COLLSCAN) at a scale
where the plan is meaningful: this module seeds 20,000 synthetic attendance
records and 200 employees into the test database before calling explain.

Synthetic data lives in Jan-Apr 2026 and uses EMP9xxxxx codes so it cannot
interfere with the sample-data assertions of the other test modules.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.main import attendance_logs, employees, db
from tests.conftest import create_employee

BULK_DEPARTMENT = "BulkDept0"


@pytest.fixture(scope="module")
def bulk_data(client):
    employees.delete_many({"emp_code": {"$regex": "^EMP9"}})
    attendance_logs.delete_many({"emp_code": {"$regex": "^EMP9"}})

    employee_docs = []
    log_docs = []
    base_day = datetime(2026, 1, 5, tzinfo=timezone.utc)
    for i in range(200):
        code = f"EMP9{i:05d}"
        employee_docs.append(
            {
                "emp_code": code,
                "name": f"Bulk {i}",
                "email": f"bulk{i}@example.com",
                "department": f"BulkDept{i % 10}",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
                "created_at": base_day,
            }
        )
        for d in range(100):
            day = base_day.fromordinal(base_day.toordinal() + d).replace(tzinfo=timezone.utc)
            log_docs.append(
                {
                    "emp_code": code,
                    "date": day.date().isoformat(),
                    "status": "PRESENT",
                    "punch_in": day + timedelta(seconds=4 * 3600 + ((i + d) % 5) * 60),
                    "punch_out": day + timedelta(hours=13),
                    "work_hours": 9.0,
                    "late_minutes": (i + d) % 5,
                    "overtime_minutes": 0,
                    "half_day": False,
                    "history": [],
                }
            )
    employees.insert_many(employee_docs)
    for start in range(0, len(log_docs), 5000):
        attendance_logs.insert_many(log_docs[start : start + 5000])
    yield {"employees": len(employee_docs), "logs": len(log_docs)}
    employees.delete_many({"emp_code": {"$regex": "^EMP9"}})
    attendance_logs.delete_many({"emp_code": {"$regex": "^EMP9"}})


def collect_stages(node) -> list:
    """All `stage` values anywhere in an explain payload."""
    stages = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "stage" and isinstance(value, str):
                stages.append(value)
            else:
                stages.extend(collect_stages(value))
    elif isinstance(node, list):
        for item in node:
            stages.extend(collect_stages(item))
    return stages


def explain(client, endpoint, **params):
    response = client.get(f"/admin/explain/{endpoint}", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def assert_indexed(payload):
    stages = collect_stages(payload["explain"])
    assert "IXSCAN" in stages, f"{payload['endpoint']} did not use an index: {stages}"
    assert "COLLSCAN" not in stages, f"{payload['endpoint']} contains a collection scan: {stages}"


def test_explain_attendance_list(client, bulk_data):
    payload = explain(client, "attendance_list")
    assert payload["endpoint"] == "attendance_list"
    assert payload["collection"] == "attendance_logs"
    assert_indexed(payload)


def test_explain_employee_monthly(client, bulk_data):
    payload = explain(client, "employee_monthly", emp_code="EMP900001", month="2026-02")
    assert_indexed(payload)


def test_explain_department_summary(client, bulk_data):
    payload = explain(client, "department_summary", month="2026-02")
    assert_indexed(payload)


def test_explain_department_summary_filtered(client, bulk_data):
    payload = explain(client, "department_summary", month="2026-02", department=BULK_DEPARTMENT)
    assert_indexed(payload)


def test_explain_late_leaderboard(client, bulk_data):
    payload = explain(client, "late_leaderboard", month="2026-02")
    assert_indexed(payload)


def test_explain_department_trend(client, bulk_data):
    payload = explain(client, "department_trend", department=BULK_DEPARTMENT, **{"from": "2026-02-01", "to": "2026-03-31"})
    assert_indexed(payload)


def test_explain_missing_required_params(client, bulk_data):
    assert client.get("/admin/explain/employee_monthly").status_code == 422
    assert client.get("/admin/explain/employee_monthly", params={"emp_code": "EMP900001"}).status_code == 422
    assert client.get("/admin/explain/department_summary").status_code == 422
    assert client.get("/admin/explain/late_leaderboard").status_code == 422
    assert client.get("/admin/explain/department_trend").status_code == 422
    assert client.get("/admin/explain/department_trend", params={"department": BULK_DEPARTMENT}).status_code == 422


def test_explain_unknown_endpoint(client, bulk_data):
    assert client.get("/admin/explain/bogus").status_code == 422


def test_explain_bad_params(client, bulk_data):
    assert client.get("/admin/explain/employee_monthly", params={"emp_code": "EMP900001", "month": "2026-13"}).status_code == 422
    assert client.get("/admin/explain/department_trend", params={"department": BULK_DEPARTMENT, "from": "2026-03-01", "to": "2026-02-01"}).status_code == 422
