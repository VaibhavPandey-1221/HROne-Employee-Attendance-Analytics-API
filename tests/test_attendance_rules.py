"""Punch-in / punch-out: R1-R5, validation, races, overnight shifts."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone

import pytest

from app.main import attendance_logs, db
from tests.conftest import create_employee, ist, punch_in, punch_out


@pytest.fixture(scope="module")
def employees_ready(client):
    normal = create_employee(client, "EMP5101")  # 09:30 - 18:30
    overnight = create_employee(client, "EMP5102", shift_start="22:00", shift_end="06:00")
    return normal, overnight


def response_record(response):
    assert response.status_code in (200, 201), response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Punch-in: R1 (date, truncation), R2 (late)                                   #
# --------------------------------------------------------------------------- #
def test_punch_in_late_boundaries(client, employees_ready):
    # 09:40:00 -> on the grace boundary -> 0 late (R2 example)
    record = response_record(client.post("/attendance/punch-in", json={"emp_code": "EMP5101", "punched_at": ist(2026, 1, 5, 9, 40, 0)}))
    assert record["late_minutes"] == 0
    # 09:40:01 -> strictly more than 10 minutes -> late 10 (not 1)
    record = response_record(client.post("/attendance/punch-in", json={"emp_code": "EMP5101", "punched_at": ist(2026, 1, 6, 9, 40, 1)}))
    assert record["late_minutes"] == 10
    # 10:15:59 -> floor of 45.98 minutes = 45
    record = response_record(client.post("/attendance/punch-in", json={"emp_code": "EMP5101", "punched_at": ist(2026, 1, 7, 10, 15, 59)}))
    assert record["late_minutes"] == 45


def test_punch_in_truncates_to_whole_seconds(client, employees_ready):
    at = ist(2026, 1, 8, 9, 40, 0)
    record = response_record(client.post("/attendance/punch-in", json={"emp_code": "EMP5101", "punched_at": at + 900}))
    # 09:40:00.900 is truncated to 09:40:00, which is inside the grace period.
    assert record["punch_in"] == at
    assert record["late_minutes"] == 0
    stored = attendance_logs.find_one({"emp_code": "EMP5101", "date": "2026-01-08"})
    assert int(stored["punch_in"].replace(tzinfo=timezone.utc).timestamp() * 1000) == at
    assert stored["punch_in"].microsecond == 0


def test_punch_in_uses_ist_calendar_date(client, employees_ready):
    # 00:20 IST on Jan 10 is still Jan 9 in UTC: the record must be dated 2026-01-10.
    record = response_record(client.post("/attendance/punch-in", json={"emp_code": "EMP5101", "punched_at": ist(2026, 1, 10, 0, 20)}))
    assert record["date"] == "2026-01-10"


def test_punch_in_overnight_shift_after_midnight(client, employees_ready):
    # 00:30 IST belongs to the shift that started at 22:00 the previous day (R1).
    record = response_record(client.post("/attendance/punch-in", json={"emp_code": "EMP5102", "punched_at": ist(2026, 1, 9, 0, 30)}))
    assert record["date"] == "2026-01-08"
    assert record["late_minutes"] == 150  # 2h30 after the 22:00 shift start


def test_punch_in_new_record_shape(client, employees_ready):
    record = response_record(client.post("/attendance/punch-in", json={"emp_code": "EMP5101", "punched_at": ist(2026, 1, 11, 9, 0)}))
    assert record["emp_code"] == "EMP5101"
    assert record["status"] == "PRESENT"
    assert record["punch_out"] is None
    assert record["work_hours"] is None
    assert record["overtime_minutes"] == 0
    assert record["half_day"] is False
    assert record["history"] == []
    assert isinstance(record["punch_in"], int)
    assert "id" not in record and "_id" not in record


def test_punch_in_wfh_status(client, employees_ready):
    record = response_record(client.post("/attendance/punch-in", json={"emp_code": "EMP5101", "punched_at": ist(2026, 1, 12, 9, 0), "status": "WFH"}))
    assert record["status"] == "WFH"


def test_punch_in_errors(client, employees_ready):
    # unknown employee -> 404
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP5999", "punched_at": ist(2026, 1, 5, 9, 0)}).status_code == 404
    # duplicate punch-in for the same date -> 409
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP5101", "punched_at": ist(2026, 1, 5, 9, 50)}).status_code == 409
    # statuses the punch-in endpoint must not accept -> 422
    for status in ("ABSENT", "LEAVE", "BOGUS"):
        response = client.post("/attendance/punch-in", json={"emp_code": "EMP5101", "punched_at": ist(2026, 1, 13, 9, 0), "status": status})
        assert response.status_code == 422, status
    # bad punched_at values -> 422
    for bad in (1783312500, 1783312500000.5, "1783312500000", 999, 4_200_000_000_000):
        response = client.post("/attendance/punch-in", json={"emp_code": "EMP5101", "punched_at": bad})
        assert response.status_code == 422, bad


def test_punch_in_race_yields_one_201(client, employees_ready):
    payload = {"emp_code": "EMP5101", "punched_at": ist(2026, 1, 14, 10, 0)}

    def attempt(_):
        return client.post("/attendance/punch-in", json=payload).status_code

    with ThreadPoolExecutor(max_workers=5) as pool:
        statuses = list(pool.map(attempt, range(5)))
    assert statuses.count(201) == 1
    assert statuses.count(409) == 4


# --------------------------------------------------------------------------- #
# Punch-out: R3-R5, overnight, validation, races                               #
# --------------------------------------------------------------------------- #
def test_punch_out_normal_day(client, employees_ready):
    punch_in(client, "EMP5101", ist(2026, 1, 15, 9, 28))
    record = punch_out(client, "EMP5101", ist(2026, 1, 15, 18, 35))
    assert record["work_hours"] == 9.12
    assert record["late_minutes"] == 0
    assert record["overtime_minutes"] == 0  # 5 minutes is below the 30-minute minimum
    assert record["half_day"] is False


def test_punch_out_late_and_overtime(client, employees_ready):
    punch_in(client, "EMP5101", ist(2026, 1, 16, 10, 5))
    record = punch_out(client, "EMP5101", ist(2026, 1, 16, 19, 10))
    assert record["late_minutes"] == 35
    assert record["work_hours"] == 9.08
    assert record["overtime_minutes"] == 40


def test_punch_out_half_day_rules(client, employees_ready):
    punch_in(client, "EMP5101", ist(2026, 1, 17, 9, 30))
    record = punch_out(client, "EMP5101", ist(2026, 1, 17, 13, 0))
    assert record["work_hours"] == 3.5
    assert record["half_day"] is True  # 3.5 < 4.50

    punch_in(client, "EMP5101", ist(2026, 1, 18, 9, 30))
    record = punch_out(client, "EMP5101", ist(2026, 1, 18, 14, 0))
    assert record["work_hours"] == 4.5
    assert record["half_day"] is False  # exactly 4.50 is not a half day


def test_punch_out_rounds_half_up(client, employees_ready):
    # 7218 seconds = 2.005 hours exactly; half-up must give 2.01 (banker's would give 2.0).
    punch_in(client, "EMP5101", ist(2026, 1, 19, 9, 0, 0))
    record = punch_out(client, "EMP5101", ist(2026, 1, 19, 11, 0, 18))
    assert record["work_hours"] == 2.01


def test_punch_out_overtime_minimum(client, employees_ready):
    punch_in(client, "EMP5101", ist(2026, 1, 20, 9, 30))
    record = punch_out(client, "EMP5101", ist(2026, 1, 20, 18, 59))
    assert record["overtime_minutes"] == 0  # 29 minutes -> below 30

    punch_in(client, "EMP5101", ist(2026, 1, 21, 9, 30))
    record = punch_out(client, "EMP5101", ist(2026, 1, 21, 19, 0))
    assert record["overtime_minutes"] == 30  # exactly 30 -> counted

    punch_in(client, "EMP5101", ist(2026, 1, 22, 9, 30))
    record = punch_out(client, "EMP5101", ist(2026, 1, 22, 17, 0))
    assert record["overtime_minutes"] == 0  # left before shift end


def test_punch_out_overnight_across_midnight(client, employees_ready):
    punch_in(client, "EMP5102", ist(2026, 1, 7, 21, 55))
    record = punch_out(client, "EMP5102", ist(2026, 1, 8, 6, 40))
    assert record["date"] == "2026-01-07"  # the day the shift started
    assert record["work_hours"] == 8.75
    assert record["overtime_minutes"] == 40  # 06:00 -> 06:40 the next morning


def test_punch_out_24_hour_window(client, employees_ready):
    punch_in(client, "EMP5101", ist(2026, 1, 23, 9, 28))
    # Exactly 24 hours is allowed; more than 24 hours is 422.
    reply = client.post("/attendance/punch-out", json={"emp_code": "EMP5101", "punched_at": ist(2026, 1, 24, 9, 28, 1)})
    assert reply.status_code == 422
    record = punch_out(client, "EMP5101", ist(2026, 1, 24, 9, 28))
    assert record["work_hours"] == 24.0


def test_punch_out_errors(client, employees_ready):
    # unknown employee -> 404
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP5999", "punched_at": ist(2026, 1, 5, 18, 0)}).status_code == 404
    # no punch-in at all -> 404
    create_employee(client, "EMP5202")
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP5202", "punched_at": ist(2026, 1, 5, 18, 0)}).status_code == 404
    # already punched out -> 409
    create_employee(client, "EMP5203")
    punch_in(client, "EMP5203", ist(2026, 1, 5, 9, 30))
    punch_out(client, "EMP5203", ist(2026, 1, 5, 18, 0))
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP5203", "punched_at": ist(2026, 1, 5, 18, 30)}).status_code == 409
    # punched_at equal to punch_in -> 422
    create_employee(client, "EMP5206")
    punch_in(client, "EMP5206", ist(2026, 1, 6, 9, 30))
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP5206", "punched_at": ist(2026, 1, 6, 9, 30)}).status_code == 422


def test_punch_out_race_yields_one_200(client, employees_ready):
    create_employee(client, "EMP5204")
    punch_in(client, "EMP5204", ist(2026, 1, 6, 9, 30))
    payload = {"emp_code": "EMP5204", "punched_at": ist(2026, 1, 6, 18, 0)}

    def attempt(_):
        return client.post("/attendance/punch-out", json=payload).status_code

    with ThreadPoolExecutor(max_workers=5) as pool:
        statuses = list(pool.map(attempt, range(5)))
    assert statuses.count(200) == 1
    assert statuses.count(409) == 4
