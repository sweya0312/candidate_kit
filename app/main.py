"""Employee Attendance & Analytics API."""

import calendar
import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson.json_util import dumps as bson_dumps

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=3000,
    tz_aware=True,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENT = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENT, "ABSENT", "LEAVE")
ATTENDANCE_FIELDS = (
    "status",
    "punch_in",
    "punch_out",
    "work_hours",
    "late_minutes",
    "overtime_minutes",
    "half_day",
)

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="emp_code_unique"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="department_emp",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="department_joined",
    )
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="attendance_natural_key",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="attendance_punch_in",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_emp",
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)],
        name="attendance_analytics",
    )


def validate_iso_date(value: str) -> date:
    if not isinstance(value, str):
        raise ValueError("date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("date must be YYYY-MM-DD")
    return parsed


def parse_date(value: str) -> date:
    try:
        return validate_iso_date(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def parse_month(value: str) -> date:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    try:
        return date.fromisoformat(f"{value}-01")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="month must be YYYY-MM") from exc


def timestamp(value: int) -> datetime:
    if type(value) is not int or not 100_000_000_000 <= value <= 4_102_444_800_000:
        raise HTTPException(status_code=422, detail="timestamp must be epoch milliseconds")
    return datetime.fromtimestamp(value // 1000, UTC)


def truncate_instant(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def to_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    return int(truncate_instant(value).timestamp() * 1000)


def outdoc(document: dict) -> dict:
    result = dict(document)
    result.pop("_id", None)
    for key in ("punch_in", "punch_out", "created_at"):
        if isinstance(result.get(key), datetime):
            result[key] = to_ms(result[key])

    for entry in result.get("history", []):
        if isinstance(entry.get("at"), datetime):
            entry["at"] = to_ms(entry["at"])
        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = to_ms(change[side])

    if "date" in result:
        result.setdefault("punch_in", None)
        result.setdefault("punch_out", None)
        result.setdefault("work_hours", None)
        result.setdefault("late_minutes", 0)
        result.setdefault("overtime_minutes", 0)
        result.setdefault("half_day", False)
        result.setdefault("history", [])
    return result


def shift_datetime(day: date, clock: str) -> datetime:
    shift_time = datetime.strptime(clock, "%H:%M").time()
    return datetime.combine(day, shift_time, IST).astimezone(UTC)


def attendance_date(punch_in: datetime, employee: dict) -> str:
    local_time = truncate_instant(punch_in).astimezone(IST)
    day = local_time.date()
    if (
        employee["shift_end"] <= employee["shift_start"]
        and local_time.strftime("%H:%M") < employee["shift_end"]
    ):
        day -= timedelta(days=1)
    return day.isoformat()


def late_minutes(punch_in: datetime, employee: dict, attendance_day: str) -> int:
    shift_start = shift_datetime(parse_date(attendance_day), employee["shift_start"])
    elapsed_seconds = int((truncate_instant(punch_in) - shift_start).total_seconds())
    return elapsed_seconds // 60 if elapsed_seconds > 600 else 0


def derive_fields(
    punch_in: datetime | None,
    punch_out: datetime | None,
    employee: dict,
    attendance_day: str,
) -> tuple[float | None, int, int, bool]:
    late = late_minutes(punch_in, employee, attendance_day) if punch_in else 0
    if punch_in is None or punch_out is None:
        return None, late, 0, False

    start = truncate_instant(punch_in)
    end = truncate_instant(punch_out)
    elapsed_seconds = int((end - start).total_seconds())
    hours = (Decimal(elapsed_seconds) / Decimal(3600)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    shift_end_day = parse_date(attendance_day)
    if employee["shift_end"] <= employee["shift_start"]:
        shift_end_day += timedelta(days=1)
    shift_end = shift_datetime(shift_end_day, employee["shift_end"])
    overtime = int((end - shift_end).total_seconds()) // 60
    return (
        float(hours),
        late,
        overtime if overtime >= 30 else 0,
        hours < Decimal("4.50"),
    )


def page_offset(page: int, page_size: int) -> int:
    if page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(status_code=422, detail="invalid pagination")
    return (page - 1) * page_size


def weekdays(start: date, end: date) -> int:
    total = 0
    while start <= end:
        total += start.weekday() < 5
        start += timedelta(days=1)
    return total


def month_bounds(month: str) -> tuple[date, date]:
    start = parse_month(month)
    end = date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
    return start, end


def month_query(month: str, emp_code: str | None = None) -> tuple[dict, date, date]:
    start, end = month_bounds(month)
    query = {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}
    if emp_code is not None:
        query["emp_code"] = emp_code
    return query, start, end


def round_half_up(value: int | float, places: int) -> float:
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def attendance_list_query(
    emp_code: str | None,
    date_from: str | None,
    date_to: str | None,
    status: str | None,
) -> dict:
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        bounds = {}
        if date_from is not None:
            bounds["$gte"] = parse_date(date_from).isoformat()
        if date_to is not None:
            bounds["$lte"] = parse_date(date_to).isoformat()
        if date_from is not None and date_to is not None and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must not exceed date_to")
        query["date"] = bounds
    if status is not None:
        if status not in STATUSES:
            raise HTTPException(status_code=422, detail="invalid status")
        query["status"] = status
    return query


def employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    query, _, _ = month_query(month, emp_code)
    return [
        {"$match": query},
        {
            "$addFields": {
                "_day": {"$dateFromString": {"dateString": "$date"}},
                "_half": {"$ifNull": ["$half_day", False]},
                "_late": {"$ifNull": ["$late_minutes", 0]},
                "_overtime": {"$ifNull": ["$overtime_minutes", 0]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENT)]},
                                    {"$in": [{"$dayOfWeek": "$_day"}, [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {"$cond": ["$_half", 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$_late"},
                "total_overtime_minutes": {"$sum": "$_overtime"},
            }
        },
    ]


def department_summary_pipeline(month: str, department: str | None = None) -> list[dict]:
    start, end = month_bounds(month)
    employee_match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        employee_match["department"] = department

    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start.isoformat()]},
                                    {"$lte": ["$date", end.isoformat()]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "status": 1,
                            "half_day": 1,
                            "work_hours": 1,
                            "late_minutes": 1,
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENT)]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$logs.date"
                                                    }
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENT)]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$headcount"},
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(
    month: str, limit: int, department: str | None = None
) -> list[dict]:
    query, _, _ = month_query(month)
    query["late_minutes"] = {"$gt": 0}
    pipeline = [
        {"$match": query},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
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
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": DESCENDING},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


def department_trend_pipeline(
    department: str, first: date, last: date
) -> list[dict]:
    first_string = first.isoformat()
    last_string = last.isoformat()
    first_datetime = datetime.combine(first, time.min, UTC)
    day_count = (last - first).days + 1

    return [
        {"$match": {"department": department}},
        {
            "$facet": {
                "employees": [
                    {"$match": {"joined_on": {"$lte": last_string}}},
                    {"$group": {"_id": None, "joined_on": {"$push": "$joined_on"}}},
                ],
                "daily": [
                    {"$match": {"joined_on": {"$lte": last_string}}},
                    {
                        "$lookup": {
                            "from": "attendance_logs",
                            "let": {"code": "$emp_code"},
                            "pipeline": [
                                {
                                    "$match": {
                                        "$expr": {
                                            "$and": [
                                                {"$eq": ["$emp_code", "$$code"]},
                                                {"$gte": ["$date", first_string]},
                                                {"$lte": ["$date", last_string]},
                                            ]
                                        }
                                    }
                                },
                                {
                                    "$project": {
                                        "_id": 0,
                                        "date": 1,
                                        "status": 1,
                                        "half_day": 1,
                                        "late_minutes": 1,
                                    }
                                },
                            ],
                            "as": "logs",
                        }
                    },
                    {"$unwind": "$logs"},
                    {
                        "$group": {
                            "_id": "$logs.date",
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$logs.status", list(PRESENT)]},
                                        {
                                            "$cond": [
                                                {"$ifNull": ["$logs.half_day", False]},
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
                                        {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": "$_id",
                            "present_count": 1,
                            "late_count": 1,
                        }
                    },
                ],
            }
        },
        {
            "$project": {
                "_employees": {
                    "$ifNull": [{"$arrayElemAt": ["$employees.joined_on", 0]}, []]
                },
                "_daily": "$daily",
                "_dates": {
                    "$map": {
                        "input": {"$range": [0, day_count]},
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": first_datetime,
                                "unit": "day",
                                "amount": "$$offset",
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$_dates"},
        {
            "$set": {
                "date": "$_dates",
                "date_string": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$_dates",
                        "timezone": "UTC",
                    }
                },
            }
        },
        {
            "$set": {
                "_daily_row": {
                    "$arrayElemAt": [
                        {
                            "$filter": {
                                "input": "$_daily",
                                "as": "daily",
                                "cond": {"$eq": ["$$daily.date", "$date_string"]},
                            }
                        },
                        0,
                    ]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$_employees",
                            "as": "joined_on",
                            "cond": {"$lte": ["$$joined_on", "$date_string"]},
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "present_count": {"$ifNull": ["$_daily_row.present_count", 0]},
                "late_count": {"$ifNull": ["$_daily_row.late_count", 0]},
                "is_working_day": {
                    "$in": [{"$dayOfWeek": "$date"}, [2, 3, 4, 5, 6]]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {"$divide": ["$present_count", "$headcount"]},
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
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
                "date": "$date_string",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value: str) -> str:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("time must be HH:MM")
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        validate_iso_date(value)
        return value

    @field_validator("shift_end")
    @classmethod
    def validate_distinct_shifts(cls, value: str, info) -> str:
        if info.data.get("shift_start") == value:
            raise ValueError("shift_start must differ from shift_end")
        return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: StrictInt | None = Field(
        default=None, ge=100_000_000_000, le=4_102_444_800_000
    )
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def reject_null_timestamp(cls, value):
        if value is None:
            raise ValueError("punched_at must be epoch milliseconds")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: StrictInt | None = Field(
        default=None, ge=100_000_000_000, le=4_102_444_800_000
    )

    @field_validator("punched_at", mode="before")
    @classmethod
    def reject_null_timestamp(cls, value):
        if value is None:
            raise ValueError("punched_at must be epoch milliseconds")
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"] | None = None
    punch_in: StrictInt | None = Field(
        default=None, ge=100_000_000_000, le=4_102_444_800_000
    )
    punch_out: StrictInt | None = Field(
        default=None, ge=100_000_000_000, le=4_102_444_800_000
    )

    @field_validator("status", "punch_in", "punch_out", mode="before")
    @classmethod
    def reject_null_changes(cls, value):
        if value is None:
            raise ValueError("explicit null values are not allowed")
        return value


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    document = body.model_dump()
    if document["shift_start"] == document["shift_end"]:
        raise HTTPException(status_code=422, detail="shift times must differ")
    document["created_at"] = truncate_instant(datetime.now(UTC))
    try:
        db.employees.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return outdoc(document)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    offset = page_offset(page, page_size)
    query = {"department": department} if department is not None else {}
    items = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING)
    return {
        "items": [outdoc(item) for item in items.skip(offset).limit(page_size)],
        "total": db.employees.count_documents(query),
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at = (
        timestamp(body.punched_at)
        if body.punched_at is not None
        else truncate_instant(datetime.now(UTC))
    )
    day = attendance_date(punched_at, employee)
    document = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes(punched_at, employee, day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(
            status_code=409, detail="already punched in for this date"
        ) from exc
    return outdoc(document)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at = (
        timestamp(body.punched_at)
        if body.punched_at is not None
        else truncate_instant(datetime.now(UTC))
    )
    # The contract closes the most recent record whose punch-in is not later
    # than this punch-out instant. If that record is already closed, return 409.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="punch-in not found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")

    punch_in_at = truncate_instant(record["punch_in"])
    elapsed_seconds = (punched_at - punch_in_at).total_seconds()
    if elapsed_seconds <= 0 or elapsed_seconds > 86_400:
        raise HTTPException(status_code=422, detail="punch-out must be within 24 hours")

    work_hours, late, overtime, half_day = derive_fields(
        punch_in_at, punched_at, employee, record["date"]
    )
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_in": punch_in_at,
                "punch_out": punched_at,
                "work_hours": work_hours,
                "late_minutes": late,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="already punched out")
    return outdoc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    offset = page_offset(page, page_size)
    query = attendance_list_query(emp_code, date_from, date_to, status)
    cursor = (
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(offset)
        .limit(page_size)
    )
    return {
        "items": [outdoc(item) for item in cursor],
        "total": db.attendance_logs.count_documents(query),
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    day = parse_date(date).isoformat()
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if record is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    changes_requested = body.model_dump(
        exclude={"reason", "regularized_by"}, exclude_unset=True
    )
    status = changes_requested.get("status", record["status"])
    punch_in_at = (
        timestamp(changes_requested["punch_in"])
        if "punch_in" in changes_requested
        else record.get("punch_in")
    )
    punch_out_at = (
        timestamp(changes_requested["punch_out"])
        if "punch_out" in changes_requested
        else record.get("punch_out")
    )
    if punch_in_at is not None:
        punch_in_at = truncate_instant(punch_in_at)
    if punch_out_at is not None:
        punch_out_at = truncate_instant(punch_out_at)

    if status in ("ABSENT", "LEAVE"):
        if "punch_in" in changes_requested or "punch_out" in changes_requested:
            raise HTTPException(
                status_code=422, detail="absent/leave cannot include punch times"
            )
        punch_in_at = None
        punch_out_at = None
    elif punch_in_at is None:
        raise HTTPException(status_code=422, detail="presence status requires punch_in")
    elif attendance_date(punch_in_at, employee) != day:
        raise HTTPException(
            status_code=422, detail="punch_in must match attendance date"
        )

    if punch_out_at is not None and (
        punch_in_at is None
        or punch_out_at <= punch_in_at
        or (punch_out_at - punch_in_at).total_seconds() > 86_400
    ):
        raise HTTPException(status_code=422, detail="invalid punch_out")

    work_hours, late, overtime, half_day = derive_fields(
        punch_in_at, punch_out_at, employee, day
    )
    final_values = {
        "status": status,
        "punch_in": punch_in_at,
        "punch_out": punch_out_at,
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }

    # Legacy seeded records may omit fields whose contract defines defaults.
    # Compare against those logical defaults so a correction records only real changes.
    defaults = {
        "status": record.get("status"),
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    }
    current_values = {
        key: record[key] if key in record else default
        for key, default in defaults.items()
    }
    changed = {
        key: value
        for key, value in final_values.items()
        if current_values[key] != value
    }
    if not changed:
        raise HTTPException(status_code=422, detail="request changes nothing")

    history_changes = {
        key: {"from": current_values[key], "to": value}
        for key, value in changed.items()
    }
    history_entry = {
        "at": truncate_instant(datetime.now(UTC)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": history_changes,
    }

    # Compare the exact original values, including whether a legacy field was
    # absent. This gives optimistic locking without rejecting valid legacy rows.
    concurrency_conditions = [{"_id": record["_id"]}]
    for key in ATTENDANCE_FIELDS:
        if key in record:
            concurrency_conditions.append({key: record[key]})
        else:
            concurrency_conditions.append({key: {"$exists": False}})
    if "history" in record:
        concurrency_conditions.append({"history": record["history"]})
    else:
        concurrency_conditions.append({"history": {"$exists": False}})

    updated = db.attendance_logs.find_one_and_update(
        {"$and": concurrency_conditions},
        {"$set": final_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="record changed concurrently")
    return outdoc(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    start, end = month_bounds(month)
    result = next(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)), {})
    joined_on = max(start, parse_date(employee["joined_on"]))
    working_days = weekdays(joined_on, end)
    present_days = result.get("present_days", 0)
    attendance_pct = (
        round_half_up(present_days / working_days * 100, 2)
        if working_days
        else None
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": result.get("leave_days", 0),
        "late_count": result.get("late_count", 0),
        "total_late_minutes": result.get("total_late_minutes", 0),
        "total_overtime_minutes": result.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = None,
):
    items = list(
        db.employees.aggregate(
            department_summary_pipeline(month, department), allowDiskUse=True
        )
    )
    for item in items:
        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = round_half_up(item["avg_work_hours"], 2)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: str | None = None,
):
    items = list(
        db.attendance_logs.aggregate(
            leaderboard_pipeline(month, limit, department), allowDiskUse=True
        )
    )
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    first = parse_date(from_)
    last = parse_date(to)
    if last < first or (last - first).days + 1 > 92:
        raise HTTPException(status_code=422, detail="invalid date range")
    if db.employees.find_one({"department": department}) is None:
        raise HTTPException(status_code=404, detail="department not found")

    items = list(
        db.employees.aggregate(
            department_trend_pipeline(department, first, last), allowDiskUse=True
        )
    )
    for item in items:
        for field in ("attendance_rate", "moving_avg_7d"):
            if item[field] is not None:
                item[field] = round_half_up(item[field], 4)
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: str,
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    month: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: str | None = None,
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
):
    if endpoint == "attendance_list":
        query = attendance_list_query(emp_code, date_from, date_to, status)
        offset = page_offset(page, page_size)
        command = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": DESCENDING, "emp_code": ASCENDING},
            "skip": offset,
            "limit": page_size,
        }
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(
                status_code=422, detail="emp_code and month are required"
            )
        pipeline = employee_monthly_pipeline(emp_code, month)
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = department_summary_pipeline(month, department)
        collection = "employees"
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = leaderboard_pipeline(month, limit, department)
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    elif endpoint == "department_trend":
        if department is None or from_ is None or to is None:
            raise HTTPException(
                status_code=422, detail="department, from and to are required"
            )
        first = parse_date(from_)
        last = parse_date(to)
        if last < first or (last - first).days + 1 > 92:
            raise HTTPException(status_code=422, detail="invalid date range")
        pipeline = department_trend_pipeline(department, first, last)
        collection = "employees"
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    else:
        raise HTTPException(status_code=422, detail="unknown endpoint")

    try:
        result = db.command("explain", command, verbosity="executionStats")
    except PyMongoError as exc:
        import logging
        logging.exception("MongoDB explain failed for endpoint=%s", endpoint)
        raise HTTPException(status_code=500, detail="MongoDB explain failed") from exc

    # MongoDB explain output can contain BSON-only types (for example Timestamp).
    # Convert them to Extended JSON before returning through FastAPI.
    return json.loads(bson_dumps({
        "endpoint": endpoint,
        "collection": collection,
        "explain": result,
    }))
