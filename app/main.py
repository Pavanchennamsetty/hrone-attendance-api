"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import re
import json
from datetime import datetime, timedelta, timezone, date
from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response

from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from bson.json_util import dumps, RELAXED_JSON_OPTIONS

load_dotenv()
mongo_uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI")
if not mongo_uri:
    raise RuntimeError("Set MONGO_URI in the .env file")
client = MongoClient(mongo_uri)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
@app.on_event("startup")
def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True
    )
    # Speeds up attendance queries filtered by date
    db.attendance_logs.create_index("date")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #



def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    # Convert the punch-in time to IST.
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)

    punch_in = punch_in.astimezone(IST)

    h, m = map(int, shift_start.split(":"))

    # Shift starts on the punch-in's local calendar date.
    start = punch_in.replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0
    )

    elapsed_seconds = (punch_in - start).total_seconds()

    # Early or on-time punches are not late.
    if elapsed_seconds <= 10 * 60:
        return 0

    # Count complete minutes after shift start.
    return int(elapsed_seconds // 60)




from decimal import Decimal, ROUND_HALF_UP


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    # Truncate both timestamps to whole seconds.
    punch_in = punch_in.replace(microsecond=0)
    punch_out = punch_out.replace(microsecond=0)

    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    hours = seconds / Decimal("3600")

    return float(
        hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def compute_overtime(
    punch_in: datetime,
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    date_str: str
) -> int:
    # Treat naive MongoDB timestamps as UTC, then convert to IST.
    if punch_in.tzinfo is None:
        punch_in = punch_in.replace(tzinfo=timezone.utc)
    punch_in = punch_in.astimezone(IST)

    if punch_out.tzinfo is None:
        punch_out = punch_out.replace(tzinfo=timezone.utc)
    punch_out = punch_out.astimezone(IST)

    shift_start_hour, shift_start_minute = map(
        int, shift_start.split(":")
    )
    shift_end_hour, shift_end_minute = map(
        int, shift_end.split(":")
    )

    attendance_date = datetime.fromisoformat(date_str).date()

    shift_start_dt = datetime(
        attendance_date.year,
        attendance_date.month,
        attendance_date.day,
        shift_start_hour,
        shift_start_minute,
        tzinfo=IST
    )

    shift_end_dt = datetime(
        attendance_date.year,
        attendance_date.month,
        attendance_date.day,
        shift_end_hour,
        shift_end_minute,
        tzinfo=IST
    )

    # Overnight shift: its end is on the following day.
    if shift_end_dt <= shift_start_dt:
        shift_end_dt += timedelta(days=1)

    # Count complete minutes after scheduled shift end.
    overtime_minutes = int(
        (punch_out - shift_end_dt).total_seconds() // 60
    )

    # Overtime is counted only when at least 30 minutes are completed.
    if overtime_minutes < 30:
        return 0

    return overtime_minutes

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: EmailStr
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: date

    @field_validator("emp_code")
    @classmethod
    def validate_emp_code(cls, value):
        if not re.fullmatch(r"EMP\d{4,6}", value):
            raise ValueError("emp_code must look like EMP1001")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value):
        value = value.strip()
        if not 1 <= len(value) <= 100:
            raise ValueError("name must contain 1 to 100 characters")
        return value

    @field_validator("department")
    @classmethod
    def validate_department(cls, value):
        value = value.strip()
        if not 1 <= len(value) <= 50:
            raise ValueError("department must contain 1 to 50 characters")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("Time must use HH:MM format")
        return value

    @field_validator("shift_end")
    @classmethod
    def validate_shift_order(cls, value, info):
        start = info.data.get("shift_start")
        if start == value:
            raise ValueError("Shift start and end cannot be the same")
        return value



class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value):
        if value is not None and value < 100000000000:
            raise ValueError(
                "punched_at must be epoch milliseconds, not seconds"
            )
        return value


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class RegularizeIn(BaseModel):
    reason: str
    regularized_by: str
    status: Optional[str] = None
    punch_in: Optional[datetime] = None
    punch_out: Optional[datetime] = None

    @field_validator("reason", "regularized_by")
    @classmethod
    def validate_required_text(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be empty")
        return value




# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable"
        )




@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")

    doc = body.model_dump()
    doc["joined_on"] = body.joined_on.isoformat()
    doc["created_at"] = datetime.now(timezone.utc)

    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc



@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    if body.status not in {"PRESENT", "WFH", "ON_DUTY"}:
        raise HTTPException(status_code=422, detail="Invalid attendance status")
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    ts = (
        datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc
        ).astimezone(IST)
        if body.punched_at is not None
        else datetime.now(IST)
    )
    d = ts.date().isoformat()
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    res = db.attendance_logs.insert_one(doc)
    doc["id"] = str(res.inserted_id)
    doc.pop("_id", None)
    return doc
def to_epoch_millis(value):
    if value is None:
        return None

    # MongoDB stores these timestamps as naive UTC datetimes.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    return int(value.timestamp() * 1000)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    # Validate date format: YYYY-MM-DD
    for date_value in (date_from, date_to):
        if date_value is not None:
            try:
                parsed_date = datetime.strptime(date_value, "%Y-%m-%d")
                if parsed_date.strftime("%Y-%m-%d") != date_value:
                    raise ValueError
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail="Dates must use YYYY-MM-DD format"
                )

    # Reject an invalid date range.
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from cannot be after date_to"
        )

    # Build MongoDB filters.
    q = {}

    if emp_code:
        q["emp_code"] = emp_code

    if date_from or date_to:
        q["date"] = {}

        if date_from:
            q["date"]["$gte"] = date_from

        if date_to:
            q["date"]["$lte"] = date_to

    if status:
        q["status"] = status

    # Count matching records in MongoDB.
    total = db.attendance_logs.count_documents(q)

    # Sort and paginate inside MongoDB instead of loading every record.


    # Sort and paginate inside MongoDB instead of loading every record.
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    items = list(cursor)

    for item in items:
        item["id"] = str(item.pop("_id"))
        item["punch_in"] = to_epoch_millis(item.get("punch_in"))
        item["punch_out"] = to_epoch_millis(item.get("punch_out"))

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }



@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=404,
            detail="Employee not found"
        )

    # Convert the supplied timestamp to Indian Standard Time.
    if body.punched_at is not None:
        ts = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc
        ).astimezone(IST)
    else:
        ts = datetime.now(IST)

    # MongoDB stores datetimes in UTC. Treat stored naive values as UTC.
    ts_utc = ts.astimezone(timezone.utc).replace(tzinfo=None)

    # Find the latest open attendance record for this employee.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_out": None
        },
        sort=[("punch_in", -1)]
    )

    if not record:
        closed_record = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_out": {"$ne": None}
            },
            sort=[("punch_in", -1)]
        )

        if closed_record:
            raise HTTPException(
                status_code=409,
                detail="Attendance record was already punched out"
            )

        raise HTTPException(
            status_code=404,
            detail="No open attendance record found"
        )


    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    punch_in_ist = punch_in_utc.astimezone(IST)

    if ts <= punch_in_ist:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in"
        )

    elapsed_seconds = (ts - punch_in_ist).total_seconds()

    if elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="Punch-out cannot be more than 24 hours after punch-in"
        )


    work_hours = compute_work_hours(punch_in_ist, ts)

    overtime_minutes = compute_overtime(
        punch_in=punch_in_ist,
        punch_out=ts,
        shift_start=emp["shift_start"],
        shift_end=emp["shift_end"],
        date_str=record["date"]
    )

    half_day = work_hours < 4.50


    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": ts_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day
            }
        }
    )

    # Prevent two simultaneous requests from closing the same record.
    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record was already punched out"
        )

    record["punch_in"] = to_epoch_millis(record["punch_in"])
    record["punch_out"] = to_epoch_millis(ts_utc)
    record["work_hours"] = work_hours
    record["overtime_minutes"] = overtime_minutes
    record["half_day"] = half_day
    record["id"] = str(record.pop("_id"))

    return record



@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn
):
    # Validate date format.
    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
        if parsed_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Date must use YYYY-MM-DD format"
        )

    # Check employee and attendance record.
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date
    })
    if not record:
        raise HTTPException(404, "Attendance record not found")

    allowed = {"status", "punch_in", "punch_out"}
    changes = body.model_dump(
        exclude={"reason", "regularized_by"},
        exclude_unset=True
    )

    if not changes:
        raise HTTPException(422, "No attendance fields supplied")

    if not set(changes).issubset(allowed):
        raise HTTPException(422, "Unsupported field")

    updated = {key: record.get(key) for key in allowed}
    updated.update(changes)

    valid_statuses = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
    if updated["status"] not in valid_statuses:
        raise HTTPException(422, "Invalid attendance status")

    status = updated["status"]
    punch_in = updated["punch_in"]
    punch_out = updated["punch_out"]

    if status in {"ABSENT", "LEAVE"}:
        if "punch_in" in changes and changes["punch_in"] is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        if "punch_out" in changes and changes["punch_out"] is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        punch_in = None
        punch_out = None
    elif status in {"PRESENT", "WFH", "ON_DUTY"}:
        if punch_in is None:
            raise HTTPException(422, "Presence status requires punch_in")

    def as_ist(value):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(IST)

    punch_in_ist = as_ist(punch_in)
    punch_out_ist = as_ist(punch_out)

    if punch_in_ist and punch_in_ist.date().isoformat() != date:
        raise HTTPException(422, "punch_in must fall on attendance date")

    if punch_out_ist and punch_in_ist is None:
        raise HTTPException(422, "punch_out requires punch_in")

    if punch_out_ist and punch_in_ist:
        if punch_out_ist <= punch_in_ist:
            raise HTTPException(422, "punch_out must be after punch_in")
        if (punch_out_ist - punch_in_ist).total_seconds() > 24 * 3600:
            raise HTTPException(422, "Punch-out cannot exceed 24 hours")

    work_hours = None
    late_minutes = 0
    overtime_minutes = 0
    half_day = False

    if status in {"PRESENT", "WFH", "ON_DUTY"} and punch_in_ist:
        late_minutes = compute_late_minutes(
            punch_in_ist,
            emp["shift_start"]
        )
        if punch_out_ist:
            work_hours = compute_work_hours(
                punch_in_ist,
                punch_out_ist
            )
            overtime_minutes = compute_overtime(
                punch_in_ist,
                punch_out_ist,
                emp["shift_start"],
                emp["shift_end"],
                date
            )
            half_day = work_hours < 4.50

    set_fields = {
        "status": status,
        "punch_in": (
            punch_in_ist.astimezone(timezone.utc).replace(tzinfo=None)
            if punch_in_ist else None
        ),
        "punch_out": (
            punch_out_ist.astimezone(timezone.utc).replace(tzinfo=None)
            if punch_out_ist else None
        ),
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day
    }

    changed_fields = {
        key: {"from": record.get(key), "to": value}
        for key, value in set_fields.items()
        if record.get(key) != value
    }
    if not changed_fields:
        raise HTTPException(422, "No changes to attendance")

    audit = {
        "reason": body.reason,
        "regularized_by": body.regularized_by,
        "changed_fields": changed_fields,
        "changed_at": datetime.now(timezone.utc)
    }

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            **{
                key: record.get(key)
                for key in ("status", "punch_in", "punch_out")
            }
        },
        {
            "$set": set_fields,
            "$push": {"history": audit}
        }
    )

    if result.modified_count != 1:
        raise HTTPException(409, "Attendance changed concurrently; retry")

    return {
        "emp_code": emp_code,
        "date": date,
        **set_fields,
        "history": record.get("history", []) + [audit]
    }


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str,
    month: str
):
    # Validate month format: YYYY-MM
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(
            status_code=422,
            detail="Month must be in YYYY-MM format, for example 2026-10"
        )

    year, month_number = map(int, month.split("-"))

    if year < 2000 or year > 2100:
        raise HTTPException(
            status_code=422,
            detail="Year must be between 2000 and 2100"
        )

    emp = db.employees.find_one({"emp_code": emp_code})

    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    start_date = date(year, month_number, 1)

    if month_number == 12:
        end_date = date(year + 1, 1, 1)
    else:
        end_date = date(year, month_number + 1, 1)

    # Count weekdays from the later of month start and joining date.
    joined_on = date.fromisoformat(emp["joined_on"])
    first_day = max(start_date, joined_on)

    working_days = 0
    current_day = first_day

    while current_day < end_date:
        if current_day.weekday() < 5:
            working_days += 1
        current_day += timedelta(days=1)

    # Calculate attendance metrics in MongoDB.
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": start_date.isoformat(),
                    "$lt": end_date.isoformat()
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                }
            }
        }
    ]

    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        metrics = results[0]
    else:
        metrics = {
            "present_days": 0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0
        }

    present_days = metrics["present_days"]

    attendance_pct = (
        round(present_days / working_days, 4)
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": metrics["leave_days"],
        "late_count": metrics["late_count"],
        "total_late_minutes": metrics["total_late_minutes"],
        "total_overtime_minutes": metrics["total_overtime_minutes"],
        "attendance_pct": attendance_pct
    }



@app.get("/analytics/departments/summary")
def department_summary(
    month: Optional[str] = None,
    department: Optional[str] = None
):
    # Validate month format: YYYY-MM
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(
            status_code=422,
            detail="Month must be in YYYY-MM format, for example 2026-10"
        )

    year, month_number = map(int, month.split("-"))

    if year < 2000 or year > 2100:
        raise HTTPException(
            status_code=422,
            detail="Year must be between 2000 and 2100"
        )

    start_date = date(year, month_number, 1)

    if month_number == 12:
        end_date = date(year + 1, 1, 1)
    else:
        end_date = date(year, month_number + 1, 1)

    # Match employees who joined by the end of this month.
    employee_match = {
        "joined_on": {"$lt": end_date.isoformat()}
    }

    if department:
        employee_match["department"] = department

    employee_pipeline = [
        {"$match": employee_match},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1}
            }
        },
        {"$sort": {"_id": 1}}
    ]

    departments = list(db.employees.aggregate(employee_pipeline))

    # Aggregate monthly attendance records by employee department.
    attendance_pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": start_date.isoformat(),
                    "$lt": end_date.isoformat()
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee"
            }
        },
        {"$unwind": "$employee"},
        {
            "$match": {
                **({"employee.department": department} if department else {})
            }
        },
        {
            "$group": {
                "_id": "$employee.department",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            "$work_hours",
                            None
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "ON_DUTY"]},
                            1,
                            0
                        ]
                    }
                }
            }
        }
    ]

    attendance_results = list(
        db.attendance_logs.aggregate(attendance_pipeline)
    )

    attendance_by_department = {
        item["_id"]: item for item in attendance_results
    }

    summary = []

    for item in departments:
        dept_name = item["_id"]
        metrics = attendance_by_department.get(dept_name, {})

        summary.append({
            "department": dept_name,
            "headcount": item["headcount"],
            "present_days": metrics.get("present_days", 0),
            "avg_work_hours": round(metrics["avg_work_hours"], 2)
                if metrics.get("avg_work_hours") is not None else 0,
            "late_count": metrics.get("late_count", 0),
            "total_late_minutes": metrics.get("total_late_minutes", 0),
            "leave_count": metrics.get("leave_count", 0),
            "on_duty_count": metrics.get("on_duty_count", 0)
        })

    return {
        "month": month,
        "departments": summary
    }


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = 10,
    department: Optional[str] = None
):
    # Validate the month format: YYYY-MM
    try:
        month_date = datetime.strptime(month, "%Y-%m").date()
        if month_date.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format"
        )

    # Validate the leaderboard limit
    if limit < 1 or limit > 50:
        raise HTTPException(
            status_code=422,
            detail="limit must be between 1 and 50"
        )

    # Build the date range for the requested month
    start_date = month_date.isoformat()
    if month_date.month == 12:
        end_date = date(month_date.year + 1, 1, 1).isoformat()
    else:
        end_date = date(
            month_date.year,
            month_date.month + 1,
            1
        ).isoformat()

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": start_date,
                    "$lt": end_date
                }
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [
                                {"$ifNull": ["$late_minutes", 0]},
                                0
                            ]},
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$match": {
                "total_late_minutes": {"$gt": 0}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee"
            }
        },
        {"$unwind": "$employee"}
    ]

    # Apply the department filter before calculating ranks.
    if department is not None:
        pipeline.append({
            "$match": {
                "employee.department": department
            }
        })

    pipeline.extend([
        {
            "$set": {
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department"
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        # Apply limit AFTER ranking, retaining ties at the cutoff.
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": 1,
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "late_count": 1
            }
        }
    ])

    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": items
    }

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to")
):
    # Validate required query parameters.
    # Use aliases so the API query parameters are named "from" and "to".
    from_date = from_date
    to_date = to_date

    if not from_date or not to_date:
        raise HTTPException(
            status_code=422,
            detail="Both from and to dates are required"
        )

    try:
        start_day = date.fromisoformat(from_date)
        end_day = date.fromisoformat(to_date)

        if start_day.isoformat() != from_date:
            raise ValueError
        if end_day.isoformat() != to_date:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format"
        )

    if end_day < start_day:
        raise HTTPException(
            status_code=422,
            detail="to must be on or after from"
        )

    day_count = (end_day - start_day).days + 1
    if day_count > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days"
        )

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Department not found")

    start_dt = datetime.combine(start_day, datetime.min.time())
    end_exclusive = datetime.combine(
        end_day + timedelta(days=1),
        datetime.min.time()
    )

    pipeline = [
        {
            "$documents": [
                {"date": start_dt}
            ]
        },
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_exclusive]
                }
            }
        },
        {
            "$set": {
                "day_string": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date"
                    }
                },
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$date"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date"}, 6]}
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {
                    "day": "$day_string",
                    "dept": department
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", "$$dept"]},
                                    {"$lte": ["$joined_on", "$$day"]}
                                ]
                            }
                        }
                    },
                    {"$count": "count"}
                ],
                "as": "headcount_result"
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$headcount_result.count", 0]},
                        0
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "day": "$day_string",
                    "dept": department
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$eq": ["$date", "$$day"]
                            }
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee"
                        }
                    },
                    {"$unwind": "$employee"},
                    {
                        "$match": {
                            "$expr": {
                                "$eq": ["$employee.department", "$$dept"]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"]
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1
                                            ]
                                        },
                                        0
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                {"$ifNull": ["$late_minutes", 0]},
                                                0
                                            ]
                                        },
                                        1,
                                        0
                                    ]
                                }
                            }
                        }
                    }
                ],
                "as": "attendance_result"
            }
        },
        {
            "$set": {
                "present_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$attendance_result.present_count", 0]},
                        0
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$attendance_result.late_count", 0]},
                        0
                    ]
                }
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4
                            ]
                        },
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$day_string",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$round": ["$moving_avg_7d", 4]
                }
            }
        },
        {"$sort": {"date": 1}}
    ]

    # MongoDB database-level aggregation supports the initial $documents stage.
    items = list(db.aggregate(pipeline))

    return {
        "department": department,
        "items": items
    }


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),  # NEW PARAMETER
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
):
    # --------------------------------------------------
    # 1. Employee monthly analytics
    # --------------------------------------------------
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required for employee_monthly",
            )

        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(
                status_code=422,
                detail="Month must be in YYYY-MM format",
            )

        year, month_number = map(int, month.split("-"))

        if year < 2000 or year > 2100:
            raise HTTPException(
                status_code=422,
                detail="Year must be between 2000 and 2100",
            )

        start_date = date(year, month_number, 1)
        end_date = (
            date(year + 1, 1, 1)
            if month_number == 12
            else date(year, month_number + 1, 1)
        )

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": start_date.isoformat(),
                        "$lt": end_date.isoformat(),
                    },
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$in": [
                                        "$status",
                                        ["PRESENT", "WFH", "ON_DUTY"],
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$half_day", True]},
                                        0.5,
                                        1,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$status", "LEAVE"]},
                                1,
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$gt": [
                                        {"$ifNull": ["$late_minutes", 0]},
                                        0,
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "total_overtime_minutes": {
                        "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                    },
                }
            },
        ]

        explanation = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        stages = explanation.get("stages", [])
        cursor_stage = next(
            (
                stage.get("$cursor", {})
                for stage in stages
                if "$cursor" in stage
            ),
            {},
        )

        return {
            "endpoint": endpoint,
            "emp_code": emp_code,
            "month": month,
            "pipeline": pipeline,
            "executionStats": cursor_stage.get("executionStats", {}),
            "queryPlanner": cursor_stage.get("queryPlanner", {}),
        }


    # --------------------------------------------------
    # 2. Department summary
    # --------------------------------------------------
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for department_summary",
            )

        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(
                status_code=422,
                detail="Month must be in YYYY-MM format",
            )

        year, month_number = map(int, month.split("-"))

        if year < 2000 or year > 2100:
            raise HTTPException(
                status_code=422,
                detail="Year must be between 2000 and 2100",
            )

        start_date = date(year, month_number, 1)
        end_date = (
            date(year + 1, 1, 1)
            if month_number == 12
            else date(year, month_number + 1, 1)
        )

        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": start_date.isoformat(),
                        "$lt": end_date.isoformat(),
                    }
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
        ]

        if department:
            pipeline.append(
                {"$match": {"employee.department": department}}
            )

        pipeline.append(
            {
                "$group": {
                    "_id": "$employee.department",
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$in": [
                                        "$status",
                                        ["PRESENT", "WFH", "ON_DUTY"],
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$half_day", True]},
                                        0.5,
                                        1,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "avg_work_hours": {
                        "$avg": {
                            "$cond": [
                                {
                                    "$in": [
                                        "$status",
                                        ["PRESENT", "WFH", "ON_DUTY"],
                                    ]
                                },
                                "$work_hours",
                                None,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$gt": [
                                        {"$ifNull": ["$late_minutes", 0]},
                                        0,
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "leave_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$status", "LEAVE"]},
                                1,
                                0,
                            ]
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$status", "ON_DUTY"]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            }
        )

        explanation = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        stages = explanation.get("stages", [])
        cursor_stage = next(
            (
                stage.get("$cursor", {})
                for stage in stages
                if "$cursor" in stage
            ),
            {},
        )

        return {
            "endpoint": endpoint,
            "month": month,
            "department": department,
            "pipeline": pipeline,
            "executionStats": cursor_stage.get("executionStats", {}),
            "queryPlanner": cursor_stage.get("queryPlanner", {}),
        }


    # --------------------------------------------------
    # 3. Attendance list
    # --------------------------------------------------

    # --------------------------------------------------
    # 3. Late leaderboard
    # --------------------------------------------------
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for late_leaderboard",
            )

        try:
            month_date = datetime.strptime(month, "%Y-%m").date()
            if month_date.strftime("%Y-%m") != month:
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="month must use YYYY-MM format",
            )

        start_date = month_date.isoformat()

        if month_date.month == 12:
            end_date = date(
                month_date.year + 1, 1, 1
            ).isoformat()
        else:
            end_date = date(
                month_date.year, month_date.month + 1, 1
            ).isoformat()

        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": start_date,
                        "$lt": end_date,
                    }
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$gt": [
                                        {"$ifNull": ["$late_minutes", 0]},
                                        0,
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$match": {
                    "total_late_minutes": {"$gt": 0}
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
        ]

        if department is not None:
            pipeline.append({
                "$match": {
                    "employee.department": department
                }
            })

        pipeline.extend([
            {
                "$set": {
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "emp_code": 1,
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": 1,
                    "name": 1,
                    "department": 1,
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ])

        explanation = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        stages = explanation.get("stages", [])
        cursor_stage = next(
            (
                stage.get("$cursor", {})
                for stage in stages
                if "$cursor" in stage
            ),
            {},
        )

        return {
            "endpoint": endpoint,
            "month": month,
            "department": department,
            "limit": limit,
            "pipeline": pipeline,
            "executionStats": cursor_stage.get(
                "executionStats", {}
            ),
            "queryPlanner": cursor_stage.get(
                "queryPlanner", {}
            ),
            "stages": stages,
        }


    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(
                status_code=422,
                detail="department is required for department_trend",
            )

        from_date = from_date or date_from
        to_date = to_date or date_to

        if not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="Both from and to dates are required",
            )



        try:
            start_day = date.fromisoformat(from_date)
            end_day = date.fromisoformat(to_date)

            if start_day.isoformat() != from_date:
                raise ValueError
            if end_day.isoformat() != to_date:
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="Dates must use YYYY-MM-DD format",
            )

        if end_day < start_day:
            raise HTTPException(
                status_code=422,
                detail="to must be on or after from",
            )

        day_count = (end_day - start_day).days + 1
        if day_count > 92:
            raise HTTPException(
                status_code=422,
                detail="Date range cannot exceed 92 days",
            )

        if not db.employees.find_one({"department": department}):
            raise HTTPException(
                status_code=404,
                detail="Department not found",
            )

        start_dt = datetime.combine(
            start_day, datetime.min.time()
        )
        end_exclusive = datetime.combine(
            end_day + timedelta(days=1),
            datetime.min.time(),
        )

        pipeline = [
            {"$documents": [{"date": start_dt}]},
            {
                "$densify": {
                    "field": "date",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [start_dt, end_exclusive],
                    },
                }
            },
            {
                "$set": {
                    "day_string": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$date",
                        }
                    },
                    "is_working_day": {
                        "$and": [
                            {"$gte": [{"$dayOfWeek": "$date"}, 2]},
                            {"$lte": [{"$dayOfWeek": "$date"}, 6]},
                        ]
                    },
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "let": {
                        "day": "$day_string",
                        "dept": department,
                    },
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$department", "$$dept"]},
                                        {"$lte": ["$joined_on", "$$day"]},
                                    ]
                                }
                            }
                        },
                        {"$count": "count"},
                    ],
                    "as": "headcount_result",
                }
            },
            {
                "$set": {
                    "headcount": {
                        "$ifNull": [
                            {"$arrayElemAt": ["$headcount_result.count", 0]},
                            0,
                        ]
                    }
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {
                        "day": "$day_string",
                        "dept": department,
                    },
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {"$eq": ["$date", "$$day"]}
                            }
                        },
                        {
                            "$lookup": {
                                "from": "employees",
                                "localField": "emp_code",
                                "foreignField": "emp_code",
                                "as": "employee",
                            }
                        },
                        {"$unwind": "$employee"},
                        {
                            "$match": {
                                "$expr": {
                                    "$eq": ["$employee.department", "$$dept"]
                                }
                            }
                        },
                        {
                            "$group": {
                                "_id": None,
                                "present_count": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$in": [
                                                    "$status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                            {
                                                "$cond": [
                                                    {"$eq": ["$half_day", True]},
                                                    0.5,
                                                    1,
                                                ]
                                            },
                                            0,
                                        ]
                                    }
                                },
                                "late_count": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$gt": [
                                                    {"$ifNull": ["$late_minutes", 0]},
                                                    0,
                                                ]
                                            },
                                            1,
                                            0,
                                        ]
                                    }
                                },
                            }
                        },
                    ],
                    "as": "attendance_result",
                }
            },
            {
                "$set": {
                    "present_count": {
                        "$ifNull": [
                            {
                                "$arrayElemAt": [
                                    "$attendance_result.present_count", 0
                                ]
                            },
                            0,
                        ]
                    },
                    "late_count": {
                        "$ifNull": [
                            {
                                "$arrayElemAt": [
                                    "$attendance_result.late_count", 0
                                ]
                            },
                            0,
                        ]
                    },
                }
            },
            {
                "$set": {
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_working_day",
                                    {"$gt": ["$headcount", 0]},
                                ]
                            },
                            {
                                "$round": [
                                    {
                                        "$divide": [
                                            "$present_count", "$headcount"
                                        ]
                                    },
                                    4,
                                ]
                            },
                            None,
                        ]
                    }
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"date": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {"documents": [-6, 0]},
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": "$day_string",
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": {
                        "$round": ["$moving_avg_7d", 4]
                    },
                }
            },
            {"$sort": {"date": 1}},
        ]



        try:
            explanation = db.command(
                {
                    "aggregate": 1,
                    "pipeline": pipeline,
                    "explain": True,
                }
            )

        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Department trend explain failed: {str(exc)}",
            )


        return Response(
            content=dumps(
                {
                    "endpoint": endpoint,
                    "collection": "attendance_logs",
                    "explain": explanation,
                },
                json_options=RELAXED_JSON_OPTIONS,
            ),
            media_type="application/json",
        )




    elif endpoint == "attendance_list":
        # Build the same filters as GET /attendance.
        q = {}

        if emp_code:
            q["emp_code"] = emp_code

        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        if status:
            q["status"] = status

        # Explain the same sort and pagination as GET /attendance.
        try:
            explanation = db.command(
                "explain",
                {
                    "find": "attendance_logs",
                    "filter": q,
                    "sort": {"date": -1, "emp_code": 1},
                    "skip": (page - 1) * page_size,
                    "limit": page_size,
                },
                verbosity="executionStats",
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Attendance list explain failed: {str(exc)}",
            )

        return Response(
             content=dumps(
                 {
                     "endpoint": endpoint,
                     "collection": "attendance_logs",
                     "explain": explanation,
                 },
                 json_options=RELAXED_JSON_OPTIONS,
                ),
                media_type="application/json",
        )

    # --------------------------------------------------
    # 4. Unsupported endpoint
    # --------------------------------------------------
    else:
        raise HTTPException(
            status_code=422,
            detail=(
                "Supported endpoints: attendance_list, "
                "employee_monthly, department_summary"
            ),
        )
