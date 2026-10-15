"""Health endpoint, employee CRUD, validation, pagination and create races."""
from concurrent.futures import ThreadPoolExecutor

from tests.conftest import create_employee


def test_health_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_employee_defaults_and_shape(client):
    body = {
        "emp_code": "EMP5001",
        "name": "New Person",
        "email": "new.person@example.com",
        "department": "QA-Test",
        "joined_on": "2026-01-10",
    }
    response = client.post("/employees", json=body)
    assert response.status_code == 201, response.text
    created = response.json()
    assert created == {
        "emp_code": "EMP5001",
        "name": "New Person",
        "email": "new.person@example.com",
        "department": "QA-Test",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-10",
        "created_at": created["created_at"],
    }
    # created_at is epoch milliseconds: an integer, not a string / ISO date.
    assert isinstance(created["created_at"], int)
    assert 100_000_000_000 <= created["created_at"] <= 4_102_444_800_000
    # No internal id leaks into the response.
    assert "_id" not in created and "id" not in created


def test_create_employee_duplicate_is_409(client):
    create_employee(client, "EMP5002")
    response = client.post(
        "/employees",
        json={
            "emp_code": "EMP5002",
            "name": "Dup",
            "email": "dup@example.com",
            "department": "QA-Test",
            "joined_on": "2026-01-10",
        },
    )
    assert response.status_code == 409
    assert "detail" in response.json()


def test_create_employee_race_yields_one_201(client):
    payload = {
        "emp_code": "EMP5003",
        "name": "Racer",
        "email": "racer@example.com",
        "department": "QA-Test",
        "joined_on": "2026-01-10",
    }

    def attempt(_):
        return client.post("/employees", json=payload).status_code

    with ThreadPoolExecutor(max_workers=5) as pool:
        statuses = list(pool.map(attempt, range(5)))
    assert statuses.count(201) == 1
    assert statuses.count(409) == 4


def test_create_employee_validation(client):
    base = {
        "emp_code": "EMP5004",
        "name": "Val",
        "email": "val@example.com",
        "department": "QA-Test",
        "joined_on": "2026-01-10",
    }
    for patch in (
        {"emp_code": "NOPE1"},                      # wrong code format
        {"email": "not-an-email"},                  # email pattern
        {"shift_start": "25:00"},                   # shift pattern
        {"shift_end": "9:5"},                       # shift pattern
        {"joined_on": "2026-1-5"},                  # not a valid date
        {"joined_on": "not a date"},
        {"name": ""},                               # min length
        {"department": ""},                         # min length
    ):
        body = {**base, **patch}
        response = client.post("/employees", json=body)
        assert response.status_code == 422, f"{patch} -> {response.status_code}"
    # shift_start == shift_end is invalid per the contract.
    body = {**base, "shift_start": "09:30", "shift_end": "09:30"}
    assert client.post("/employees", json=body).status_code == 422


def test_list_employees_filter_sort_and_pagination(client):
    # EMP519x codes: the EMP510x range is owned by test_attendance_rules.py
    # (both modules share one session-scoped test database).
    for index in range(4):
        create_employee(client, f"EMP519{index}", department="QA-List")
    # Plus one in a different department that must not appear.
    create_employee(client, "EMP5199", department="QA-Other")

    response = client.get("/employees", params={"department": "QA-List", "page": 1, "page_size": 2})
    assert response.status_code == 200
    page = response.json()
    assert page["total"] == 4  # total reflects the department filter
    assert page["page"] == 1 and page["page_size"] == 2
    codes = [item["emp_code"] for item in page["items"]]
    assert codes == ["EMP5190", "EMP5191"]  # sorted ascending, page 1 skips nothing

    response = client.get("/employees", params={"department": "QA-List", "page": 2, "page_size": 2})
    codes = [item["emp_code"] for item in response.json()["items"]]
    assert codes == ["EMP5192", "EMP5193"]


def test_list_employees_param_validation(client):
    assert client.get("/employees", params={"page": 0}).status_code == 422
    assert client.get("/employees", params={"page_size": 101}).status_code == 422
    assert client.get("/employees", params={"page_size": 0}).status_code == 422
