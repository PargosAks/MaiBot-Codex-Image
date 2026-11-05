"""Bounded plugin-owned thoughts, with no member/message or free-text input.

The caller must authorize the full Host binding and mint a request token before
calling record(). request_id is its SHA-256 hex digest, never a model-supplied ID.
This writer grants no authority. It uses an independent SQLite database; it never
opens notebook/artist databases, invokes models, or schedules work. Call through
asyncio.to_thread from async adapters. Multiple writers serialize in SQLite.

Successful and rejected valid requests are immutable receipts. Replays return
the original result, even after restart; rate-limited requests need a new token
for a later attempt. Receipts are never evicted. Full stores reject new requests.
The data directory must be private to the plugin's trusted service account:
path checks reject links/replacements but cannot sandbox a hostile OS account
that can race filesystem operations. No unsafe storage is deleted or repaired.
"""

from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Callable, Dict, Iterator, Optional
import hashlib
import json
import math
import re
import sqlite3
import stat
import time

from .group_notebook import _PATTERNS, _compatible, _title


DATABASE_NAME = "group_thoughts.sqlite3"
SCHEMA_VERSION = 1
MAX_TOPICS_PER_GROUP = 128
MAX_REQUESTS_PER_GROUP = 10000
INTERVAL_SECONDS = 600
ALLOWED_TAGS = frozenset(_PATTERNS)

# UI readers may use this schema, but must independently authorize the binding.
_SCHEMA = (
    """CREATE TABLE thought_groups (
        space_id TEXT PRIMARY KEY, platform TEXT NOT NULL, stream_id TEXT NOT NULL,
        group_id TEXT NOT NULL, owner_id TEXT NOT NULL, last_accepted_at REAL,
        UNIQUE(platform, stream_id), UNIQUE(platform, group_id))""",
    """CREATE TRIGGER thought_binding_immutable
        BEFORE UPDATE OF space_id, platform, stream_id, group_id, owner_id ON thought_groups
        BEGIN SELECT RAISE(ABORT, 'immutable thought binding'); END""",
    """CREATE TABLE thought_topics (
        space_id TEXT NOT NULL REFERENCES thought_groups(space_id), theme_key TEXT NOT NULL,
        note_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
        record_count INTEGER NOT NULL CHECK(record_count >= 1),
        first_seen REAL NOT NULL, last_seen REAL NOT NULL,
        PRIMARY KEY(space_id, theme_key))""",
    """CREATE TABLE thought_requests (
        space_id TEXT NOT NULL REFERENCES thought_groups(space_id), request_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL, accepted INTEGER NOT NULL CHECK(accepted IN (0, 1)),
        reason TEXT NOT NULL, note_id TEXT REFERENCES thought_topics(note_id),
        summary TEXT, created_at REAL NOT NULL, PRIMARY KEY(space_id, request_id))""",
)


class ThoughtsError(RuntimeError):
    """Predefined diagnostic code; SQLite errors retain their original exception."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _result(accepted: bool, reason: str, note_id: Optional[str] = None,
            summary: Optional[str] = None) -> Dict:
    return {"accepted": accepted, "reason": reason, "note_id": note_id, "summary": summary}


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or "\0" in value:
        raise ValueError(f"invalid {name}")
    return value  # Never normalize a trusted ID into a different identity.


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()


class GroupThoughts:
    def __init__(self, data_dir: Path, clock: Callable[[], float] = time.time):
        if not callable(clock):
            raise ValueError("invalid clock")
        directory = Path(data_dir)
        if ".." in directory.parts:
            raise ThoughtsError("unsafe_path")
        self.path = directory.absolute() / DATABASE_NAME
        self._clock = clock
        self._lock = RLock()
        self._closed = False
        self._file_identity = None
        self._parent_identities = {}
        self._assert_safe_storage()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._assert_safe_storage()
        self._parent_identities = {parent: self._identity(parent.lstat()) for parent in self.path.parents}
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._file_identity = self._identity(self.path.lstat())
            self._assert_safe_storage()
            # Do not switch journal modes: that can checkpoint/rewrite unknown state.
            if self._db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise ThoughtsError("schema_mismatch")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA trusted_schema=OFF")
            self._db.execute("PRAGMA synchronous=FULL")
            with self._transaction():
                version = self._db.execute("PRAGMA user_version").fetchone()[0]
                actual = self._schema()
                if version == 0 and not actual:
                    for statement in _SCHEMA:
                        self._db.execute(statement)
                    self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                elif version != SCHEMA_VERSION or actual != {" ".join(sql.split()) for sql in _SCHEMA}:
                    raise ThoughtsError("schema_mismatch")
                for table, maximum in (("thought_topics", MAX_TOPICS_PER_GROUP),
                                       ("thought_requests", MAX_REQUESTS_PER_GROUP)):
                    if self._db.execute(
                        f"SELECT 1 FROM {table} GROUP BY space_id HAVING count(*) > ? LIMIT 1", (maximum,),
                    ).fetchone():
                        raise ThoughtsError("limit_conflict")
                if self._db.execute("PRAGMA foreign_key_check").fetchone():
                    raise ThoughtsError("schema_mismatch")
        except BaseException:
            self._assert_safe_storage()
            self._db.close()
            raise

    def _schema(self):
        return {" ".join(row[0].split()) for row in self._db.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'",
        )}

    @staticmethod
    def _identity(info):
        return info.st_dev, info.st_ino

    @staticmethod
    def _linked(info) -> bool:
        return stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024)
        )

    def _assert_safe_storage(self):
        for parent in self.path.parents:
            try:
                info = parent.lstat()
            except FileNotFoundError:
                if parent in self._parent_identities:
                    raise ThoughtsError("unsafe_path") from None
                continue
            if self._linked(info) or not stat.S_ISDIR(info.st_mode):
                raise ThoughtsError("unsafe_path")
            expected = self._parent_identities.get(parent)
            if expected is not None and self._identity(info) != expected:
                raise ThoughtsError("unsafe_path")
        for path in (self.path, *(Path(str(self.path) + suffix) for suffix in ("-journal", "-wal", "-shm"))):
            try:
                info = path.lstat()
            except FileNotFoundError:
                if path == self.path and self._file_identity is not None:
                    raise ThoughtsError("unsafe_path") from None
                continue
            if self._linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ThoughtsError("unsafe_path")
            if path == self.path and self._file_identity is not None and self._identity(info) != self._file_identity:
                raise ThoughtsError("unsafe_path")
            if path == self.path:
                # Even a WAL header without sidecars can make SQLite create
                # -wal/-shm at connect time. Reject before SQLite opens it.
                with path.open("rb") as source:
                    header = source.read(20)
                if header.startswith(b"SQLite format 3\0") and header[18:20] != b"\x01\x01":
                    raise ThoughtsError("schema_mismatch")
            # DELETE-mode databases never own WAL files. Do not let connect/close
            # interpret or checkpoint unexpected regular sidecars either.
            if path != self.path and path.name.endswith(("-wal", "-shm")):
                raise ThoughtsError("unsafe_path")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._assert_safe_storage()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._assert_safe_storage()
            self._db.commit()
        except BaseException:
            # A path substitution is not permission to touch its new target.
            # If unsafe, restore the paths, then close/reopen this writer to
            # roll back its outstanding transaction before accepting more work.
            self._assert_safe_storage()
            self._db.rollback()
            raise

    def record(
        self, *, space_id: str, platform: str, stream_id: str, group_id: str,
        owner_id: str, tags, request_id: str,
    ) -> Dict:
        """Record only controlled tags, never model prose or member statements.

        Invalid argument types/IDs/tags/hash raise ValueError. Expected admission
        failures return accepted=False; storage/transaction errors raise. All
        valid receipts (including rejection) are persistent and replay exactly.
        """
        with self._lock:
            if self._closed:
                raise ThoughtsError("closed")
            self._assert_safe_storage()
            binding = tuple(_identifier(value, name) for name, value in (
                ("space_id", space_id), ("platform", platform), ("stream_id", stream_id),
                ("group_id", group_id), ("owner_id", owner_id),
            ))
            if not isinstance(request_id, str) or re.fullmatch(r"[0-9a-fA-F]{64}", request_id) is None:
                raise ValueError("invalid request_id")
            request_id = request_id.lower()
            if not isinstance(tags, (list, tuple, set, frozenset)) or not 1 <= len(tags) <= len(ALLOWED_TAGS):
                raise ValueError("invalid tags")
            if any(not isinstance(tag, str) or tag not in ALLOWED_TAGS for tag in tags):
                raise ValueError("invalid tags")
            controlled = frozenset(tags)
            if not _compatible(controlled, frozenset()):
                raise ValueError("incompatible tags")
            theme_key = "+".join(sorted(controlled))
            fingerprint = _hash(["plugin_thought", *binding, theme_key])
            with self._transaction():
                row = self._db.execute("SELECT * FROM thought_groups WHERE space_id=?", (space_id,)).fetchone()
                if row is not None:
                    if tuple(row[name] for name in ("space_id", "platform", "stream_id", "group_id", "owner_id")) != binding:
                        return _result(False, "binding_conflict")
                elif self._db.execute(
                    "SELECT 1 FROM thought_groups WHERE platform=? AND (stream_id=? OR group_id=?)",
                    (platform, stream_id, group_id),
                ).fetchone():
                    return _result(False, "binding_conflict")
                receipt = self._db.execute(
                    "SELECT * FROM thought_requests WHERE space_id=? AND request_id=?", (space_id, request_id),
                ).fetchone()
                if receipt is not None:
                    if receipt["fingerprint"] != fingerprint:
                        return _result(False, "request_conflict")
                    return _result(bool(receipt["accepted"]), receipt["reason"], receipt["note_id"], receipt["summary"])
                if self._db.execute(
                    "SELECT count(*) FROM thought_requests WHERE space_id=?", (space_id,),
                ).fetchone()[0] >= MAX_REQUESTS_PER_GROUP:
                    return _result(False, "request_limit")
                now = self._clock()
                if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
                    raise ValueError("invalid clock")
                now = float(now)
                topic = self._db.execute(
                    "SELECT note_id FROM thought_topics WHERE space_id=? AND theme_key=?", (space_id, theme_key),
                ).fetchone()
                if row is None:
                    self._db.execute(
                        "INSERT INTO thought_groups VALUES(?,?,?,?,?,NULL)", binding,
                    )
                if row is not None and row["last_accepted_at"] is not None and now - row["last_accepted_at"] < INTERVAL_SECONDS:
                    result = _result(False, "rate_limited")
                elif topic is None and self._db.execute(
                    "SELECT count(*) FROM thought_topics WHERE space_id=?", (space_id,),
                ).fetchone()[0] >= MAX_TOPICS_PER_GROUP:
                    result = _result(False, "topic_limit")
                else:
                    note_id = "gh_" + _hash(["note", *binding, theme_key])
                    title = _title(controlled)
                    self._db.execute(
                        """INSERT INTO thought_topics VALUES(?,?,?,?,1,?,?)
                        ON CONFLICT(space_id, theme_key) DO UPDATE SET
                            record_count=record_count+1, last_seen=excluded.last_seen""",
                        (space_id, theme_key, note_id, title, now, now),
                    )
                    self._db.execute("UPDATE thought_groups SET last_accepted_at=? WHERE space_id=?", (now, space_id))
                    result = _result(True, "updated" if topic else "created", note_id, title)
                self._db.execute(
                    "INSERT INTO thought_requests VALUES(?,?,?,?,?,?,?,?)",
                    (space_id, request_id, fingerprint, int(result["accepted"]), result["reason"],
                     result["note_id"], result["summary"], now),
                )
            return result

    def topics(self, *, space_id: str, platform: str, stream_id: str, group_id: str,
               owner_id: str) -> list[dict]:
        """Return bounded self-authored themes for one exact trusted binding.

        This is not message ingestion. Reconstruct the controlled title and ID
        before handing a persisted theme to the autonomous planning boundary.
        """
        with self._lock:
            if self._closed:
                raise ThoughtsError("closed")
            self._assert_safe_storage()
            binding = tuple(_identifier(value, name) for name, value in (
                ("space_id", space_id), ("platform", platform), ("stream_id", stream_id),
                ("group_id", group_id), ("owner_id", owner_id),
            ))
            row = self._db.execute("SELECT * FROM thought_groups WHERE space_id=?", (space_id,)).fetchone()
            if row is None:
                return []
            if tuple(row[name] for name in ("space_id", "platform", "stream_id", "group_id", "owner_id")) != binding:
                raise ThoughtsError("binding_conflict")
            rows = self._db.execute(
                "SELECT theme_key,note_id,title FROM thought_topics WHERE space_id=? "
                "ORDER BY first_seen,note_id LIMIT ?", (space_id, MAX_TOPICS_PER_GROUP + 1),
            ).fetchall()
            if len(rows) > MAX_TOPICS_PER_GROUP:
                raise ThoughtsError("limit_conflict")
            result = []
            for row in rows:
                controlled = frozenset(row["theme_key"].split("+"))
                if (not controlled or not controlled <= ALLOWED_TAGS
                        or "+".join(sorted(controlled)) != row["theme_key"]
                        or not _compatible(controlled, frozenset())
                        or row["title"] != _title(controlled)
                        or row["note_id"] != "gh_" + _hash(["note", *binding, row["theme_key"]])):
                    raise ThoughtsError("invalid_topic")
                result.append({"note_id": row["note_id"], "summary": row["title"]})
            self._assert_safe_storage()
            return result

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._assert_safe_storage()
                self._db.close()
                self._closed = True
