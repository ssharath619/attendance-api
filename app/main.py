"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import calendar
import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, ServerSelectionTimeoutError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=3000, tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# --------------------------------------------------------------------------- #
# Constants (defined before the endpoints, because endpoint signatures use them)
# --------------------------------------------------------------------------- #
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
ExplainTarget = Literal[
    "attendance_list", "employee_monthly", "department_summary",
    "late_leaderboard", "department_trend",
]
EPOCH_MIN = 100000000000
EPOCH_MAX = 4102444800000
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
ATT_SORT = [("date", -1), ("emp_code", 1)]


# --------------------------------------------------------------------------- #
# Startup: indexes
# --------------------------------------------------------------------------- #
def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index("joined_on")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])


@app.on_event("startup")
def on_startup():
    try:
        create_indexes()
    except ServerSelectionTimeoutError as e:
        print("database not reachable, indexes not created:", e)


# --------------------------------------------------------------------------- #
# Helpers: time and attendance rules
# --------------------------------------------------------------------------- #
def parse_hhmm(value: str):
    h, m = value.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_hhmm(shift_end) <= parse_hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = punch_in.astimezone(IST)
    if is_overnight(shift_start, shift_end) and (local.hour, local.minute) < parse_hhmm(shift_end):
        local = local - timedelta(days=1)
    return local.date().isoformat()


def shift_boundary(date_str: str, hhmm: str) -> datetime:
    h, m = parse_hhmm(hhmm)
    day = datetime.fromisoformat(date_str)
    return day.replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    start = shift_boundary(date_str, shift_start)
    seconds = int((punch_in - start).total_seconds())
    if seconds > 600:
        return seconds // 60
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    end = shift_boundary(date_str, shift_end)
    if is_overnight(shift_start, shift_end):
        end = end + timedelta(days=1)
    seconds = int((punch_out - end).total_seconds())
    minutes = seconds // 60
    if minutes >= 30:
        return minutes
    return 0


def instant_from_ms(ms):
    if ms is None:
        ts = datetime.now(timezone.utc)
    else:
        ts = datetime.fromtimestamp(ms // 1000, tz=timezone.utc)
    return ts.replace(microsecond=0)


def to_ms(value):
    if value is None:
        return None
    return round(value.timestamp() * 1000)


def history_to_api(entry):
    changes = {}
    for field, change in entry.get("changes", {}).items():
        if field in ("punch_in", "punch_out"):
            changes[field] = {"from": to_ms(change.get("from")), "to": to_ms(change.get("to"))}
        else:
            changes[field] = change
    return {"at": to_ms(entry["at"]), "by": entry["by"], "reason": entry["reason"], "changes": changes}


def record_to_api(doc):
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [history_to_api(h) for h in doc.get("history", [])],
    }


def employee_to_api(doc):
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


def round_half_up(value, places=2):
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(round(value, 9))).quantize(q, rounding=ROUND_HALF_UP))


def month_bounds(month: str):
    year, mon = int(month[:4]), int(month[5:7])
    last_day = calendar.monthrange(year, mon)[1]
    first = date(year, mon, 1)
    last = date(year, mon, last_day)
    next_first = last + timedelta(days=1)
    return first, last, next_first


def count_working_days(first: date, last: date) -> int:
    total = 0
    d = first
    while d <= last:
        if d.weekday() < 5:
            total += 1
        d += timedelta(days=1)
    return total


# --------------------------------------------------------------------------- #
# Query / pipeline builders (used by BOTH the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def build_attendance_query(emp_code, date_from, date_to, status):
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from.isoformat()
        if date_to is not None:
            q["date"]["$lte"] = date_to.isoformat()
    if status is not None:
        q["status"] = status
    return q


def monthly_pipeline(emp_code: str, month: str):
    first, last, next_first = month_bounds(month)
    return [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": first.isoformat(), "$lt": next_first.isoformat()},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", PRESENT_STATUSES]},
                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]},
                ]},
                {"$cond": ["$half_day", 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]


def summary_pipeline(month: str, department):
    first, last, next_first = month_bounds(month)
    emp_match = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        emp_match["department"] = department
    is_present = {"$in": ["$logs.status", PRESENT_STATUSES]}
    weekday = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [2, 3, 4, 5, 6]]}
    return [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lt": next_first.isoformat()}}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "emps": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [is_present, weekday]},
                {"$cond": ["$logs.half_day", 0.5, 1]},
                0,
            ]}},
            "avg_work_hours": {"$avg": {"$cond": [is_present, "$logs.work_hours", None]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$logs.late_minutes"},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$emps"},
            "present_days": 1,
            "avg_work_hours": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(month: str, limit: int, department):
    first, last, next_first = month_bounds(month)
    pipeline = [
        {"$match": {
            "date": {"$gte": first.isoformat(), "$lt": next_first.isoformat()},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp",
        }},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return pipeline


def trend_inputs(department: str, date_from: date, date_to: date):
    if date_to < date_from:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (date_to - date_from).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "department not found")
    return [e["emp_code"] for e in emps], [e["joined_on"] for e in emps]


def trend_pipeline(date_from: date, date_to: date, codes, joined):
    start_dt = datetime(date_from.year, date_from.month, date_from.day, tzinfo=timezone.utc)
    end_dt = datetime(date_to.year, date_to.month, date_to.day, tzinfo=timezone.utc) + timedelta(days=1)
    rate_raw = {"$divide": ["$present_count", "$headcount"]}
    rate_4dp = {"$divide": [{"$floor": {"$add": [{"$multiply": [rate_raw, 10000]}, 0.5]}}, 10000]}
    return [
        {"$match": {
            "emp_code": {"$in": codes},
            "date": {"$gte": date_from.isoformat(), "$lte": date_to.isoformat()},
        }},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", PRESENT_STATUSES]},
                {"$cond": ["$half_day", 0.5, 1]},
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0,
            "day": {"$dateFromString": {"dateString": "$_id"}},
            "present_count": 1,
            "late_count": 1,
        }},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]}}},
        {"$set": {
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, [2, 3, 4, 5, 6]]},
        }},
        {"$set": {
            "headcount": {"$size": {"$filter": {
                "input": {"$literal": joined},
                "as": "j",
                "cond": {"$lte": ["$$j", "$date"]},
            }}},
        }},
        {"$set": {
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                rate_4dp,
                None,
            ]},
        }},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, "current"]}}},
        }},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1,
        }},
        {"$sort": {"date": 1}},
    ]


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

    @field_validator("joined_on")
    @classmethod
    def joined_on_must_be_real_date(cls, value):
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must be a real calendar date")
        return value

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    punch_out: Optional[StrictInt] = Field(default=None, ge=EPOCH_MIN, le=EPOCH_MAX)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database not reachable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_to_api(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    docs = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    return {
        "items": [employee_to_api(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = instant_from_ms(body.punched_at)
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_to_api(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = instant_from_ms(body.punched_at)

    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    seconds = (ts - rec["punch_in"]).total_seconds()
    if seconds <= 0:
        raise HTTPException(422, "punched_at must be after punch_in")
    if seconds > 24 * 3600:
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    hours = compute_work_hours(rec["punch_in"], ts)
    changes = {
        "punch_out": ts,
        "work_hours": hours,
        "overtime_minutes": compute_overtime(ts, emp["shift_start"], emp["shift_end"], rec["date"]),
        "half_day": hours < 4.5,
    }
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": changes},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_to_api(updated)


@app.patch("/attendance/{emp_code}/{record_date}")
def regularize(emp_code: str, record_date: date, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    d = record_date.isoformat()
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": d})
    if rec is None:
        raise HTTPException(404, "no record for that date")

    new_status = body.status or rec["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        new_in = None
        new_out = None
    else:
        new_in = instant_from_ms(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        new_out = instant_from_ms(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if new_in is None:
            raise HTTPException(422, "a presence status needs punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != d:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None:
            secs = (new_out - new_in).total_seconds()
            if secs <= 0 or secs > 24 * 3600:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if new_in is None:
        final = {"late_minutes": 0}
    else:
        final = {"late_minutes": compute_late_minutes(new_in, emp["shift_start"], d)}
    if new_in is not None and new_out is not None:
        hours = compute_work_hours(new_in, new_out)
        final["work_hours"] = hours
        final["overtime_minutes"] = compute_overtime(new_out, emp["shift_start"], emp["shift_end"], d)
        final["half_day"] = hours < 4.5
    else:
        final["work_hours"] = None
        final["overtime_minutes"] = 0
        final["half_day"] = False
    final["status"] = new_status
    final["punch_in"] = new_in
    final["punch_out"] = new_out

    defaults = {"late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    changes = {}
    for key, new_value in final.items():
        old_value = rec.get(key, defaults.get(key))
        if old_value != new_value:
            changes[key] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    history_len = len(rec.get("history", []))
    if history_len == 0:
        guard = {"$or": [{"history": {"$size": 0}}, {"history": {"$exists": False}}]}
    else:
        guard = {"history": {"$size": history_len}}

    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], **guard},
        {"$set": final, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently, retry")
    return record_to_api(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    q = build_attendance_query(emp_code, date_from, date_to, status)

    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort(ATT_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [record_to_api(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    first, last, next_first = month_bounds(month)
    result = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, month)))
    agg = result[0] if result else {}

    joined = date.fromisoformat(emp["joined_on"])
    working_days = count_working_days(max(first, joined), last) if joined <= last else 0
    present_days = round_half_up(agg.get("present_days", 0), 2)
    if working_days == 0:
        pct = None
    else:
        pct = round_half_up(present_days / working_days * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=MONTH_PATTERN),
    department: Optional[str] = None,
):
    items = []
    for row in db.employees.aggregate(summary_pipeline(month, department)):
        avg = row.get("avg_work_hours")
        row["present_days"] = round_half_up(row["present_days"], 2)
        row["avg_work_hours"] = round_half_up(avg, 2) if avg is not None else None
        items.append(row)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: date = Query(..., alias="from"),
    date_to: date = Query(..., alias="to"),
):
    codes, joined = trend_inputs(department, date_from, date_to)
    items = []
    for row in db.attendance_logs.aggregate(trend_pipeline(date_from, date_to, codes, joined)):
        if row["attendance_rate"] is not None:
            row["attendance_rate"] = round_half_up(row["attendance_rate"], 4)
        if row["moving_avg_7d"] is not None:
            row["moving_avg_7d"] = round_half_up(row["moving_avg_7d"], 4)
        items.append(row)
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Status] = None,
    trend_from: Optional[date] = Query(None, alias="from"),
    trend_to: Optional[date] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    def need(**values):
        missing = [k for k, v in values.items() if v is None]
        if missing:
            raise HTTPException(422, "missing parameter(s): " + ", ".join(missing))

    if endpoint == "attendance_list":
        if date_from is not None and date_to is not None and date_from > date_to:
            raise HTTPException(422, "date_from must not be after date_to")
        collection = "attendance_logs"
        inner = {
            "find": collection,
            "filter": build_attendance_query(emp_code, date_from, date_to, status),
            "sort": dict(ATT_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            need(emp_code=emp_code, month=month)
            collection = "attendance_logs"
            pipeline = monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            need(month=month)
            collection = "employees"
            pipeline = summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            need(month=month)
            collection = "attendance_logs"
            pipeline = leaderboard_pipeline(month, limit, department)
        else:
            need(department=department, **{"from": trend_from, "to": trend_to})
            collection = "attendance_logs"
            codes, joined = trend_inputs(department, trend_from, trend_to)
            pipeline = trend_pipeline(trend_from, trend_to, codes, joined)
        inner = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    result = db.command({"explain": inner, "verbosity": "executionStats"})
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(result)),
    }
# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
