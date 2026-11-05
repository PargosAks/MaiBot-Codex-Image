"""Persist send intent before contacting the host. Unknown sends are never retried automatically."""

from pathlib import Path
from threading import RLock
import sqlite3


class Deliveries:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS delivery (job TEXT PRIMARY KEY, state TEXT NOT NULL)"
        )
        with self.db:
            self.db.execute(
                "UPDATE delivery SET state='send_unknown' WHERE state='sending'"
            )

    def begin(self, job: str, manual: bool = False) -> bool:
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT state FROM delivery WHERE job=?", (job,)
            ).fetchone()
            if row and (row[0] in {"sending", "send_unknown"} or not manual):
                return False
            self.db.execute(
                "INSERT INTO delivery VALUES(?,'sending') ON CONFLICT(job) DO UPDATE SET state='sending'",
                (job,),
            )
            return True

    def finish(self, job: str, state: str):
        with self.lock, self.db:
            self.db.execute("UPDATE delivery SET state=? WHERE job=?", (state, job))

    def close(self):
        with self.lock:
            self.db.close()
