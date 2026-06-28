"""SQLite-backed job store.

One row per uploaded video. Tracks processing status, the rendered output path,
the editable metadata (as JSON), and a per-platform publish result map. Also
answers "did I already publish today?" for the one-per-day guard.

Pure stdlib (sqlite3 + json). Thread-safe via a lock plus a fresh connection
per operation, which is plenty for a single-user personal service.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .metadata import VideoMeta

# Processing lifecycle.
STATUS_NEW = "new"
STATUS_PROCESSING = "processing"   # transcribe / segment / reframe in progress
STATUS_READY = "ready"             # rendered + metadata drafted, awaiting review
STATUS_PUBLISHING = "publishing"
STATUS_DONE = "done"
STATUS_ERROR = "error"


@dataclass
class Job:
    id: str
    created_at: float
    status: str
    source_path: str = ""
    output_path: str = ""
    duration: float = 0.0
    stage: str = ""                       # human-readable current step
    error: str = ""
    batch_id: str = ""                    # groups shorts cut from one long video
    topic: str = ""                       # this short's distinct topic
    meta: VideoMeta = field(default_factory=VideoMeta)
    transcript: str = ""                  # plain transcript text (for reference)
    segment: tuple[float, float] | None = None  # picked (start, end), if any
    results: dict[str, dict] = field(default_factory=dict)  # platform -> result

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "created_human": datetime.fromtimestamp(
                self.created_at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "status": self.status,
            "stage": self.stage,
            "error": self.error,
            "duration": round(self.duration, 1),
            "has_output": bool(self.output_path),
            "batch_id": self.batch_id,
            "topic": self.topic,
            "segment": list(self.segment) if self.segment else None,
            "meta": self.meta.to_dict(),
            "transcript": self.transcript,
            "results": self.results,
        }


class JobStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    created_at REAL,
                    created_day TEXT,
                    status TEXT,
                    source_path TEXT,
                    output_path TEXT,
                    duration REAL,
                    stage TEXT,
                    error TEXT,
                    meta_json TEXT,
                    transcript TEXT,
                    segment_json TEXT,
                    results_json TEXT,
                    published_day TEXT
                )"""
            )
            # Migrations for DBs created before these columns existed.
            for col in ("batch_id TEXT", "topic TEXT"):
                try:
                    c.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass  # already present
            # Indexes: the batch-progress poll filters by batch_id, the library by
            # created_at — turn those repeated scans into index lookups.
            c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_batch "
                      "ON jobs(batch_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created "
                      "ON jobs(created_at)")

    # --- create / read ------------------------------------------------------
    def create(self, source_path: str, batch_id: str = "",
               topic: str = "") -> Job:
        job = Job(id=uuid.uuid4().hex[:12], created_at=time.time(),
                  status=STATUS_NEW, source_path=source_path,
                  batch_id=batch_id, topic=topic)
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO jobs (id, created_at, created_day, status,
                       source_path, output_path, duration, stage, error,
                       meta_json, transcript, segment_json, results_json,
                       published_day, batch_id, topic)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job.id, job.created_at, _today(), job.status, source_path,
                 "", 0.0, "", "", json.dumps(job.meta.to_dict()), "",
                 "null", "{}", "", batch_id, topic),
            )
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def list_recent(self, limit: int = 25) -> list[Job]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [_row_to_job(r) for r in rows]

    def list_by_batch(self, batch_id: str) -> list[Job]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM jobs WHERE batch_id=? ORDER BY created_at",
                (batch_id,)).fetchall()
        return [_row_to_job(r) for r in rows]

    def todays_job(self) -> Job | None:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT * FROM jobs WHERE created_day=? ORDER BY created_at DESC "
                "LIMIT 1", (_today(),)).fetchone()
        return _row_to_job(row) if row else None

    def published_today(self) -> bool:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE published_day=?",
                (_today(),)).fetchone()
        return bool(row and row["n"])

    # --- update -------------------------------------------------------------
    def update(self, job: Job) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """UPDATE jobs SET status=?, output_path=?, duration=?, stage=?,
                       error=?, meta_json=?, transcript=?, segment_json=?,
                       results_json=?, batch_id=?, topic=? WHERE id=?""",
                (job.status, job.output_path, job.duration, job.stage, job.error,
                 json.dumps(job.meta.to_dict()), job.transcript,
                 json.dumps(list(job.segment) if job.segment else None),
                 json.dumps(job.results), job.batch_id, job.topic, job.id),
            )

    def mark_published_today(self, job_id: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE jobs SET published_day=? WHERE id=?",
                      (_today(), job_id))


def _today() -> str:
    return date.today().isoformat()


def _row_to_job(row: sqlite3.Row) -> Job:
    seg = json.loads(row["segment_json"] or "null")
    return Job(
        id=row["id"],
        created_at=row["created_at"],
        status=row["status"],
        source_path=row["source_path"] or "",
        output_path=row["output_path"] or "",
        duration=row["duration"] or 0.0,
        stage=row["stage"] or "",
        error=row["error"] or "",
        meta=VideoMeta.from_dict(json.loads(row["meta_json"] or "{}")),
        transcript=row["transcript"] or "",
        segment=tuple(seg) if seg else None,
        results=json.loads(row["results_json"] or "{}"),
        batch_id=_col(row, "batch_id"),
        topic=_col(row, "topic"),
    )


def _col(row: sqlite3.Row, name: str) -> str:
    try:
        return row[name] or ""
    except (IndexError, KeyError):
        return ""
