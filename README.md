# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
```

Set two environment variables (or put them in a local `.env` file, which is not committed):

- `MONGO_URI` (default `mongodb://localhost:27017`)
- `MONGO_DB` (default `attendance_db`)

Then start the server (MongoDB 6.0 or newer is required, 7.0 recommended):

```bash
uvicorn app.main:app --port 8000
```

Interactive docs: http://localhost:8000/docs

Indexes are created automatically on startup.

## Files

- `app/main.py`: the whole API
- `REVIEW.md`: defects found in the starter and how they were fixed
- `DECISIONS.md`: short design answers
