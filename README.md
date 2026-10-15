# Employee Attendance & Analytics API

A FastAPI service backed by MongoDB for employee records, attendance punch-in/punch-out, manual attendance corrections, and attendance analytics.

## Requirements

- Python 3.11 or newer
- MongoDB 6 or newer, running locally or reachable through a MongoDB connection URI

## Project layout

```text
candidate_kit/
├── app/
│   └── main.py
├── sample_data/
│   ├── employees.json
│   └── attendance_logs.json
├── .env
├── .env.example
├── requirements.txt
├── sample_seed.py
├── openapi.yaml
├── DATA_MODEL.md
├── DECISIONS.md
└── REVIEW.md
```

## Setup

1. Create and activate a virtual environment (recommended):

   **Windows PowerShell**
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

   **Linux/macOS**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:

   ```bash
   python -m pip install -r requirements.txt
   ```

3. Configure MongoDB in a local `.env` file in the project root:

   ```dotenv
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=employee_attendance
   ```

   Change `MONGO_URI` if your MongoDB instance uses authentication or a hosted URI. Keep credentials private and do not commit `.env` to version control. Real environment variables take precedence over `.env` values.

4. Ensure MongoDB is running and reachable before starting the API.

## Load the sample data

From the project root, run:

```bash
python sample_seed.py
```

**Warning:** the seed script replaces the contents of the `employees` and `attendance_logs` collections in the configured database. Use it only with a local/test database, never with production or shared data.

## Run the API

From the project root:

```bash
python -m uvicorn app.main:app --port 8000
```

If port 8000 is already occupied, stop the existing server or choose another port, for example `--port 8001`.

Useful URLs while the server is running:

- Health: `http://127.0.0.1:8000/health`
- Interactive API docs: `http://127.0.0.1:8000/docs`
- OpenAPI JSON: `http://127.0.0.1:8000/openapi.json`

## API overview

The API contract, exact schemas, status codes, parameters, and business rules are defined in `openapi.yaml`. Available operation groups include:

- Employee creation and listing
- Attendance punch-in and punch-out
- Attendance listing and manual correction
- Monthly employee analytics
- Department summaries and daily trends
- Late-arrival leaderboard
- Query-plan inspection through the admin explain endpoint

## Data and time conventions

- Employee records use `emp_code` as the business identifier.
- Attendance records are identified by (`emp_code`, `date`).
- Calendar dates use `YYYY-MM-DD`; months use `YYYY-MM-DD`'s `YYYY-MM` form; shift times use 24-hour `HH:MM`.
- API instants are integer epoch milliseconds; MongoDB stores instants as BSON datetimes.
- Calendar and shift calculations follow the India Standard Time (IST) rules in `openapi.yaml`.
- Derived attendance values are computed by the API; consult `openapi.yaml` and `DATA_MODEL.md` for the exact rules.

## Testing

The dependencies include `pytest` and `httpx` for testing. Run any project tests from the repository root with:

```bash
python -m pytest -q
```

This command only runs tests that exist in the project; it does not create missing tests. Verify MongoDB configuration before running integration tests. Do not run seed operations against production data.

## Reference documents

- `openapi.yaml` — API contract and business rules
- `DATA_MODEL.md` — MongoDB collection and document model
- `DECISIONS.md` — design rationale
- `REVIEW.md` — review notes and identified issues
