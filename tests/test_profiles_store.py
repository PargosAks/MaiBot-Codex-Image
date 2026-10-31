"""Offline creative catalogues, immutable plans and schema migration tests."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
import hashlib
import sqlite3

import pytest

from image_plugin_under_test import store as store_module
from image_plugin_under_test.policy import Delegation, Scope, StoreError, canonical_json
from image_plugin_under_test.profiles import (
    normalize_objective,
    render_prompt,
    validate_catalog,
    validate_choice,
    validate_profile,
    validate_series,
)
from image_plugin_under_test.store import SCHEMA_VERSION, ArtistStore


SCOPE = Scope("test", "creative-stream", "creative-owner")
OTHER = Scope("test", "other-stream", "other-owner")
PROFILE = {
    "profile_id": "watercolor", "revision": 1,
    "data": {"title": "水彩小品", "palette": "暖白与青蓝", "technique": "透明水彩",
             "stable": {"人物": "短发", "画风": "柔和"},
             "variable": {"光线": "清晨", "构图": "近景"}, "source": "用户明确设置"},
}
SERIES = {"series_id": "windows", "title": "窗边", "goal": "研究一天中的光线变化",
          "profile_id": "watercolor", "profile_revision": 1}
CHOICE = {"profile_id": "watercolor", "profile_revision": 1, "series_id": "windows",
          "changes": {"光线": "黄昏"}, "reason": "在同一画风下探索不同光线"}
BRIEF = {"intent": "Study light", "subject": "Window", "mood": "quiet", "prompt": "a quiet window",
         "completion_criteria": "clear warm lighting"}


def error(code="INVALID_ARGUMENT"):
    return pytest.raises(StoreError, check=lambda exc: exc.code == code)


@pytest.fixture
def ledger(tmp_path):
    db = ArtistStore(tmp_path / "creative.sqlite3")
    db.create_space("s", SCOPE, binding_id="creative-test-binding")
    db.grant("s", SCOPE, Delegation("explicit-consent", "host-admin", continuous=True))
    yield db
    db.close()


def configure(db, profiles=None, series=None, *, auth=1):
    return db.configure_creative("s", SCOPE, profiles=deepcopy([PROFILE] if profiles is None else profiles),
                                 series=deepcopy([SERIES] if series is None else series),
                                 expected_auth_version=auth)


def plan(db, work="w", *, creative=CHOICE, brief=None, auth=1):
    return db.plan_work("s", SCOPE, work_id=work, brief=deepcopy(BRIEF if brief is None else brief),
                        inspiration_keys=[], creative=deepcopy(creative), expected_auth_version=auth)


def finish(db, work="w"):
    db.update_artist_work("s", SCOPE, work, changes={"status": "finished"})


def raw_catalog(db):
    return {table: [tuple(row) for row in db.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("profile_versions", "series", "series_members")}


def test_catalogue_shapes_idempotency_and_no_authority_or_budget_side_effects(ledger):
    db = ledger
    grants = db.audit_authorizations("s", SCOPE)
    assert configure(db) == {"profiles": [PROFILE], "series": [SERIES]}
    before = raw_catalog(db)
    assert configure(db) == configure(db)
    assert raw_catalog(db) == before
    profiles, series = db.list_profiles("s", SCOPE), db.list_series("s", SCOPE)
    assert profiles == [PROFILE] and series == [SERIES]
    profiles[0]["data"]["stable"]["人物"] = "changed outside"
    series[0]["goal"] = "changed outside"
    assert db.list_profiles("s", SCOPE) == [PROFILE]
    assert db.list_series("s", SCOPE) == [SERIES]
    assert db.audit_authorizations("s", SCOPE) == grants
    assert db.audit_operations("s", SCOPE) == []
    assert db.db.execute("SELECT count(*) FROM limits").fetchone()[0] == 0


def test_plan_freezes_complete_trusted_profile_and_series_without_mutating_callers(ledger):
    db = ledger
    configure(db)
    choice, brief = deepcopy(CHOICE), deepcopy(BRIEF)
    saved = db.plan_work("s", SCOPE, work_id="w", brief=brief, inspiration_keys=[],
                         creative=choice, expected_auth_version=1)
    assert saved["brief"]["creative"] == {**CHOICE, "profile": PROFILE["data"], "series": SERIES}
    assert brief == BRIEF and choice == CHOICE
    assert plan(db) == saved
    assert db.db.execute("SELECT series_id,work_id FROM series_members").fetchone()[:] == ("windows", "w")
    assert db.audit_operations("s", SCOPE) == []
    with error("REQUEST_CONFLICT"):
        plan(db, creative={**CHOICE, "reason": "changed reason"})
    with error("REQUEST_CONFLICT"):
        plan(db, creative=None)
    with error():
        plan(db, work="forged", creative=None, brief={**BRIEF, "creative": saved["brief"]["creative"]})


def test_reconfiguration_and_close_do_not_change_existing_plan_or_render_prompt(ledger):
    db = ledger
    configure(db)
    saved = plan(db)
    rendered = render_prompt(saved["brief"]["prompt"], saved["brief"]["creative"])
    changed_series = {**SERIES, "title": "新标题", "goal": "新的系列说明"}
    with error("REQUEST_CONFLICT"):
        configure(db, series=[changed_series])
    assert db.list_series("s", SCOPE) == [SERIES]
    db.close_series("s", SCOPE, "windows", expected_auth_version=1)
    configure(db, profiles=[], series=[])
    assert plan(db) == saved  # Inspection does not make a new selection.
    assert render_prompt(saved["brief"]["prompt"], saved["brief"]["creative"]) == rendered
    assert db.get_artist_work("s", SCOPE, "w") == saved
    reopened = ArtistStore(db.path)
    try:
        assert plan(reopened) == saved
        assert reopened.list_profiles("s", SCOPE) == []
        assert reopened.list_series("s", SCOPE) == []
        assert render_prompt(saved["brief"]["prompt"], saved["brief"]["creative"]) == rendered
    finally:
        reopened.close()


def test_new_revision_is_active_old_revision_retained_and_series_binding_fixed(ledger):
    db = ledger
    configure(db)
    saved = plan(db)
    finish(db)
    saved = db.get_artist_work("s", SCOPE, "w")
    revised = deepcopy(PROFILE)
    revised["revision"] = 2
    revised["data"]["palette"] = "浓烈的红色"
    revised_series = {**SERIES, "series_id": "new-windows", "profile_revision": 2}
    configure(db, profiles=[revised], series=[revised_series])
    assert db.list_profiles("s", SCOPE) == [revised]
    rows = db.db.execute("SELECT revision,active FROM profile_versions ORDER BY revision").fetchall()
    assert [tuple(row) for row in rows] == [(1, 0), (2, 1)]
    assert db.get_artist_work("s", SCOPE, "w") == saved
    with error():
        plan(db, "old-revision")
    next_choice = {**CHOICE, "profile_revision": 2, "series_id": "new-windows"}
    next_work = plan(db, "next", creative=next_choice)
    assert next_work["brief"]["creative"]["profile"] == revised["data"]
    before = raw_catalog(db)
    with error("REQUEST_CONFLICT"):
        configure(db, profiles=[revised], series=[{**SERIES, "profile_revision": 2}])
    assert raw_catalog(db) == before


def test_profile_conflict_rolls_back_visibility_and_all_new_catalogue_rows(ledger):
    db = ledger
    configure(db)
    before = raw_catalog(db)
    changed = deepcopy(PROFILE)
    changed["data"]["palette"] = "attempted mutation"
    extra = {**deepcopy(PROFILE), "profile_id": "extra"}
    with error("REQUEST_CONFLICT"):
        configure(db, profiles=[extra, changed])
    assert raw_catalog(db) == before
    assert db.list_profiles("s", SCOPE) == [PROFILE]


@pytest.mark.parametrize("field,value", [("title", "new title"), ("goal", "new goal")])
def test_series_title_and_goal_are_immutable_and_conflict_rolls_back_entire_sync(ledger, field, value):
    db = ledger
    configure(db)
    before = raw_catalog(db)
    extra = {**deepcopy(PROFILE), "profile_id": "extra"}
    with error("REQUEST_CONFLICT"):
        configure(db, profiles=[PROFILE, extra], series=[{**SERIES, field: value}])
    assert raw_catalog(db) == before
    assert db.list_series("s", SCOPE) == [SERIES]


@pytest.mark.parametrize("closed", [False, True])
def test_removed_or_closed_series_cannot_be_newly_selected_but_snapshot_survives(ledger, closed):
    db = ledger
    configure(db)
    saved = plan(db)
    finish(db)
    saved = db.get_artist_work("s", SCOPE, "w")
    if closed:
        db.close_series("s", SCOPE, "windows", expected_auth_version=1)
        db.close_series("s", SCOPE, "windows", expected_auth_version=1)
        configure(db)
    else:
        configure(db, series=[])
    assert db.list_series("s", SCOPE) == []
    with error():
        plan(db, "new-work", brief={**BRIEF, "intent": "A different objective"})
    assert db.get_artist_work("s", SCOPE, "w") == saved
    assert db.db.execute("SELECT count(*) FROM series").fetchone()[0] == 1
    if closed:
        configure(db, profiles=[], series=[])
        configure(db)
        assert db.list_series("s", SCOPE) == []
        reopened = ArtistStore(db.path)
        try:
            configure(reopened)
            assert reopened.list_series("s", SCOPE) == []
        finally:
            reopened.close()


def test_removed_profile_cannot_be_selected_and_exact_revision_can_be_reenabled(ledger):
    db = ledger
    configure(db)
    configure(db, profiles=[], series=[])
    assert db.list_profiles("s", SCOPE) == []
    with error():
        plan(db, creative={**CHOICE, "series_id": None})
    configure(db)
    assert plan(db)["brief"]["creative"]["profile"] == PROFILE["data"]


def test_no_creative_preserves_old_brief_and_does_not_require_catalogue(ledger):
    db = ledger
    assert db.list_profiles("s", SCOPE) == [] and db.list_series("s", SCOPE) == []
    saved = plan(db, creative=None)
    assert saved["brief"] == BRIEF
    assert "creative" not in saved["brief"]
    assert plan(db, creative=None) == saved
    assert render_prompt("  原文\nwith spacing  ", None) == "  原文\nwith spacing  "
    assert raw_catalog(db) == {"profile_versions": [], "series": [], "series_members": []}


@pytest.mark.parametrize("status", ["planned", "deferred", "creating", "review_pending"])
def test_active_work_still_blocks_different_series_objective_or_plain_work(ledger, status):
    db = ledger
    configure(db)
    plan(db)
    db.update_artist_work("s", SCOPE, "w", changes={"status": status})
    for creative in (CHOICE, None):
        with error("WORK_BUSY"):
            plan(db, "other-work", creative=creative, brief={**BRIEF, "intent": "a new goal"})
    assert len(db.list_artist_works("s", SCOPE)) == 1


@pytest.mark.parametrize("status", ["finished", "abandoned"])
def test_series_rejects_normalized_duplicate_objective_across_work_ids_and_restart(ledger, status):
    db = ledger
    configure(db)
    plan(db)
    db.update_artist_work("s", SCOPE, "w", changes={"status": status})
    changed = {**BRIEF, "intent": "  ＳＴＵＤＹ\t LIGHT  ", "subject": "\nwindow  "}
    with error("REQUEST_CONFLICT"):
        plan(db, "duplicate", brief=changed)
    reopened = ArtistStore(db.path)
    try:
        with error("REQUEST_CONFLICT"):
            plan(reopened, "duplicate-after-restart", brief=changed)
        assert plan(reopened, "different", brief={**BRIEF, "subject": "Garden"})["work_id"] == "different"
    finally:
        reopened.close()
    assert db.db.execute("SELECT count(*) FROM series_members").fetchone()[0] == 2


def test_same_objective_in_distinct_series_is_allowed(ledger):
    db = ledger
    second = {**SERIES, "series_id": "other-series"}
    configure(db, series=[SERIES, second])
    plan(db)
    finish(db)
    assert plan(db, "other", creative={**CHOICE, "series_id": "other-series"})["work_id"] == "other"


def test_cross_space_catalogues_and_plan_choices_do_not_leak(ledger):
    db = ledger
    configure(db)
    db.create_space("other", OTHER, binding_id="creative-test-binding")
    db.grant("other", OTHER, Delegation("other-consent", "host-admin", continuous=True))
    assert db.list_profiles("other", OTHER) == []
    assert db.list_series("other", OTHER) == []
    with error():
        db.plan_work("other", OTHER, work_id="w", brief=BRIEF, inspiration_keys=[],
                     creative=CHOICE, expected_auth_version=1)
    for call in (
        lambda: db.configure_creative("s", OTHER, profiles=[PROFILE], series=[SERIES], expected_auth_version=1),
        lambda: db.list_profiles("s", OTHER),
        lambda: db.list_series("s", OTHER),
        lambda: db.close_series("other", OTHER, "windows", expected_auth_version=1),
    ):
        with error("NOT_FOUND"):
            call()
    other_profile = deepcopy(PROFILE)
    other_profile["data"]["palette"] = "separate owner palette"
    db.configure_creative("other", OTHER, profiles=[other_profile], series=[SERIES], expected_auth_version=1)
    assert db.list_profiles("s", SCOPE) == [PROFILE]
    assert db.list_profiles("other", OTHER) == [other_profile]


def test_every_creative_write_rechecks_authorization_and_old_plans_remain_readable(ledger):
    db = ledger
    configure(db)
    saved = plan(db)
    before = raw_catalog(db)
    db.revoke("s", SCOPE, source_ref="withdrawn", granted_by="admin")
    assert db.get_artist_work("s", SCOPE, "w") == saved
    for auth, code in ((1, "AUTH_CHANGED"), (2, "AUTH_REQUIRED")):
        for call in (
            lambda: configure(db, auth=auth),
            lambda: db.close_series("s", SCOPE, "windows", expected_auth_version=auth),
            lambda: plan(db, auth=auth),
        ):
            with error(code):
                call()
    for call in (lambda: db.list_profiles("s", SCOPE), lambda: db.list_series("s", SCOPE)):
        with error("AUTH_REQUIRED"):
            call()
    assert raw_catalog(db) == before
    db.grant("s", SCOPE, Delegation("new-consent", "admin", continuous=True))
    with error("AUTH_CHANGED"):
        configure(db, auth=1)
    configure(db, auth=3)
    assert plan(db, auth=3) == saved


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(scope="forged"),
    lambda p: p.update(revision=True),
    lambda p: p.update(revision=0),
    lambda p: p.update(profile_id="x" * 65),
    lambda p: p["data"].update(title=""),
    lambda p: p["data"].update(palette="x" * 401),
    lambda p: p["data"].update(source="x" * 401),
    lambda p: p["data"].update(authorized=True),
    lambda p: p["data"].update(stable={"owner": "admin"}),
    lambda p: p["data"].update(stable={" Auth_Version ": "1"}),
    lambda p: p["data"].update(stable={"ＯＷＮＥＲ": "admin"}),
    lambda p: p["data"].update(stable={"permission": "allow"}),
    lambda p: p["data"].update(stable={"x": "a", "X": "b"}),
    lambda p: p["data"].update(variable={"人物": "overlap"}),
    lambda p: p["data"].update(stable={" LIGHT ": "fixed"}, variable={"light": "mutable"}),
    lambda p: p["data"].update(variable={str(i): "v" for i in range(13)}),
    lambda p: p["data"].update(stable={"key": "x" * 401}),
    lambda p: p["data"].update(stable={"key": {"owner": "forged"}}),
    lambda p: p["data"].update(technique="bad\x00text"),
])
def test_profile_shape_bounds_overlap_and_authority_fields_rejected(ledger, mutation):
    value = deepcopy(PROFILE)
    mutation(value)
    with error():
        validate_profile(value)
    with error():
        configure(ledger, profiles=[value])
    assert ledger.list_profiles("s", SCOPE) == []


def test_catalogue_limits_duplicates_and_unavailable_series_bindings(ledger):
    for profiles, series in (
        ([{**PROFILE, "profile_id": str(i)} for i in range(9)], []),
        ([PROFILE], [{**SERIES, "series_id": str(i)} for i in range(9)]),
        ([PROFILE, PROFILE], []),
        ([PROFILE, {**PROFILE, "revision": 2}], []),
        ([PROFILE], [SERIES, SERIES]),
        ([], [SERIES]),
        ([PROFILE], [{**SERIES, "profile_revision": 2}]),
    ):
        with error():
            validate_catalog(profiles, series)
        with error():
            configure(ledger, profiles=profiles, series=series)
    validate_catalog([PROFILE], [SERIES])
    assert validate_series(SERIES) == SERIES
    with error():
        validate_series({**SERIES, "scope": "owner"})


@pytest.mark.parametrize("changes", [{"人物": "new stable value"}, {"unknown": "new"}, {"owner": "forged"}])
def test_choice_cannot_override_stable_undeclared_or_authority_attributes(ledger, changes):
    configure(ledger)
    choice = {**CHOICE, "changes": changes}
    with error():
        validate_choice(choice, [PROFILE], [SERIES])
    with error():
        plan(ledger, creative=choice)
    assert ledger.list_artist_works("s", SCOPE) == []


def test_choice_matches_profile_and_series_and_never_contains_a_model_snapshot(ledger):
    configure(ledger)
    for choice in (
        {**CHOICE, "profile_revision": 2}, {**CHOICE, "series_id": "missing"},
        {**CHOICE, "profile_id": "missing"}, {**CHOICE, "profile": PROFILE["data"]},
        {**CHOICE, "reason": ""},
    ):
        with error():
            plan(ledger, creative=choice)
    choice = {**CHOICE, "series_id": None}
    validated = validate_choice(choice, [PROFILE], [])
    validated["changes"]["光线"] = "caller mutation"
    assert choice["changes"] == CHOICE["changes"]
    saved = plan(ledger, creative=choice)
    assert saved["brief"]["creative"]["series"] is None
    assert ledger.db.execute("SELECT count(*) FROM series_members").fetchone()[0] == 0


def test_render_prompt_applies_frozen_palette_technique_stable_and_changed_defaults(ledger):
    configure(ledger)
    saved = plan(ledger)
    creative = saved["brief"]["creative"]
    prompt = render_prompt(BRIEF["prompt"], creative)
    for text in (BRIEF["prompt"], "暖白与青蓝", "透明水彩", "人物：短发", "画风：柔和", "光线：黄昏", "构图：近景",
                 SERIES["goal"]):
        assert text in prompt
    assert "光线：清晨" not in prompt
    reordered = deepcopy(creative)
    reordered["profile"]["stable"] = dict(reversed(list(reordered["profile"]["stable"].items())))
    assert render_prompt(BRIEF["prompt"], reordered) == prompt
    assert creative == {**CHOICE, "profile": PROFILE["data"], "series": SERIES}


def test_prompt_limit_is_explicit_without_truncation_or_partial_plan(ledger):
    configure(ledger)
    creative = {**deepcopy(CHOICE), "profile": deepcopy(PROFILE["data"]), "series": deepcopy(SERIES)}
    suffix_length = len(render_prompt("", creative))
    assert len(render_prompt("x" * (4000 - suffix_length), creative)) == 4000
    with error("PROMPT_TOO_LONG"):
        render_prompt("x" * (4001 - suffix_length), creative)
    assert render_prompt("x" * 4000, None) == "x" * 4000
    with error("PROMPT_TOO_LONG"):
        render_prompt("x" * 4001, None)
    with error("PROMPT_TOO_LONG"):
        plan(ledger, brief={**BRIEF, "prompt": "x" * 3999})
    assert ledger.list_artist_works("s", SCOPE) == []
    assert ledger.db.execute("SELECT count(*) FROM works").fetchone()[0] == 0
    assert ledger.db.execute("SELECT count(*) FROM series_members").fetchone()[0] == 0


def test_concurrent_series_objective_creation_cannot_bypass_active_work_gate(ledger):
    db = ledger
    configure(db)
    barrier = Barrier(2)

    def worker(identifier):
        connection = ArtistStore(db.path)
        try:
            barrier.wait(timeout=5)
            try:
                return plan(connection, identifier)["work_id"]
            except StoreError as exc:
                return exc.code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(worker, ["first", "second"]))
    assert outcomes.count("WORK_BUSY") == 1
    assert db.db.execute("SELECT count(*) FROM series_members").fetchone()[0] == 1


def seed_old(path, version):
    db = sqlite3.connect(path)
    schemas = (store_module._SCHEMA, store_module._SCHEMA_2, store_module._SCHEMA_3)
    for statements in schemas[:version]:
        for statement in statements:
            db.execute(statement)
    db.execute("INSERT INTO spaces VALUES('s',?,'old-binding',1,1)", (SCOPE.snapshot(),))
    db.execute("INSERT INTO grants VALUES('s',1,1,'consent','adapter','then')")
    db.execute("INSERT INTO works VALUES('s','w','then')")
    context = {"current_work": {"work_id": "w", "brief": BRIEF, "best_version": None},
               "current_version": {"version_id": "v", "sha256": hashlib.sha256(b"image").hexdigest(),
                                   "mime": "image/png"}}
    if version == 3:
        context.update(review_contract=2, review_inputs=[{**context["current_version"], "roles": ["current"]}])
    params = {"task": "review", "version_id": "v", "context": context}
    digest = hashlib.sha256(canonical_json(context).encode()).hexdigest()
    if version == 3:
        params.update(review_contract=2, input_digest=digest)
    review = {"decision": "accept", "reason": "valid historical proof", "strengths": [], "issues": []}
    for index, state in enumerate(("reserved", "unknown", "succeeded"), 1):
        db.execute(
            """INSERT INTO operations VALUES(?,'s','w',?,'think','think',?,?,'old-binding',1,'idle',
            NULL,?,'2026-10-31',NULL,?,'then','then')""",
            (index, state, canonical_json(params), SCOPE.snapshot(), state,
             canonical_json(review) if state == "succeeded" else None),
        )
    if version >= 2:
        db.execute("INSERT INTO artist_works(space_id,work_id,status,brief,inspiration_keys) "
                   "VALUES('s','w','finished',?,'[]')", (canonical_json(BRIEF),))
        db.execute("INSERT INTO versions VALUES('s','v','w','render','job',?,?,'image/png',NULL,'then')",
                   (b"image", context["current_version"]["sha256"]))
        db.execute("INSERT INTO reviews VALUES(1,'s','w','v','succeeded',?,'then')", (canonical_json(review),))
    if version == 3:
        db.execute("INSERT INTO review_snapshots VALUES('s','v',1,?,?)", (canonical_json(context), digest))
    db.execute(f"PRAGMA user_version={version}")
    db.commit()
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    before = {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    db.close()
    return before


@pytest.mark.parametrize("version", [1, 2, 3])
def test_migration_1_2_3_preserves_old_ledger_snapshots_and_acceptance(tmp_path, version):
    path = tmp_path / "old.sqlite3"
    before = seed_old(path, version)
    for _ in range(2):
        db = ArtistStore(path)
        try:
            assert db.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            for table, rows in before.items():
                assert [tuple(row) for row in db.db.execute(f"SELECT * FROM {table}")] == rows
            assert db.list_profiles("s", SCOPE) == [] and db.list_series("s", SCOPE) == []
            assert [op["state"] for op in db.audit_operations("s", SCOPE)] == ["reserved", "unknown", "succeeded"]
            if version >= 2:
                assert db.version_accepted("s", SCOPE, "v")
        finally:
            db.close()


def test_review_snapshot_accepts_catalogue_metadata_without_live_config_drift(tmp_path):
    path = tmp_path / "review-with-profiles.sqlite3"
    seed_old(path, 2)
    db = ArtistStore(path)
    try:
        configure(db)
        version = db.get_version("s", SCOPE, "v")
        context = {
            "current_work": {"work_id": "w", "brief": BRIEF, "best_version": None},
            "current_version": {key: version[key] for key in ("version_id", "sha256", "mime")},
            "review_contract": 2,
            "review_inputs": [{**{key: version[key] for key in ("version_id", "sha256", "mime")},
                               "roles": ["current"]}],
            "profiles": db.list_profiles("s", SCOPE), "series": db.list_series("s", SCOPE),
        }
        snapshot = db.freeze_review_snapshot("s", SCOPE, version_id="v", context=context,
                                             expected_auth_version=1)
        configure(db, profiles=[], series=[])
        assert db.freeze_review_snapshot("s", SCOPE, version_id="v", context=context,
                                         expected_auth_version=1) == snapshot
        assert snapshot["context"]["profiles"] == [PROFILE]
        assert snapshot["context"]["series"] == [SERIES]
    finally:
        db.close()


@pytest.mark.parametrize("version", [1, 2, 3])
def test_failed_schema_4_migration_rolls_back_ddl_rows_and_version(tmp_path, monkeypatch, version):
    path = tmp_path / "old.sqlite3"
    before = seed_old(path, version)
    with monkeypatch.context() as patch:
        patch.setattr(store_module, "_SCHEMA_4", (*store_module._SCHEMA_4, "CREATE TABLE works(bad TEXT)"))
        with pytest.raises(sqlite3.DatabaseError):
            ArtistStore(path)
    db = sqlite3.connect(path)
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == version
        assert {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} == set(before)
        for table, rows in before.items():
            assert db.execute(f"SELECT * FROM {table}").fetchall() == rows
    finally:
        db.close()
    ArtistStore(path).close()


def test_objective_normalization_keeps_field_boundaries():
    assert normalize_objective("  ＬＩＧＨＴ\tSTUDY", " WINDOW ") == normalize_objective("light study", "window")
    assert normalize_objective("a b", "c") != normalize_objective("a", "b c")
