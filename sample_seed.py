"""Load the project's sample MongoDB documents.

Usage:
    python sample_seed.py

Reads MONGO_URI and MONGO_DB from the process environment or a local .env file.
Real environment variables take precedence over values in .env.

WARNING: This script replaces the contents of the `employees` and
`attendance_logs` collections with the sample data. Do not run it against a
production or shared database.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError


BASE_DIR = Path(__file__).resolve().parent
SAMPLE_DIR = BASE_DIR / "sample_data"
COLLECTION_NAMES = ("employees", "attendance_logs")
DEFAULT_MONGO_URI = "mongodb://localhost:27017"
DEFAULT_MONGO_DB = "attendance_db"


def load_sample_documents() -> dict[str, list[dict[str, Any]]]:
    """Read and validate both sample files before modifying the database."""
    documents: dict[str, list[dict[str, Any]]] = {}

    for collection_name in COLLECTION_NAMES:
        path = SAMPLE_DIR / f"{collection_name}.json"
        if not path.is_file():
            raise FileNotFoundError(f"Sample data file not found: {path}")

        parsed = json_util.loads(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, list):
            raise ValueError(f"{path.name} must contain a JSON array")
        if not all(isinstance(document, dict) for document in parsed):
            raise ValueError(f"Every entry in {path.name} must be a JSON object")
        if not parsed:
            raise ValueError(f"Refusing to load an empty sample file: {path.name}")
        documents[collection_name] = parsed

    # Validate the natural keys before clearing either collection.
    employee_codes = [doc.get("emp_code") for doc in documents["employees"]]
    if any(not isinstance(code, str) or not code for code in employee_codes):
        raise ValueError("Every employee sample must have a non-empty emp_code")
    if len(employee_codes) != len(set(employee_codes)):
        raise ValueError("employees.json contains duplicate emp_code values")

    attendance_keys = [
        (doc.get("emp_code"), doc.get("date"))
        for doc in documents["attendance_logs"]
    ]
    if any(
        not isinstance(code, str) or not code or not isinstance(day, str) or not day
        for code, day in attendance_keys
    ):
        raise ValueError("Every attendance sample must have emp_code and date")
    if len(attendance_keys) != len(set(attendance_keys)):
        raise ValueError("attendance_logs.json contains duplicate (emp_code, date) keys")

    return documents


def main() -> None:
    # load_dotenv does not override actual environment variables by default.
    load_dotenv(BASE_DIR / ".env")
    mongo_uri = os.getenv("MONGO_URI", DEFAULT_MONGO_URI)
    database_name = os.getenv("MONGO_DB", DEFAULT_MONGO_DB)

    # Parse and validate all files before connecting or deleting anything.
    documents = load_sample_documents()
    client = MongoClient(
        mongo_uri,
        serverSelectionTimeoutMS=5000,
        tz_aware=True,
    )

    try:
        client.admin.command("ping")
        db = client[database_name]

        # This intentionally resets these collections, matching the sample
        # loader's documented behavior. Both files were validated above first.
        for collection_name in COLLECTION_NAMES:
            collection = db[collection_name]
            collection.delete_many({})
            collection.insert_many(documents[collection_name], ordered=True)
            print(
                f"loaded {len(documents[collection_name]):>2} documents into "
                f"'{database_name}.{collection_name}'"
            )
    except PyMongoError as exc:
        raise SystemExit(f"MongoDB seed failed: {exc}") from exc
    finally:
        client.close()


if __name__ == "__main__":
    main()
