"""Test bootstrap.

The suite runs against a *dedicated test database*: MONGO_DB + "_test".
It never touches the database named by MONGO_DB itself.
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from bson import json_util
from dotenv import dotenv_values
from starlette.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]

# --- Environment: real env vars win; otherwise fall back to the local .env ---
_env = dotenv_values(REPO_ROOT / ".env")
os.environ.setdefault("MONGO_URI", _env.get("MONGO_URI") or "mongodb://localhost:27017")
_base_db = os.environ.get("MONGO_DB") or _env.get("MONGO_DB") or "attendance_db"
os.environ["MONGO_DB"] = f"{_base_db}_test"

import app.main as app_module  # noqa: E402  (must come after env setup)
from app.main import app, db  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
SAMPLE_DATA = REPO_ROOT / "sample_data"


@pytest.fixture(scope="session")
def client():
    """TestClient with lifespan: creates the indexes on the empty test DB."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session", autouse=True)
def _clean_test_database():
    db.employees.drop()
    db.attendance_logs.drop()
    yield
    db.employees.drop()
    db.attendance_logs.drop()


@pytest.fixture(scope="session")
def seeded(client):
    """Loads the nine sample documents (sample_data/) into the test database."""
    options = json_util.JSONOptions(tz_aware=True)
    docs = {
        name: json_util.loads((SAMPLE_DATA / f"{name}.json").read_text(), json_options=options)
        for name in ("employees", "attendance_logs")
    }
    db.employees.insert_many(docs["employees"])
    db.attendance_logs.insert_many(docs["attendance_logs"])
    return docs


# --------------------------------------------------------------------------- #
# Helpers shared by test modules                                               #
# --------------------------------------------------------------------------- #
def ist(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> int:
    """Epoch milliseconds for an IST wall-clock time."""
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp() * 1000)


def create_employee(
    client: TestClient,
    emp_code: str,
    department: str = "QA-Test",
    shift_start: str = "09:30",
    shift_end: str = "18:30",
    joined_on: str = "2026-01-01",
) -> dict:
    response = client.post(
        "/employees",
        json={
            "emp_code": emp_code,
            "name": f"Test {emp_code}",
            "email": f"{emp_code.lower()}@example.com",
            "department": department,
            "shift_start": shift_start,
            "shift_end": shift_end,
            "joined_on": joined_on,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def punch_in(client: TestClient, emp_code: str, at_ms: int, status: str = "PRESENT") -> dict:
    response = client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": at_ms, "status": status})
    assert response.status_code == 201, response.text
    return response.json()


def punch_out(client: TestClient, emp_code: str, at_ms: int) -> dict:
    response = client.post("/attendance/punch-out", json={"emp_code": emp_code, "punched_at": at_ms})
    assert response.status_code == 200, response.text
    return response.json()


def raw_attendance_record(**overrides) -> dict:
    """A stored attendance document, for cases the API cannot create (legacy/ABSENT seeds)."""
    doc = {
        "emp_code": "EMP0000",
        "date": "2026-01-05",
        "status": "PRESENT",
        "punch_in": datetime(2026, 1, 5, 4, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 1, 5, 13, 0, tzinfo=timezone.utc),
        "work_hours": 9.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    doc.update(overrides)
    return doc
