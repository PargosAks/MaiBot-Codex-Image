"""Isolated catalogue decisions over real SQLite; all external effects scripted."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
import json
import sqlite3

import pytest

from image_plugin_under_test.domain import store as store_module
from image_plugin_under_test.domain.policy import Delegation, Limits, Scope, StoreError, canonical_json
from image_plugin_under_test.domain.store import ArtistStore, SCHEMA_VERSION
from image_plugin_under_test.creation.thinking import Thinking
from test_artist_flow import BRIEF, SCOPE, step, studio as studio_fixture
from test_profiles_flow import PROFILES, SERIES


studio = studio_fixture
DATA = PROFILES[0]["data"]
NEW = {"action": "create_profile", "reason": "Explore a personal style", "data": DATA}


def thought(store, decision, request="catalog", *, task="plan", state="succeeded", context=None):
    if context is None:
        context = {"profiles": store.list_profiles("s", SCOPE), "series": store.list_series("s", SCOPE)}
    auth = store.get_space("s", SCOPE)["auth_version"]
    store.reserve("s", SCOPE, request_id=request, action="think", params={"task": task, "context": context},
                  trigger="idle", expected_auth_version=auth)
    if state != "reserved":
        store.mark_submitting("s", SCOPE, request)
        if state == "unknown":
            store.mark_unknown("s", SCOPE, request, reason="synthetic-lost-response")
        else:
            store.settle("s", SCOPE, request, outcome=state,
                         evidence=canonical_json({"ideas": [], "decision": decision}))
    return request


def apply(store, request="catalog", auth=1):
    return store.apply_catalog_decision("s", SCOPE, request_id=request, expected_auth_version=auth)


def catalogue(store):
    return {"profiles": store.list_profiles("s", SCOPE), "series": store.list_series("s", SCOPE)}


async def decide(runtime, decision):
    async def generate(request):
        assert request["task"] == "plan"
        return {"ideas": [], "decision": deepcopy(decision)}
    runtime.thinking = Thinking(generate)


async def test_create_revise_series_and_frozen_work_with_no_implicit_render(studio):
    runtime, store, _, _, renderer, sender = studio
    await decide(runtime, NEW)
    first = await runtime.tick()
    assert first["state"] == "profile_created"
    profile = first["entry"]
    assert profile["revision"] == 1
    series_decision = {"action": "create_series", "reason": "Study windows", "title": "Windows",
                       "goal": "Explore window light", "profile_id": profile["profile_id"], "profile_revision": 1}
    await decide(runtime, series_decision)
    series = (await step(studio))["entry"]
    choice = {"profile_id": profile["profile_id"], "profile_revision": 1, "series_id": series["series_id"],
              "changes": {}, "reason": "Keep continuity"}
    work = store.plan_work("s", SCOPE, work_id="frozen", brief=BRIEF, inspiration_keys=[], creative=choice,
                           expected_auth_version=1)
    revision = {"action": "revise_profile", "reason": "Explore cooler colors", "profile_id": profile["profile_id"],
                "base_revision": 1, "data": {**deepcopy(DATA), "palette": "cold blue"}}
    await decide(runtime, revision)
    assert (await step(studio))["state"] == "profile_revised"
    assert {p["revision"] for p in store.list_profiles("s", SCOPE)} == {1, 2}
    assert store.list_series("s", SCOPE) == [series]
    assert store.get_artist_work("s", SCOPE, "frozen")["brief"] == work["brief"]
    sources = [dict(row) for row in store.db.execute("SELECT * FROM creative_sources")]
    assert len(sources) == 3 and all(row["origin"] == "autonomous" for row in sources)
    assert all(json.loads(row["snapshot"])["context"] is not None for row in sources)
    assert all(row["source_ref"] and row["auth_version"] == 1 for row in sources)
    assert renderer.calls == sender.calls == []
    assert len(store.audit_operations("s", SCOPE)) == 3
    store.close_series("s", SCOPE, series["series_id"], expected_auth_version=1)
    assert [p["revision"] for p in store.list_profiles("s", SCOPE)] == [2]
    assert store.plan_work("s", SCOPE, work_id="frozen", brief=BRIEF, inspiration_keys=[], creative=choice,
                           expected_auth_version=1) == work


def test_import_once_empty_reload_and_explicit_compatibility(studio):
    _, store, *_ = studio
    assert store.import_creative("s", SCOPE, profiles=PROFILES, series=SERIES, expected_auth_version=1)
    thought(store, NEW)
    profile = apply(store)
    assert not store.import_creative("s", SCOPE, profiles=[], series=[], expected_auth_version=1)
    assert not store.import_creative("s", SCOPE, profiles=[{"bad": "ignored after import"}], series=[], expected_auth_version=1)
    assert len(store.list_profiles("s", SCOPE)) == 2
    store.configure_creative("s", SCOPE, profiles=[], series=[], expected_auth_version=1)
    assert catalogue(store) == {"profiles": [profile], "series": []}
    assert not store.import_creative("s", SCOPE, profiles=PROFILES, series=SERIES, expected_auth_version=1)
    store.configure_creative("s", SCOPE, profiles=PROFILES, series=SERIES, expected_auth_version=1)
    assert len(store.list_profiles("s", SCOPE)) == 2
    store.close_series("s", SCOPE, SERIES[0]["series_id"], expected_auth_version=1)
    store.configure_creative("s", SCOPE, profiles=PROFILES, series=SERIES, expected_auth_version=1)
    assert store.list_series("s", SCOPE) == []


def test_empty_first_import_is_still_once_and_existing_catalogue_is_preserved(studio):
    _, store, *_ = studio
    assert store.import_creative("s", SCOPE, profiles=[], series=[], expected_auth_version=1)
    assert not store.import_creative("s", SCOPE, profiles=PROFILES, series=SERIES, expected_auth_version=1)
    assert catalogue(store) == {"profiles": [], "series": []}


def test_preexisting_autonomous_catalogue_wins_over_first_config_import(studio):
    _, store, *_ = studio
    thought(store, NEW)
    created = apply(store)
    assert store.import_creative("s", SCOPE, profiles=[{"obsolete": "invalid legacy config"}], series=[], expected_auth_version=1)
    assert catalogue(store) == {"profiles": [created], "series": []}
    marker = store.db.execute("SELECT source_snapshot FROM creative_imports").fetchone()
    assert json.loads(marker[0])["mode"] == "preserved_existing"


def test_invalid_first_import_rolls_back_and_can_be_corrected(studio):
    _, store, *_ = studio
    with pytest.raises(StoreError):
        store.import_creative("s", SCOPE, profiles=[{"bad": "data"}], series=[], expected_auth_version=1)
    assert store.db.execute("SELECT count(*) FROM creative_imports").fetchone()[0] == 0
    assert store.import_creative("s", SCOPE, profiles=PROFILES, series=SERIES, expected_auth_version=1)
    assert store.creative_catalog("s", SCOPE) == {"profiles": PROFILES, "series": SERIES}


async def test_provenance_freezes_real_chat_instead_of_promoting_model_source_text(studio):
    runtime, store, *_ = studio
    await runtime.on_chat(message_id="synthetic-real-id", text="Consider warm window light")
    await decide(runtime, {**NEW, "data": {**DATA, "source": "model claim of authorization"}})
    assert (await runtime.run_once())["state"] == "profile_created"
    source = dict(store.db.execute("SELECT * FROM creative_sources").fetchone())
    snapshot = json.loads(source["snapshot"])
    assert snapshot["context"]["events"][0]["source_ref"] == "synthetic-real-id"
    assert source["origin"] == "autonomous"
    assert source["source_ref"] != "model claim of authorization"
    assert source["auth_version"] == 1
    assert len(store.audit_authorizations("s", SCOPE)) == 1


def test_replay_after_reopen_uses_receipt_without_new_revision_or_budget(studio):
    _, store, *_ = studio
    thought(store, NEW)
    original = apply(store)
    before = store.audit_operations("s", SCOPE)
    reopened = ArtistStore(store.path)
    try:
        assert apply(reopened) == original
        assert catalogue(reopened) == {"profiles": [original], "series": []}
        assert reopened.audit_operations("s", SCOPE) == before
        reopened.configure_creative("s", SCOPE, profiles=[], series=[], expected_auth_version=1)
        assert apply(reopened) == original
    finally:
        reopened.close()


@pytest.mark.parametrize("state", ["reserved", "unknown", "failed"])
def test_unconfirmed_or_failed_thought_cannot_write_catalogue(studio, state):
    _, store, *_ = studio
    thought(store, NEW, state=state)
    with pytest.raises(StoreError):
        apply(store)
    assert catalogue(store) == {"profiles": [], "series": []}


def test_authorization_scope_and_task_are_rechecked(studio):
    _, store, *_ = studio
    thought(store, NEW, task="inspire")
    with pytest.raises(StoreError, match="Only planning"):
        apply(store)
    thought(store, NEW, "good")
    store.revoke("s", SCOPE, source_ref="withdraw", granted_by="test-admin")
    with pytest.raises(StoreError):
        apply(store, "good")
    store.grant("s", SCOPE, Delegation("new-consent", "test-admin", continuous=True))
    with pytest.raises(StoreError):
        apply(store, "good", auth=3)
    with pytest.raises(StoreError):
        store.apply_catalog_decision("s", Scope("synthetic", "other", "owner"), request_id="good", expected_auth_version=3)
    assert catalogue(store) == {"profiles": [], "series": []}


async def test_empty_catalogue_can_create_directly_or_wait_and_zero_budget_blocks_catalogue(studio):
    runtime, store, _, _, renderer, sender = studio
    await decide(runtime, {"action": "wait", "reason": "Rest"})
    assert (await runtime.tick())["state"] == "waiting"
    await decide(runtime, NEW)
    store.set_limits("s", SCOPE, Limits())
    assert (await step(studio))["state"] == "blocked"
    assert catalogue(store) == {"profiles": [], "series": []}
    assert renderer.calls == sender.calls == []


async def test_no_profile_required_for_direct_creation(studio):
    runtime, store, _, _, renderer, sender = studio
    assert (await runtime.tick())["state"] == "planned"
    assert "creative" not in store.list_artist_works("s", SCOPE)[0]["brief"]
    assert catalogue(store) == {"profiles": [], "series": []}
    assert renderer.calls == sender.calls == []


def test_revision_race_and_same_request_are_serialized(studio):
    _, store, *_ = studio
    thought(store, NEW)
    profile = apply(store)
    revision = {"action": "revise_profile", "reason": "New palette", "profile_id": profile["profile_id"],
                "base_revision": 1, "data": {**deepcopy(DATA), "palette": "cold blue"}}
    thought(store, revision, "rev-a")
    thought(store, revision, "rev-b")
    barrier = Barrier(2)

    def worker(request):
        db = ArtistStore(store.path)
        try:
            barrier.wait(timeout=5)
            try:
                return apply(db, request)
            except StoreError as exc:
                return exc.code
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ["rev-a", "rev-b"]))
    assert sum(isinstance(item, dict) for item in results) == 1
    assert store.db.execute("SELECT count(*) FROM profile_versions").fetchone()[0] == 2
    winner = "rev-a" if isinstance(results[0], dict) else "rev-b"
    assert apply(store, winner)["revision"] == 2


def test_series_cannot_restart_closed_goal_and_live_limit_is_rechecked(studio):
    _, store, *_ = studio
    thought(store, NEW)
    profile = apply(store)
    decision = {"action": "create_series", "reason": "Study", "title": "Windows", "goal": "Window Light",
                "profile_id": profile["profile_id"], "profile_revision": 1}
    thought(store, decision, "series-a")
    series = apply(store, "series-a")
    store.close_series("s", SCOPE, series["series_id"], expected_auth_version=1)
    thought(store, {**decision, "goal": "  ＷＩＮＤＯＷ   LIGHT "}, "series-b")
    with pytest.raises(StoreError, match="goal already exists"):
        apply(store, "series-b")
    assert store.db.execute("SELECT count(*) FROM series").fetchone()[0] == 1
    thought(store, NEW, "stale")
    profiles = [{**deepcopy(PROFILES[0]), "profile_id": f"configured-{i}"} for i in range(7)]
    store.configure_creative("s", SCOPE, profiles=profiles, series=[], expected_auth_version=1)
    with pytest.raises(StoreError, match="limit reached"):
        apply(store, "stale")
    assert len(store.list_profiles("s", SCOPE)) == 8


def test_revision_history_limit_rolls_back_without_losing_latest(studio, monkeypatch):
    _, store, *_ = studio
    thought(store, NEW)
    created = apply(store)
    monkeypatch.setattr(store_module, "MAX_PROFILE_VERSIONS", 1)
    thought(store, {"action": "revise_profile", "reason": "study", "profile_id": created["profile_id"],
                    "base_revision": 1, "data": {**DATA, "palette": "blue"}}, "overflow")
    with pytest.raises(StoreError, check=lambda exc: exc.code == "CATALOG_LIMIT"):
        apply(store, "overflow")
    assert catalogue(store) == {"profiles": [created], "series": []}
    assert store.db.execute("SELECT count(*) FROM creative_decisions").fetchone()[0] == 1


def test_work_selects_exact_new_revision_while_series_keeps_old_revision_visible(studio):
    _, store, *_ = studio
    store.configure_creative("s", SCOPE, profiles=PROFILES, series=SERIES, expected_auth_version=1)
    identity = PROFILES[0]["profile_id"]
    changed = {**deepcopy(DATA), "palette": "new blue palette"}
    thought(store, {"action": "revise_profile", "reason": "Palette study", "profile_id": identity,
                    "base_revision": 1, "data": changed})
    apply(store)
    assert [p["revision"] for p in store.creative_catalog("s", SCOPE)["profiles"]] == [1, 2]
    choice = {"profile_id": identity, "profile_revision": 2, "series_id": None, "changes": {}, "reason": "New study"}
    work = store.plan_work("s", SCOPE, work_id="new-study", brief=BRIEF, inspiration_keys=[], creative=choice,
                           expected_auth_version=1)
    assert work["brief"]["creative"]["profile"] == changed
    assert store.list_series("s", SCOPE)[0]["profile_revision"] == 1


@pytest.mark.parametrize("decision", [
    {**NEW, "owner": "forged"}, {**NEW, "revision": 1}, {**NEW, "source_ids": ["not-visible"]},
    {**NEW, "data": {**DATA, "authorized": True}},
    {**NEW, "data": {**DATA, "stable": {"budget": "unlimited"}}},
    {"action": "revise_profile", "reason": "bad", "profile_id": "missing", "base_revision": True, "data": DATA},
    {"action": "create_series", "reason": "bad", "title": "X", "goal": "Y", "profile_id": "missing", "profile_revision": 1},
])
async def test_model_rejects_forgery_and_unknown_bindings_without_catalogue_writes(studio, decision):
    runtime, store, _, _, renderer, sender = studio
    await decide(runtime, decision)
    assert (await runtime.tick()) == {"state": "blocked", "reason": "THINK_FAILED"}
    assert catalogue(store) == {"profiles": [], "series": []}
    assert renderer.calls == sender.calls == []


def seed_v6(path):
    db = sqlite3.connect(path)
    for version in ("_SCHEMA", "_SCHEMA_2", "_SCHEMA_3", "_SCHEMA_4", "_SCHEMA_5", "_SCHEMA_6"):
        for statement in getattr(store_module, version):
            db.execute(statement)
    db.execute("PRAGMA user_version=6")
    db.commit()
    db.close()


def test_v6_migration_reopen_and_failed_migration_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "old-v6.sqlite3"
    seed_v6(path)
    with monkeypatch.context() as patch:
        patch.setattr(store_module, "_SCHEMA_7", (*store_module._SCHEMA_7, "CREATE TABLE works(bad TEXT)"))
        with pytest.raises(sqlite3.DatabaseError):
            ArtistStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='creative_sources'").fetchone()
    for _ in range(2):
        db = ArtistStore(path)
        assert db.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.db.execute("PRAGMA foreign_key_check").fetchall() == []
        db.close()


def test_v6_catalogue_migration_preserves_closed_and_disabled_rows(tmp_path):
    path = tmp_path / "populated-v6.sqlite3"
    seed_v6(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO spaces VALUES('s',?,'old-binding',1,1)", (SCOPE.snapshot(),))
        db.execute("INSERT INTO profile_versions VALUES('s',?,?,?,0)",
                   (PROFILES[0]["profile_id"], 1, canonical_json(DATA)))
        series = SERIES[0]
        db.execute("INSERT INTO series VALUES('s',?,?,?,?,?,0,1)",
                   tuple(series[key] for key in ("series_id", "title", "goal", "profile_id", "profile_revision")))
        before = {name: db.execute(f"SELECT * FROM {name}").fetchall() for name in ("spaces", "profile_versions", "series")}
    db = ArtistStore(path)
    try:
        for table, rows in before.items():
            assert [tuple(row) for row in db.db.execute(f"SELECT * FROM {table}")] == rows
        db.import_creative("s", SCOPE, profiles=PROFILES, series=SERIES, expected_auth_version=1)
        assert catalogue(db) == {"profiles": [], "series": []}
        assert {row[0] for row in db.db.execute("SELECT origin FROM creative_sources")} == {"legacy"}
        marker = json.loads(db.db.execute("SELECT source_snapshot FROM creative_imports").fetchone()[0])
        assert marker["mode"] == "preserved_existing"
        assert marker["series"][0]["closed"] == 1
        assert db.db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()
