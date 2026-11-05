"""Offline writer checks. Run with --noconftest; every database is under tmp_path."""

from concurrent.futures import ThreadPoolExecutor
from itertools import islice, product
from pathlib import Path
from types import ModuleType, SimpleNamespace
import hashlib
import importlib
import json
import os
import sqlite3
import subprocess
import sys

import pytest


# Import just the two offline modules, never plugin.py or a live Host fixture.
PLUGIN = Path(__file__).resolve().parents[1]
PACKAGE = "offline_group_thought_tests"
package = ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN)]
sys.modules[PACKAGE] = package
thoughts = importlib.import_module(PACKAGE + ".inspiration.group_thoughts")
notebook = importlib.import_module(PACKAGE + ".inspiration.group_notebook")
GroupThoughts = thoughts.GroupThoughts
ThoughtsError = thoughts.ThoughtsError


class Clock:
    value = 1000.0

    def __call__(self):
        return self.value


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    writer = GroupThoughts(tmp_path / "data", clock=clock)
    yield writer
    writer.close()


def binding(space="a"):
    return {"space_id": space, "platform": "synthetic", "stream_id": "stream-" + space,
            "group_id": "group-" + space, "owner_id": "owner-" + space}


def token(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def record(store, request="first", tags=("rain", "night", "store"), space="a", **changes):
    args = dict(**binding(space), tags=tags, request_id=token(request))
    args.update(changes)
    return store.record(**args)


def rows(store, sql, args=()):
    with sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True) as db:
        return db.execute(sql, args).fetchall()


def error(code):
    return pytest.raises(ThoughtsError, match="^" + code + "$")


def test_vocabulary_title_and_independent_storage(store):
    assert thoughts.ALLOWED_TAGS == frozenset(notebook._PATTERNS)
    assert len(thoughts.ALLOWED_TAGS) == 16
    result = record(store)
    assert result == {"accepted": True, "reason": "created", "note_id": result["note_id"],
                      "summary": "雨夜里的便利店"}
    assert result["note_id"].startswith("gh_")
    assert store.path.name == "group_thoughts.sqlite3"
    assert {path.name for path in store.path.parent.iterdir()} == {thoughts.DATABASE_NAME}
    assert rows(store, "PRAGMA user_version") == [(1,)]
    tables = rows(store, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    assert tables == [("thought_groups",), ("thought_requests",), ("thought_topics",)]
    assert rows(store, "SELECT theme_key,record_count,first_seen,last_seen FROM thought_topics") == [
        ("night+rain+store", 1, 1000.0, 1000.0),
    ]


def test_immediate_persistence_replay_canonical_tags_and_snapshot(store, clock):
    original = record(store)
    assert record(store, tags=["store", "rain", "night", "rain"]) == original
    clock.value += 600
    updated = record(store, request="second")
    assert updated["reason"] == "updated"
    assert updated["note_id"] == original["note_id"]
    # A replay returns its original receipt, even after the topic has advanced.
    assert record(store) == original
    assert rows(store, "SELECT record_count,first_seen,last_seen FROM thought_topics") == [(2, 1000, 1600)]
    store.close()
    reopened = GroupThoughts(store.path.parent, clock=clock)
    try:
        assert record(reopened) == original
        assert record(reopened, request="second") == updated
        assert record(reopened, request="second", tags=["forest"])["reason"] == "request_conflict"
        assert rows(reopened, "SELECT count(*) FROM thought_requests") == [(2,)]
    finally:
        reopened.close()


def test_topics_recheck_binding_and_controlled_content_without_writing(store):
    first = record(store)
    record(store, space="b", tags=["forest"])
    before = store.path.read_bytes()
    assert store.topics(**binding("a")) == [{"note_id": first["note_id"], "summary": first["summary"]}]
    assert store.path.read_bytes() == before
    assert store.topics(**binding("missing")) == []
    with error("binding_conflict"):
        store.topics(**(binding("a") | {"group_id": "group-b"}))
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE thought_topics SET title='untrusted prose' WHERE space_id='a'")
    with error("invalid_topic"):
        store.topics(**binding("a"))


@pytest.mark.parametrize("column,value", [
    ("theme_key", "forest+store"), ("theme_key", "unknown"), ("note_id", "gh_" + "f" * 64),
])
def test_topics_refuse_changed_identity_or_invalid_theme(store, column, value):
    record(store)
    with sqlite3.connect(store.path) as db:
        # Foreign keys are off on this isolated corruption connection only.
        db.execute(f"UPDATE thought_topics SET {column}=?", (value,))
    with error("invalid_topic"):
        store.topics(**binding())


def test_fresh_interpreter_replays_without_clock_or_host(store):
    first = record(store)
    store.close()
    script = """
import importlib,json,sys,types
p=types.ModuleType('isolated_thoughts'); p.__path__=[sys.argv[1]]; sys.modules[p.__name__]=p
m=importlib.import_module(p.__name__+'.inspiration.group_thoughts')
s=m.GroupThoughts(sys.argv[2], clock=lambda: float('nan'))
r=s.record(space_id='a',platform='synthetic',stream_id='stream-a',group_id='group-a',
           owner_id='owner-a',tags=['rain','night','store'],request_id=sys.argv[3])
print(json.dumps(r,ensure_ascii=True)); s.close()
"""
    run = subprocess.run([sys.executable, "-B", "-c", script, str(PLUGIN), str(store.path.parent), token("first")],
                         capture_output=True, text=True, timeout=20, check=True)
    assert json.loads(run.stdout) == first


@pytest.mark.parametrize("field", ["platform", "stream_id", "group_id", "owner_id"])
def test_binding_is_immutable_and_conflicts_do_not_leak_or_write(store, field):
    record(store)
    before = store.path.read_bytes()
    result = record(store, **{field: "wrong"})
    assert result == {"accepted": False, "reason": "binding_conflict", "note_id": None, "summary": None}
    assert store.path.read_bytes() == before
    with sqlite3.connect(store.path) as db, pytest.raises(sqlite3.IntegrityError, match="immutable thought binding"):
        db.execute(f"UPDATE thought_groups SET {field}='other'")


@pytest.mark.parametrize("field", ["stream_id", "group_id"])
def test_alias_space_cannot_bypass_group_quota(store, field):
    record(store)
    args = {field: binding()[field]}
    assert record(store, space="b", **args)["reason"] == "binding_conflict"
    assert rows(store, "SELECT count(*) FROM thought_groups") == [(1,)]


def test_cross_group_and_platform_isolation(store):
    a = record(store)
    b = record(store, space="b")
    c = record(store, space="c", platform="other", stream_id="stream-a", group_id="group-a")
    assert all(item["accepted"] for item in (a, b, c))
    assert len({item["note_id"] for item in (a, b, c)}) == 3
    assert rows(store, "SELECT count(*) FROM thought_requests") == [(3,)]
    assert record(store, tags=["forest"])["reason"] == "request_conflict"
    assert record(store, space="b") == b


def test_rate_limit_is_sliding_persistent_and_rejections_are_not_successes(store, clock):
    first = record(store)
    clock.value = 1599.999
    rejected = record(store, request="blocked", tags=["forest"])
    assert rejected["reason"] == "rate_limited"
    assert rejected["note_id"] is None and rejected["summary"] is None
    assert rows(store, "SELECT record_count FROM thought_topics") == [(1,)]
    assert rows(store, "SELECT last_accepted_at FROM thought_groups") == [(1000,)]
    store.close()
    reopened = GroupThoughts(store.path.parent, clock=clock)
    try:
        clock.value = 1600
        assert record(reopened, request="blocked", tags=["forest"]) == rejected
        assert record(reopened, request="blocked", tags=["coast"])["reason"] == "request_conflict"
        update = record(reopened, request="new")
        assert update["accepted"] and update["note_id"] == first["note_id"]
        assert rows(reopened, "SELECT record_count,last_seen FROM thought_topics") == [(2, 1600)]
        clock.value = 1200  # Rollback across restarts cannot reopen an earlier interval.
        assert record(reopened, request="backwards")["reason"] == "rate_limited"
        clock.value = 2200
        assert record(reopened, request="after-backwards")["accepted"]
    finally:
        reopened.close()


def test_rate_limited_existing_topic_does_not_update_count(store, clock):
    record(store)
    assert record(store, request="no-update")["reason"] == "rate_limited"
    clock.value = 1600
    assert record(store, request="next", tags=["forest"])["accepted"]
    assert rows(store, "SELECT theme_key,record_count FROM thought_topics ORDER BY theme_key") == [
        ("forest", 1), ("night+rain+store", 1),
    ]


def test_topic_capacity_128_preserves_history_and_still_updates_existing(store, clock):
    themes = list(islice(product(
        ["store", "forest", "coast", "lake", "mountain", "street"],
        ["rain", "snow", "mist", "clear"], ["night", "dawn", "sunset"], ["warm", "cool"],
    ), 129))
    for i, tags in enumerate(themes[:128]):
        clock.value = 1000 + i * 600
        assert record(store, request=i, tags=tags)["accepted"]
    clock.value += 600
    assert record(store, request=128, tags=themes[128])["reason"] == "topic_limit"
    assert rows(store, "SELECT count(*),sum(record_count) FROM thought_topics") == [(128, 128)]
    # A rejected 129th theme consumes no successful interval.
    assert record(store, request="existing", tags=themes[0])["reason"] == "updated"
    assert rows(store, "SELECT count(*),sum(record_count) FROM thought_topics") == [(128, 129)]
    store.close()
    reopened = GroupThoughts(store.path.parent, clock=clock)
    try:
        assert rows(reopened, "SELECT count(*) FROM thought_topics") == [(128,)]
    finally:
        reopened.close()


def test_request_capacity_is_bounded_without_eviction_and_replays_survive_full(store, clock):
    first = record(store)
    # Seed realistic receipts directly to test the real 10000 boundary cheaply.
    with sqlite3.connect(store.path) as db:
        receipt = db.execute("SELECT * FROM thought_requests").fetchone()
        db.executemany("INSERT INTO thought_requests VALUES(?,?,?,?,?,?,?,?)", (
            (receipt[0], token(i), *receipt[2:]) for i in range(9999)
        ))
    clock.value += 600
    assert record(store, request="over-cap")["reason"] == "request_limit"
    assert record(store) == first
    assert record(store, tags=["forest"])["reason"] == "request_conflict"
    assert rows(store, "SELECT count(*) FROM thought_requests") == [(10000,)]
    assert rows(store, "SELECT record_count FROM thought_topics") == [(1,)]
    assert record(store, space="other")["accepted"]
    store.close()
    reopened = GroupThoughts(store.path.parent, clock=clock)
    try:
        assert record(reopened) == first
        assert record(reopened, request="another")["reason"] == "request_limit"
    finally:
        reopened.close()


def test_two_connections_racing_new_requests_admit_only_one(tmp_path, clock):
    a = GroupThoughts(tmp_path, clock=clock)
    b = GroupThoughts(tmp_path, clock=clock)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: record(a if i % 2 else b, request=i), range(16)))
        assert sum(item["accepted"] for item in results) == 1
        assert rows(a, "SELECT count(*) FROM thought_requests") == [(16,)]
        assert rows(a, "SELECT record_count FROM thought_topics") == [(1,)]
    finally:
        a.close()
        b.close()


def test_concurrent_same_request_is_one_successful_receipt(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: record(store), range(16)))
    assert all(result == results[0] for result in results)
    assert rows(store, "SELECT record_count FROM thought_topics") == [(1,)]
    assert rows(store, "SELECT count(*) FROM thought_requests") == [(1,)]


@pytest.mark.parametrize("tags", [None, "rain", {}, [], ["member said rain"], ["rain", 2],
                                   ["rain", "clear"], ["forest", "coast"], ["night", "dawn"],
                                   ["warm", "cool"], ["RAIN"], ["snow"] * 17])
def test_invalid_tags_never_persist_or_bind(store, tags):
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="tags"):
        record(store, tags=tags)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("field", ["space_id", "platform", "stream_id", "group_id", "owner_id"])
def test_invalid_ids_never_persist(store, field):
    for invalid in (None, "", " ", "x" * 257, "nul\0id"):
        with pytest.raises(ValueError, match=field):
            record(store, **{field: invalid})
    assert rows(store, "SELECT count(*) FROM thought_groups") == [(0,)]


@pytest.mark.parametrize("value", [None, "", "raw-token", "x" * 64, "f" * 63, "0" * 65])
def test_request_must_be_a_token_hash(store, value):
    with pytest.raises(ValueError, match="request_id"):
        record(store, request_id=value)
    assert rows(store, "SELECT count(*) FROM thought_requests") == [(0,)]


def test_hash_casing_cannot_bypass_idempotency(store):
    first = record(store)
    assert record(store, request_id=token("first").upper()) == first
    assert rows(store, "SELECT count(*) FROM thought_requests") == [(1,)]


@pytest.mark.parametrize("value", [True, None, "1000", float("nan"), float("inf"), -1])
def test_invalid_clock_rolls_back_without_consuming_request(store, clock, value):
    clock.value = value
    with pytest.raises(ValueError, match="clock"):
        record(store)
    assert rows(store, "SELECT count(*) FROM thought_groups") == [(0,)]
    clock.value = 0
    assert record(store)["accepted"]


@pytest.mark.parametrize("existing", [False, True])
def test_sql_failure_rolls_back_binding_topic_receipt_and_throttle(store, clock, existing):
    if existing:
        record(store, request="original")
        clock.value += 600
    store._db.execute("""CREATE TRIGGER fail_request BEFORE INSERT ON thought_requests
                       BEGIN SELECT RAISE(ABORT, 'injected transaction failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected transaction failure"):
        record(store)
    assert rows(store, "SELECT count(*) FROM thought_requests") == [(int(existing),)]
    assert rows(store, "SELECT count(*),coalesce(sum(record_count),0) FROM thought_topics") == [
        (int(existing), int(existing)),
    ]
    assert rows(store, "SELECT last_accepted_at FROM thought_groups") == ([(1000,)] if existing else [])
    store._db.execute("DROP TRIGGER fail_request")
    assert record(store)["accepted"]


def test_commit_failure_is_not_reported_as_accepted_and_can_retry(store, monkeypatch):
    real = store._db

    class FailedCommit:
        execute = real.execute
        rollback = real.rollback

        def commit(self):
            raise sqlite3.OperationalError("injected commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_db", FailedCommit())
        with pytest.raises(sqlite3.OperationalError, match="commit failure"):
            record(store)
    assert rows(store, "SELECT count(*) FROM thought_groups") == [(0,)]
    assert record(store)["accepted"]


def link_or_skip(link, target, kind="hardlink", directory=False):
    try:
        if kind == "symlink":
            link.symlink_to(target, target_is_directory=directory)
        else:
            os.link(target, link)
    except OSError as exc:
        if exc.errno not in (1, 13, 95) and getattr(exc, "winerror", None) not in (5, 1314):
            raise
        pytest.skip("OS link permission unavailable")


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
@pytest.mark.parametrize("kind", ["hardlink", "symlink"])
def test_linked_database_and_sidecars_preserve_external_file(tmp_path, suffix, kind):
    root = tmp_path / "data"
    root.mkdir()
    outside = tmp_path / "sentinel"
    outside.write_bytes(b"must remain unchanged")
    link_or_skip(root / (thoughts.DATABASE_NAME + suffix), outside, kind)
    with error("unsafe_path"):
        GroupThoughts(root)
    assert outside.read_bytes() == b"must remain unchanged"
    if suffix:
        assert not (root / thoughts.DATABASE_NAME).exists()


def test_linked_parent_and_dangling_sidecar_do_not_create_external_files(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    link_or_skip(linked, outside, "symlink", directory=True)
    with error("unsafe_path"):
        GroupThoughts(linked)
    assert not list(outside.iterdir())
    root = tmp_path / "data"
    root.mkdir()
    missing = outside / "missing"
    link_or_skip(root / (thoughts.DATABASE_NAME + "-journal"), missing, "symlink")
    with error("unsafe_path"):
        GroupThoughts(root)
    assert not missing.exists()


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_unexpected_regular_wal_sidecar_is_not_checkpointed_or_deleted(tmp_path, suffix):
    sidecar = tmp_path / (thoughts.DATABASE_NAME + suffix)
    sidecar.write_bytes(b"unowned sidecar")
    with error("unsafe_path"):
        GroupThoughts(tmp_path)
    assert sidecar.read_bytes() == b"unowned sidecar"
    assert not (tmp_path / thoughts.DATABASE_NAME).exists()


def test_wal_header_without_sidecars_is_rejected_before_sqlite_creates_anything(tmp_path):
    path = tmp_path / thoughts.DATABASE_NAME
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sentinel(value TEXT)")
    data = bytearray(path.read_bytes())
    data[18:20] = b"\x02\x02"
    path.write_bytes(data)
    with error("schema_mismatch"):
        GroupThoughts(tmp_path)
    assert path.read_bytes() == bytes(data)
    assert {item.name for item in tmp_path.iterdir()} == {thoughts.DATABASE_NAME}


def test_live_link_blocks_record_and_close_without_changing_either_database(store, tmp_path):
    record(store)
    outside = tmp_path / "sentinel"
    outside.write_bytes(b"safe")
    sidecar = Path(str(store.path) + "-journal")
    link_or_skip(sidecar, outside)
    before = store.path.read_bytes()
    try:
        with error("unsafe_path"):
            record(store, request="blocked")
        with error("unsafe_path"):
            store.close()
        assert store.path.read_bytes() == before
        assert outside.read_bytes() == b"safe"
    finally:
        sidecar.unlink()
    assert record(store)["accepted"]


def test_simulated_reparse_parent_and_replaced_database_are_rejected(store, monkeypatch):
    original = Path.lstat
    for target, attr in ((store.path.parent, "st_file_attributes"), (store.path, "st_ino")):
        def lstat(path, *args, target=target, attr=attr, **kwargs):
            info = original(path, *args, **kwargs)
            if path != target:
                return info
            data = {"st_mode": info.st_mode, "st_nlink": info.st_nlink, "st_dev": info.st_dev,
                    "st_ino": info.st_ino, "st_file_attributes": getattr(info, "st_file_attributes", 0)}
            data[attr] = 1024 if attr == "st_file_attributes" else info.st_ino + 1
            return SimpleNamespace(**data)
        with monkeypatch.context() as patch:
            patch.setattr(Path, "lstat", lstat)
            with error("unsafe_path"):
                record(store)
    assert record(store)["accepted"]


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
def test_simulated_multiple_links_block_before_open_without_modification(tmp_path, monkeypatch, suffix):
    target = tmp_path / (thoughts.DATABASE_NAME + suffix)
    target.write_bytes(b"independent sentinel; only link metadata simulated")
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == target:
            return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino,
                                   st_nlink=2, st_file_attributes=getattr(info, "st_file_attributes", 0))
        return info

    monkeypatch.setattr(Path, "lstat", lstat)
    before = target.read_bytes()
    with error("unsafe_path"):
        GroupThoughts(tmp_path)
    assert target.read_bytes() == before
    if suffix:
        assert not (tmp_path / thoughts.DATABASE_NAME).exists()


def test_simulated_linked_live_journal_blocks_record_and_close(store, monkeypatch):
    record(store)
    journal = Path(str(store.path) + "-journal")
    journal.write_bytes(b"simulated linked journal sentinel")
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == journal:
            return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino,
                                   st_nlink=2, st_file_attributes=getattr(info, "st_file_attributes", 0))
        return info

    before = store.path.read_bytes(), journal.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", lstat)
        with error("unsafe_path"):
            record(store, request="blocked")
        with error("unsafe_path"):
            store.close()
        assert (store.path.read_bytes(), journal.read_bytes()) == before
    journal.unlink()
    assert record(store)["accepted"]


def test_unsafe_storage_mid_transaction_cannot_commit_and_closes_after_restoration(store, monkeypatch):
    original = store._assert_safe_storage
    checks = 0

    def safety_check():
        nonlocal checks
        checks += 1
        # record() check, BEGIN check, then pre-COMMIT check and error check.
        if checks >= 3:
            raise ThoughtsError("unsafe_path")
        original()

    with monkeypatch.context() as patch:
        patch.setattr(store, "_assert_safe_storage", safety_check)
        with error("unsafe_path"):
            record(store)
    # Readers cannot see the uncommitted topic or receipt. Safe close rolls back.
    assert rows(store, "SELECT count(*) FROM thought_topics") == [(0,)]
    store.close()
    reopened = GroupThoughts(store.path.parent)
    try:
        assert record(reopened)["accepted"]
        assert rows(reopened, "SELECT record_count FROM thought_topics") == [(1,)]
    finally:
        reopened.close()


@pytest.mark.parametrize("kind", ["foreign", "future", "missing_trigger", "corrupt"])
def test_unknown_schema_or_corruption_is_preserved(tmp_path, kind):
    path = tmp_path / thoughts.DATABASE_NAME
    if kind == "corrupt":
        path.write_bytes(b"not a SQLite database")
    elif kind == "missing_trigger":
        writer = GroupThoughts(tmp_path)
        record(writer)
        writer.close()
        with sqlite3.connect(path) as db:
            db.execute("DROP TRIGGER thought_binding_immutable")
    else:
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE sentinel(value TEXT)")
            db.execute("INSERT INTO sentinel VALUES('preserve')")
            if kind == "future":
                db.execute("PRAGMA user_version=99")
    before = path.read_bytes()
    with pytest.raises((ThoughtsError, sqlite3.DatabaseError)):
        GroupThoughts(tmp_path)
    assert path.read_bytes() == before


def test_notebook_and_artist_sentinels_are_never_opened_or_changed(tmp_path, clock):
    for name in ("group_notebook.sqlite3", "artist.sqlite3"):
        (tmp_path / name).write_bytes(b"unrelated private state")
    store = GroupThoughts(tmp_path, clock=clock)
    assert record(store)["accepted"]
    store.close()
    assert (tmp_path / "group_notebook.sqlite3").read_bytes() == b"unrelated private state"
    assert (tmp_path / "artist.sqlite3").read_bytes() == b"unrelated private state"


def test_close_is_idempotent_and_record_after_close_fails(store):
    record(store)
    store.close()
    store.close()
    with error("closed"):
        record(store)
