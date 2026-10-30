"""Offline policy/ledger tests using synthetic SQLite files, never a running Host."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
import hashlib
import sqlite3

import pytest

from image_plugin_under_test.policy import Delegation, Limits, Scope, StoreError
from image_plugin_under_test.store import SCHEMA_VERSION, ArtistStore


SCOPE = Scope("qq", "host-stream", "real-user")
OTHER = Scope("qq", "other-stream", "other-user")
LIMITS = Limits(think=10, render=10, share=10, max_inflight=10, max_revisions=3)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 30, 15, 59, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def ledger(tmp_path):
    clock = Clock()
    store = ArtistStore(tmp_path / "artist.sqlite3", clock=clock)
    store.create_space("s", SCOPE, binding_id="service-install-A")
    store.grant("s", SCOPE, Delegation("explicit-consent-1", "host-admin", continuous=True))
    store.set_global_limits(LIMITS)
    store.set_limits("s", SCOPE, LIMITS)
    store.create_work("s", SCOPE, "work")
    yield store, clock
    store.close()


def reserve(store, key="r1", action="submit", **kwargs):
    return store.reserve(
        "s",
        SCOPE,
        request_id=key,
        action=action,
        params=kwargs.pop("params", {"prompt": "a painting"}),
        work_id="work" if action in {"submit", "edit"} else None,
        **kwargs,
    )


def error(code):
    return pytest.raises(StoreError, match=".", check=lambda exc: exc.code == code)


def test_default_deny_and_explicit_typed_delegation(tmp_path):
    store = ArtistStore(tmp_path / "fresh.sqlite3")
    try:
        with error("INVALID_SCOPE"):
            store.create_space("s", {"platform": "qq", "stream_id": "x", "user_id": "y"}, binding_id="b")
        store.create_space("s", SCOPE, binding_id="b")
        store.set_global_limits(LIMITS)
        store.set_limits("s", SCOPE, LIMITS)
        with error("AUTH_REQUIRED"):
            store.create_work("s", SCOPE, "work")
        with error("AUTH_REQUIRED"):
            store.grant("s", SCOPE, {"allowed_users": [SCOPE.user_id]})
        with error("AUTH_REQUIRED"):
            Delegation("whitelist", "admin", continuous=False)
        store.grant("s", SCOPE, Delegation("explicit-ongoing-consent", "host-admin", continuous=True))
        store.create_work("s", SCOPE, "work")
        reserve(store)
        with error("OWNER_CONFLICT"):
            store.create_space("s", OTHER, binding_id="b")
        with error("OWNER_CONFLICT"):
            store.create_space("s", SCOPE, binding_id="different-service")
    finally:
        store.close()


@pytest.mark.parametrize("configuration", [None, Limits()])
def test_no_policy_or_zero_budget_blocks(tmp_path, configuration):
    store = ArtistStore(tmp_path / "fresh.sqlite3")
    try:
        store.create_space("s", SCOPE, binding_id="b")
        store.grant("s", SCOPE, Delegation("explicit", "admin", continuous=True))
        store.create_work("s", SCOPE, "work")
        if configuration is not None:
            store.set_global_limits(configuration)
            store.set_limits("s", SCOPE, configuration)
        with error("BUDGET_EXCEEDED"):
            reserve(store)
        assert store.audit_operations("s", SCOPE) == []
    finally:
        store.close()


def test_frozen_identity_params_and_idempotency(ledger):
    store, _ = ledger
    params = {"prompt": "first", "options": {"color": "blue"}}
    first = reserve(store, params=params, trigger="chat", trigger_ref="current-message")
    params["options"]["color"] = "red"
    repeated = reserve(store, params={"options": {"color": "blue"}, "prompt": "first"})
    assert repeated["id"] == first["id"]
    assert repeated["scope_snapshot"] == SCOPE.snapshot()
    assert repeated["trigger_ref"] == "current-message"
    assert store.audit_authorizations("s", SCOPE)[0]["source_ref"] == "explicit-consent-1"
    assert repeated["params"]["options"]["color"] == "blue"
    repeated["params"]["options"]["color"] = "green"
    assert store.get_operation("s", SCOPE, "r1")["params"]["options"]["color"] == "blue"
    for action, payload in (("submit", {"prompt": "different"}), ("edit", first["params"])):
        with error("REQUEST_CONFLICT"):
            reserve(store, action=action, params=payload)
    assert store.budget_usage("s", SCOPE)["render"]["held"] == 1
    # Same owner and backend cannot evade MCP's key through another local space.
    store.create_space("alias", SCOPE, binding_id="service-install-A")
    with error("REQUEST_CONFLICT"):
        store.reserve("alias", SCOPE, request_id="r1", action="think", params={})


@pytest.mark.parametrize("units", [-1, 0, 2, True, 1.0])
def test_fixed_units_and_no_model_identity_overrides(ledger, units):
    store, _ = ledger
    with error("INVALID_ARGUMENT"):
        reserve(store, units=units)
    with error("INVALID_ARGUMENT"):
        reserve(store, params={"scope": {"user_id": "model-claim"}})
    assert not store.audit_operations("s", SCOPE)


def test_scope_isolation_for_reads_writes_and_audits(ledger):
    store, _ = ledger
    reserve(store)
    store.create_space("other", OTHER, binding_id="service-install-A")
    for call in (
        lambda: store.get_work("s", OTHER, "work"),
        lambda: store.get_operation("s", OTHER, "r1"),
        lambda: store.audit_operations("s", OTHER),
        lambda: store.audit_authorizations("s", OTHER),
        lambda: store.budget_usage("s", OTHER),
        lambda: store.set_limits("s", OTHER, LIMITS),
        lambda: store.revoke("s", OTHER, source_ref="fake", granted_by="x"),
        lambda: store.mark_submitting("s", OTHER, "r1"),
        lambda: store.get_operation("other", OTHER, "r1"),
        lambda: store.legacy_lock("s", OTHER, "job"),
    ):
        with error("NOT_FOUND"):
            call()
    assert store.get_operation("s", SCOPE, "r1")["state"] == "reserved"


@pytest.mark.parametrize("change", ["revoke", "regrant", "space-limit", "global-limit"])
def test_reservation_rechecked_before_external_call(ledger, change):
    store, _ = ledger
    reserve(store)
    if change in {"revoke", "regrant"}:
        store.revoke("s", SCOPE, source_ref="withdrawal", granted_by="admin")
        if change == "regrant":
            store.grant("s", SCOPE, Delegation("new-consent", "admin", continuous=True))
    elif change == "space-limit":
        store.set_limits("s", SCOPE, Limits())
    else:
        store.set_global_limits(Limits())
    expected = {"revoke": "AUTH_REQUIRED", "regrant": "AUTH_CHANGED"}.get(change, "BUDGET_EXCEEDED")
    with error(expected):
        store.mark_submitting("s", SCOPE, "r1")
    assert store.get_operation("s", SCOPE, "r1")["state"] == "reserved"
    # Idempotent access after revocation is inspection, not renewed permission.
    assert reserve(store)["state"] == "reserved"


@pytest.mark.parametrize("state", ["reserved", "submitting", "accepted", "unknown"])
def test_pending_work_blocks_new_render_but_not_thinking(ledger, state):
    store, _ = ledger
    original = reserve(store)
    if state != "reserved":
        store.mark_submitting("s", SCOPE, "r1")
    if state == "accepted":
        store.mark_accepted("s", SCOPE, "r1", job_id="original-job")
    elif state == "unknown":
        store.mark_unknown("s", SCOPE, "r1", reason="submit response lost")
    # High category/inflight budgets cannot bypass a work's unresolved render.
    for action in ("submit", "edit"):
        with error("WORK_BUSY"):
            reserve(store, f"new-{action}", action=action)
    assert reserve(store)["id"] == original["id"]
    reserve(store, "independent-thought", action="think")
    assert store.budget_usage("s", SCOPE)["render"]["held"] == 1


def test_failed_acceptance_consumes_and_unknown_never_refunds(ledger):
    store, _ = ledger
    reserve(store)
    store.mark_submitting("s", SCOPE, "r1")
    store.mark_accepted("s", SCOPE, "r1", job_id="original-job")
    with error("INVALID_STATE"):
        store.reject("s", SCOPE, "r1", code="QUEUE_FULL", evidence="later rejection")
    store.settle("s", SCOPE, "r1", outcome="failed", evidence="original job terminal failure")
    assert store.budget_usage("s", SCOPE)["render"] == {"held": 0, "consumed": 1, "total": 1}
    assert reserve(store)["state"] == "failed"
    with error("INVALID_STATE"):
        store.mark_submitting("s", SCOPE, "r1")
    reserve(store, "r2")
    store.mark_submitting("s", SCOPE, "r2")
    with error("REQUEST_CONFLICT"):
        store.reject("s", SCOPE, "r2", code="REQUEST_CONFLICT", evidence="server key conflict")
    assert store.get_operation("s", SCOPE, "r2")["state"] == "unknown"
    for call in (
        lambda: store.reject("s", SCOPE, "r2", code="NOT_FOUND", evidence="lookup returned false"),
        lambda: store.reconcile("s", SCOPE, "r2", outcome="rejected", evidence="not found"),
        lambda: store.settle("s", SCOPE, "r2", outcome="failed", evidence="timeout"),
        lambda: store.mark_submitting("s", SCOPE, "r2"),
    ):
        with pytest.raises(StoreError):
            call()
    store.reconcile("s", SCOPE, "r2", outcome="accepted", evidence="matching lookup digest", job_id="job-2")
    with error("REQUEST_CONFLICT"):
        store.mark_accepted("s", SCOPE, "r2", job_id="different-job")
    store.settle("s", SCOPE, "r2", outcome="succeeded", evidence="job-2 completed")
    assert store.budget_usage("s", SCOPE)["render"]["consumed"] == 2


def test_only_proven_preaccept_rejection_releases(ledger):
    store, _ = ledger
    reserve(store)
    store.mark_submitting("s", SCOPE, "r1")
    store.reject(
        "s", SCOPE, "r1", code="QUEUE_FULL", evidence="first and only submit rejected before acceptance"
    )
    assert store.budget_usage("s", SCOPE)["render"]["total"] == 0
    assert reserve(store)["state"] == "rejected"
    with error("INVALID_STATE"):
        store.mark_submitting("s", SCOPE, "r1")


@pytest.mark.parametrize("code", ["TIMEOUT", "NETWORK", "NOT_FOUND", "UNKNOWN", "BAD_RESPONSE"])
def test_ambiguous_rejection_code_cannot_refund(ledger, code):
    store, _ = ledger
    reserve(store)
    store.mark_submitting("s", SCOPE, "r1")
    with error("INVALID_ARGUMENT"):
        store.reject("s", SCOPE, "r1", code=code, evidence="not proof of rejection")
    assert store.budget_usage("s", SCOPE)["render"]["held"] == 1
    assert store.get_operation("s", SCOPE, "r1")["state"] == "submitting"


def test_local_cancellation_and_render_terminal_need_original_job(ledger):
    store, _ = ledger
    reserve(store)
    with error("INVALID_STATE"):
        store.reject("s", SCOPE, "r1", code="QUEUE_FULL", evidence="no submit yet")
    store.cancel_reservation("s", SCOPE, "r1", reason="plan no longer relevant")
    assert store.budget_usage("s", SCOPE)["render"]["total"] == 0
    reserve(store, "r2")
    store.mark_submitting("s", SCOPE, "r2")
    with error("INVALID_STATE"):
        store.cancel_reservation("s", SCOPE, "r2", reason="too late")
    with error("INVALID_ARGUMENT"):
        store.settle("s", SCOPE, "r2", outcome="failed", evidence="no identifiable original job")
    store.settle("s", SCOPE, "r2", outcome="failed", evidence="job terminal", job_id="job-2")
    assert store.get_operation("s", SCOPE, "r2")["job_id"] == "job-2"


def test_share_requires_own_render_binding_and_can_finish(ledger):
    store, _ = ledger
    reserve(store)
    store.mark_submitting("s", SCOPE, "r1")
    store.mark_accepted("s", SCOPE, "r1", job_id="own-job")
    assert store.check_share("s", SCOPE, "own-job")
    store.create_space("other", OTHER, binding_id="service-install-A")
    store.grant("other", OTHER, Delegation("consent", "admin", continuous=True))
    store.set_limits("other", OTHER, LIMITS)
    with error("NOT_FOUND"):
        store.reserve("other", OTHER, request_id="steal", action="share", params={"job_id": "own-job"})
    assert store.budget_usage("other", OTHER)["share"]["total"] == 0
    reserve(store, "share", action="share", params={"job_id": "own-job"})
    store.mark_submitting("s", SCOPE, "share")
    store.settle("s", SCOPE, "share", outcome="succeeded", evidence="definite send receipt")
    assert store.budget_usage("s", SCOPE)["share"]["consumed"] == 1
    with error("SHARE_LOCK"):
        store.check_share("s", SCOPE, "own-job")


def test_hong_kong_day_rollover_keeps_unknown_hold(ledger):
    store, clock = ledger
    store.set_limits("s", SCOPE, Limits(render=1, max_inflight=10))
    reserve(store)
    store.mark_submitting("s", SCOPE, "r1")
    store.mark_unknown("s", SCOPE, "r1", reason="connection lost")
    assert store.budget_usage("s", SCOPE)["day"] == "2026-10-30"
    clock.now += timedelta(minutes=2)
    assert store.budget_usage("s", SCOPE)["day"] == "2026-10-31"
    with error("BUDGET_EXCEEDED"):
        reserve(store, "r2")
    clock.now += timedelta(days=4)
    with error("BUDGET_EXCEEDED"):
        reserve(store, "r2")
    store.reconcile("s", SCOPE, "r1", outcome="failed", job_id="old-job", evidence="original job terminal")
    reserve(store, "r2")


def test_day_at_start_and_clock_not_from_params(ledger):
    store, clock = ledger
    reserve(store)
    clock.now += timedelta(minutes=2)
    started = store.mark_submitting("s", SCOPE, "r1")
    assert started["budget_day"] == "2026-10-31"
    store.settle("s", SCOPE, "r1", outcome="failed", evidence="definitive failure", job_id="failed-job")
    reserve(store, "r2", action="think", params={"day": "1900-01-01"})
    assert store.budget_usage("s", SCOPE)["think"]["held"] == 1
    assert store.get_operation("s", SCOPE, "r2")["budget_day"] == "2026-10-31"
    clock.now = datetime(2026, 1, 1)
    with error("INVALID_CLOCK"):
        store.budget_usage("s", SCOPE)


def test_submit_edit_share_think_and_cumulative_revisions(ledger):
    store, _ = ledger
    limits = Limits(think=1, render=5, share=1, max_inflight=10, max_revisions=1)
    store.set_limits("s", SCOPE, limits)
    reserve(store)
    store.mark_submitting("s", SCOPE, "r1")
    store.settle(
        "s", SCOPE, "r1", outcome="succeeded", evidence="completed initial generation", job_id="original"
    )
    reserve(store, "revision", action="edit", params={"source_job_id": "original", "prompt": "refine"})
    store.mark_submitting("s", SCOPE, "revision")
    store.settle("s", SCOPE, "revision", outcome="failed", evidence="accepted edit failed", job_id="edit-job")
    assert store.get_work("s", SCOPE, "work")["revision_attempts"] == 1
    # Reopening/re-creating a plan does not reset cumulative work revision count.
    store.create_work("s", SCOPE, "work")
    with error("REVISION_LIMIT"):
        reserve(store, "revision2", action="edit")
    reserve(store, "thought", action="think")
    with error("BUDGET_EXCEEDED"):
        reserve(store, "thought2", action="think")
    reserve(store, "share", action="share", params={"job_id": "original"})
    store.mark_submitting("s", SCOPE, "share")
    store.mark_unknown("s", SCOPE, "share", reason="receipt lost")
    with error("SHARE_LOCK"):
        store.check_share("s", SCOPE, "original")
    assert store.budget_usage("s", SCOPE)["render"]["consumed"] == 2
    assert store.budget_usage("s", SCOPE)["share"]["held"] == 1


def test_reopen_is_passive_and_recovery_is_explicit(ledger):
    store, clock = ledger
    reserve(store)
    store.mark_submitting("s", SCOPE, "r1")
    second = ArtistStore(store.path, clock=clock)
    try:
        assert second.get_operation("s", SCOPE, "r1")["state"] == "submitting"
        assert second.budget_usage("s", SCOPE)["render"]["held"] == 1
        with error("OFFLINE_REQUIRED"):
            second.recover_inflight("s", SCOPE)
        assert second.recover_inflight("s", SCOPE, offline=True) == 1
        assert second.recover_inflight("s", SCOPE, offline=True) == 0
        assert store.get_operation("s", SCOPE, "r1")["state"] == "unknown"
        store.revoke("s", SCOPE, source_ref="withdrawn", granted_by="admin")
    finally:
        second.close()
    third = ArtistStore(store.path, clock=clock)
    try:
        with error("AUTH_REQUIRED"):
            reserve(third, "new")
        assert len(third.audit_authorizations("s", SCOPE)) == 2
        assert third.db.execute("SELECT render FROM limits WHERE scope_key='s'").fetchone()[0] == 10
    finally:
        third.close()


@pytest.mark.parametrize("global_cap,space_cap", [(1, 10), (10, 1)])
def test_real_concurrent_connections_cannot_overreserve(ledger, global_cap, space_cap):
    store, clock = ledger
    store.set_global_limits(Limits(render=global_cap, max_inflight=10))
    store.set_limits("s", SCOPE, Limits(render=space_cap, max_inflight=10))
    barrier = Barrier(2)

    def worker(key):
        db = ArtistStore(store.path, clock=clock)
        try:
            barrier.wait(timeout=5)
            try:
                reserve(db, key)
                return "reserved"
            except StoreError as exc:
                return exc.code
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ["r1", "r2"]))
    assert sorted(results) == ["BUDGET_EXCEEDED", "reserved"]
    assert len(store.audit_operations("s", SCOPE)) == 1


def test_global_budget_serializes_different_spaces(ledger):
    store, clock = ledger
    store.create_space("other", OTHER, binding_id="service-install-A")
    store.grant("other", OTHER, Delegation("consent-2", "admin", continuous=True))
    store.set_limits("other", OTHER, LIMITS)
    store.set_global_limits(Limits(think=1, max_inflight=10))
    barrier = Barrier(2)

    def worker(pair):
        space, scope = pair
        db = ArtistStore(store.path, clock=clock)
        try:
            barrier.wait(timeout=5)
            try:
                db.reserve(space, scope, request_id="same-owner-scoped-key", action="think", params={})
                return "reserved"
            except StoreError as exc:
                return exc.code
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(worker, [("s", SCOPE), ("other", OTHER)])) == ["BUDGET_EXCEEDED", "reserved"]


def make_legacy(path, rows):
    db = sqlite3.connect(path)
    with db:
        db.execute("CREATE TABLE delivery (job TEXT PRIMARY KEY, state TEXT NOT NULL)")
        db.executemany("INSERT INTO delivery VALUES(?,?)", rows)
    db.close()


def test_legacy_backup_all_states_and_idempotent_import(ledger, tmp_path):
    store, _ = ledger
    source, backup = tmp_path / "delivery.sqlite3", tmp_path / "backup.sqlite3"
    states = ("sending", "send_unknown", "sent", "send_failed")
    make_legacy(source, list(zip(states, states, strict=True)))
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    with error("OFFLINE_REQUIRED"):
        store.migrate_legacy_deliveries(source, backup)
    first = store.migrate_legacy_deliveries(source, backup, offline=True)
    assert first["rows"] == 4 and first["schema_version"] == SCHEMA_VERSION
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    read = sqlite3.connect(backup)
    assert read.execute("SELECT job,state FROM delivery ORDER BY job").fetchall() == sorted(
        zip(states, states, strict=True)
    )
    read.close()
    assert store.db.execute(
        "SELECT original_state,state FROM legacy_locks WHERE job='sending'",
    ).fetchone()[:] == ("sending", "send_unknown")
    for job in states:
        assert store.legacy_lock("s", SCOPE, job)
        with error("LEGACY_LOCK"):
            store.check_share("s", SCOPE, job)
    with pytest.raises(FileExistsError):
        store.migrate_legacy_deliveries(source, backup, offline=True)
    assert store.migrate_legacy_deliveries(source, tmp_path / "backup2.sqlite3", offline=True)[
        "already_imported"
    ]
    # A later legacy state change must never replace the original conservative lock.
    writer = sqlite3.connect(source)
    with writer:
        writer.execute("UPDATE delivery SET state='sent' WHERE job='sending'")
    writer.close()
    store.migrate_legacy_deliveries(source, tmp_path / "backup3.sqlite3", offline=True)
    assert (
        store.db.execute("SELECT state FROM legacy_locks WHERE job='sending'").fetchone()[0] == "send_unknown"
    )


@pytest.mark.parametrize("kind", ["missing", "corrupt", "extra-column", "unknown-state", "future-version"])
def test_invalid_legacy_never_creates_or_partially_imports(ledger, tmp_path, kind):
    store, _ = ledger
    source, backup = tmp_path / "source.sqlite3", tmp_path / "backup.sqlite3"
    if kind == "corrupt":
        source.write_bytes(b"synthetic not sqlite")
    elif kind != "missing":
        make_legacy(source, [("good", "sent"), ("bad", "mystery" if kind == "unknown-state" else "sent")])
        writer = sqlite3.connect(source)
        with writer:
            if kind == "extra-column":
                writer.execute("ALTER TABLE delivery ADD COLUMN surprise TEXT")
            elif kind == "future-version":
                writer.execute("PRAGMA user_version=99")
        writer.close()
    with pytest.raises(StoreError):
        store.migrate_legacy_deliveries(source, backup, offline=True)
    assert store.db.execute("SELECT count(*) FROM legacy_locks").fetchone()[0] == 0
    assert not backup.exists()
    if kind == "missing":
        assert not source.exists()


def test_future_and_unversioned_artist_schema_rejected(tmp_path):
    for version in (0, SCHEMA_VERSION + 1):
        path = tmp_path / f"version-{version}.sqlite3"
        db = sqlite3.connect(path)
        with db:
            db.execute("CREATE TABLE unknown(value TEXT)")
            db.execute(f"PRAGMA user_version={version}")
        db.close()
        before = path.read_bytes()
        with error("SCHEMA_UNSUPPORTED"):
            ArtistStore(path)
        assert path.read_bytes() == before
