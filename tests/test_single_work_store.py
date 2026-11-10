"""Permanent single-work limits on real local SQLite connections."""

# ruff: noqa: I001 -- parent AGENTS.md requires from-imports before plain imports.

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
import sqlite3

import pytest

from image_plugin_under_test.domain import store as store_module
from image_plugin_under_test.domain.policy import Delegation, Limits, Scope, StoreError, canonical_json
from image_plugin_under_test.domain.store import ArtistStore


SCOPE = Scope("qq", "group-stream", "owner")
OTHER_OWNER = Scope(SCOPE.platform, SCOPE.stream_id, "other-owner")
OTHER = Scope("qq", "other-stream", "other-owner")
LIMITS = Limits(think=10, render=10, share=10, max_inflight=10, max_revisions=5)
BRIEF = {
    "intent": "study light", "subject": "window", "mood": "quiet", "prompt": "soft light",
    "completion_criteria": "recognizable window",
}


def error(code):
    return pytest.raises(StoreError, check=lambda exc: exc.code == code)


def authorize(db, space="s", scope=SCOPE, binding="backend"):
    db.create_space(space, scope, binding_id=binding)
    db.grant(space, scope, Delegation("explicit consent", "trusted-adapter", True))
    db.set_limits(space, scope, LIMITS)


@pytest.fixture
def ledger(tmp_path):
    db = ArtistStore(tmp_path / "artist.sqlite3")
    authorize(db)
    db.set_global_limits(LIMITS)
    yield db
    db.close()


def status(*, enabled=True, work=None, create=False, edit=False, share=False):
    return {
        "enabled": enabled, "work_id": work, "create_used": create, "edit_used": edit, "share_used": share,
    }


def plan(db, work="work", *, space="s", scope=SCOPE, inspiration_keys=()):
    return db.plan_work(space, scope, work_id=work, brief=BRIEF, inspiration_keys=list(inspiration_keys))


def reserve(db, action, request, *, params=None, work="work", space="s", scope=SCOPE):
    return db.reserve(space, scope, request_id=request, action=action,
                      work_id=work, params={"prompt": "synthetic"} if params is None else params)


def edit_params(version):
    return {"prompt": "synthetic revision", "source_job_id": version["job_id"], "source_sha256": version["sha256"]}


def share_params(version):
    return {"job_id": version["job_id"], "version_id": version["version_id"]}


def render(db, request="original", *, source=None, space="s", scope=SCOPE, work="work", state="succeeded"):
    reserve(db, "submit" if source is None else "edit", request, space=space, scope=scope, work=work,
            params=None if source is None else edit_params(source))
    db.mark_submitting(space, scope, request)
    assert db.validate_submitting(space, scope, request)["state"] == "submitting"
    job = "job-" + request
    db.mark_accepted(space, scope, request, job_id=job)
    if state != "accepted":
        db.settle(space, scope, request, outcome=state, evidence="synthetic terminal")
    return db.record_version(
        space, scope, version_id="version-" + request, work_id=work, request_id=request, job_id=job,
        data=("synthetic image " + request).encode(), mime="image/png",
        parent_id=source["version_id"] if source is not None else None,
    )


def accept(db, version):
    request = "review-" + version["request_id"]
    context = {"current_work": {"work_id": version["work_id"]},
               "current_version": {key: version[key] for key in ("version_id", "sha256", "mime")}}
    review = {"decision": "accept", "reason": "meets synthetic brief"}
    reserve(db, "think", request, work=version["work_id"],
            params={"task": "review", "version_id": version["version_id"], "context": context})
    db.mark_submitting("s", SCOPE, request)
    db.settle("s", SCOPE, request, outcome="succeeded", evidence=canonical_json(review))
    db.record_review("s", SCOPE, version_id=version["version_id"], request_id=request, review=review)


def race(db, left, right):
    start = Barrier(2, timeout=10)

    def attempt(action):
        connection = ArtistStore(db.path)
        try:
            start.wait()
            try:
                return action(connection)
            except StoreError as exc:
                return exc.code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        return tuple(pool.map(attempt, (left, right)))


def test_default_is_read_only_and_old_spaces_keep_their_behavior(ledger):
    db = ledger
    before = db.db.total_changes
    assert db.configure_single_work("s", SCOPE) == status(enabled=False)
    assert db.single_work_status("s", SCOPE) == status(enabled=False)
    assert not db.single_work_for_stream("qq", SCOPE.stream_id)
    assert db.db.total_changes == before
    db.create_work("s", SCOPE, "work")
    db.create_work("s", SCOPE, "second")
    initial = render(db)
    render(db, "second-render", work="second")
    render(db, "another-initial")
    reserve(db, "share", "legacy-job-only", work=None, params={"job_id": initial["job_id"]})
    assert db.configure_single_work("s", SCOPE, False) == status(enabled=False)
    with error("SINGLE_WORK_HISTORY"):
        db.configure_single_work("s", SCOPE, True)
    assert db.single_work_status("s", SCOPE) == status(enabled=False)


def test_latch_blocks_space_alias_and_failed_plan_rolls_back_work_claim(ledger):
    db = ledger
    authorize(db, "alias", binding="other-backend")
    authorize(db, "same-stream", OTHER_OWNER, binding="other-backend")
    assert db.configure_single_work("s", SCOPE, True) == status()
    assert db.single_work_for_stream(SCOPE.platform, SCOPE.stream_id)
    assert not db.single_work_for_stream("other-platform", SCOPE.stream_id)
    assert not db.single_work_for_stream(SCOPE.platform, OTHER.stream_id)
    with error("NOT_FOUND"):
        db.single_work_status("s", OTHER_OWNER)
    with error("SINGLE_WORK_SCOPE_CONFLICT"):
        db.create_space("new-alias", SCOPE, binding_id="new-backend")
    with error("SINGLE_WORK_SCOPE_CONFLICT"):
        db.create_space("new-owner", OTHER_OWNER, binding_id="new-backend")
    with error("SINGLE_WORK_SCOPE_CONFLICT"):
        db.configure_single_work("alias", SCOPE, False)
    with error("SINGLE_WORK_SCOPE_CONFLICT"):
        db.configure_single_work("alias", SCOPE, True)
    with error("SINGLE_WORK_SCOPE_CONFLICT"):
        db.create_work("alias", SCOPE, "work")
    with error("SINGLE_WORK_SCOPE_CONFLICT"):
        plan(db, space="alias")
    with error("SINGLE_WORK_SCOPE_CONFLICT"):
        reserve(db, "submit", "bypass", space="alias")
    with error("SINGLE_WORK_SCOPE_CONFLICT"):
        db.configure_single_work("same-stream", OTHER_OWNER, False)
    with error("SINGLE_WORK_SCOPE_CONFLICT"):
        plan(db, space="same-stream", scope=OTHER_OWNER)
    with error("SINGLE_WORK_SCOPE_CONFLICT"):
        reserve(db, "share", "bypass", space="same-stream", scope=OTHER_OWNER, work=None)
    with error("NOT_FOUND"):
        plan(db, "rejected-plan", inspiration_keys=["missing-inspiration"])
    assert db.single_work_status("s", SCOPE) == status()
    with error("NOT_FOUND"):
        db.get_work("s", SCOPE, "rejected-plan")
    assert db.create_work("s", SCOPE, "work")["work_id"] == "work"
    assert plan(db)["work_id"] == "work"
    assert plan(db)["work_id"] == "work"
    assert db.configure_single_work("s", SCOPE, True) == status(work="work")
    with error("SINGLE_WORK_LATCHED"):
        db.configure_single_work("s", SCOPE, False)
    with error("SINGLE_WORK_LIMIT"):
        db.create_work("s", SCOPE, "different")
    with error("SINGLE_WORK_LIMIT"):
        plan(db, "different")


@pytest.mark.parametrize("prior_scope", [SCOPE, OTHER_OWNER])
def test_new_space_cannot_hide_old_history_in_the_stream(ledger, prior_scope):
    db = ledger
    authorize(db, "alias", prior_scope, binding="other-backend")
    db.create_work("alias", prior_scope, "old")
    with error("SINGLE_WORK_HISTORY"):
        db.configure_single_work("s", SCOPE, True)
    assert not db.single_work_for_stream(SCOPE.platform, SCOPE.stream_id)


def test_enable_and_first_plan_serialize(ledger):
    db = ledger
    policy, work = race(db, lambda conn: conn.configure_single_work("s", SCOPE, True), plan)
    assert work["work_id"] == "work"
    assert policy in (status(), "SINGLE_WORK_HISTORY")
    assert db.single_work_status("s", SCOPE) == (
        status(work="work") if policy == status() else status(enabled=False)
    )


@pytest.mark.parametrize("failure", ["alias", "disable", "invalid", "malformed"])
def test_batch_failure_rolls_back_new_latch_and_preserves_all_old_records(ledger, failure):
    db = ledger
    authorize(db, "old", OTHER)
    alias_scope = Scope(OTHER.platform, OTHER.stream_id, "alias-owner")
    authorize(db, "alias", alias_scope, binding="another-backend")
    db.configure_single_work("old", OTHER, True)
    plan(db, space="old", scope=OTHER)
    reserve(db, "submit", "old-reservation", space="old", scope=OTHER)
    tables = [row[0] for row in db.db.execute("SELECT name FROM sqlite_master WHERE type='table'")]

    def snapshot():
        return {table: [tuple(row) for row in db.db.execute(f'SELECT * FROM "{table}"')] for table in tables}

    before = snapshot()
    bad_item, code = {
        "alias": (("alias", alias_scope, True), "SINGLE_WORK_SCOPE_CONFLICT"),
        "disable": (("old", OTHER, False), "SINGLE_WORK_LATCHED"),
        "invalid": (("old", OTHER, 0), "INVALID_ARGUMENT"),
        "malformed": (("old", OTHER), "INVALID_ARGUMENT"),
    }[failure]
    with error(code):
        db.configure_single_work_batch([("s", SCOPE, True), bad_item])
    assert snapshot() == before
    assert not db.single_work_for_stream(SCOPE.platform, SCOPE.stream_id)
    assert db.single_work_status("old", OTHER) == status(work="work", create=True)
    # The failed batch left neither a latch nor an open transaction behind.
    assert db.configure_single_work_batch([("s", SCOPE, True), ("old", OTHER, True)]) == [
        status(), status(work="work", create=True),
    ]


def test_successful_batch_is_idempotent_and_false_keeps_old_space_unrestricted(ledger):
    db = ledger
    authorize(db, "ordinary", OTHER)
    db.create_work("ordinary", OTHER, "old-work")
    items = [("s", SCOPE, True), ("ordinary", OTHER, False)]
    assert db.configure_single_work_batch(items) == [status(), status(enabled=False)]
    before = db.db.total_changes
    assert db.configure_single_work_batch(items) == [status(), status(enabled=False)]
    assert db.configure_single_work_batch([]) == []
    assert db.db.total_changes == before
    assert db.create_work("ordinary", OTHER, "new-work")["work_id"] == "new-work"


def test_concurrent_batches_cannot_latch_aliases_and_losing_batch_rolls_back(ledger):
    db = ledger
    authorize(db, "alias", OTHER_OWNER, binding="other-backend")
    left_scope = Scope("qq", "left-stream", "left-owner")
    right_scope = Scope("qq", "right-stream", "right-owner")
    authorize(db, "left", left_scope)
    authorize(db, "right", right_scope)
    results = race(
        db,
        lambda conn: conn.configure_single_work_batch([("left", left_scope, True), ("s", SCOPE, True)]),
        lambda conn: conn.configure_single_work_batch([("right", right_scope, True), ("alias", OTHER_OWNER, True)]),
    )
    assert results.count("SINGLE_WORK_SCOPE_CONFLICT") == 1
    assert results.count([status(), status()]) == 1
    for index, (first, scope, contested, owner) in enumerate([
        ("left", left_scope, "s", SCOPE), ("right", right_scope, "alias", OTHER_OWNER),
    ]):
        enabled = isinstance(results[index], list)
        assert db.single_work_status(first, scope) == status(enabled=enabled)
        assert db.single_work_status(contested, owner) == status(enabled=enabled)
    assert db.db.execute("SELECT count(*) FROM single_work_policies").fetchone()[0] == 2


def test_concurrent_alias_activation_and_competing_work_claims(ledger):
    db = ledger
    authorize(db, "alias", OTHER_OWNER, binding="other-backend")
    policies = race(db, lambda conn: conn.configure_single_work("s", SCOPE, True),
                    lambda conn: conn.configure_single_work("alias", OTHER_OWNER, True))
    assert policies.count(status()) == 1
    assert policies.count("SINGLE_WORK_SCOPE_CONFLICT") == 1
    winner, scope = (("s", SCOPE), ("alias", OTHER_OWNER))[policies.index(status())]
    outcomes = race(
        db, lambda conn: conn.create_work(winner, scope, "created"),
        lambda conn: plan(conn, "planned", space=winner, scope=scope),
    )
    assert outcomes.count("SINGLE_WORK_LIMIT") == 1
    work = next(result["work_id"] for result in outcomes if isinstance(result, dict))
    assert db.single_work_status(winner, scope) == status(work=work)
    assert db.db.execute("SELECT count(*) FROM works").fetchone()[0] == 1


@pytest.mark.parametrize("action", ["submit", "edit", "share"])
def test_budget_failure_leaves_no_claim_and_two_connections_cannot_reserve_twice(ledger, action):
    db = ledger
    db.configure_single_work("s", SCOPE, True)
    plan(db)
    params = {"prompt": "synthetic"}
    if action != "submit":
        initial = render(db)
        params = edit_params(initial)
        if action == "share":
            accept(db, initial)
            params = share_params(initial)
    db.set_limits("s", SCOPE, Limits())
    with error("BUDGET_EXCEEDED"):
        reserve(db, action, "too-early", params=params)
    assert not any(op["action"] == action for op in db.audit_operations("s", SCOPE))
    db.set_limits("s", SCOPE, LIMITS)
    outcomes = race(
        db, lambda conn: reserve(conn, action, "attempt-a", params=params),
        lambda conn: reserve(conn, action, "attempt-b", params=params),
    )
    assert outcomes.count("SINGLE_WORK_LIMIT") == 1
    winner = next(result for result in outcomes if isinstance(result, dict))
    assert reserve(db, action, winner["request_id"], params=params) == winner
    assert db.mark_submitting("s", SCOPE, winner["request_id"])["state"] == "submitting"
    assert db.validate_submitting("s", SCOPE, winner["request_id"])["state"] == "submitting"
    with error("INVALID_STATE"):
        db.mark_submitting("s", SCOPE, winner["request_id"])


@pytest.mark.parametrize("outcome", ["rejected", "failed", "unknown"])
def test_original_attempt_is_permanent_across_outcomes_day_restart_and_authorization(ledger, outcome):
    db = ledger
    db.configure_single_work("s", SCOPE, True)
    plan(db)
    reserve(db, "submit", "first")
    db.mark_submitting("s", SCOPE, "first")
    if outcome == "rejected":
        db.reject("s", SCOPE, "first", code="QUEUE_FULL", evidence="synthetic preaccept denial")
    elif outcome == "failed":
        db.settle("s", SCOPE, "first", outcome=outcome, evidence="synthetic failure", job_id="job-failed")
    else:
        db.mark_unknown("s", SCOPE, "first", reason="synthetic missing receipt")
    assert db.single_work_status("s", SCOPE) == status(work="work", create=True)
    db.revoke("s", SCOPE, source_ref="removed from config", granted_by="trusted-adapter")
    later = ArtistStore(db.path, clock=lambda: db.clock() + timedelta(days=2))
    try:
        assert later.single_work_for_stream(SCOPE.platform, SCOPE.stream_id)
        assert later.budget_usage("s", SCOPE)["render"]["total"] == int(outcome == "unknown")
        assert later.single_work_status("s", SCOPE) == status(work="work", create=True)
        with error("SINGLE_WORK_LATCHED"):
            later.configure_single_work("s", SCOPE, False)
        later.grant("s", SCOPE, Delegation("new explicit consent", "trusted-adapter", True))
        assert later.configure_single_work("s", SCOPE, True) == status(work="work", create=True)
        replay = reserve(later, "submit", "first")
        assert (replay["state"], replay["auth_version"]) == (outcome, 1)
        with error("REQUEST_CONFLICT"):
            reserve(later, "submit", "first", params={"prompt": "changed"})
        with error("SINGLE_WORK_LIMIT"):
            reserve(later, "submit", "another")
    finally:
        later.close()


def test_reserved_claim_cannot_be_dispatched_after_regrant_or_refunded_by_cancel(ledger):
    db = ledger
    db.configure_single_work("s", SCOPE, True)
    plan(db)
    first = reserve(db, "submit", "old")
    db.revoke("s", SCOPE, source_ref="withdrawn", granted_by="trusted-adapter")
    db.grant("s", SCOPE, Delegation("new consent", "trusted-adapter", True))
    assert reserve(db, "submit", "old") == first
    with error("AUTH_CHANGED"):
        db.mark_submitting("s", SCOPE, "old")
    db.cancel_reservation("s", SCOPE, "old", reason="stale grant")
    assert db.budget_usage("s", SCOPE)["render"]["total"] == 0
    with error("SINGLE_WORK_LIMIT"):
        reserve(db, "submit", "new")


def test_edit_requires_the_successful_original_of_the_locked_work(ledger):
    db = ledger
    db.configure_single_work("s", SCOPE, True)
    plan(db)
    authorize(db, "other", OTHER)
    db.create_work("other", OTHER, "work")
    foreign = render(db, "foreign", space="other", scope=OTHER)
    initial = render(db, state="accepted")
    with error("SINGLE_WORK_SOURCE"):
        reserve(db, "edit", "revision", params=edit_params(initial))
    db.settle("s", SCOPE, initial["request_id"], outcome="succeeded", evidence="synthetic complete")
    for params in (edit_params(foreign), {**edit_params(initial), "source_sha256": foreign["sha256"]}):
        with error("SINGLE_WORK_SOURCE"):
            reserve(db, "edit", "revision", params=params)
    with error("SINGLE_WORK_LIMIT"):
        reserve(db, "edit", "revision", work="different", params=edit_params(initial))
    reserve(db, "edit", "revision", params=edit_params(initial))
    db.mark_submitting("s", SCOPE, "revision")
    db.reject("s", SCOPE, "revision", code="SOURCE_NOT_FOUND", evidence="synthetic rejection")
    assert reserve(db, "edit", "revision", params=edit_params(initial))["state"] == "rejected"
    assert db.single_work_status("s", SCOPE) == status(work="work", create=True, edit=True)
    with error("SINGLE_WORK_LIMIT"):
        reserve(db, "edit", "another-revision", params=edit_params(initial))


def test_share_checks_persisted_job_version_and_visual_acceptance_and_is_permanent(ledger):
    db = ledger
    db.configure_single_work("s", SCOPE, True)
    plan(db)
    authorize(db, "other", OTHER)
    db.create_work("other", OTHER, "work")
    foreign = render(db, "foreign", space="other", scope=OTHER)
    initial = render(db)
    revised = render(db, "revised", source=initial)
    accept(db, revised)
    with error("REVIEW_REQUIRED"):
        reserve(db, "share", "delivery", params=share_params(initial))
    with error("SINGLE_WORK_LIMIT"):
        reserve(db, "share", "delivery", work="different", params=share_params(revised))
    for params in (
        share_params(foreign), {"job_id": revised["job_id"], "version_id": foreign["version_id"]},
        {"job_id": initial["job_id"], "version_id": revised["version_id"]}, {"job_id": revised["job_id"]},
    ):
        with error("SINGLE_WORK_VERSION"):
            reserve(db, "share", "delivery", params=params, work=None)
    assert not db.single_work_status("s", SCOPE)["share_used"]
    reserve(db, "share", "delivery", params=share_params(revised), work=None)
    db.mark_submitting("s", SCOPE, "delivery")
    assert db.validate_submitting("s", SCOPE, "delivery")["state"] == "submitting"
    db.mark_unknown("s", SCOPE, "delivery", reason="synthetic uncertain send")
    assert reserve(db, "share", "delivery", params=share_params(revised), work=None)["state"] == "unknown"
    assert db.single_work_status("s", SCOPE) == status(work="work", create=True, edit=True, share=True)
    accept(db, initial)
    with error("SINGLE_WORK_LIMIT"):
        reserve(db, "share", "another-version", params=share_params(initial))


@pytest.mark.parametrize("gate", ["mark_submitting", "validate_submitting"])
def test_share_dispatch_rechecks_review_evidence_without_refunding_claim(ledger, gate):
    db = ledger
    db.configure_single_work("s", SCOPE, True)
    plan(db)
    initial = render(db)
    accept(db, initial)
    reserve(db, "share", "delivery", params=share_params(initial))
    expected_state = "reserved"
    if gate == "validate_submitting":
        db.mark_submitting("s", SCOPE, "delivery")
        expected_state = "submitting"
    # Simulate inconsistent persisted evidence after reservation, from a
    # separate connection; dispatch must fail closed without changing the claim.
    with sqlite3.connect(db.path) as other:
        other.execute("UPDATE operations SET evidence='inconsistent evidence' WHERE action='think'")
    other.close()
    with error("REVIEW_REQUIRED"):
        getattr(db, gate)("s", SCOPE, "delivery")
    assert db.get_operation("s", SCOPE, "delivery")["state"] == expected_state
    assert db.single_work_status("s", SCOPE)["share_used"]
    with error("SINGLE_WORK_LIMIT"):
        reserve(db, "share", "replacement", params=share_params(initial))


def test_single_work_share_preserves_group_permission_epoch(ledger):
    db = ledger
    db.configure_single_work("s", SCOPE, True)
    plan(db)
    initial = render(db)
    accept(db, initial)

    def configure(mode):
        db.configure_share_policy("s", SCOPE, group_id="group", mode=mode, expected_auth_version=1)

    configure("collect_only")
    with error("SHARE_NOT_ALLOWED"):
        reserve(db, "share", "delivery", params=share_params(initial))
    assert not db.single_work_status("s", SCOPE)["share_used"]
    configure("original_group")
    reserve(db, "share", "delivery", params=share_params(initial))
    db.mark_submitting("s", SCOPE, "delivery")
    configure("collect_only")
    configure("original_group")
    with error("SHARE_POLICY_CHANGED"):
        db.validate_submitting("s", SCOPE, "delivery")
    assert db.single_work_status("s", SCOPE)["share_used"]


def test_schema_9_upgrade_preserves_every_old_table_and_does_not_opt_in(tmp_path):
    path = tmp_path / "artist.sqlite3"
    with sqlite3.connect(path) as old:
        old.execute("PRAGMA foreign_keys=ON")
        for version in range(1, 10):
            for statement in getattr(store_module, "_SCHEMA" if version == 1 else f"_SCHEMA_{version}"):
                old.execute(statement)
        old.execute("INSERT INTO spaces VALUES('s',?,'backend',1,1)", (SCOPE.snapshot(),))
        old.execute("INSERT INTO grants VALUES('s',1,1,'old grant','admin','then')")
        old.execute("INSERT INTO works VALUES('s','old','then')")
        old.execute(
            """INSERT INTO operations(id,space_id,work_id,request_id,action,category,params_json,
            scope_snapshot,binding_id,auth_version,trigger,state,budget_day,job_id,created_at,updated_at)
            VALUES(1,'s','old','render','submit','render','{}',?,'backend',1,'idle',
            'succeeded','2026-11-04','old-job','then','then')""", (SCOPE.snapshot(),),
        )
        old.execute("INSERT INTO versions VALUES('s','old-version','old','render','old-job',?,'digest','image/png',NULL,'then')",
                    (b"old bytes",))
        old.execute("INSERT INTO legacy_locks VALUES('legacy-job','sending','unknown','old-digest')")
        old.execute("INSERT INTO group_share_policy VALUES('s',?,?,?,'group','collect_only',1,1)",
                    (SCOPE.snapshot(), SCOPE.platform, SCOPE.stream_id))
        old.execute("PRAGMA user_version=9")
        old.commit()
        tables = [row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        previous = {table: old.execute(f'SELECT * FROM "{table}"').fetchall() for table in tables}
    old.close()
    db = ArtistStore(path)
    try:
        assert db.db.execute("PRAGMA user_version").fetchone()[0] == 10
        assert db.db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.single_work_status("s", SCOPE) == status(enabled=False)
        assert not db.single_work_for_stream(SCOPE.platform, SCOPE.stream_id)
        for table, rows in previous.items():
            assert [tuple(row) for row in db.db.execute(f'SELECT * FROM "{table}"')] == rows
        with error("SINGLE_WORK_HISTORY"):
            db.configure_single_work("s", SCOPE, True)
        assert db.create_work("s", SCOPE, "new")["work_id"] == "new"
    finally:
        db.close()
