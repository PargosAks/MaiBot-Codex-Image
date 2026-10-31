"""Persistent artist state, with synthetic SQLite files and no external clients."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
import hashlib
import sqlite3

import pytest

from image_plugin_under_test import store as store_module
from image_plugin_under_test.policy import Delegation, Limits, Scope, StoreError
from image_plugin_under_test.store import ArtistStore


SCOPE = Scope("test", "verified-stream", "verified-owner")
OTHER = Scope("test", "other-stream", "other-owner")
BRIEF = {
    "intent": "study light",
    "subject": "window",
    "mood": "quiet",
    "prompt": "soft light",
    "completion_criteria": ["recognizable window", "warm lighting"],
}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 31, 4, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def ledger(tmp_path):
    clock = Clock()
    db = ArtistStore(tmp_path / "artist.sqlite3", clock=clock)
    db.create_space("s", SCOPE, binding_id="synthetic-binding")
    db.grant("s", SCOPE, Delegation("explicit-ongoing-consent", "trusted-adapter", continuous=True))
    limits = Limits(think=50, render=50, share=0, max_inflight=50, max_revisions=5)
    db.set_global_limits(limits)
    db.set_limits("s", SCOPE, limits)
    yield db, clock
    db.close()


def error(code):
    return pytest.raises(StoreError, check=lambda exc: exc.code == code)


def render(db, *, work="w", request="r", job="job"):
    db.create_work("s", SCOPE, work)
    db.reserve("s", SCOPE, request_id=request, action="submit", work_id=work, params={"prompt": "synthetic"})
    db.mark_submitting("s", SCOPE, request)
    db.mark_accepted("s", SCOPE, request, job_id=job)


def version(db, **kwargs):
    defaults = {
        "work_id": "w",
        "version_id": "v",
        "request_id": "r",
        "job_id": "job",
        "data": b"synthetic image bytes, not decoded",
        "mime": "image/png",
    }
    defaults.update(kwargs)
    return db.record_version("s", SCOPE, **defaults)


def thought(db, request="review", work="w"):
    db.reserve("s", SCOPE, request_id=request, action="think", work_id=work, params={"purpose": "review"})
    db.mark_submitting("s", SCOPE, request)


def test_wake_watermark_dedup_and_late_old_finish(ledger):
    db, _ = ledger
    assert db.enqueue_wake(
        "s", SCOPE, event_id="chat-1", kind="chat", text="a window", source_ref="host-msg-1"
    )
    assert not db.enqueue_wake(
        "s", SCOPE, event_id="chat-1", kind="chat", text="a window", source_ref="host-msg-1"
    )
    with error("REQUEST_CONFLICT"):
        db.enqueue_wake("s", SCOPE, event_id="chat-1", kind="chat", text="different", source_ref="host-msg-1")
    with error("INVALID_ARGUMENT"):
        db.enqueue_wake("s", SCOPE, event_id="bad", kind="chat")
    first = db.claim_wake("s", SCOPE, token="first")
    assert first["auth_version"] == 1
    assert db.claim_wake("s", SCOPE, token="first") == first
    assert db.claim_wake("s", SCOPE, token="second") is None
    db.enqueue_wake("s", SCOPE, event_id="chat-2", kind="chat", source_ref="host-msg-2")
    db.finish_wake("s", SCOPE, token="first")
    assert db.claim_wake("s", SCOPE, token="first") is None
    second = db.claim_wake("s", SCOPE, token="second")
    assert [event["event_id"] for event in second["events"]] == ["chat-2"]
    db.finish_wake("s", SCOPE, token="first")  # Must not ack the newer claim.
    assert db.claim_wake("s", SCOPE, token="second") == second
    db.finish_wake("s", SCOPE, token="second")
    assert db.claim_wake("s", SCOPE, token="third") is None
    assert not db.enqueue_wake(
        "s", SCOPE, event_id="chat-1", kind="chat", text="a window", source_ref="host-msg-1"
    )


def test_wake_batches_are_bounded_and_preserve_all_backlog(ledger):
    db, _ = ledger
    ids = [f"event-{index}" for index in range(130)]
    for event_id in ids:
        db.enqueue_wake("s", SCOPE, event_id=event_id, kind="completion")
    first = db.claim_wake("s", SCOPE, token="batch-1")
    assert [event["event_id"] for event in first["events"]] == ids[:64]
    db.enqueue_wake("s", SCOPE, event_id="late", kind="completion")
    db.finish_wake("s", SCOPE, token="batch-1")
    second = db.claim_wake("s", SCOPE, token="batch-2")
    assert [event["event_id"] for event in second["events"]] == ids[64:128]
    assert first["through"] < second["through"]
    db.finish_wake("s", SCOPE, token="batch-2")
    third = db.claim_wake("s", SCOPE, token="batch-3")
    assert [event["event_id"] for event in third["events"]] == ids[128:] + ["late"]
    db.finish_wake("s", SCOPE, token="batch-3")
    assert db.claim_wake("s", SCOPE, token="empty") is None


def test_wake_input_character_limits(ledger):
    db, _ = ledger
    for values in (
        {"event_id": "e" * 201, "source_ref": "source", "text": ""},
        {"event_id": "event", "source_ref": "s" * 201, "text": ""},
        {"event_id": "event", "source_ref": "source", "text": "绘" * 4097},
    ):
        with error("INVALID_ARGUMENT"):
            db.enqueue_wake("s", SCOPE, kind="chat", **values)
    assert db.enqueue_wake(
        "s", SCOPE, kind="chat", event_id="e" * 200, source_ref="s" * 200, text="绘" * 4096
    )
    assert len(db.claim_wake("s", SCOPE, token="valid")["events"]) == 1


def test_two_connections_only_one_claim_and_recovery_is_explicit(ledger):
    db, clock = ledger
    db.enqueue_wake("s", SCOPE, event_id="event", kind="idle")
    render(db)
    barrier = Barrier(2)

    def worker(token):
        second = ArtistStore(db.path, clock=clock)
        try:
            barrier.wait(timeout=5)
            return second.claim_wake("s", SCOPE, token=token)
        finally:
            second.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(worker, ["one", "two"]))
    assert sum(claim is not None for claim in claims) == 1
    held = next(claim for claim in claims if claim)
    second = ArtistStore(db.path, clock=clock)
    try:
        assert second.claim_wake("s", SCOPE, token="third") is None
        assert second.get_operation("s", SCOPE, "r")["state"] == "accepted"
        with error("OFFLINE_REQUIRED"):
            second.recover_artist("s", SCOPE)
        assert second.recover_artist("s", SCOPE, offline=True) == 1
        assert db.get_operation("s", SCOPE, "r")["state"] == "unknown"
        with error("CLAIM_MISMATCH"):
            db.finish_wake("s", SCOPE, token=held["token"])
        recovered = db.claim_wake("s", SCOPE, token="new-worker")
        assert recovered["events"] == held["events"]
    finally:
        second.close()


def test_idle_schedule_merges_and_never_replays_missed_ticks(ledger):
    db, clock = ledger
    assert db.schedule_wake("s", SCOPE, interval_seconds=3600)
    assert not db.schedule_wake("s", SCOPE, interval_seconds=3600)
    clock.now += timedelta(days=10)
    assert not db.schedule_wake("s", SCOPE, interval_seconds=3600)  # Existing idle coalesces.
    batch = db.claim_wake("s", SCOPE, token="batch")
    assert len(batch["events"]) == 1
    db.finish_wake("s", SCOPE, token="batch")
    assert not db.schedule_wake("s", SCOPE, interval_seconds=3600)
    clock.now += timedelta(hours=1)
    assert db.schedule_wake("s", SCOPE, interval_seconds=3600)
    assert len(db.claim_wake("s", SCOPE, token="next")["events"]) == 1


def test_failed_wake_backoff_survives_reopen_and_keeps_new_events(ledger):
    db, clock = ledger
    db.enqueue_wake("s", SCOPE, event_id="message", kind="chat", source_ref="verified-ref")
    batch = db.claim_wake("s", SCOPE, token="failed")
    db.enqueue_wake("s", SCOPE, event_id="new-message", kind="chat", source_ref="verified-new-ref")
    db.finish_wake("s", SCOPE, token="failed", acknowledge=False, retry_after=300)
    second = ArtistStore(db.path, clock=clock)
    try:
        assert second.schedule_wake("s", SCOPE, interval_seconds=60)
        assert second.claim_wake("s", SCOPE, token="early") is None
        clock.now += timedelta(seconds=299)
        assert second.claim_wake("s", SCOPE, token="still-early") is None
        clock.now += timedelta(seconds=1)
        next_batch = second.claim_wake("s", SCOPE, token="retry")
        assert next_batch["through"] > batch["through"]
        assert [event["event_id"] for event in next_batch["events"]][:2] == ["message", "new-message"]
        assert len(next_batch["events"]) == 3
    finally:
        second.close()


def test_inspiration_merges_sources_expires_and_caps_at_100(ledger):
    db, clock = ledger
    db.remember_inspiration("s", SCOPE, key="window", summary="light", source_refs=["a"], ttl_seconds=60)
    merged = db.remember_inspiration(
        "s", SCOPE, key="window", summary="warm light", source_refs=["a", "b"], ttl_seconds=60
    )
    assert merged["source_refs"] == ["a", "b"]
    with error("REQUEST_CONFLICT"):
        db.remember_inspiration(
            "s", SCOPE, key="window", summary="not an experience", source_refs=[], origin="idle"
        )
    clock.now += timedelta(seconds=60)
    assert db.list_inspirations("s", SCOPE) == []
    for index in range(101):
        db.remember_inspiration("s", SCOPE, key=str(index), summary="practice", source_refs=[], origin="idle")
    ideas = db.list_inspirations("s", SCOPE)
    assert len(ideas) == 100 and "0" not in {idea["key"] for idea in ideas}
    assert all(idea["origin"] == "idle" and idea["source_refs"] == [] for idea in ideas)


def test_plan_brief_immutable_consumption_and_one_active_work(ledger):
    db, _ = ledger
    db.remember_inspiration("s", SCOPE, key="idea", summary="warm window", source_refs=["real-msg"])
    plan = db.plan_work("s", SCOPE, work_id="w", brief=BRIEF, inspiration_keys=["idea"])
    assert plan["status"] == "planned" and plan["brief"] == BRIEF
    assert all(
        plan[field] is None
        for field in (
            "reason",
            "pending_request",
            "parent_version",
            "edit_prompt",
            "best_version",
            "next_check_at",
        )
    )
    assert db.list_inspirations("s", SCOPE)[0]["consumed_by"] == "w"
    assert db.plan_work("s", SCOPE, work_id="w", brief=BRIEF, inspiration_keys=["idea"]) == plan
    with error("REQUEST_CONFLICT"):
        db.plan_work("s", SCOPE, work_id="w", brief={**BRIEF, "prompt": "changed"}, inspiration_keys=["idea"])
    with error("INVALID_ARGUMENT"):
        db.update_artist_work("s", SCOPE, "w", changes={"brief": {**BRIEF, "prompt": "changed"}})
    with error("WORK_BUSY"):
        db.plan_work("s", SCOPE, work_id="second", brief=BRIEF, inspiration_keys=[])
    db.update_artist_work("s", SCOPE, "w", changes={"status": "finished"})
    db.plan_work("s", SCOPE, work_id="second", brief=BRIEF, inspiration_keys=[])
    with error("WORK_BUSY"):
        db.update_artist_work("s", SCOPE, "w", changes={"status": "planned"})
    assert len(db.list_artist_works("s", SCOPE)) == 2


def test_expected_authorization_prevents_old_responses_after_regrant(ledger):
    db, _ = ledger
    db.enqueue_wake("s", SCOPE, event_id="wake", kind="idle")
    claimed = db.claim_wake("s", SCOPE, token="worker")
    db.revoke("s", SCOPE, source_ref="withdrawn", granted_by="trusted-adapter")
    db.grant("s", SCOPE, Delegation("new explicit consent", "trusted-adapter", continuous=True))
    assert db.claim_wake("s", SCOPE, token="worker")["auth_version"] == claimed["auth_version"]
    for call in (
        lambda: db.reserve(
            "s", SCOPE, request_id="old-thought", action="think", params={}, expected_auth_version=1
        ),
        lambda: db.plan_work(
            "s", SCOPE, work_id="old-plan", brief=BRIEF, inspiration_keys=[], expected_auth_version=1
        ),
        lambda: db.remember_inspiration(
            "s", SCOPE, key="old-idea", summary="old", source_refs=["ref"], expected_auth_version=1
        ),
    ):
        with error("AUTH_CHANGED"):
            call()
    db.plan_work("s", SCOPE, work_id="new-plan", brief=BRIEF, inspiration_keys=[], expected_auth_version=3)


def test_revocation_blocks_new_decisions_but_retains_results(ledger):
    db, _ = ledger
    db.plan_work("s", SCOPE, work_id="w", brief=BRIEF, inspiration_keys=[])
    render(db)
    thought(db)
    db.enqueue_wake("s", SCOPE, event_id="idle", kind="idle")
    db.revoke("s", SCOPE, source_ref="withdrawn", granted_by="trusted-adapter")
    for call in (
        lambda: db.enqueue_wake("s", SCOPE, event_id="new", kind="idle"),
        lambda: db.schedule_wake("s", SCOPE, interval_seconds=60),
        lambda: db.claim_wake("s", SCOPE, token="new"),
        lambda: db.remember_inspiration("s", SCOPE, key="new", summary="new", source_refs=[], origin="idle"),
        lambda: db.list_inspirations("s", SCOPE),
        lambda: db.plan_work("s", SCOPE, work_id="new", brief=BRIEF, inspiration_keys=[]),
        lambda: db.update_artist_work("s", SCOPE, "w", changes={"status": "planned"}),
    ):
        with error("AUTH_REQUIRED"):
            call()
    version(db)
    db.record_review("s", SCOPE, version_id="v", request_id="review", review={"decision": "keep"})
    assert (
        db.update_artist_work("s", SCOPE, "w", changes={"reason": "retained after withdrawal"})["reason"]
        == "retained after withdrawal"
    )
    final = db.update_artist_work(
        "s", SCOPE, "w", changes={"status": "finished", "best_version": "v", "pending_request": None}
    )
    assert final["best_version"] == "v"


def test_version_and_review_bindings_are_immutable(ledger):
    db, _ = ledger
    render(db)
    original = version(db)
    assert original["sha256"] == hashlib.sha256(original["data"]).hexdigest()
    assert version(db) == original
    for kwargs in ({"data": b"different"}, {"version_id": "second-for-same-request"}):
        with error("REQUEST_CONFLICT"):
            version(db, **kwargs)
    with error("NOT_FOUND"):
        version(db, job_id="unrelated-job")
    with error("INVALID_ARGUMENT"):
        version(db, parent_id="v")
    with error("INVALID_ARGUMENT"):
        version(db, data=b"x" * (8 * 1024 * 1024 + 1))
    metadata = db.list_versions("s", SCOPE, "w")[0]
    assert "data" not in metadata and metadata["sha256"] == original["sha256"]
    thought(db)
    review = db.record_review("s", SCOPE, version_id="v", request_id="review", review={"decision": "keep"})
    assert (
        db.record_review("s", SCOPE, version_id="v", request_id="review", review={"decision": "keep"})
        == review
    )
    with error("REQUEST_CONFLICT"):
        db.record_review("s", SCOPE, version_id="v", request_id="review", review={"decision": "revise"})
    thought(db, "failed-review")
    db.settle("s", SCOPE, "failed-review", outcome="failed", evidence="model returned invalid review")
    with error("NOT_FOUND"):
        db.record_review("s", SCOPE, version_id="v", request_id="failed-review", review={})
    assert db.list_reviews("s", SCOPE, "w") == [review]


@pytest.mark.parametrize("state", ["accepted", "unknown", "succeeded", "failed"])
def test_artifact_persists_independently_from_render_completion(ledger, state):
    db, _ = ledger
    render(db)
    if state == "unknown":
        db.mark_unknown("s", SCOPE, "r", reason="turn completion lost, image available")
    elif state != "accepted":
        db.settle("s", SCOPE, "r", outcome=state, evidence="known turn outcome, image available")
    saved = version(db)
    assert version(db) == saved
    assert db.get_version("s", SCOPE, "v")["data"] == saved["data"]
    assert db.get_operation("s", SCOPE, "r")["state"] == state
    assert len(db.list_versions("s", SCOPE, "w")) == 1


def test_version_parent_and_workflow_references_stay_with_work(ledger):
    db, _ = ledger
    db.plan_work("s", SCOPE, work_id="w", brief=BRIEF, inspiration_keys=[])
    render(db)
    version(db)
    render(db, work="other-work", request="other-request", job="other-job")
    version(
        db, work_id="other-work", version_id="other-version", request_id="other-request", job_id="other-job"
    )
    for changes in (
        {"parent_version": "other-version"},
        {"best_version": "other-version"},
        {"pending_request": "other-request"},
    ):
        with error("NOT_FOUND"):
            db.update_artist_work("s", SCOPE, "w", changes=changes)
    db.settle("s", SCOPE, "r", outcome="succeeded", evidence="terminal job")
    render(db, request="r2", job="job2")
    with error("NOT_FOUND"):
        version(db, version_id="v2", request_id="r2", job_id="job2", parent_id="other-version")
    child = version(db, version_id="v2", request_id="r2", job_id="job2", parent_id="v")
    assert child["parent_id"] == "v"
    updated = db.update_artist_work(
        "s", SCOPE, "w", changes={"parent_version": "v", "best_version": "v2", "pending_request": "r2"}
    )
    assert updated["brief"] == BRIEF
    assert (
        db.update_artist_work("s", SCOPE, "w", changes={"pending_request": None})["pending_request"] is None
    )


def test_all_new_apis_are_space_isolated(ledger):
    db, _ = ledger
    db.plan_work("s", SCOPE, work_id="w", brief=BRIEF, inspiration_keys=[])
    render(db)
    version(db)
    db.create_space("other", OTHER, binding_id="synthetic-binding")
    db.grant("other", OTHER, Delegation("other-consent", "adapter", continuous=True))
    db.remember_inspiration("other", OTHER, key="private", summary="private", source_refs=["private-ref"])
    for call in (
        lambda: db.enqueue_wake("s", OTHER, event_id="bad", kind="idle"),
        lambda: db.claim_wake("s", OTHER, token="bad"),
        lambda: db.finish_wake("s", OTHER, token="bad"),
        lambda: db.list_inspirations("s", OTHER),
        lambda: db.get_artist_work("s", OTHER, "w"),
        lambda: db.list_artist_works("s", OTHER),
        lambda: db.get_version("s", OTHER, "v"),
        lambda: db.list_versions("s", OTHER, "w"),
        lambda: db.list_reviews("s", OTHER, "w"),
        lambda: db.update_artist_work("s", OTHER, "w", changes={"status": "abandoned"}),
        lambda: db.plan_work("s", SCOPE, work_id="w", brief=BRIEF, inspiration_keys=["private"]),
    ):
        with pytest.raises(StoreError):
            call()
    assert db.get_artist_work("s", SCOPE, "w")["status"] == "planned"


def create_v1(path):
    db = sqlite3.connect(path)
    for statement in store_module._SCHEMA:
        db.execute(statement)
    db.execute("INSERT INTO spaces VALUES('s',?,'old-binding',1,1)", (SCOPE.snapshot(),))
    db.execute("INSERT INTO grants VALUES('s',1,1,'old-consent','adapter','2026-10-30')")
    db.execute("INSERT INTO limits VALUES('*',10,10,10,10,3)")
    db.execute("INSERT INTO limits VALUES('s',10,10,10,10,3)")
    db.execute("INSERT INTO works VALUES('s','old-work','2026-10-30')")
    db.execute(
        """INSERT INTO operations VALUES(1,'s','old-work','old-request','submit','render','{}',?,
               'old-binding',1,'chat','source-message','unknown','2026-10-30','old-job','timeout','then','then')""",
        (SCOPE.snapshot(),),
    )
    db.execute("INSERT INTO legacy_locks VALUES('sent-job','sending','send_unknown','digest')")
    db.execute("PRAGMA user_version=1")
    db.commit()
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    before = {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    db.close()
    return before


def test_schema_1_upgrade_preserves_all_old_records_and_is_passive(tmp_path):
    path = tmp_path / "v1.sqlite3"
    before = create_v1(path)
    db = ArtistStore(path)
    try:
        assert db.db.execute("PRAGMA user_version").fetchone()[0] == store_module.SCHEMA_VERSION
        for table, rows in before.items():
            assert [tuple(row) for row in db.db.execute(f"SELECT * FROM {table}")] == rows
        assert db.get_operation("s", SCOPE, "old-request")["state"] == "unknown"
        assert db.legacy_lock("s", SCOPE, "sent-job")
        assert db.list_artist_works("s", SCOPE) == []
        assert db.get_work("s", SCOPE, "old-work")["work_id"] == "old-work"
    finally:
        db.close()


def test_schema_upgrade_failure_rolls_back_ddl_and_version(tmp_path, monkeypatch):
    path = tmp_path / "v1.sqlite3"
    before = create_v1(path)
    with monkeypatch.context() as patch:
        patch.setattr(
            store_module, "_SCHEMA_2", (*store_module._SCHEMA_2[:2], "CREATE TABLE works(bad TEXT)")
        )
        with pytest.raises(sqlite3.DatabaseError):
            ArtistStore(path)
    read = sqlite3.connect(path)
    try:
        assert read.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = [row[0] for row in read.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        assert set(tables) == set(before)
        for table, rows in before.items():
            assert read.execute(f"SELECT * FROM {table}").fetchall() == rows
    finally:
        read.close()
    ArtistStore(path).close()
