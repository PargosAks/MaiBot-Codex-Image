from pathlib import Path
from threading import RLock
import hashlib
import sqlite3
import time
import uuid


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_id TEXT NOT NULL,
                digest TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL,
                thread_id TEXT, turn_id TEXT, turn_status TEXT, error_code TEXT,
                created REAL NOT NULL, UNIQUE(owner, request_id)
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE, path TEXT NOT NULL,
                sha256 TEXT NOT NULL, size INTEGER NOT NULL, mime TEXT NOT NULL
            );
        """)
        # Never restart an operation whose result might already exist remotely.
        with self.db:
            self.db.execute("""UPDATE jobs SET status=CASE WHEN id IN
                (SELECT job_id FROM artifacts) THEN 'artifact_ready' ELSE 'outcome_unknown' END,
                error_code='SERVICE_RESTARTED' WHERE status IN ('starting','running')""")

    def submit(self, owner: str, request_id: str, prompt: str, max_queue: int) -> dict:
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM jobs WHERE owner=? AND request_id=?", (owner, request_id)
            ).fetchone()
            if row:
                if row["digest"] != digest:
                    raise ValueError("REQUEST_CONFLICT")
                return self._public(row)
            count = self.db.execute(
                "SELECT count(*) FROM jobs WHERE status='queued'"
            ).fetchone()[0]
            if count >= max_queue:
                raise ValueError("QUEUE_FULL")
            job_id = "img_" + uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO jobs(id,owner,request_id,digest,prompt,status,created) "
                "VALUES(?,?,?,?,?,'queued',?)",
                (job_id, owner, request_id, digest, prompt, time.time()),
            )
            return self.get(owner, job_id)

    def claim(self) -> dict | None:
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1"
            ).fetchone()
            if not row:
                return None
            self.db.execute(
                "UPDATE jobs SET status='starting' WHERE id=?", (row["id"],)
            )
            return dict(row)

    def update(self, job_id: str, **fields) -> None:
        if not fields or not set(fields) <= {
            "status",
            "thread_id",
            "turn_id",
            "turn_status",
            "error_code",
        }:
            raise ValueError("Invalid update")
        with self.lock, self.db:
            self.db.execute(
                f"UPDATE jobs SET {','.join(k + '=?' for k in fields)} WHERE id=?",
                (*fields.values(), job_id),
            )

    def artifact(self, job_id: str, path: Path, data: bytes, mime: str) -> dict:
        asset_id = "asset_" + uuid.uuid4().hex
        digest = hashlib.sha256(data).hexdigest()
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO artifacts VALUES(?,?,?,?,?,?)",
                (asset_id, job_id, str(path), digest, len(data), mime),
            )
            self.db.execute(
                "UPDATE jobs SET status='artifact_ready' WHERE id=?", (job_id,)
            )
        return {"asset_id": asset_id}

    def get(self, owner: str, job_id: str) -> dict:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM jobs WHERE id=? AND owner=?", (job_id, owner)
            ).fetchone()
            if not row:
                raise ValueError("JOB_NOT_FOUND")
            return self._public(row)

    def _public(self, row: sqlite3.Row) -> dict:
        artifacts = self.db.execute(
            "SELECT id AS asset_id,sha256,size AS size_bytes,mime AS mime_type "
            "FROM artifacts WHERE job_id=?",
            (row["id"],),
        ).fetchall()
        return {
            "job_id": row["id"],
            "status": row["status"],
            "turn_status": row["turn_status"],
            "error_code": row["error_code"],
            "artifacts": [dict(a) for a in artifacts],
        }

    def result(self, owner: str, job_id: str) -> dict:
        with self.lock:
            self.get(owner, job_id)
            row = self.db.execute(
                "SELECT * FROM artifacts WHERE job_id=?", (job_id,)
            ).fetchone()
            if not row:
                raise ValueError("IMAGE_NOT_READY")
            return dict(row)

    def close(self) -> None:
        with self.lock:
            self.db.close()
