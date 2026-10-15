"""Analytics against the nine sample documents (exact, hand-computed expectations).

July 2026 has 23 Mon-Fri working days (1st is a Wednesday).
"""
import pytest


@pytest.fixture(scope="module", autouse=True)
def _samples(seeded):
    return seeded


def test_monthly_sample_employees(client, seeded):
    expected = {
        "EMP0001": {"working_days": 23, "present_days": 2.0, "leave_days": 0, "late_count": 0,
                    "total_late_minutes": 0, "total_overtime_minutes": 0, "attendance_pct": 8.70},
        "EMP0002": {"working_days": 15, "present_days": 1.0, "leave_days": 0, "late_count": 0,
                    "total_late_minutes": 0, "total_overtime_minutes": 0, "attendance_pct": 6.67},
        "EMP0003": {"working_days": 23, "present_days": 1.0, "leave_days": 1, "late_count": 1,
                    "total_late_minutes": 35, "total_overtime_minutes": 40, "attendance_pct": 4.35},
        "EMP0004": {"working_days": 23, "present_days": 0.5, "leave_days": 0, "late_count": 0,
                    "total_late_minutes": 0, "total_overtime_minutes": 0, "attendance_pct": 2.17},
        "EMP0005": {"working_days": 23, "present_days": 1.0, "leave_days": 0, "late_count": 0,
                    "total_late_minutes": 0, "total_overtime_minutes": 40, "attendance_pct": 4.35},
        "EMP0006": {"working_days": 23, "present_days": 1.0, "leave_days": 0, "late_count": 0,
                    "total_late_minutes": 0, "total_overtime_minutes": 0, "attendance_pct": 4.35},
    }
    for emp_code, expected_fields in expected.items():
        response = client.get(f"/analytics/employees/{emp_code}/monthly", params={"month": "2026-07"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["emp_code"] == emp_code
        assert body["month"] == "2026-07"
        for key, value in expected_fields.items():
            assert body[key] == pytest.approx(value), f"{emp_code}.{key}: {body[key]} != {value}"


def test_monthly_errors(client, seeded):
    assert client.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"}).status_code == 404
    assert client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-1"}).status_code == 422
    assert client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-13"}).status_code == 422
    assert client.get("/analytics/employees/EMP0001/monthly").status_code == 422  # month required


def test_department_summary_sample(client, seeded):
    response = client.get("/analytics/departments/summary", params={"month": "2026-07"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["month"] == "2026-07"
    items = {item["department"]: item for item in body["items"]}
    assert [item["department"] for item in body["items"]] == sorted(items)

    engineering = items["Engineering"]
    assert engineering == {
        "department": "Engineering", "headcount": 2, "present_days": pytest.approx(3.0),
        "avg_work_hours": pytest.approx(9.04), "late_count": 0, "total_late_minutes": 0,
        "leave_count": 0, "on_duty_count": 0,
    }
    sales = items["Sales"]
    assert sales == {
        "department": "Sales", "headcount": 2, "present_days": pytest.approx(1.5),
        "avg_work_hours": pytest.approx(6.29), "late_count": 1, "total_late_minutes": 35,
        "leave_count": 1, "on_duty_count": 0,
    }
    support = items["Support"]
    assert support == {
        "department": "Support", "headcount": 2, "present_days": pytest.approx(2.0),
        "avg_work_hours": pytest.approx(8.75), "late_count": 0, "total_late_minutes": 0,
        "leave_count": 0, "on_duty_count": 0,
    }
    # Departments with employees but no logs still appear with a headcount.
    assert all(item["headcount"] >= 1 for item in body["items"])

    # Department filter restricts the result.
    response = client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Sales"})
    filtered = response.json()["items"]
    assert [item["department"] for item in filtered] == ["Sales"]


def test_department_summary_errors(client, seeded):
    assert client.get("/analytics/departments/summary", params={"month": "2026-1"}).status_code == 422
    assert client.get("/analytics/departments/summary").status_code == 422


def test_leaderboard_sample(client, seeded):
    response = client.get("/analytics/leaderboard/late", params={"month": "2026-07"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["month"] == "2026-07"
    assert body["items"] == [
        {"rank": 1, "emp_code": "EMP0003", "name": "Neha Iyer", "department": "Sales",
         "total_late_minutes": 35, "late_count": 1}
    ]
    # Department filter with no late-comers yields an empty leaderboard.
    response = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": "Engineering"})
    assert response.json()["items"] == []
    # limit validation
    assert client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 0}).status_code == 422
    assert client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 51}).status_code == 422


def test_trend_support_sample(client, seeded):
    response = client.get("/analytics/departments/Support/trend", params={"from": "2026-07-01", "to": "2026-07-10"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["department"] == "Support"
    items = body["items"]
    assert len(items) == 10  # one row per calendar day, even without records

    expected_dates = [f"2026-07-{d:02d}" for d in range(1, 11)]
    assert [item["date"] for item in items] == expected_dates
    assert [item["is_working_day"] for item in items] == [True, True, True, False, False, True, True, True, True, True]
    assert [item["headcount"] for item in items] == [2] * 10
    assert [item["present_count"] for item in items] == [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 0.0]
    assert [item["late_count"] for item in items] == [0] * 10
    rates = [item["attendance_rate"] for item in items]
    assert rates[:5] == [0.0, 0.0, 0.0, None, None]
    assert rates[5:] == [0.5, 0.5, 0.0, 0.0, 0.0]
    moving = [item["moving_avg_7d"] for item in items]
    expected_moving = [0.0, 0.0, 0.0, 0.0, 0.0, 0.125, 0.2, 0.2, 0.2, 0.2]
    for got, want in zip(moving, expected_moving):
        assert got == pytest.approx(want, abs=1e-9), f"moving: {moving} vs {expected_moving}"


def test_trend_engineering_headcount_growth(client, seeded):
    response = client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-10", "to": "2026-07-16"})
    body = response.json()
    items = body["items"]
    assert len(items) == 7
    # EMP0002 joined 2026-07-13 (R9: counted from that day on).
    assert [item["headcount"] for item in items] == [1, 1, 1, 2, 2, 2, 2]
    assert [item["present_count"] for item in items] == [0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    assert items[0]["attendance_rate"] == 0.0
    assert items[1]["attendance_rate"] is None  # weekend
    assert items[3]["attendance_rate"] == 0.0  # working day, no records
    assert items[4]["attendance_rate"] == pytest.approx(0.5)
    moving = [item["moving_avg_7d"] for item in items]
    expected_moving = [0.0, 0.0, 0.0, 0.0, 0.1667, 0.125, 0.1]
    for got, want in zip(moving, expected_moving):
        assert got == pytest.approx(want, abs=1e-9), f"moving: {moving} vs {expected_moving}"


def test_trend_errors(client, seeded):
    assert client.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).status_code == 404
    assert client.get("/analytics/departments/Support/trend", params={"from": "2026-07-10", "to": "2026-07-01"}).status_code == 422
    assert client.get("/analytics/departments/Support/trend", params={"from": "2026-01-01", "to": "2026-04-10"}).status_code == 422  # > 92 days
    # 92 days exactly is allowed: 2026-01-01..2026-04-02.
    assert client.get("/analytics/departments/Support/trend", params={"from": "2026-01-01", "to": "2026-04-02"}).status_code == 200
    assert client.get("/analytics/departments/Support/trend").status_code == 422  # from/to required
