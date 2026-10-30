"""Integration boundaries independent of the ledger implementation tests."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import sqlite3

import pytest

from image_plugin_under_test.policy import Delegation, Limits, Scope, StoreError
from image_plugin_under_test.store import ArtistStore


SCOPE = Scope("test-platform", "verified-stream", "verified-owner")


@pytest.fixture
def store(tmp_path):
    ledger = ArtistStore(tmp_path / "artist.sqlite3")
    ledger.create_space("studio", SCOPE, binding_id="synthetic-service")
    ledger.grant("studio", SCOPE, Delegation("explicit-host-consent", "trusted-admin", continuous=True))
    limits = Limits(think=10, render=10, share=10, max_inflight=10, max_revisions=2)
    ledger.set_global_limits(limits)
    ledger.set_limits("studio", SCOPE, limits)
    ledger.create_work("studio", SCOPE, "work")
    yield ledger
    ledger.close()


def test_same_request_race_then_only_one_dispatch(store):
    """Repeated delivery of one wake must not create two budget holds or dispatches."""
    barrier = Barrier(2)

    def attempt(phase):
        connection = ArtistStore(store.path)
        try:
            barrier.wait(timeout=5)
            if phase == "reserve":
                return connection.reserve(
                    "studio", SCOPE, request_id="stable-request", action="submit",
                    work_id="work", params={"prompt": "synthetic description"},
                )["id"]
            try:
                connection.mark_submitting("studio", SCOPE, "stable-request")
                return "started"
            except StoreError as exc:
                return exc.code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        operation_ids = list(pool.map(attempt, ["reserve", "reserve"]))
    assert operation_ids[0] == operation_ids[1]
    assert store.budget_usage("studio", SCOPE)["render"]["held"] == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["start", "start"]))
    assert sorted(results) == ["INVALID_STATE", "started"]


def test_legacy_backup_includes_committed_wal_rows(store, tmp_path):
    """Copying only the database file would lose these committed send locks."""
    source, backup = tmp_path / "legacy.sqlite3", tmp_path / "snapshot.sqlite3"
    legacy = sqlite3.connect(source)
    try:
        legacy.execute("PRAGMA journal_mode=WAL")
        legacy.execute("PRAGMA wal_autocheckpoint=0")
        legacy.execute("CREATE TABLE delivery(job TEXT PRIMARY KEY, state TEXT NOT NULL)")
        legacy.commit()
        legacy.execute("INSERT INTO delivery VALUES('wal-job', 'send_unknown')")
        legacy.commit()
        assert source.with_name(source.name + "-wal").stat().st_size > 0
        # No writer is active; the idle connection keeps the WAL from checkpointing.
        store.migrate_legacy_deliveries(source, backup, offline=True)
        assert store.legacy_lock("studio", SCOPE, "wal-job")
        snapshot = sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)
        try:
            assert snapshot.execute("SELECT job,state FROM delivery").fetchall() == [("wal-job", "send_unknown")]
        finally:
            snapshot.close()
        assert legacy.execute("SELECT job,state FROM delivery").fetchall() == [("wal-job", "send_unknown")]
    finally:
        legacy.close()


def test_destination_failure_rolls_back_entire_migration(store, tmp_path):
    """A destination write failure must not leave a partial set of legacy locks."""
    source, backup = tmp_path / "legacy.sqlite3", tmp_path / "snapshot.sqlite3"
    legacy = sqlite3.connect(source)
    with legacy:
        legacy.execute("CREATE TABLE delivery(job TEXT PRIMARY KEY, state TEXT NOT NULL)")
        legacy.executemany("INSERT INTO delivery VALUES(?,?)", [("a", "sent"), ("b", "sending")])
    legacy.close()
    source_bytes = source.read_bytes()
    store.db.execute("""CREATE TRIGGER synthetic_write_failure BEFORE INSERT ON legacy_locks
                      WHEN NEW.job='b' BEGIN SELECT RAISE(ABORT, 'synthetic disk failure'); END""")
    with pytest.raises(StoreError):
        store.migrate_legacy_deliveries(source, backup, offline=True)
    assert store.db.execute("SELECT count(*) FROM legacy_locks").fetchone()[0] == 0
    assert store.db.execute("SELECT count(*) FROM legacy_migrations").fetchone()[0] == 0
    assert source.read_bytes() == source_bytes
    snapshot = sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)
    try:
        assert snapshot.execute("SELECT count(*) FROM delivery").fetchone()[0] == 2
    finally:
        snapshot.close()
