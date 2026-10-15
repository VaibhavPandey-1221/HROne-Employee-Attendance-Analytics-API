"""PATCH /attendance/{emp_code}/{date}: corrections, recomputation, audit trail, races."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.main import attendance_logs
from tests.conftest import create_employee, ist, punch_in, punch_out, raw_attendance_record


@pytest.fixture(scope="module")
def base_employee(client):
    create_employee(client, "EMP5301")
    return "EMP5301"


def patch(client, emp_code, date, **body):
    payload = {"reason": "test correction", "regularized_by": "tester"}
    payload.update(body)
    return client.patch(f"/attendance/{emp_code}/{date}", json=payload)


def test_patch_punch_in_recomputes_and_appends_history(client, base_employee):
    punch_in(client, "EMP5301", ist(2026, 1, 5, 9, 45))
    response = patch(client, "EMP5301", "2026-01-05", punch_in=ist(2026, 1, 5, 9, 28))
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["punch_in"] == ist(2026, 1, 5, 9, 28)
    assert record["late_minutes"] == 0
    assert record["work_hours"] is None  # still open
    assert len(record["history"]) == 1
    entry = record["history"][0]
    assert entry["by"] == "tester"
    assert entry["reason"] == "test correction"
    assert isinstance(entry["at"], int)
    assert entry["changes"] == {
        "punch_in": {"from": ist(2026, 1, 5, 9, 45), "to": ist(2026, 1, 5, 9, 28)},
        "late_minutes": {"from": 15, "to": 0},
    }


def test_patch_appends_never_overwrites(client, base_employee):
    response = patch(client, "EMP5301", "2026-01-05", punch_in=ist(2026, 1, 5, 9, 20))
    record = response.json()
    assert len(record["history"]) == 2
    # Only punch_in actually changed this time (both late values are 0).
    assert set(record["history"][1]["changes"].keys()) == {"punch_in"}
    assert record["history"][0]["changes"]["punch_in"]["to"] == ist(2026, 1, 5, 9, 28)


def test_patch_status_only_records_status_change(client, base_employee):
    response = patch(client, "EMP5301", "2026-01-05", status="WFH")
    record = response.json()
    assert record["status"] == "WFH"
    assert record["history"][-1]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}


def test_patch_no_effective_change_is_422(client, base_employee):
    response = patch(client, "EMP5301", "2026-01-05", status="WFH")
    assert response.status_code == 422


def test_patch_ignores_derived_fields_in_body(client, base_employee):
    response = patch(
        client,
        "EMP5301",
        "2026-01-05",
        punch_in=ist(2026, 1, 5, 9, 15),
        work_hours=1.23,       # read-only, must be ignored
        late_minutes=999,      # read-only, must be ignored
        half_day=True,         # read-only, must be ignored
    )
    assert response.status_code == 200
    record = response.json()
    assert record["late_minutes"] == 0
    assert record["work_hours"] is None
    assert record["half_day"] is False


def test_patch_punch_in_recomputes_closed_day(client):
    create_employee(client, "EMP5302")
    punch_in(client, "EMP5302", ist(2026, 1, 5, 9, 30))
    punch_out(client, "EMP5302", ist(2026, 1, 5, 18, 30))
    response = patch(client, "EMP5302", "2026-01-05", punch_in=ist(2026, 1, 5, 9, 28))
    record = response.json()
    assert record["work_hours"] == 9.03
    assert record["history"][0]["changes"] == {
        "punch_in": {"from": ist(2026, 1, 5, 9, 30), "to": ist(2026, 1, 5, 9, 28)},
        "work_hours": {"from": 9.0, "to": 9.03},
    }


def test_patch_to_absent_clears_punches(client):
    create_employee(client, "EMP5303")
    punch_in(client, "EMP5303", ist(2026, 1, 6, 9, 30))
    punch_out(client, "EMP5303", ist(2026, 1, 6, 18, 30))
    response = patch(client, "EMP5303", "2026-01-06", status="ABSENT")
    record = response.json()
    assert record["status"] == "ABSENT"
    assert record["punch_in"] is None and record["punch_out"] is None
    assert record["work_hours"] is None
    assert record["late_minutes"] == 0
    assert set(record["history"][0]["changes"].keys()) == {"status", "punch_in", "punch_out", "work_hours"}
    stored = attendance_logs.find_one({"emp_code": "EMP5303", "date": "2026-01-06"})
    assert stored["punch_in"] is None and stored["punch_out"] is None


def test_patch_absent_back_to_present(client):
    response = patch(client, "EMP5303", "2026-01-06", status="PRESENT", punch_in=ist(2026, 1, 6, 9, 30))
    record = response.json()
    assert record["status"] == "PRESENT"
    assert record["punch_in"] == ist(2026, 1, 6, 9, 30)
    assert set(record["history"][-1]["changes"].keys()) == {"status", "punch_in"}


def test_patch_absent_leave_rules(client):
    create_employee(client, "EMP5304")
    # Seeded back-office style record the API cannot create.
    attendance_logs.insert_one(raw_attendance_record(
        emp_code="EMP5304", date="2026-01-07", status="ABSENT",
        punch_in=None, punch_out=None, work_hours=None,
    ))
    # ABSENT + supplied punch -> 422
    assert patch(client, "EMP5304", "2026-01-07", punch_in=ist(2026, 1, 7, 9, 30)).status_code == 422
    assert patch(client, "EMP5304", "2026-01-07", status="LEAVE", punch_out=ist(2026, 1, 7, 18, 0)).status_code == 422
    # PRESENT without any punch_in (none stored) -> 422
    assert patch(client, "EMP5304", "2026-01-07", status="PRESENT").status_code == 422
    # LEAVE with no punches -> fine
    response = patch(client, "EMP5304", "2026-01-07", status="LEAVE")
    assert response.status_code == 200
    assert response.json()["status"] == "LEAVE"


def test_patch_punch_in_must_keep_attendance_date(client):
    create_employee(client, "EMP5305")
    attendance_logs.insert_one(raw_attendance_record(
        emp_code="EMP5305", date="2026-01-07",
        punch_in=None, punch_out=None, work_hours=None, status="PRESENT",
    ))
    # 10:00 IST on the 8th belongs to the 8th -> not this record's date
    assert patch(client, "EMP5305", "2026-01-07", punch_in=ist(2026, 1, 8, 10, 0)).status_code == 422


def test_patch_overnight_punch_in_previous_day_is_allowed(client):
    create_employee(client, "EMP5306", shift_start="22:00", shift_end="06:00")
    attendance_logs.insert_one(raw_attendance_record(
        emp_code="EMP5306", date="2026-01-07",
        punch_in=None, punch_out=None, work_hours=None, status="PRESENT",
    ))
    # 00:30 IST on Jan 8 belongs to the Jan 7 overnight shift (R1).
    response = patch(client, "EMP5306", "2026-01-07", punch_in=ist(2026, 1, 8, 0, 30))
    assert response.status_code == 200, response.text
    assert response.json()["late_minutes"] == 150


def test_patch_punch_out_ordering_rules(client):
    create_employee(client, "EMP5307")
    punch_in(client, "EMP5307", ist(2026, 1, 8, 9, 30))
    # punch_out before punch_in -> 422
    assert patch(client, "EMP5307", "2026-01-08", punch_out=ist(2026, 1, 8, 9, 0)).status_code == 422
    # more than 24h -> 422
    assert patch(client, "EMP5307", "2026-01-08", punch_out=ist(2026, 1, 9, 9, 30, 1)).status_code == 422
    # valid -> 200, derived fields computed from the final state
    response = patch(client, "EMP5307", "2026-01-08", punch_out=ist(2026, 1, 8, 19, 30))
    record = response.json()
    assert record["work_hours"] == 10.0
    assert record["overtime_minutes"] == 60
    # existing punch_out must stay valid when punch_in moves after it -> 422
    response = patch(client, "EMP5307", "2026-01-08", punch_in=ist(2026, 1, 8, 20, 0))
    assert response.status_code == 422


def test_patch_legacy_record_without_history_or_half_day(client):
    create_employee(client, "EMP5309")
    legacy = raw_attendance_record(
        emp_code="EMP5309", date="2026-01-09",
        punch_in=None, punch_out=None, work_hours=None,
        status="PRESENT",
    )
    legacy.pop("history")
    legacy.pop("half_day")
    attendance_logs.insert_one(legacy)
    response = patch(client, "EMP5309", "2026-01-09", punch_in=ist(2026, 1, 9, 9, 28))
    assert response.status_code == 200, response.text
    record = response.json()
    assert len(record["history"]) == 1
    assert record["half_day"] is False
    assert record["history"][0]["changes"]["punch_in"]["to"] == ist(2026, 1, 9, 9, 28)


def test_patch_errors(client):
    # unknown employee -> 404
    assert patch(client, "EMP5999", "2026-01-05", status="WFH").status_code == 404
    create_employee(client, "EMP5311")
    # known employee, no record for the date -> 404
    assert patch(client, "EMP5311", "2026-01-05", status="WFH").status_code == 404
    punch_in(client, "EMP5311", ist(2026, 1, 5, 9, 30))
    # invalid path date -> 422
    assert patch(client, "EMP5311", "2026-13-01", status="WFH").status_code == 422
    # reason too short / regularized_by missing -> 422
    response = client.patch("/attendance/EMP5311/2026-01-05", json={"status": "WFH", "reason": "abc", "regularized_by": "x"})
    assert response.status_code == 422
    response = client.patch("/attendance/EMP5311/2026-01-05", json={"status": "WFH", "reason": "abcdef"})
    assert response.status_code == 422
    # explicit null punch_in -> 422
    response = client.patch("/attendance/EMP5311/2026-01-05", json={"punch_in": None, "reason": "abcdef", "regularized_by": "x"})
    assert response.status_code == 422


def test_concurrent_patches_do_not_lose_history(client):
    create_employee(client, "EMP5310")
    punch_in(client, "EMP5310", ist(2026, 1, 10, 9, 30))

    times = [ist(2026, 1, 10, 9, 10), ist(2026, 1, 10, 9, 20)]

    def attempt(at):
        return patch(client, "EMP5310", "2026-01-10", punch_in=at).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(attempt, times))
    assert set(statuses) <= {200, 409}
    assert statuses.count(200) >= 1
    stored = attendance_logs.find_one({"emp_code": "EMP5310", "date": "2026-01-10"})
    # One history entry per successful correction: nothing is silently lost.
    assert len(stored["history"]) == statuses.count(200)
