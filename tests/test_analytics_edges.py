"""Analytics edge cases on an isolated department, month 2026-02:
ties, employees with no logs, weekend records, mid-month joiners, empty months.
"""
import pytest

from app.main import attendance_logs
from tests.conftest import create_employee, ist, punch_in, punch_out, raw_attendance_record


@pytest.fixture(scope="module", autouse=True)
def edge_data(client):
    """Six EdgeTest employees: two tie at 60 late minutes, one has no logs."""
    create_employee(client, "EMP6001", department="EdgeTest", joined_on="2026-01-15")
    create_employee(client, "EMP6002", department="EdgeTest", joined_on="2026-01-15")
    create_employee(client, "EMP6003", department="EdgeTest", joined_on="2026-01-15")
    create_employee(client, "EMP6004", department="EdgeTest", joined_on="2026-01-15")  # no logs
    create_employee(client, "EMP6005", department="EdgeTest", joined_on="2026-01-15")  # weekend only
    create_employee(client, "EMP6006", department="EdgeTest", joined_on="2026-01-15")

    # EMP6001: 30 + 30 late, plus one ON_DUTY day.
    punch_in(client, "EMP6001", ist(2026, 2, 2, 10, 0))
    punch_out(client, "EMP6001", ist(2026, 2, 2, 18, 0))
    punch_in(client, "EMP6001", ist(2026, 2, 3, 10, 0))
    punch_out(client, "EMP6001", ist(2026, 2, 3, 18, 0))
    punch_in(client, "EMP6001", ist(2026, 2, 4, 9, 30), status="ON_DUTY")
    punch_out(client, "EMP6001", ist(2026, 2, 4, 17, 30))
    # EMP6002: 60 minutes late in one record (ties with EMP6001 on the total).
    punch_in(client, "EMP6002", ist(2026, 2, 2, 10, 30))
    punch_out(client, "EMP6002", ist(2026, 2, 2, 18, 30))
    # EMP6003: 90 late minutes, the leader.
    punch_in(client, "EMP6003", ist(2026, 2, 3, 11, 0))
    punch_out(client, "EMP6003", ist(2026, 2, 3, 20, 0))
    # EMP6005: Saturday record with overtime (counts for overtime, not present_days).
    punch_in(client, "EMP6005", ist(2026, 2, 7, 9, 30))
    punch_out(client, "EMP6005", ist(2026, 2, 7, 19, 15))
    # EMP6006: one LEAVE day. The API cannot create ABSENT/LEAVE records and
    # PATCH requires an existing record (404 otherwise, see openapi.yaml), so
    # seed it directly, the way the grader seeds back-office records.
    attendance_logs.insert_one(raw_attendance_record(
        emp_code="EMP6006", date="2026-02-02", status="LEAVE",
        punch_in=None, punch_out=None, work_hours=None,
    ))

    # Trend department: two employees, second joins mid-range.
    create_employee(client, "EMP6007", department="EdgeTrend", joined_on="2026-02-01")
    create_employee(client, "EMP6008", department="EdgeTrend", joined_on="2026-02-20")
    # Department whose only employee joins *after* February.
    create_employee(client, "EMP6009", department="EmptyDept", joined_on="2026-03-01")


def test_leaderboard_ties_and_limit(client, edge_data):
    response = client.get("/analytics/leaderboard/late", params={"month": "2026-02", "department": "EdgeTest"})
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert items == [
        {"rank": 1, "emp_code": "EMP6003", "name": "Test EMP6003", "department": "EdgeTest",
         "total_late_minutes": 90, "late_count": 1},
        {"rank": 2, "emp_code": "EMP6001", "name": "Test EMP6001", "department": "EdgeTest",
         "total_late_minutes": 60, "late_count": 2},
        {"rank": 2, "emp_code": "EMP6002", "name": "Test EMP6002", "department": "EdgeTest",
         "total_late_minutes": 60, "late_count": 1},
    ]
    # limit=2 keeps every employee whose rank <= 2, so the tie row is included.
    response = client.get("/analytics/leaderboard/late", params={"month": "2026-02", "department": "EdgeTest", "limit": 2})
    assert [item["emp_code"] for item in response.json()["items"]] == ["EMP6003", "EMP6001", "EMP6002"]
    # limit=1 keeps only rank 1.
    response = client.get("/analytics/leaderboard/late", params={"month": "2026-02", "department": "EdgeTest", "limit": 1})
    assert [item["emp_code"] for item in response.json()["items"]] == ["EMP6003"]


def test_department_summary_edge_values(client, edge_data):
    response = client.get("/analytics/departments/summary", params={"month": "2026-02", "department": "EdgeTest"})
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0] == {
        "department": "EdgeTest",
        "headcount": 6,              # includes EMP6004, who has no logs at all
        "present_days": pytest.approx(5.0),  # 5 full weekday days; the Saturday does not count
        "avg_work_hours": pytest.approx(8.46),  # includes the weekend record's 9.75
        "late_count": 4,
        "total_late_minutes": 210,
        "leave_count": 1,
        "on_duty_count": 1,
    }


def test_weekend_record_contributes_to_totals_only(client, edge_data):
    response = client.get("/analytics/employees/EMP6005/monthly", params={"month": "2026-02"})
    body = response.json()
    assert body["present_days"] == 0.0        # weekend records are not present days
    assert body["total_overtime_minutes"] == 45
    assert body["late_count"] == 0
    assert body["attendance_pct"] == pytest.approx(0.0)


def test_employee_with_no_logs(client, edge_data):
    response = client.get("/analytics/employees/EMP6004/monthly", params={"month": "2026-02"})
    body = response.json()
    assert body == {
        "emp_code": "EMP6004", "month": "2026-02",
        "working_days": 20, "present_days": 0.0, "leave_days": 0, "late_count": 0,
        "total_late_minutes": 0, "total_overtime_minutes": 0, "attendance_pct": pytest.approx(0.0),
    }


def test_monthly_joined_after_month_end(client, edge_data):
    response = client.get("/analytics/employees/EMP6009/monthly", params={"month": "2026-02"})
    body = response.json()
    assert body["working_days"] == 0
    assert body["attendance_pct"] is None  # 0/0 -> null, not 0


def test_department_with_headcount_zero_is_omitted(client, edge_data):
    response = client.get("/analytics/departments/summary", params={"month": "2026-02"})
    departments = [item["department"] for item in response.json()["items"]]
    assert "EmptyDept" not in departments  # nobody has joined by the end of February
    response = client.get("/analytics/departments/summary", params={"month": "2026-03"})
    march = {item["department"]: item for item in response.json()["items"]}
    assert "EmptyDept" in march
    assert march["EmptyDept"]["headcount"] == 1
    assert march["EmptyDept"]["present_days"] == 0.0
    assert march["EmptyDept"]["avg_work_hours"] is None  # no qualifying records


def test_trend_mid_month_joiner_and_no_records(client, edge_data):
    response = client.get("/analytics/departments/EdgeTrend/trend", params={"from": "2026-02-16", "to": "2026-02-22"})
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 7
    assert [item["headcount"] for item in items] == [1, 1, 1, 1, 2, 2, 2]
    assert [item["present_count"] for item in items] == [0.0] * 7
    assert [item["is_working_day"] for item in items] == [True, True, True, True, True, False, False]
    assert [item["attendance_rate"] for item in items] == [0.0, 0.0, 0.0, 0.0, 0.0, None, None]
    assert [item["moving_avg_7d"] for item in items] == [0.0] * 7


def test_trend_single_day_range(client, edge_data):
    response = client.get("/analytics/departments/EdgeTrend/trend", params={"from": "2026-02-21", "to": "2026-02-21"})
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["is_working_day"] is False
    assert items[0]["attendance_rate"] is None
    assert items[0]["moving_avg_7d"] is None  # window holds no non-null rate
