"""Synthetic group note adoption, migration and rollback; no Host or network."""

from datetime import datetime, timedelta, timezone
import hashlib
import sqlite3

import pytest

from image_plugin_under_test.domain.policy import Delegation, Limits, Scope, StoreError
from image_plugin_under_test.domain.store import ArtistStore


SCOPE = Scope("qq", "group-stream", "delegator")
OTHER = Scope("qq", "other-stream", "other-owner")
BRIEF = {"intent": "quiet scene", "subject": "rainy street", "mood": "peaceful",
         "prompt": "A quiet rainy street", "completion_criteria": ["rain visible"]}


def note(value="rain"):
    return {"note_id": "gh_" + hashlib.sha256(value.encode()).hexdigest(), "summary": value}


def key(value="rain"):
    return "group-thought:" + note(value)["note_id"]


def error(code):
    return pytest.raises(StoreError, check=lambda exc: exc.code == code)


@pytest.fixture
def ledger(tmp_path):
    now = [datetime(2026, 11, 4, tzinfo=timezone.utc)]
    store = ArtistStore(tmp_path / "artist.sqlite3", clock=lambda: now[0])
    store.create_space("s", SCOPE, binding_id="backend")
    store.grant("s", SCOPE, Delegation("explicit-consent", "admin", True))
    limits = Limits(think=3, render=2, share=1, max_inflight=1, max_revisions=1)
    store.set_global_limits(limits)
    store.set_limits("s", SCOPE, limits)
    yield store, now
    store.close()


def sync(store, thoughts=None, **changes):
    args = dict(group_id="g1", thoughts=[note()] if thoughts is None else thoughts,
                enabled=True, expected_auth_version=store.get_space("s", SCOPE)["auth_version"])
    args.update(changes)
    return store.sync_group_thoughts("s", SCOPE, **args)


def plan(store, work="painting", keys=None, **kwargs):
    return store.plan_work("s", SCOPE, work_id=work, brief=BRIEF,
                           inspiration_keys=[key()] if keys is None else keys, **kwargs)


def rows(store, table):
    return [tuple(row) for row in store.db.execute("SELECT * FROM " + table)]


def test_sync_is_durable_idempotent_completion_wake_and_does_not_spend(ledger):
    store, _ = ledger
    before = store.budget_usage("s", SCOPE)
    assert sync(store) is True
    first = rows(store, "group_thought_links")
    assert sync(store) is False
    assert sync(store, []) is False
    assert rows(store, "group_thought_links") == first
    wakes = store.db.execute("SELECT event_id,kind,text,source_ref FROM artist_wakes").fetchall()
    assert [tuple(row) for row in wakes] == [("thought:" + note()["note_id"], "completion", "", None)]
    inspiration, = store.list_inspirations("s", SCOPE)
    assert (inspiration["key"], inspiration["origin"], inspiration["source_refs"]) == (
        key(), "group_thought", [note()["note_id"]])
    assert store.budget_usage("s", SCOPE) == before
    assert not store.audit_operations("s", SCOPE)
    path = store.path
    store.close()
    reopened = ArtistStore(path)
    try:
        assert reopened.list_inspirations("s", SCOPE)[0]["key"] == key()
        assert sync(reopened) is False
    finally:
        reopened.close()


def test_group_notes_survive_normal_cache_ttl_and_pruning(ledger):
    store, now = ledger
    sync(store)
    for index in range(105):
        store.remember_inspiration("s", SCOPE, key=str(index), summary="ordinary idea",
                                   source_refs=[], origin="idle", ttl_seconds=1)
    now[0] += timedelta(days=400)
    assert [item["key"] for item in store.list_inspirations("s", SCOPE)] == [key()]
    assert len(rows(store, "inspirations")) == 100
    plan(store)
    adopted, = rows(store, "group_thought_links")
    assert adopted[5] == "painting" and adopted[6] is not None
    assert store.list_inspirations("s", SCOPE) == []


def test_adoption_disable_reenable_replay_and_terminal_work_cannot_redraw(ledger):
    store, _ = ledger
    sync(store)
    work = plan(store)
    adopted = rows(store, "group_thought_links")
    assert sync(store, [], enabled=False) is False
    assert store.get_artist_work("s", SCOPE, "painting") == work
    assert plan(store) == work  # Existing work replay does not require a new adoption.
    assert sync(store, enabled=True) is False
    assert rows(store, "group_thought_links") == adopted
    store.update_artist_work("s", SCOPE, "painting", changes={"status": "abandoned"})
    with error("NOT_FOUND"):
        plan(store, "second")
    assert store.list_inspirations("s", SCOPE) == []
    assert len(rows(store, "artist_wakes")) == 1


def test_disable_blocks_new_adoption_and_does_not_wake(ledger):
    store, _ = ledger
    sync(store)
    sync(store, [], enabled=False)
    assert not store.list_inspirations("s", SCOPE)
    with error("NOT_FOUND"):
        plan(store)
    assert sync(store, [note("night")], enabled=False) is True
    assert len(rows(store, "artist_wakes")) == 1
    sync(store, [], enabled=True)
    assert {item["key"] for item in store.list_inspirations("s", SCOPE)} == {key(), key("night")}
    assert len(rows(store, "artist_wakes")) == 1


def test_revoke_and_regrant_requires_explicit_current_policy_sync(ledger):
    store, _ = ledger
    sync(store)
    original = store.get_space("s", SCOPE)["auth_version"]
    store.revoke("s", SCOPE, source_ref="revoke", granted_by="admin")
    with error("AUTH_REQUIRED"):
        store.list_inspirations("s", SCOPE)
    with error("AUTH_CHANGED"):
        sync(store, expected_auth_version=original)
    with error("AUTH_REQUIRED"):
        sync(store)
    store.grant("s", SCOPE, Delegation("new-consent", "admin", True))
    assert not store.list_inspirations("s", SCOPE)
    with error("NOT_FOUND"):
        plan(store)
    assert sync(store, []) is False
    assert store.list_inspirations("s", SCOPE)[0]["key"] == key()
    with error("AUTH_CHANGED"):
        plan(store, expected_auth_version=original)
    plan(store)


def test_scope_group_owner_and_note_isolation(ledger):
    store, _ = ledger
    sync(store)
    with error("OWNER_CONFLICT"):
        sync(store, group_id="changed-group")
    with error("NOT_FOUND"):
        store.sync_group_thoughts("s", OTHER, group_id="g1", thoughts=[], enabled=False,
                                  expected_auth_version=1)
    store.create_space("other", OTHER, binding_id="backend")
    version = store.grant("other", OTHER, Delegation("other-consent", "admin", True))
    with error("OWNER_CONFLICT"):
        store.sync_group_thoughts("other", OTHER, group_id="g1", thoughts=[], enabled=True,
                                  expected_auth_version=version)
    with error("OWNER_CONFLICT"):
        store.sync_group_thoughts("other", OTHER, group_id="g2", thoughts=[note()], enabled=True,
                                  expected_auth_version=version)
    assert store.sync_group_thoughts("other", OTHER, group_id="g2", thoughts=[note("night")],
                                    enabled=True, expected_auth_version=version)
    with error("NOT_FOUND"):
        store.plan_work("other", OTHER, work_id="forged", brief=BRIEF, inspiration_keys=[key()])
    assert store.list_inspirations("s", SCOPE)[0]["key"] == key()


def test_changed_summary_rolls_back_policy_links_and_all_new_wakes(ledger):
    store, _ = ledger
    sync(store)
    sync(store, [], enabled=False)
    before = {name: rows(store, name) for name in ("group_thought_policy", "group_thought_links", "artist_wakes")}
    with error("REQUEST_CONFLICT"):
        sync(store, [note("night"), {**note(), "summary": "changed"}])
    assert {name: rows(store, name) for name in before} == before


def test_plan_rollback_preserves_unadopted_note_and_records(ledger):
    store, _ = ledger
    sync(store)
    before = {name: rows(store, name) for name in ("group_thought_links", "works", "artist_works", "operations")}
    store.db.execute("""CREATE TEMP TRIGGER fail_adoption BEFORE UPDATE OF adopted_by
                        ON group_thought_links BEGIN SELECT RAISE(ABORT,'test rollback'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="test rollback"):
        plan(store)
    assert {name: rows(store, name) for name in before} == before
    store.db.execute("DROP TRIGGER fail_adoption")
    with error("NOT_FOUND"):
        plan(store, keys=[key(), "missing"])
    assert {name: rows(store, name) for name in before} == before
    plan(store)


def test_sync_wake_failure_rolls_back_note_and_policy(ledger):
    store, _ = ledger
    store.db.execute("""CREATE TEMP TRIGGER fail_wake BEFORE INSERT ON artist_wakes
                        BEGIN SELECT RAISE(ABORT,'test wake rollback'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="test wake rollback"):
        sync(store)
    assert rows(store, "group_thought_links") == rows(store, "group_thought_policy") == []


def test_private_initialization_never_binds_group_and_can_safely_disable(ledger):
    store, _ = ledger
    assert sync(store, [], group_id="", enabled=False) is False
    assert rows(store, "group_thought_policy") == []
    for kwargs in ({"thoughts": [], "enabled": True}, {"thoughts": [note()], "enabled": False}):
        with error("INVALID_ARGUMENT"):
            sync(store, group_id="", **kwargs)
    sync(store)
    assert sync(store, [], group_id="", enabled=False) is False
    assert not store.list_inspirations("s", SCOPE)
    assert rows(store, "group_thought_links")


@pytest.mark.parametrize("change", [
    {"thoughts": [{"note_id": "gh_short", "summary": "rain"}]},
    {"thoughts": [{"note_id": "gh_" + "z" * 64, "summary": "rain"}]},
    {"thoughts": [{**note(), "summary": "x" * 513}]},
    {"thoughts": [{**note(), "summary": "rain\nnight"}]},
    {"thoughts": [{**note(), "summary": " rain"}]},
    {"thoughts": [{**note(), "owner": "forged"}]},
    {"thoughts": {}}, {"enabled": 1}, {"expected_auth_version": None},
    {"expected_auth_version": True}, {"group_id": "x" * 201},
    {"thoughts": [note(str(index)) for index in range(129)]},
])
def test_bounded_typed_snapshot_validation(ledger, change):
    store, _ = ledger
    with error("INVALID_ARGUMENT"):
        sync(store, **change)
    assert rows(store, "group_thought_policy") == rows(store, "group_thought_links") == []


def test_persistent_capacity_includes_already_adopted_notes(ledger):
    store, _ = ledger
    sync(store, [note(str(index)) for index in range(128)])
    plan(store, keys=[key("0")])
    with error("INVALID_ARGUMENT"):
        sync(store, [note("overflow")])
    assert len(rows(store, "group_thought_links")) == 128
    assert len(rows(store, "artist_wakes")) == 128


def test_model_cannot_forge_group_origin_or_reserved_keys(ledger):
    store, _ = ledger
    for origin, identifier in (("group_thought", "forged"), ("idle", key())):
        with error("INVALID_ARGUMENT"):
            store.remember_inspiration("s", SCOPE, key=identifier, summary="forged",
                                       source_refs=[note()["note_id"]], origin=origin)
    assert rows(store, "inspirations") == rows(store, "group_thought_links") == []


def test_legacy_ordinary_reserved_key_collision_rejects_without_deleting_data(ledger):
    store, _ = ledger
    store.remember_inspiration("s", SCOPE, key="legacy", summary="old content", source_refs=[], origin="idle")
    store.db.execute("UPDATE inspirations SET key=? WHERE space_id='s'", (key(),))
    original = rows(store, "inspirations")
    with error("REQUEST_CONFLICT"):
        sync(store)
    assert rows(store, "inspirations") == original
    assert rows(store, "group_thought_policy") == rows(store, "group_thought_links") == []
    with error("NOT_FOUND"):
        plan(store)


def test_combined_inspirations_are_globally_sorted_for_artist_tail(ledger):
    store, now = ledger
    sync(store, [note(str(index)) for index in range(60)])
    now[0] += timedelta(minutes=1)
    for origin in ("chat", "idle"):
        store.remember_inspiration("s", SCOPE, key=origin, summary="fresh " + origin,
                                   source_refs=["verified"] if origin == "chat" else [], origin=origin)
    ideas = store.list_inspirations("s", SCOPE)
    assert [(idea["updated_at"], idea["key"]) for idea in ideas] == sorted(
        (idea["updated_at"], idea["key"]) for idea in ideas)
    assert {idea["origin"] for idea in ideas[-50:]} == {"chat", "idle", "group_thought"}


def test_migration_7_to_8_preserves_every_existing_table_and_quota(ledger):
    store, _ = ledger
    store.remember_inspiration("s", SCOPE, key="normal", summary="retained", source_refs=[], origin="idle")
    store.create_work("s", SCOPE, "old-work")
    store.reserve("s", SCOPE, request_id="old-unknown", action="submit", work_id="old-work", params={})
    store.mark_submitting("s", SCOPE, "old-unknown")
    store.mark_unknown("s", SCOPE, "old-unknown", reason="receipt uncertain")
    store.db.execute("DROP TABLE share_policy_claims")
    store.db.execute("DROP TABLE group_share_policy")
    store.db.execute("DROP TABLE group_thought_links")
    store.db.execute("DROP TABLE group_thought_policy")
    store.db.execute("PRAGMA user_version=7")
    tables = [row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    before = {table: rows(store, table) for table in tables}
    usage = store.budget_usage("s", SCOPE)
    path = store.path
    store.close()
    # Compare migration effects on the same budget day, not today's wall clock.
    migrated = ArtistStore(path, clock=store.clock)
    try:
        assert migrated.db.execute("PRAGMA user_version").fetchone()[0] == 9
        assert {table: rows(migrated, table) for table in tables} == before
        assert migrated.budget_usage("s", SCOPE) == usage
        assert migrated.get_operation("s", SCOPE, "old-unknown")["state"] == "unknown"
        assert sync(migrated)
    finally:
        migrated.close()
