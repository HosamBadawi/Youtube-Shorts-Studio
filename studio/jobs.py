"""SQLite-backed job store.

One row per generated short. Tracks processing status, the rendered output and
thumbnail paths, the editable metadata (as JSON), the YouTube upload result,
and the batch (one long source video) each short came from. Batches get their
own small table so progress survives a server restart.

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
STATUS_READY = "ready"             # rendered + copy drafted, awaiting review
STATUS_PUBLISHING = "publishing"   # uploading to YouTube
STATUS_DONE = "done"
STATUS_ERROR = "error"


@dataclass
class Job:
    id: str
    created_at: float
    status: str
    source_path: str = ""
    output_path: str = ""
    thumb_path: str = ""                  # composed thumbnail JPEG
    thumb_api: str = ""                   # thumbnails.set outcome: ok|failed|…
    duration: float = 0.0
    stage: str = ""                       # human-readable current step
    error: str = ""
    batch_id: str = ""                    # groups shorts cut from one long video
    topic: str = ""                       # this short's distinct topic
    score: float = 0.0                    # the segmenter's 0-100 hook score
    reason: str = ""                      # why the AI picked this segment
    youtube_id: str = ""                  # set after a successful upload
    privacy: str = ""                     # per-short override ("" = config default)
    meta: VideoMeta = field(default_factory=VideoMeta)
    transcript: str = ""                  # plain transcript text (for reference)
    segment: tuple[float, float] | None = None  # picked (start, end), if any
    results: dict[str, dict] = field(default_factory=dict)  # upload result

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
            "has_thumb": bool(self.thumb_path),
            "thumb_api": self.thumb_api,
            "batch_id": self.batch_id,
            "topic": self.topic,
            "score": round(self.score, 1),
            "reason": self.reason,
            "youtube_id": self.youtube_id,
            "privacy": self.privacy,
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
            for col in ("batch_id TEXT", "topic TEXT", "thumb_path TEXT",
                        "thumb_api TEXT", "score REAL", "reason TEXT",
                        "youtube_id TEXT", "privacy TEXT"):
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
            # Batch progress persisted so a restart mid-generation doesn't
            # orphan the web UI's progress view.
            c.execute(
                """CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY,
                    created_at REAL,
                    source TEXT,
                    stage TEXT,
                    percent REAL,
                    done INTEGER,
                    error TEXT,
                    note TEXT
                )"""
            )

    # --- create / read ------------------------------------------------------
    def create(self, source_path: str, batch_id: str = "",
               topic: str = "", score: float = 0.0, reason: str = "") -> Job:
        job = Job(id=uuid.uuid4().hex[:12], created_at=time.time(),
                  status=STATUS_NEW, source_path=source_path,
                  batch_id=batch_id, topic=topic, score=score, reason=reason)
        with self._lock, self._conn() as c:
            c.execute(
                """INSERT INTO jobs (id, created_at, created_day, status,
                       source_path, output_path, duration, stage, error,
                       meta_json, transcript, segment_json, results_json,
                       published_day, batch_id, topic, thumb_path, thumb_api,
                       score, reason, youtube_id, privacy)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job.id, job.created_at, _today(), job.status, source_path,
                 "", 0.0, "", "", json.dumps(job.meta.to_dict()), "",
                 "null", "{}", "", batch_id, topic, "", "",
                 score, reason, "", ""),
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

    def used_segments_for_source(self, source_path: str
                                 ) -> list[tuple[float, float]]:
        """Time spans already turned into shorts from this source video — any
        batch, any day. New generations exclude these so two shorts can NEVER
        overlap. Paths are compared resolved (absolute vs relative forms of
        the same file still match); only successfully rendered shorts count
        (an errored job never produced a short, so its span stays available).
        """
        try:
            want = Path(source_path).resolve()
        except OSError:
            want = Path(source_path)
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT source_path, segment_json FROM jobs"
                " WHERE segment_json IS NOT NULL AND segment_json != 'null'"
                "   AND status IN (?, ?, ?)",
                (STATUS_READY, STATUS_PUBLISHING, STATUS_DONE)).fetchall()
        spans: list[tuple[float, float]] = []
        for r in rows:
            try:
                if Path(r["source_path"] or "").resolve() != want:
                    continue
            except OSError:
                continue
            seg = json.loads(r["segment_json"] or "null")
            if seg and len(seg) == 2:
                spans.append((float(seg[0]), float(seg[1])))
        spans.sort()
        return spans

    def published_today(self) -> bool:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE published_day=?",
                (_today(),)).fetchone()
        return bool(row and row["n"])

    # --- update -------------------------------------------------------------
    _PATCHABLE = {"status", "stage", "error", "output_path", "thumb_path",
                  "thumb_api", "youtube_id", "privacy", "results_json",
                  "meta_json"}

    def patch(self, job_id: str, **fields) -> None:
        """Narrow column update. Concurrent actors (render worker, thumbnail
        rebuild, upload) each own disjoint columns — patching only what they
        changed prevents a stale full-row snapshot from clobbering the rest."""
        cols = {k: v for k, v in fields.items() if k in self._PATCHABLE}
        if not cols:
            return
        sets = ", ".join(f"{k}=?" for k in cols)
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE jobs SET {sets} WHERE id=?",
                      (*cols.values(), job_id))

    def patch_meta(self, job_id: str, meta: VideoMeta) -> None:
        self.patch(job_id, meta_json=json.dumps(meta.to_dict()))

    def reconcile_interrupted(self) -> int:
        """Called once at boot: a hard kill mid-generation leaves batches
        done=0 and jobs stuck 'processing'/'publishing' with no thread behind
        them — mark them failed so the UI stops polling ghosts."""
        with self._lock, self._conn() as c:
            a = c.execute(
                "UPDATE jobs SET status=?, stage='', error=?"
                " WHERE status IN (?, ?, ?)",
                (STATUS_ERROR, "interrupted by a server restart",
                 STATUS_NEW, STATUS_PROCESSING, STATUS_PUBLISHING)).rowcount
            b = c.execute(
                "UPDATE batches SET done=1, error=?"
                " WHERE done=0",
                ("interrupted by a server restart",)).rowcount
        return (a or 0) + (b or 0)

    def update(self, job: Job) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """UPDATE jobs SET status=?, output_path=?, duration=?, stage=?,
                       error=?, meta_json=?, transcript=?, segment_json=?,
                       results_json=?, batch_id=?, topic=?, thumb_path=?,
                       thumb_api=?, score=?, reason=?, youtube_id=?, privacy=?
                   WHERE id=?""",
                (job.status, job.output_path, job.duration, job.stage, job.error,
                 json.dumps(job.meta.to_dict()), job.transcript,
                 json.dumps(list(job.segment) if job.segment else None),
                 json.dumps(job.results), job.batch_id, job.topic,
                 job.thumb_path, job.thumb_api, job.score, job.reason,
                 job.youtube_id, job.privacy, job.id),
            )

    def mark_published_today(self, job_id: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE jobs SET published_day=? WHERE id=?",
                      (_today(), job_id))

    # --- batches -------------------------------------------------------------
    def batch_start(self, batch_id: str, source: str) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO batches (id, created_at, source, stage,"
                " percent, done, error, note) VALUES (?,?,?,?,?,?,?,?)",
                (batch_id, time.time(), source, "starting", 0.0, 0, "", ""))

    def batch_update(self, batch_id: str, stage: str | None = None,
                     percent: float | None = None, done: bool | None = None,
                     error: str | None = None, note: str | None = None) -> None:
        sets, vals = [], []
        for col, val in (("stage", stage), ("percent", percent),
                         ("error", error), ("note", note)):
            if val is not None:
                sets.append(f"{col}=?")
                vals.append(val)
        if done is not None:
            sets.append("done=?")
            vals.append(1 if done else 0)
        if not sets:
            return
        vals.append(batch_id)
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE batches SET {', '.join(sets)} WHERE id=?", vals)

    def batch_get(self, batch_id: str) -> dict | None:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM batches WHERE id=?",
                            (batch_id,)).fetchone()
        if not row:
            return None
        return {"id": row["id"], "source": row["source"] or "",
                "stage": row["stage"] or "", "percent": row["percent"] or 0.0,
                "done": bool(row["done"]), "error": row["error"] or "",
                "note": row["note"] or ""}

    def batch_list_recent(self, limit: int = 10) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM batches ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [{"id": r["id"], "source": r["source"] or "",
                 "stage": r["stage"] or "", "percent": r["percent"] or 0.0,
                 "done": bool(r["done"]), "error": r["error"] or "",
                 "note": r["note"] or ""} for r in rows]


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
        thumb_path=_col(row, "thumb_path"),
        thumb_api=_col(row, "thumb_api"),
        score=_fcol(row, "score"),
        reason=_col(row, "reason"),
        youtube_id=_col(row, "youtube_id"),
        privacy=_col(row, "privacy"),
    )


def _col(row: sqlite3.Row, name: str) -> str:
    try:
        return row[name] or ""
    except (IndexError, KeyError):
        return ""


def _fcol(row: sqlite3.Row, name: str) -> float:
    try:
        return float(row[name] or 0.0)
    except (IndexError, KeyError, TypeError, ValueError):
        return 0.0
