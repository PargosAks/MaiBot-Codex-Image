"""Offline collection/share authorization epochs; no Host, MCP or network."""

from datetime import datetime, timezone
import sqlite3

import pytest

from image_plugin_under_test.policy import Delegation, Limits, Scope, StoreError
from image_plugin_under_test.store import ArtistStore


SCOPE = Scope("qq", "group-stream", "owner")
LIMITS = Limits(think=5, render=5, share=5, max_inflight=10, max_revisions=1)


def error(code):
    return pytest.raises(StoreError, check=lambda exc: exc.code == code)


@pytest.fixture
def ledger(tmp_path):
    store = ArtistStore(tmp_path / "artist.sqlite3", clock=lambda: datetime(2026, 11, 4, tzinfo=timezone.utc))
    store.create_space("s", SCOPE, binding_id="backend")
    store.grant("s", SCOPE, Delegation("explicit", "admin", True))
    store.set_global_limits(LIMITS)
    store.set_limits("s", SCOPE, LIMITS)
    store.create_work("s", SCOPE, "work")
    yield store
    store.close()


def configure(store, mode="original_group", group="group1", **kwargs):
    return store.configure_share_policy(
        "s", SCOPE, group_id=group, mode=mode,
        expected_auth_version=kwargs.pop("expected_auth_version", store.get_space("s", SCOPE)["auth_version"]),
        **kwargs,
    )


def policy(store):
    row = store.db.execute("SELECT * FROM group_share_policy WHERE space_id='s'").fetchone()
    return None if row is None else dict(row)


def rows(store, table):
    return [tuple(row) for row in store.db.execute("SELECT * FROM " + table)]


def render(store, request="render", job="job"):
    store.reserve("s", SCOPE, request_id=request, action="submit", params={"prompt": "rain"}, work_id="work")
    store.mark_submitting("s", SCOPE, request)
    store.mark_accepted("s", SCOPE, request, job_id=job)
    store.settle("s", SCOPE, request, outcome="succeeded", evidence="mock PNG")


def share(store, request="share", job="job"):
    return store.reserve("s", SCOPE, request_id=request, action="share", params={"job_id": job})


def thoughts(store, group="group1", enabled=True):
    return store.sync_group_thoughts(
        "s", SCOPE, group_id=group, thoughts=[], enabled=enabled,
        expected_auth_version=store.get_space("s", SCOPE)["auth_version"],
    )


def test_private_legacy_and_private_configuration_noop(ledger):
    store = ledger
    assert store.sharing_policy("s", SCOPE) == {"mode": "private", "allowed": True}
    before = rows(store, "spaces"), rows(store, "grants")
    assert configure(store, "private", "") == {"mode": "private", "allowed": True}
    assert policy(store) is None
    assert before == (rows(store, "spaces"), rows(store, "grants"))
    assert store.check_share_target("s", SCOPE, group_id="")
    with error("SHARE_TARGET_MISMATCH"):
        store.check_share_target("s", SCOPE, group_id="group1")
    render(store)
    share(store)
    assert rows(store, "share_policy_claims") == []
    store.mark_submitting("s", SCOPE, "share")
    assert store.validate_submitting("s", SCOPE, "share")["state"] == "submitting"
    store.settle("s", SCOPE, "share", outcome="succeeded", evidence="mock receipt")


@pytest.mark.parametrize("enabled", [False, True])
def test_known_old_group_is_collect_only_even_when_collection_disabled(ledger, enabled):
    store = ledger
    thoughts(store, enabled=enabled)
    assert store.sharing_policy("s", SCOPE) == {"mode": "collect_only", "allowed": False}
    before = store.budget_usage("s", SCOPE)
    with error("SHARE_NOT_ALLOWED"):
        share(store, job="nonexistent")
    assert store.budget_usage("s", SCOPE) == before
    assert rows(store, "operations") == []
    with error("SHARE_NOT_ALLOWED"):
        store.check_share_target("s", SCOPE, group_id="group1")
    with error("SHARE_TARGET_MISMATCH"):
        store.check_share_target("s", SCOPE, group_id="")


def test_collection_and_share_permissions_are_independent(ledger):
    store = ledger
    configure(store, "collect_only")
    thoughts(store, enabled=True)
    assert store.sharing_policy("s", SCOPE) == {"mode": "collect_only", "allowed": False}
    render(store)
    with error("SHARE_NOT_ALLOWED"):
        share(store)
    configure(store)
    thoughts(store, enabled=False)
    assert store.sharing_policy("s", SCOPE) == {"mode": "original_group", "allowed": True}
    assert store.check_share_target("s", SCOPE, group_id="group1")
    op = share(store)
    assert rows(store, "share_policy_claims") == [(op["id"], 2)]
    store.mark_submitting("s", SCOPE, "share")
    store.validate_submitting("s", SCOPE, "share")
    store.settle("s", SCOPE, "share", outcome="succeeded", evidence="mock receipt")
    configure(store, "collect_only")
    assert share(store)["state"] == "succeeded"


def test_same_configuration_reload_keeps_epoch_and_existing_claim(ledger):
    store = ledger
    configure(store)
    render(store)
    op = share(store)
    first = policy(store)
    for _ in range(3):
        configure(store)
    assert policy(store) == first
    store.mark_submitting("s", SCOPE, "share")
    store.validate_submitting("s", SCOPE, "share")
    assert rows(store, "share_policy_claims") == [(op["id"], 1)]
    path = store.path
    store.close()
    reopened = ArtistStore(path, clock=store.clock)
    try:
        configure(reopened)
        assert policy(reopened) == first
        reopened.validate_submitting("s", SCOPE, "share")
    finally:
        reopened.close()


@pytest.mark.parametrize("state", ["reserved", "submitting"])
@pytest.mark.parametrize("restore", [False, True])
def test_revocation_blocks_old_claim_without_rewriting_record_or_hold(ledger, state, restore):
    store = ledger
    configure(store)
    render(store)
    share(store)
    if state == "submitting":
        store.mark_submitting("s", SCOPE, "share")
    before = rows(store, "operations"), rows(store, "share_policy_claims"), store.budget_usage("s", SCOPE)
    configure(store, "collect_only")
    if restore:
        configure(store)
    assert policy(store)["policy_version"] == (3 if restore else 2)
    with error("SHARE_POLICY_CHANGED"):
        if state == "reserved":
            store.mark_submitting("s", SCOPE, "share")
        else:
            store.validate_submitting("s", SCOPE, "share")
    assert before == (rows(store, "operations"), rows(store, "share_policy_claims"), store.budget_usage("s", SCOPE))
    assert share(store)["state"] == state  # Inspection is not a new dispatch.


@pytest.mark.parametrize("state", ["unknown", "succeeded", "failed"])
def test_historical_outcomes_remain_idempotently_queryable(ledger, state):
    store = ledger
    configure(store)
    render(store)
    share(store)
    store.mark_submitting("s", SCOPE, "share")
    if state == "unknown":
        store.mark_unknown("s", SCOPE, "share", reason="mock timeout")
    else:
        store.settle("s", SCOPE, "share", outcome=state, evidence="mock receipt")
    before = rows(store, "operations"), store.budget_usage("s", SCOPE)
    configure(store, "collect_only")
    assert share(store)["state"] == state
    assert before == (rows(store, "operations"), store.budget_usage("s", SCOPE))
    with error("INVALID_STATE"):
        store.mark_submitting("s", SCOPE, "share")
    if state == "unknown":
        configure(store)
        with error("SHARE_LOCK"):
            share(store, request="another")


def test_cancel_reserved_claim_then_explicit_fresh_share_uses_new_epoch(ledger):
    store = ledger
    configure(store)
    render(store)
    old = share(store)
    configure(store, "collect_only")
    store.cancel_reservation("s", SCOPE, "share", reason="permission revoked before dispatch")
    assert share(store)["state"] == "rejected"
    configure(store)
    new = share(store, "fresh")
    assert rows(store, "share_policy_claims") == [(old["id"], 1), (new["id"], 3)]
    store.mark_submitting("s", SCOPE, "fresh")
    store.validate_submitting("s", SCOPE, "fresh")
    assert store.budget_usage("s", SCOPE)["share"]["total"] == 1


def test_authorization_version_advances_policy_even_when_mode_unchanged(ledger):
    store = ledger
    configure(store)
    render(store)
    share(store)
    oldauth = store.get_space("s", SCOPE)["auth_version"]
    store.revoke("s", SCOPE, source_ref="revoked", granted_by="admin")
    assert store.sharing_policy("s", SCOPE) == {"mode": "original_group", "allowed": False}
    store.grant("s", SCOPE, Delegation("new consent", "admin", True))
    with error("SHARE_POLICY_CHANGED"):
        share(store, "fresh")
    with error("AUTH_CHANGED"):
        configure(store, expected_auth_version=oldauth)
    configure(store)
    assert policy(store)["policy_version"] == 2
    with error("AUTH_CHANGED"):
        store.mark_submitting("s", SCOPE, "share")
    assert share(store)["state"] == "reserved"


@pytest.mark.parametrize("first", ["thought", "share"])
def test_group_binding_is_immutable_across_both_policy_registries(ledger, first):
    store = ledger
    (thoughts if first == "thought" else configure)(store)
    before = rows(store, "group_share_policy"), rows(store, "group_thought_policy")
    with error("OWNER_CONFLICT"):
        configure(store, group="group2")
    with error("OWNER_CONFLICT"):
        configure(store, "private", "")
    if first == "share":
        with error("OWNER_CONFLICT"):
            thoughts(store, "group2")
    assert before == (rows(store, "group_share_policy"), rows(store, "group_thought_policy"))
    with error("SHARE_TARGET_MISMATCH"):
        store.check_share_target("s", SCOPE, group_id="group2")
    with error("SHARE_TARGET_MISMATCH"):
        store.check_share_target("s", SCOPE, group_id="")


@pytest.mark.parametrize("source", ["thought", "share"])
@pytest.mark.parametrize("same_stream", [False, True])
def test_cross_space_stream_or_group_binding_collision(ledger, source, same_stream):
    store = ledger
    (thoughts if source == "thought" else configure)(store)
    other = Scope("qq", SCOPE.stream_id if same_stream else "other-stream", "other-owner")
    store.create_space("other", other, binding_id="backend")
    store.grant("other", other, Delegation("explicit", "admin", True))
    group = "different-group" if same_stream else "group1"
    with error("OWNER_CONFLICT"):
        store.configure_share_policy("other", other, group_id=group, mode="original_group", expected_auth_version=1)
    with error("OWNER_CONFLICT"):
        store.sync_group_thoughts("other", other, group_id=group, enabled=True, thoughts=[], expected_auth_version=1)
    assert store.sharing_policy("other", other) == {"mode": "private", "allowed": True}
    assert not store.db.execute("SELECT 1 FROM group_share_policy WHERE space_id='other'").fetchone()


def test_same_group_identifier_on_different_platform_is_allowed(ledger):
    store = ledger
    configure(store)
    other = Scope("other-platform", SCOPE.stream_id, "owner")
    store.create_space("other", other, binding_id="backend")
    store.grant("other", other, Delegation("explicit", "admin", True))
    assert store.configure_share_policy("other", other, group_id="group1", mode="original_group", expected_auth_version=1)["allowed"]
    with error("NOT_FOUND"):
        store.sharing_policy("s", other)
    with error("NOT_FOUND"):
        store.check_share_target("s", other, group_id="group1")


@pytest.mark.parametrize("group,mode,auth", [
    ("", "original_group", 1), ("group1", "private", 1), (" group1", "collect_only", 1),
    ("group\n", "original_group", 1), (None, "private", 1), ("x" * 201, "original_group", 1),
    ("group1", "invalid", 1), ("group1", [], 1), ("group1", "collect_only", True),
    ("group1", "collect_only", -1),
])
def test_invalid_configuration_is_atomic(ledger, group, mode, auth):
    with error("INVALID_ARGUMENT"):
        configure(ledger, mode, group, expected_auth_version=auth)
    assert rows(ledger, "group_share_policy") == []


def test_share_reservation_and_epoch_claim_roll_back_together(ledger):
    store = ledger
    configure(store)
    render(store)
    before = rows(store, "operations"), store.budget_usage("s", SCOPE)
    store.db.execute("""CREATE TRIGGER mock_disk_failure BEFORE INSERT ON share_policy_claims
                        BEGIN SELECT RAISE(ABORT,'mock disk failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="mock disk failure"):
        share(store)
    assert before == (rows(store, "operations"), store.budget_usage("s", SCOPE))
    assert rows(store, "share_policy_claims") == []


@pytest.mark.parametrize("known_group", [False, True])
@pytest.mark.parametrize("state", ["reserved", "submitting", "unknown", "succeeded"])
def test_schema8_migration_preserves_history_and_group_old_claims_never_dispatch(ledger, known_group, state):
    store = ledger
    render(store)
    share(store)  # Build a legacy claim without any sharing epoch.
    if state != "reserved":
        store.mark_submitting("s", SCOPE, "share")
    if state == "unknown":
        store.mark_unknown("s", SCOPE, "share", reason="mock unknown")
    elif state == "succeeded":
        store.settle("s", SCOPE, "share", outcome="succeeded", evidence="mock sent")
    if known_group:
        thoughts(store)
    before = {table: rows(store, table) for table in (
        "operations", "grants", "limits", "spaces", "group_thought_policy", "artist_wakes")}
    usage = store.budget_usage("s", SCOPE)
    store.db.execute("DROP TABLE share_policy_claims")
    store.db.execute("DROP TABLE group_share_policy")
    store.db.execute("PRAGMA user_version=8")
    path = store.path
    store.close()
    migrated = ArtistStore(path, clock=store.clock)
    try:
        assert migrated.db.execute("PRAGMA user_version").fetchone()[0] == 9
        assert {table: rows(migrated, table) for table in before} == before
        assert migrated.budget_usage("s", SCOPE) == usage
        assert share(migrated)["state"] == state
        assert migrated.sharing_policy("s", SCOPE) == (
            {"mode": "collect_only", "allowed": False} if known_group else {"mode": "private", "allowed": True})
        if state in {"reserved", "submitting"}:
            if known_group:
                configure(migrated)
                with error("SHARE_POLICY_CHANGED"):
                    if state == "reserved":
                        migrated.mark_submitting("s", SCOPE, "share")
                    else:
                        migrated.validate_submitting("s", SCOPE, "share")
            elif state == "reserved":
                migrated.mark_submitting("s", SCOPE, "share")
            else:
                migrated.validate_submitting("s", SCOPE, "share")
        assert migrated.budget_usage("s", SCOPE) == usage
        assert not migrated.db.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        migrated.close()


def test_old_private_reservation_does_not_become_a_group_dispatch_permit(ledger):
    store = ledger
    render(store)
    share(store)
    configure(store)
    with error("SHARE_POLICY_CHANGED"):
        store.mark_submitting("s", SCOPE, "share")
    assert share(store)["state"] == "reserved"
