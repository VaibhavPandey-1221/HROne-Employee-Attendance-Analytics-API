"""
Employee Attendance & Analytics API
===================================

FastAPI + MongoDB service implementing the HROne engineering assignment
contract (see openapi.yaml and DATA_MODEL.md in the repository root).

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real
      environment variables always take priority).

All application code lives in this single file, as required by the assignment.
"""
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import date as date_cls, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, StringConstraints, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override real environment variables

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000, tz_aware=True)
db = client[MONGO_DB]
employees = db["employees"]
attendance_logs = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")
DAY_SECONDS = 24 * 60 * 60

Millis = Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]


# --------------------------------------------------------------------------- #
# Startup: indexes (idempotent)                                                #
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    employees.create_index("emp_code", unique=True, name="uniq_emp_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")
    employees.create_index("joined_on", name="joined_on")
    attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uniq_emp_date"
    )
    attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
    attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punchin")


@asynccontextmanager
async def lifespan(_: FastAPI):
    last_error: Optional[Exception] = None
    for _attempt in range(10):
        try:
            ensure_indexes()
            last_error = None
            break
        except PyMongoError as exc:  # database not reachable yet; retry briefly
            last_error = exc
            time.sleep(1)
    if last_error is not None:
        print(f"[startup] WARNING: could not create indexes ({type(last_error).__name__}); "
              f"/health will report the database as down until it is reachable.")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time / conversion helpers                                                    #
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    """Current UTC time, truncated to whole seconds (R1)."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    seconds, _remainder_ms = divmod(ms, 1000)
    return EPOCH + timedelta(seconds=seconds)


def dt_to_ms(value: Optional[datetime]) -> Optional[int]:
    """Aware (or naive-UTC) datetime -> epoch milliseconds, or None."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = value - EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def round_half_up(value: float, digits: int) -> float:
    """Round a float half-up (R8), avoiding Python's banker's rounding."""
    quantum = Decimal(1).scaleb(-digits)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Shift / attendance-date helpers (R1)                                         #
# --------------------------------------------------------------------------- #
def parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def shift_instant(date_str: str, hhmm: str, plus_days: int = 0) -> datetime:
    """The UTC instant of a shift boundary (HH:MM, IST) on a given IST calendar date."""
    hour, minute = parse_hhmm(hhmm)
    day = date_cls.fromisoformat(date_str)
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)
    return (local + timedelta(days=plus_days)).astimezone(timezone.utc)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; overnight shifts roll back before shift_end."""
    local = punch_in.astimezone(IST)
    if is_overnight(shift_start, shift_end):
        end_hour, end_minute = parse_hhmm(shift_end)
        if (local.hour, local.minute, local.second) < (end_hour, end_minute, 0):
            local -= timedelta(days=1)
    return local.date().isoformat()


# --------------------------------------------------------------------------- #
# Derived-field helpers (R2 - R5)                                              #
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str, record_date: str) -> int:
    """R2: grace period 10 minutes; floor of whole minutes since shift_start when late."""
    elapsed_seconds = int((punch_in - shift_instant(record_date, shift_start)).total_seconds())
    return elapsed_seconds // 60 if elapsed_seconds > 10 * 60 else 0


def compute_punch_out_fields(
    punch_in: datetime, punch_out: datetime, shift_start: str, shift_end: str, record_date: str
) -> tuple[float, int, bool]:
    """R3-R5: (work_hours rounded half-up to 2dp, overtime_minutes, half_day)."""
    end_date_offset = 1 if is_overnight(shift_start, shift_end) else 0
    end_instant = shift_instant(record_date, shift_end, plus_days=end_date_offset)
    overtime_seconds = int((punch_out - end_instant).total_seconds())
    overtime = overtime_seconds // 60 if overtime_seconds >= 30 * 60 else 0

    worked_seconds = int((punch_out - punch_in).total_seconds())
    work_hours = (Decimal(worked_seconds) / Decimal(3600)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    half_day = work_hours < Decimal("4.50")
    return float(work_hours), overtime, half_day


# --------------------------------------------------------------------------- #
# API serialization                                                            #
# --------------------------------------------------------------------------- #
def employee_to_api(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def _change_value(field: str, value: Any) -> Any:
    if field in ("punch_in", "punch_out"):
        return dt_to_ms(value)
    return value


def record_to_api(doc: dict) -> dict:
    """Attendance document -> contract response (epoch ms instants, no _id)."""
    history = []
    for entry in doc.get("history") or []:
        changes = {
            field: {"from": _change_value(field, pair.get("from")), "to": _change_value(field, pair.get("to"))}
            for field, pair in (entry.get("changes") or {}).items()
        }
        history.append(
            {
                "at": dt_to_ms(entry.get("at")),
                "by": entry.get("by"),
                "reason": entry.get("reason"),
                "changes": changes,
            }
        )
    work_hours = doc.get("work_hours")
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": float(work_hours) if work_hours is not None else None,
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Request models                                                               #
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: Annotated[str, StringConstraints(pattern=r"^EMP\d{4,6}$")]
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_is_a_date(cls, value: str) -> str:
        try:
            date_cls.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        return value


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[Millis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[Millis] = None


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[Millis] = None
    punch_out: Optional[Millis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System                                                                       #
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB is not reachable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees                                                                    #
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start must differ from shift_end")
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": now_utc(),
    }
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        # Enforced by the unique index on emp_code, so concurrent duplicates
        # yield exactly one 201 and the rest 409.
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return employee_to_api(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = employees.count_documents(query)
    cursor = (
        employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_to_api(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance: punch-in / punch-out                                             #
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    punched_at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    record_date = attendance_date_for(punched_at, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": record_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, emp["shift_start"], record_date),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        # Unique index on (emp_code, date) makes the duplicate-check atomic:
        # of simultaneous identical punch-ins exactly one gets 201, rest 409.
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return record_to_api(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    punched_at = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    record = attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="this record is already punched out")

    punch_in_time = record["punch_in"]
    if punched_at <= punch_in_time:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")
    if (punched_at - punch_in_time).total_seconds() > DAY_SECONDS:
        raise HTTPException(status_code=422, detail="punched_at must be within 24 hours of punch_in")

    work_hours, overtime, half_day = compute_punch_out_fields(
        punch_in_time, punched_at, emp["shift_start"], emp["shift_end"], record["date"]
    )
    updated = attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        # Lost a race against a simultaneous punch-out.
        raise HTTPException(status_code=409, detail="this record is already punched out")
    return record_to_api(updated)


# --------------------------------------------------------------------------- #
# Attendance: list                                                             #
# --------------------------------------------------------------------------- #
def build_attendance_list_query(
    emp_code: Optional[str],
    date_from: Optional[date_cls],
    date_to: Optional[date_cls],
    status: Optional[str],
    page: int,
    page_size: int,
):
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    date_range: dict = {}
    if date_from is not None:
        date_range["$gte"] = date_from.isoformat()
    if date_to is not None:
        date_range["$lte"] = date_to.isoformat()
    if date_range:
        query["date"] = date_range
    if status is not None:
        query["status"] = status
    sort = {"date": DESCENDING, "emp_code": ASCENDING}
    return query, sort, (page - 1) * page_size, page_size


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date_cls] = None,
    date_to: Optional[date_cls] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query, sort, skip, limit = build_attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
    total = attendance_logs.count_documents(query)
    cursor = (
        attendance_logs.find(query)
        .sort(list(sort.items()))
        .skip(skip)
        .limit(limit)
    )
    return {
        "items": [record_to_api(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance: regularize (manual correction + audit trail)                     #
# --------------------------------------------------------------------------- #
HISTORY_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: date_cls, body: RegularizeIn):
    for field_name in ("status", "punch_in", "punch_out"):
        if field_name in body.model_fields_set and getattr(body, field_name) is None:
            raise HTTPException(status_code=422, detail=f"{field_name} cannot be null")

    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    record = attendance_logs.find_one({"emp_code": emp_code, "date": date.isoformat()})
    if record is None:
        raise HTTPException(status_code=404, detail="no attendance record for that date")

    # Effective current values (legacy documents may omit some fields).
    current = {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": int(record.get("late_minutes") or 0),
        "overtime_minutes": int(record.get("overtime_minutes") or 0),
        "half_day": bool(record.get("half_day", False)),
    }

    supplied_punch_in = "punch_in" in body.model_fields_set
    supplied_punch_out = "punch_out" in body.model_fields_set
    new_status = body.status if "status" in body.model_fields_set else current["status"]
    new_punch_in = ms_to_dt(body.punch_in) if supplied_punch_in else current["punch_in"]
    new_punch_out = ms_to_dt(body.punch_out) if supplied_punch_out else current["punch_out"]

    if new_status in ("ABSENT", "LEAVE"):
        if supplied_punch_in or supplied_punch_out:
            raise HTTPException(status_code=422, detail=f"status {new_status} cannot have punch times")
        new_punch_in = None
        new_punch_out = None
    else:
        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="a presence status requires punch_in")
        if attendance_date_for(new_punch_in, emp["shift_start"], emp["shift_end"]) != record["date"]:
            raise HTTPException(
                status_code=422, detail="punch_in must fall on the record's attendance date (R1)"
            )
        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > DAY_SECONDS:
                raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    # Recompute derived fields from the final state (R2-R5).
    if new_status in ("ABSENT", "LEAVE"):
        new_work, new_late, new_overtime, new_half_day = None, 0, 0, False
    else:
        new_late = compute_late_minutes(new_punch_in, emp["shift_start"], record["date"])
        if new_punch_out is None:
            new_work, new_overtime, new_half_day = None, 0, False
        else:
            new_work, new_overtime, new_half_day = compute_punch_out_fields(
                new_punch_in, new_punch_out, emp["shift_start"], emp["shift_end"], record["date"]
            )

    updated = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work,
        "late_minutes": new_late,
        "overtime_minutes": new_overtime,
        "half_day": new_half_day,
    }

    changes = {}
    for field_name in HISTORY_FIELDS:
        if current[field_name] != updated[field_name]:
            changes[field_name] = {"from": current[field_name], "to": updated[field_name]}
    if not changes:
        raise HTTPException(status_code=422, detail="the request does not change anything")

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency guard: every successful PATCH appends exactly one
    # history entry and writes the values it read, so matching the previous
    # state makes concurrent edits explicit (the loser gets 409) instead of
    # silently losing history entries or overwriting fresh punch-outs.
    guard: dict = {
        "emp_code": emp_code,
        "date": record["date"],
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "late_minutes": record.get("late_minutes"),
    }
    if "history" in record:
        guard["history"] = record["history"]
    else:
        guard["history"] = {"$exists": False}

    result = attendance_logs.find_one_and_update(
        guard,
        {"$set": {key: value for key, value in updated.items() if key in changes},
         "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if result is None:
        raise HTTPException(status_code=409, detail="the record was modified concurrently; retry")
    return record_to_api(result)


# --------------------------------------------------------------------------- #
# Analytics: shared pipeline pieces (aggregations run in MongoDB)              #
# --------------------------------------------------------------------------- #
STAT_KEYS = (
    "present2",
    "leave_count",
    "on_duty_count",
    "late_count",
    "total_late_minutes",
    "total_overtime_minutes",
    "work_hours_sum",
    "work_hours_count",
)


def _log_stats_accumulators() -> dict:
    """Per-record accumulators shared by the employee and department summaries."""
    is_present = {"$in": ["$status", list(PRESENCE_STATUSES)]}
    is_weekday = {"$not": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [1, 7]]}}
    has_work_hours = {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}
    return {
        # present2 counts half days as 1 and full days as 2 (divide by 2 for details).
        "present2": {
            "$sum": {
                "$cond": [
                    {"$and": [is_present, is_weekday]},
                    {"$cond": [{"$eq": ["$half_day", True]}, 1, 2]},
                    0,
                ]
            }
        },
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "work_hours_sum": {
            "$sum": {"$cond": [{"$and": [is_present, has_work_hours]}, {"$ifNull": ["$work_hours", 0]}, 0]}
        },
        "work_hours_count": {"$sum": {"$cond": [{"$and": [is_present, has_work_hours]}, 1, 0]}},
    }


def month_bounds(month: str) -> tuple[str, str]:
    """String range that covers every date of the month (dates are YYYY-MM-DD strings)."""
    return f"{month}-01", f"{month}-31"


def working_days_in_month(joined_on: str, month: str) -> int:
    """R7: Mon-Fri days between max(joined_on, month start) and month end."""
    year, mon = int(month[:4]), int(month[5:7])
    first = date_cls(year, mon, 1)
    last = date_cls(year + (mon == 12), 1 if mon == 12 else mon + 1, 1) - timedelta(days=1)
    day = max(first, date_cls.fromisoformat(joined_on))
    count = 0
    while day <= last:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    start, end = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": None, **_log_stats_accumulators()}},
    ]


def build_department_summary_pipeline(month: str, department: Optional[str]) -> list:
    start, end = month_bounds(month)
    employee_match: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        employee_match["department"] = department
    return [
        # Logs of the month, summarised per employee...
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": "$emp_code", **_log_stats_accumulators()}},
        {"$project": {"_id": 0, "emp_code": "$_id", **{key: 1 for key in STAT_KEYS}}},
        # ...unioned with every employee who has joined by month end, so people
        # with zero logs still contribute to headcount.
        {"$unionWith": {
            "coll": "employees",
            "pipeline": [
                {"$match": employee_match},
                {"$project": {"_id": 0, "emp_code": 1, "department": 1}},
            ],
        }},
        {"$group": {
            "_id": "$emp_code",
            "department": {"$max": "$department"},
            **{key: {"$sum": f"${key}"} for key in STAT_KEYS},
        }},
        # Keep only real employees: drops log-only rows (unknown emp_code) and
        # everyone who joined after the month end.
        {"$match": {"department": {"$type": "string"}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
                    **{key: {"$sum": f"${key}"} for key in STAT_KEYS}}},
        {"$sort": {"_id": 1}},
    ]


def build_late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    start, end = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs whose employee does not exist are ignored
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # Standard competition ranking over the totals only: ties share a rank
        # and the next rank is skipped (1, 2, 2, 4). limit applies after ranking.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> list:
    first = date_cls.fromisoformat(from_date)
    last = date_cls.fromisoformat(to_date)
    from_dt = datetime(first.year, first.month, first.day, tzinfo=timezone.utc)
    to_dt = datetime(last.year, last.month, last.day, tzinfo=timezone.utc)
    # $densify's explicit upper bound is exclusive (all supported MongoDB
    # versions, 6.0 through at least 8.0), so densify to the start of the day
    # *after* `to`. That yields exactly one row per calendar day, `to`
    # included, even when the last day has no records; it also keeps a
    # single-day range non-empty on 8.0+, where equal bounds are an empty set.
    to_exclusive = to_dt + timedelta(days=1)
    is_present = {"$in": ["$status", list(PRESENCE_STATUSES)]}
    return [
        # Only the department's logs: join each record to its employee.
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp",
                     "pipeline": [{"$match": {"department": department}},
                                  {"$project": {"_id": 0, "emp_code": 1}}]}},
        {"$match": {"emp.0": {"$exists": True}}},
        {"$group": {"_id": "$date",
                    "present2": {"$sum": {"$cond": [is_present, {"$cond": [{"$eq": ["$half_day", True]}, 1, 2]}, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$project": {"_id": 0, "date": "$_id", "present2": 1, "late_count": 1}},
        {"$addFields": {"dateObj": {"$dateFromString": {"dateString": "$date"}}}},
        # Exactly one row per calendar day in the range, weekends included,
        # including days with no records at all.
        {"$densify": {"field": "dateObj", "range": {"step": 1, "unit": "day", "bounds": [from_dt, to_exclusive]}}},
        {"$addFields": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dateObj"}},
                        "present2": {"$ifNull": ["$present2", 0]},
                        "late_count": {"$ifNull": ["$late_count", 0]}}},
        # Daily headcount (R9): join-date histogram for the department, then a
        # running count of everyone joined on or before each row's day.
        {"$lookup": {"from": "employees", "pipeline": [
            {"$match": {"department": department}},
            {"$group": {"_id": "$joined_on", "n": {"$sum": 1}}}], "as": "joiners"}},
        {"$addFields": {"headcount": {"$sum": {"$map": {
            "input": {"$ifNull": ["$joiners", []]},
            "as": "joiner",
            "in": {"$cond": [{"$lte": ["$$joiner._id", "$date"]}, "$$joiner.n", 0]}}}}}},
        {"$addFields": {"isWorkingDay": {"$not": {"$in": [{"$dayOfWeek": "$dateObj"}, [1, 7]]}}}},
        # attendance_rate quarter-rounded half-up to 4 decimals, kept scaled as
        # an integer (rate x 10000) so the moving average is the mean of the
        # reported values, not of full-precision floats.
        {"$addFields": {"rateScaled": {"$cond": [
            {"$and": ["$isWorkingDay", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [
                {"$add": [{"$multiply": ["$present2", 10000]}, "$headcount"]},
                {"$multiply": [2, "$headcount"]}]}},
            None]}}},
        {"$setWindowFields": {"sortBy": {"dateObj": 1},
                              "output": {"avgRateScaled": {"$avg": "$rateScaled", "window": {"documents": [-6, 0]}}}}},
        {"$addFields": {"movingScaled": {"$cond": [{"$ne": ["$avgRateScaled", None]},
                                                   {"$floor": {"$add": ["$avgRateScaled", 0.5]}}, None]}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": "$isWorkingDay", "headcount": 1,
                      "present_count": {"$divide": ["$present2", 2]}, "late_count": 1,
                      "attendance_rate": {"$cond": [{"$ne": ["$rateScaled", None]},
                                                    {"$divide": ["$rateScaled", 10000]}, None]},
                      "moving_avg_7d": {"$cond": [{"$ne": ["$movingScaled", None]},
                                                  {"$divide": ["$movingScaled", 10000]}, None]}}},
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# Analytics endpoints                                                          #
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    rows = list(attendance_logs.aggregate(build_employee_monthly_pipeline(emp_code, month)))
    stats = rows[0] if rows else {}
    present2 = int(stats.get("present2", 0))
    work_days = working_days_in_month(emp["joined_on"], month)
    if work_days == 0:
        attendance_pct = None
    else:
        attendance_pct = float(
            (Decimal(present2 * 100) / Decimal(2 * work_days)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": work_days,
        "present_days": present2 / 2,
        "leave_days": int(stats.get("leave_count", 0)),
        "late_count": int(stats.get("late_count", 0)),
        "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    rows = list(attendance_logs.aggregate(build_department_summary_pipeline(month, department)))
    items = []
    for row in rows:
        work_hours_count = int(row.get("work_hours_count", 0))
        avg_work_hours = (
            round_half_up(row["work_hours_sum"] / work_hours_count, 2) if work_hours_count > 0 else None
        )
        items.append({
            "department": row["_id"],
            "headcount": int(row.get("headcount", 0)),
            "present_days": int(row.get("present2", 0)) / 2,
            "avg_work_hours": avg_work_hours,
            "late_count": int(row.get("late_count", 0)),
            "total_late_minutes": int(row.get("total_late_minutes", 0)),
            "leave_count": int(row.get("leave_count", 0)),
            "on_duty_count": int(row.get("on_duty_count", 0)),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    rows = list(attendance_logs.aggregate(build_late_leaderboard_pipeline(month, limit, department)))
    items = [
        {
            "rank": int(row["rank"]),
            "emp_code": row["emp_code"],
            "name": row["name"],
            "department": row["department"],
            "total_late_minutes": int(row["total_late_minutes"]),
            "late_count": int(row["late_count"]),
        }
        for row in rows
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date_cls = Query(..., alias="from"),
    to_date: date_cls = Query(..., alias="to"),
):
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="'to' must be on or after 'from'")
    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(status_code=422, detail="the range must not exceed 92 days")
    if employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="unknown department")

    rows = list(attendance_logs.aggregate(
        build_department_trend_pipeline(department, from_date.isoformat(), to_date.isoformat())
    ))
    items = [
        {
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": float(row["present_count"]),
            "late_count": int(row["late_count"]),
            "attendance_rate": row["attendance_rate"],
            "moving_avg_7d": row["moving_avg_7d"],
        }
        for row in rows
    ]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: MongoDB explain for the endpoints' queries                            #
# --------------------------------------------------------------------------- #
EXPLAIN_ENDPOINTS = (
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
)


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date_cls] = None,
    date_to: Optional[date_cls] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[date_cls] = Query(None, alias="from"),
    to_date: Optional[date_cls] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint not in EXPLAIN_ENDPOINTS:
        raise HTTPException(status_code=422, detail=f"endpoint must be one of {', '.join(EXPLAIN_ENDPOINTS)}")

    if endpoint == "attendance_list":
        # Same query, sort and pagination as GET /attendance.
        query, sort, skip, limit_value = build_attendance_list_query(
            emp_code, date_from, date_to, status, page, page_size
        )
        command = {"explain": {"find": "attendance_logs", "filter": query, "sort": sort,
                               "skip": skip, "limit": limit_value}, "verbosity": "executionStats"}
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        command = {"explain": {"aggregate": "attendance_logs",
                               "pipeline": build_employee_monthly_pipeline(emp_code, month),
                               "cursor": {}}, "verbosity": "executionStats"}
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        command = {"explain": {"aggregate": "attendance_logs",
                               "pipeline": build_department_summary_pipeline(month, department),
                               "cursor": {}}, "verbosity": "executionStats"}
        collection = "attendance_logs"
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        command = {"explain": {"aggregate": "attendance_logs",
                               "pipeline": build_late_leaderboard_pipeline(month, limit, department),
                               "cursor": {}}, "verbosity": "executionStats"}
        collection = "attendance_logs"
    else:  # department_trend
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from and to are required")
        if to_date < from_date:
            raise HTTPException(status_code=422, detail="'to' must be on or after 'from'")
        if (to_date - from_date).days + 1 > 92:
            raise HTTPException(status_code=422, detail="the range must not exceed 92 days")
        command = {"explain": {"aggregate": "attendance_logs",
                               "pipeline": build_department_trend_pipeline(
                                   department, from_date.isoformat(), to_date.isoformat()),
                               "cursor": {}}, "verbosity": "executionStats"}
        collection = "attendance_logs"

    explain_doc = db.command(command)
    return {"endpoint": endpoint, "collection": collection,
            "explain": json.loads(json_util.dumps(explain_doc))}

