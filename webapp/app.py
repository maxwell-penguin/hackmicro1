"""
webapp/app.py

Minimal local-only web UI for MigraLoop. This is a thin adapter over the
existing, already-tested pipeline (src/agents/, src/core/, src/orchestrator.py)
-- it does not reimplement any drift/synthesis/sandbox/guardian logic, it just:

  1. takes an uploaded physical.db + pasted target_schema_sql from a browser,
  2. writes them to a throwaway temp directory shaped like a benchmark case
     directory (physical.db + target_schema.sql), and
  3. calls src.orchestrator.run_case() against it with a real
     MigrationSynthesizer() -- a real Anthropic API call, using the
     ANTHROPIC_API_KEY already in .env.

Local-only by design: no auth, no rate limiting, no deployment config, no
persistence across requests. Job state lives in an in-memory dict for the
lifetime of the process, which is fine for a single-user localhost demo and
wrong for anything else -- do not lift this into a shared/public service
without adding real persistence, auth, and concurrency limits first.

Progress reporting is intentionally coarse (queued -> running -> done/error).
run_case() is one blocking call with no callback/streaming hook, and this
file isn't allowed to modify src/orchestrator.py to add one -- so rather than
fabricate fake step-by-step progress the backend can't actually observe, the
frontend polls this coarse status and shows a plain "running" state, then
renders the full step-by-step trajectory only once the real result is in.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

load_dotenv()

from src.agents.synthesizer import MigrationSynthesizer  # noqa: E402  (must follow load_dotenv())
from src.orchestrator import run_case  # noqa: E402

WEBAPP_DIR = Path(__file__).resolve().parent

app = FastAPI(title="MigraLoop (local demo)")

# In-memory job store. Single-process, single-user, no persistence --
# restarting the server loses all job history, which is fine here.
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEBAPP_DIR / "upload.html")


def _validate_sqlite_file(path: Path) -> Optional[str]:
    """Cheap sanity check that the upload is actually a SQLite database
    before handing it to the real pipeline."""
    try:
        conn = sqlite3.connect(str(path))
        conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
        conn.close()
    except sqlite3.DatabaseError as e:
        return f"Uploaded file is not a valid SQLite database ({e})."
    return None


def _validate_schema_sql(sql: str) -> Optional[str]:
    """Same idea for the pasted target schema -- catch a syntax error here
    with a clean message instead of letting it surface deep inside the
    orchestrator's own schema introspection."""
    try:
        conn = sqlite3.connect(":memory:")
        conn.executescript(sql)
        conn.close()
    except sqlite3.Error as e:
        return f"Target schema SQL failed to parse ({e})."
    return None


def _run_job(job_id: str, case_dir: Path) -> None:
    with _JOBS_LOCK:
        _JOBS[job_id]["status"] = "running"

    try:
        synthesizer = MigrationSynthesizer()  # real client, real ANTHROPIC_API_KEY
        result = run_case(case_dir, synthesizer=synthesizer)

        trajectory = None
        if result.trajectory_path and Path(result.trajectory_path).exists():
            trajectory = json.loads(Path(result.trajectory_path).read_text())

        with _JOBS_LOCK:
            _JOBS[job_id].update(
                {
                    "status": "done",
                    "result": {
                        "outcome": result.outcome,
                        "attempts": result.attempts,
                        "final_migration_sql": result.final_migration_sql,
                        "attempt_errors": result.attempt_errors,
                        "trajectory": trajectory,
                    },
                }
            )
    except Exception as e:  # noqa: BLE001 -- deliberately broad: an Anthropic API
        # error, a network hiccup, or any other failure in the real pipeline must
        # surface as a clean job-status error to the browser, never crash the
        # server process or leave the job stuck at "running" forever.
        with _JOBS_LOCK:
            _JOBS[job_id].update({"status": "error", "error": f"{type(e).__name__}: {e}"})
    finally:
        shutil.rmtree(case_dir, ignore_errors=True)


@app.post("/api/run")
async def api_run(
    physical_db: UploadFile = File(...),
    target_schema_sql: str = Form(...),
) -> dict:
    if not target_schema_sql.strip():
        raise HTTPException(400, "Target schema SQL is required.")

    # Case-dir name becomes the trajectory filename (trajectories/advanced/<name>.json,
    # via the orchestrator's existing TrajectoryLogger) -- the "web_" prefix from
    # mkdtemp keeps these visually distinct from the 6 committed benchmark
    # trajectories; see .gitignore for why they never get committed.
    case_dir = Path(tempfile.mkdtemp(prefix="web_"))
    db_path = case_dir / "physical.db"
    schema_path = case_dir / "target_schema.sql"

    db_path.write_bytes(await physical_db.read())
    schema_path.write_text(target_schema_sql)

    db_error = _validate_sqlite_file(db_path)
    if db_error:
        shutil.rmtree(case_dir, ignore_errors=True)
        raise HTTPException(400, db_error)

    schema_error = _validate_schema_sql(target_schema_sql)
    if schema_error:
        shutil.rmtree(case_dir, ignore_errors=True)
        raise HTTPException(400, schema_error)

    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[job_id] = {"status": "queued", "result": None, "error": None}

    threading.Thread(target=_run_job, args=(job_id, case_dir), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/status/{job_id}")
def api_status(job_id: str) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job id.")
    return job
