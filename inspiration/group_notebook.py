"""Offline, bounded group themes; accepts only identity/filter-verified Host input.

One owning instance per data directory. Calls are serialized across threads; use
asyncio.to_thread from an event loop. No Host, model, scheduler or send integration.
bind() is for a trusted adapter, not a model tool. Read APIs require the adapter
to authorize the requested space before calling; this module grants no authority.

Text is mapped immediately to a closed vocabulary and never retained or logged.
Only vocabulary tokens and scoped ID hashes wait in memory. Admission hashes are
committed before returning accepted: crash/clear/close may lose an unflushed theme,
but replay cannot count it twice. Dedup hashes are never evicted; full means reject.
Windows are UTC-aligned [start, start+600); due input is discarded after a further
600 seconds without a flush. close() discards pending input and never flushes it.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Callable, Dict, FrozenSet, Iterator, Optional, Tuple
import hashlib
import json
import math
import re
import sqlite3
import stat
import time


WINDOW_SECONDS = 600
SCHEMA_VERSION = 1
DATABASE_NAME = "group_notebook.sqlite3"


class NotebookError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class NotebookLimits:
    """Limits may be lowered, but not raised above these hard ceilings."""

    max_groups: int = 64
    max_group_messages: int = 64
    max_member_messages: int = 8
    max_text_chars: int = 4096
    max_topics_per_group: int = 128
    max_seen_per_group: int = 10000

    def __post_init__(self):
        for name, ceiling in (
            ("max_groups", 64), ("max_group_messages", 64), ("max_member_messages", 8),
            ("max_text_chars", 4096), ("max_topics_per_group", 128), ("max_seen_per_group", 10000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError(f"invalid {name}")


@dataclass(frozen=True)
class GroupBinding:
    space_id: str
    platform: str
    group_id: str
    stream_id: str
    owner_id: str


@dataclass(frozen=True)
class IngestResult:
    accepted: bool
    reason: str


@dataclass(frozen=True)
class Topic:
    space_id: str
    theme_key: str
    title: str
    tags: Tuple[str, ...]
    source_count: int
    member_count: int
    first_seen: float
    last_seen: float


@dataclass(frozen=True)
class NotebookSummary:
    space_id: str
    topic_count: int
    seen_count: int
    pending_count: int
    pending_member_count: int
    window_start: Optional[int]
    window_end: Optional[int]
    limits: NotebookLimits
    read_only: bool = True


@dataclass(frozen=True)
class _Contribution:
    member_key: str
    tags: FrozenSet[str]
    observed_at: float


@dataclass
class _Pending:
    start: int
    entries: Dict[str, _Contribution] = field(default_factory=dict)


# Deliberately small, local vocabulary. Titles use only fixed labels below;
# message content cannot become a prompt, command, title or query fragment.
_PATTERNS = {
    "rain": re.compile(r"雨夜|下雨|雨天|大雨|小雨|暴雨|雨好大|雨很大|雨声|\brain(?:y|ing)?\b", re.I),
    "night": re.compile(r"雨夜|夜晚|深夜|夜色|今晚|今夜|夜里|晚上|\bnight\b", re.I),
    "store": re.compile(r"便利店|便利商店|\bconvenience\s+store\b", re.I),
    "light": re.compile(r"灯光|亮着灯|灯还亮|灯亮|灯火|霓虹|\blights?\b", re.I),
    "forest": re.compile(r"森林|山林|树林|林间|\bforest\b|\bwoods\b", re.I),
    "coast": re.compile(r"海边|海岸|海滩|沙滩|\bcoast\b|\bbeach\b|\bseaside\b", re.I),
    "lake": re.compile(r"湖边|湖畔|湖面|\blake\b", re.I),
    "mountain": re.compile(r"山峰|山顶|山脉|雪山|\bmountains?\b", re.I),
    "street": re.compile(r"街道|街头|街景|街巷|小巷|\bstreet\b|\balley\b", re.I),
    "mist": re.compile(r"晨雾|薄雾|浓雾|雾气|\bmist(?:y)?\b|\bfog(?:gy)?\b", re.I),
    "snow": re.compile(r"下雪|雪景|雪花|积雪|雪山|\bsnow(?:y|ing)?\b", re.I),
    "clear": re.compile(r"晴空|晴天|蓝天|\bclear\s+sky\b", re.I),
    "dawn": re.compile(r"晨雾|清晨|黎明|日出|晨光|\bdawn\b|\bsunrise\b", re.I),
    "sunset": re.compile(r"落日|日落|夕阳|晚霞|黄昏|\bsunset\b", re.I),
    "warm": re.compile(r"暖光|暖色灯光|暖色调|金色光|\bwarm\s+light\b", re.I),
    "cool": re.compile(r"冷光|冷色灯光|冷色调|蓝色光|\bcool\s+light\b", re.I),
}
_SCENES = frozenset({"store", "forest", "coast", "lake", "mountain", "street"})
_SCENE_LABELS = {"store": "便利店", "forest": "山林", "coast": "海边", "lake": "湖畔",
                 "mountain": "山峰", "street": "街巷"}
_DETAIL_LABELS = {"rain": "雨景", "night": "夜色", "light": "灯光", "mist": "薄雾",
                  "snow": "雪景", "clear": "晴空", "dawn": "晨光", "sunset": "落日",
                  "warm": "暖色光", "cool": "冷色光"}
_TITLES = {
    "rain": "雨景", "night": "夜景", "store": "便利店", "light": "灯光",
    "night+rain": "雨夜", "rain+store": "雨中的便利店", "night+store": "夜里的便利店",
    "light+rain": "雨中灯光", "light+night": "夜间灯光", "light+store": "便利店灯光",
    "night+rain+store": "雨夜里的便利店", "light+night+rain": "雨夜灯光",
    "light+rain+store": "雨中的便利店灯光", "light+night+store": "夜里的便利店灯光",
    "light+night+rain+store": "雨夜里的便利店灯光",
}
_SCHEMA = (
    """CREATE TABLE notebook_groups (
        space_id TEXT PRIMARY KEY, platform TEXT NOT NULL, group_id TEXT NOT NULL,
        stream_id TEXT NOT NULL, owner_id TEXT NOT NULL,
        UNIQUE(platform, group_id), UNIQUE(platform, stream_id))""",
    """CREATE TRIGGER notebook_binding_immutable BEFORE UPDATE ON notebook_groups
        BEGIN SELECT RAISE(ABORT, 'immutable notebook binding'); END""",
    """CREATE TABLE notebook_topics (
        space_id TEXT NOT NULL REFERENCES notebook_groups, theme_key TEXT NOT NULL,
        title TEXT NOT NULL, source_count INTEGER NOT NULL CHECK(source_count >= 0),
        member_count INTEGER NOT NULL CHECK(member_count >= 0), first_seen REAL NOT NULL,
        last_seen REAL NOT NULL, PRIMARY KEY(space_id, theme_key))""",
    """CREATE TABLE notebook_seen (
        space_id TEXT NOT NULL REFERENCES notebook_groups, message_key TEXT NOT NULL,
        member_key TEXT NOT NULL, window_start INTEGER NOT NULL, theme_key TEXT,
        PRIMARY KEY(space_id, message_key),
        FOREIGN KEY(space_id, theme_key) REFERENCES notebook_topics(space_id, theme_key))""",
    """CREATE INDEX notebook_seen_window
        ON notebook_seen(space_id, window_start, member_key)""",
    """CREATE TABLE notebook_topic_members (
        space_id TEXT NOT NULL, theme_key TEXT NOT NULL, member_key TEXT NOT NULL,
        PRIMARY KEY(space_id, theme_key, member_key),
        FOREIGN KEY(space_id, theme_key) REFERENCES notebook_topics(space_id, theme_key))""",
)


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or "\0" in value:
        raise ValueError(f"invalid {name}")
    return value  # Do not normalize trusted IDs into another identity.


def _digest(kind: str, binding: GroupBinding, value: str) -> str:
    # Framing and the full binding avoid ambiguous concatenation and cross-group links.
    encoded = json.dumps(
        [kind, binding.space_id, binding.platform, binding.group_id, binding.stream_id, binding.owner_id, value],
        ensure_ascii=True, separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _extract(text: str) -> FrozenSet[str]:
    tags = frozenset(key for key, pattern in _PATTERNS.items() if pattern.search(text))
    # A bare temporal word (e.g. "今晚讨论预算") is not a visual scene.
    if not tags - {"night", "dawn", "sunset"} or not _compatible(tags, frozenset()):
        return frozenset()
    return tags


def _compatible(left: FrozenSet[str], right: FrozenSet[str]) -> bool:
    tags = left | right
    return all(len(tags & category) <= 1 for category in (
        _SCENES, {"night", "dawn", "sunset"}, {"rain", "snow", "mist", "clear"}, {"warm", "cool"},
    ))


def _title(tags: FrozenSet[str]) -> str:
    key = "+".join(sorted(tags))
    if key in _TITLES:
        return _TITLES[key]
    scene = next((_SCENE_LABELS[tag] for tag in sorted(tags & _SCENES)), "")
    return scene + "·".join(_DETAIL_LABELS[tag] for tag in _DETAIL_LABELS if tag in tags)


def _aggregate(entries):
    """Merge only compatible scenes; unanchored details need an unambiguous scene.

    A message mentioning conflicting scenes/times is rejected by _extract.
    Two distinct scenes in one window never share a topic or source count.
    """
    scenes = {tag for _, entry in entries for tag in entry.tags & _SCENES}
    buckets = []
    ordered = sorted(entries, key=lambda item: (not bool(item[1].tags & _SCENES), item[1].observed_at, item[0]))
    for message_key, entry in ordered:
        scene = entry.tags & _SCENES
        candidates = [bucket for bucket in buckets if _compatible(bucket[0], entry.tags) and (
            (scene and bucket[0] & _SCENES == scene)
            or (not scene and (not bucket[0] & _SCENES or len(scenes) == 1))
        )]
        if len(candidates) == 1:
            candidates[0][0] |= entry.tags
            candidates[0][1].append((message_key, entry))
        else:
            buckets.append([entry.tags, [(message_key, entry)]])
    return [("+".join(sorted(tags)), contributions) for tags, contributions in buckets]


class GroupNotebook:
    def __init__(
        self, data_dir: Path, *, clock: Callable[[], float] = time.time,
        limits: Optional[NotebookLimits] = None,
    ):
        self.path = Path(data_dir).absolute() / DATABASE_NAME
        self.limits = NotebookLimits() if limits is None else limits
        if not isinstance(self.limits, NotebookLimits) or not callable(clock):
            raise ValueError("invalid notebook configuration")
        self._clock = clock
        self._last_now = 0.0
        self._lock = RLock()
        self._closed = False
        self._pending: Dict[str, _Pending] = {}
        self._file_identity = None
        self._assert_safe_storage()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._assert_safe_storage()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._file_identity = self._identity(self.path.lstat())
            self._assert_safe_storage()
            # The database is independent; avoid persistent WAL and close-time checkpoints.
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA foreign_keys=ON")
            with self._transaction():
                version = self._db.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    if self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                        raise NotebookError("schema_mismatch")
                    for statement in _SCHEMA:
                        self._db.execute(statement)
                    self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                elif version != SCHEMA_VERSION:
                    raise NotebookError("schema_mismatch")
                self._check_existing_limits()
        except BaseException:
            self._db.close()
            raise

    @staticmethod
    def _identity(info):
        return info.st_dev, info.st_ino

    @staticmethod
    def _linked(info) -> bool:
        return stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        )

    def _assert_safe_storage(self):
        # Check before SQLite opens/replays a journal and before every write/close.
        # The directory must be trusted: these checks are not an OS sandbox against
        # another process replacing a file during a SQLite syscall.
        for parent in reversed(self.path.parents):
            try:
                info = parent.lstat()
            except FileNotFoundError:
                continue
            if self._linked(info) or not stat.S_ISDIR(info.st_mode):
                raise NotebookError("unsafe_path")
        for path in (self.path, *(Path(str(self.path) + suffix) for suffix in ("-journal", "-wal", "-shm"))):
            try:
                info = path.lstat()
            except FileNotFoundError:
                if path == self.path and self._file_identity is not None:
                    raise NotebookError("unsafe_path") from None
                continue
            if self._linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise NotebookError("unsafe_path")
            if path == self.path and self._file_identity is not None and self._identity(info) != self._file_identity:
                raise NotebookError("unsafe_path")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._assert_safe_storage()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._assert_safe_storage()
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise

    def _check_existing_limits(self):
        if self._db.execute("SELECT count(*) FROM notebook_groups").fetchone()[0] > self.limits.max_groups:
            raise NotebookError("limit_conflict")
        for table, maximum in (
            ("notebook_seen", self.limits.max_seen_per_group),
            ("notebook_topics", self.limits.max_topics_per_group),
        ):
            if self._db.execute(
                f"SELECT 1 FROM {table} GROUP BY space_id HAVING count(*) > ? LIMIT 1", (maximum,),
            ).fetchone():
                raise NotebookError("limit_conflict")

    def _require_open(self):
        if self._closed:
            raise NotebookError("closed")
        self._assert_safe_storage()

    def _now(self) -> float:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid clock")
        # A wall-clock correction must not reopen an earlier in-memory window.
        self._last_now = max(float(value), self._last_now)
        return self._last_now

    def _binding(self, space_id: str) -> Optional[GroupBinding]:
        row = self._db.execute("SELECT * FROM notebook_groups WHERE space_id=?", (space_id,)).fetchone()
        return GroupBinding(**dict(row)) if row else None

    def bind(self, *, space_id: str, platform: str, group_id: str, stream_id: str, owner_id: str) -> GroupBinding:
        """Permanently bind a verified space; rebinding/multiple routes are rejected."""
        with self._lock:
            self._require_open()
            binding = GroupBinding(*(
                _identifier(value, name) for name, value in (
                    ("space_id", space_id), ("platform", platform),
                    ("group_id", group_id), ("stream_id", stream_id), ("owner_id", owner_id),
                )
            ))
            with self._transaction():
                existing = self._binding(space_id)
                if existing == binding:
                    return existing
                if existing or self._db.execute(
                    "SELECT 1 FROM notebook_groups WHERE platform=? AND (group_id=? OR stream_id=?)",
                    (platform, group_id, stream_id),
                ).fetchone():
                    raise NotebookError("binding_conflict")
                if self._db.execute("SELECT count(*) FROM notebook_groups").fetchone()[0] >= self.limits.max_groups:
                    raise NotebookError("group_limit")
                self._db.execute(
                    "INSERT INTO notebook_groups VALUES(?,?,?,?,?)", (space_id, platform, group_id, stream_id, owner_id),
                )
            return binding

    def observe(
        self, *, space_id: str, platform: str, group_id: str, stream_id: str, owner_id: str,
        member_id: str, message_id: str, text: str,
    ) -> IngestResult:
        """Admit a verified message, never its authority or verbatim contents.

        A due window in this space is flushed before admitting new visual input.
        Nonvisual/rejected input never creates a binding or dedup entry.
        """
        with self._lock:
            self._require_open()
            for name, value in (
                ("space_id", space_id), ("platform", platform), ("group_id", group_id),
                ("stream_id", stream_id), ("owner_id", owner_id),
                ("member_id", member_id), ("message_id", message_id),
            ):
                _identifier(value, name)
            if not isinstance(text, str):
                raise ValueError("invalid text")
            binding = self._binding(space_id)
            if binding is None:
                return IngestResult(False, "unbound")
            if binding != GroupBinding(space_id, platform, group_id, stream_id, owner_id):
                return IngestResult(False, "binding_conflict")
            if len(text) > self.limits.max_text_chars:
                return IngestResult(False, "text_limit")
            message_key = _digest("message", binding, message_id)
            member_key = _digest("member", binding, member_id)
            if self._db.execute(
                "SELECT 1 FROM notebook_seen WHERE space_id=? AND message_key=?", (space_id, message_key),
            ).fetchone():
                return IngestResult(False, "duplicate")
            tags = _extract(text)
            if not tags:
                return IngestResult(False, "no_theme")
            now = self._now()
            self._flush_space(space_id, now)
            start = int(now // WINDOW_SECONDS) * WINDOW_SECONDS
            pending = self._pending.get(space_id)
            candidate = _Contribution(member_key, tags, now)
            candidates = list(pending.entries.items()) if pending else []
            candidates.append((message_key, candidate))
            predicted_keys = {key for key, _ in _aggregate(candidates)}
            with self._transaction():
                # Serialize the check with insertion, including separate SQLite connections.
                if self._db.execute(
                    "SELECT 1 FROM notebook_seen WHERE space_id=? AND message_key=?", (space_id, message_key),
                ).fetchone():
                    return IngestResult(False, "duplicate")
                counts = self._db.execute(
                    """SELECT count(*), coalesce(sum(window_start=?), 0),
                        coalesce(sum(window_start=? AND member_key=?), 0)
                        FROM notebook_seen WHERE space_id=?""", (start, start, member_key, space_id),
                ).fetchone()
                for reached, reason in (
                    (counts[0] >= self.limits.max_seen_per_group, "seen_limit"),
                    (counts[1] >= self.limits.max_group_messages, "group_limit"),
                    (counts[2] >= self.limits.max_member_messages, "member_limit"),
                ):
                    if reached:
                        return IngestResult(False, reason)
                existing_keys = {row[0] for row in self._db.execute(
                    "SELECT theme_key FROM notebook_topics WHERE space_id=?", (space_id,),
                )}
                if len(existing_keys | predicted_keys) > self.limits.max_topics_per_group:
                    return IngestResult(False, "topic_limit")
                self._db.execute(
                    "INSERT INTO notebook_seen VALUES(?,?,?,?,NULL)", (space_id, message_key, member_key, start),
                )
            if pending is None:
                pending = _Pending(start)
                self._pending[space_id] = pending
            pending.entries[message_key] = candidate
            return IngestResult(True, "accepted")

    ingest = observe

    def _flush_space(self, space_id: str, now: float) -> Tuple[Topic, ...]:
        pending = self._pending.get(space_id)
        if pending is None or now < pending.start + WINDOW_SECONDS:
            return ()
        if now >= pending.start + 2 * WINDOW_SECONDS:
            del self._pending[space_id]
            return ()
        with self._transaction():
            # theme_key is an accounting receipt: retry after a committed flush
            # cannot add the same sources a second time, even if RAM survived.
            entries = []
            for message_key, entry in pending.entries.items():
                row = self._db.execute(
                    "SELECT theme_key FROM notebook_seen WHERE space_id=? AND message_key=?",
                    (space_id, message_key),
                ).fetchone()
                if row is not None and row[0] is None:
                    entries.append((message_key, entry))
            aggregates = _aggregate(entries)
            existing_keys = {row[0] for row in self._db.execute(
                "SELECT theme_key FROM notebook_topics WHERE space_id=?", (space_id,),
            )}
            if len(existing_keys | {key for key, _ in aggregates}) > self.limits.max_topics_per_group:
                raise NotebookError("topic_limit")
            topics = []
            for theme_key, contributions in aggregates:
                self._db.execute(
                    """INSERT INTO notebook_topics VALUES(?,?,?,?,0,?,?)
                        ON CONFLICT(space_id, theme_key) DO UPDATE SET
                        source_count=source_count+excluded.source_count,
                        first_seen=min(first_seen, excluded.first_seen),
                        last_seen=max(last_seen, excluded.last_seen)""",
                    (space_id, theme_key, _title(frozenset(theme_key.split("+"))), len(contributions),
                     min(entry.observed_at for _, entry in contributions),
                     max(entry.observed_at for _, entry in contributions)),
                )
                for message_key, entry in contributions:
                    self._db.execute(
                        "INSERT OR IGNORE INTO notebook_topic_members VALUES(?,?,?)",
                        (space_id, theme_key, entry.member_key),
                    )
                    self._db.execute(
                        "UPDATE notebook_seen SET theme_key=? WHERE space_id=? AND message_key=?",
                        (theme_key, space_id, message_key),
                    )
                self._db.execute(
                    """UPDATE notebook_topics SET member_count=(SELECT count(*) FROM notebook_topic_members
                        WHERE space_id=? AND theme_key=?) WHERE space_id=? AND theme_key=?""",
                    (space_id, theme_key, space_id, theme_key),
                )
                topics.append(self._topic(self._db.execute(
                    "SELECT * FROM notebook_topics WHERE space_id=? AND theme_key=?", (space_id, theme_key),
                ).fetchone()))
        del self._pending[space_id]
        return tuple(topics)

    def flush(self) -> Tuple[Topic, ...]:
        """Flush due spaces; on failure that space stays pending for a bounded retry.

        Earlier spaces may already have committed. Their receipts prevent recounts.
        No timer is started: the trusted caller must periodically invoke this method.
        """
        with self._lock:
            self._require_open()
            now = self._now()
            topics = []
            for space_id in sorted(self._pending):
                topics.extend(self._flush_space(space_id, now))
            return tuple(topics)

    flush_due = flush

    def clear_pending(self, *, space_id: str) -> int:
        """Drop this space's pending tokens; keep binding, dedup and rate limits."""
        with self._lock:
            self._require_open()
            _identifier(space_id, "space_id")
            pending = self._pending.pop(space_id, None)
            return len(pending.entries) if pending else 0

    @staticmethod
    def _topic(row: sqlite3.Row) -> Topic:
        return Topic(tags=tuple(row["theme_key"].split("+")), **dict(row))

    def list_topics(self, *, space_id: str, limit: int = 20) -> Tuple[Topic, ...]:
        """Authorized-space-only snapshot; no flush, admission or database writes."""
        with self._lock:
            self._require_open()
            _identifier(space_id, "space_id")
            if type(limit) is not int or not 1 <= limit <= 100:
                raise ValueError("invalid limit")
            if self._binding(space_id) is None:
                raise NotebookError("unbound")
            rows = self._db.execute(
                "SELECT * FROM notebook_topics WHERE space_id=? ORDER BY last_seen DESC, theme_key ASC LIMIT ?",
                (space_id, limit),
            ).fetchall()
            return tuple(self._topic(row) for row in rows)

    def get_summary(self, *, space_id: str) -> NotebookSummary:
        """Counts only; never expose raw messages, member IDs or dedup keys."""
        with self._lock:
            self._require_open()
            _identifier(space_id, "space_id")
            if self._binding(space_id) is None:
                raise NotebookError("unbound")
            pending = self._pending.get(space_id)
            return NotebookSummary(
                space_id=space_id,
                topic_count=self._db.execute(
                    "SELECT count(*) FROM notebook_topics WHERE space_id=?", (space_id,),
                ).fetchone()[0],
                seen_count=self._db.execute(
                    "SELECT count(*) FROM notebook_seen WHERE space_id=?", (space_id,),
                ).fetchone()[0],
                pending_count=len(pending.entries) if pending else 0,
                pending_member_count=len({entry.member_key for entry in pending.entries.values()}) if pending else 0,
                window_start=pending.start if pending else None,
                window_end=pending.start + WINDOW_SECONDS if pending else None,
                limits=self.limits,
            )

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._assert_safe_storage()
                self._pending.clear()
                self._db.close()
                self._closed = True
