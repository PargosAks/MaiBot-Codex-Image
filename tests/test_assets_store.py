"""Scoped reviewed-work assets and text-only references; no external clients."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
import hashlib
import json
import sqlite3

import pytest

from image_plugin_under_test import store as store_module
from image_plugin_under_test.assets import reference_prompt, validate_references
from image_plugin_under_test.policy import Delegation, Limits, Scope, StoreError, canonical_json
from image_plugin_under_test.profiles import render_prompt
from image_plugin_under_test.store import ArtistStore


SCOPE = Scope("test", "asset-stream", "asset-owner")
OTHER = Scope("test", "other-stream", "other-owner")
BRIEF = {"intent": "study light", "subject": "window", "mood": "quiet", "prompt": "a quiet window",
         "completion_criteria": "warm lighting"}
SUMMARY_KEYS = {"asset_id", "summary", "roles", "tags", "source_kind"}
DETAIL_KEYS = SUMMARY_KEYS | {"sha256", "mime", "description"}


def error(code="INVALID_ARGUMENT"):
    return pytest.raises(StoreError, check=lambda exc: exc.code == code)


@pytest.fixture
def ledger(tmp_path):
    db = ArtistStore(tmp_path / "assets.sqlite3")
    db.create_space("s", SCOPE, binding_id="asset-test-binding")
    db.grant("s", SCOPE, Delegation("explicit-consent", "host-admin", continuous=True))
    limits = Limits(think=1000, render=1000, share=0, max_inflight=1000, max_revisions=5)
    db.set_global_limits(limits)
    db.set_limits("s", SCOPE, limits)
    yield db
    db.close()


def reviewed(db, identifier="v", *, data=None, state="succeeded", decision="accept", selected=None,
             review_reason="温暖的光线符合完成标准", strengths=None, contract=1, space="s", scope=SCOPE):
    work, request, thought = "work-" + identifier, "render-" + identifier, "review-" + identifier
    db.plan_work(space, scope, work_id=work, brief=BRIEF, inspiration_keys=[], expected_auth_version=1)
    db.reserve(space, scope, request_id=request, action="submit", work_id=work, params={"prompt": BRIEF["prompt"]})
    db.mark_submitting(space, scope, request)
    db.settle(space, scope, request, outcome="succeeded", evidence="synthetic complete", job_id="job-" + identifier)
    version = db.record_version(space, scope, work_id=work, version_id=identifier, request_id=request,
                                job_id="job-" + identifier, data=data if data is not None else identifier.encode(),
                                mime="image/png")
    context = {"current_work": {"work_id": work, "brief": BRIEF, "best_version": None},
               "current_version": {key: version[key] for key in ("version_id", "sha256", "mime")}}
    if contract == 2:
        context.update(review_contract=2, review_inputs=[{**context["current_version"], "roles": ["current"]}])
        snapshot = db.freeze_review_snapshot(space, scope, version_id=identifier, context=context,
                                             expected_auth_version=1)
    params = {"task": "review", "version_id": identifier, "context": context}
    if contract == 2:
        params.update(review_contract=2, input_digest=snapshot["input_digest"])
    review = {"decision": decision, "reason": review_reason,
              "strengths": ["柔和配色", "清晰构图"] if strengths is None else strengths, "issues": []}
    if selected is not None:
        review["best_version_id"] = selected
    if state != "absent":
        db.reserve(space, scope, request_id=thought, action="think", work_id=work, params=params)
        if state != "reserved":
            db.mark_submitting(space, scope, thought)
        if state in {"succeeded", "failed"}:
            db.settle(space, scope, thought, outcome=state, evidence=canonical_json(review))
        elif state == "unknown":
            db.mark_unknown(space, scope, thought, reason="lost response")
        if state in {"succeeded", "unknown", "submitting"}:
            db.record_review(space, scope, version_id=identifier, request_id=thought, review=review)
    db.update_artist_work(space, scope, work, changes={"status": "finished", "best_version": identifier})
    return version


def archive(db, identifier="v", *, roles=None, tags=None, reason="可复用配色与构图", auth=1):
    return db.archive_version("s", SCOPE, version_id=identifier, roles=["palette", "composition"] if roles is None else roles,
                              tags=["window", "warm"] if tags is None else tags,
                              reason=reason, expected_auth_version=auth)


def refs(asset, role="palette", reason="借鉴温暖配色"):
    return [{"asset_id": asset["asset_id"], "role": role, "reason": reason}]


def plan(db, references=None, *, work="next", brief=None, creative=None):
    return db.plan_work("s", SCOPE, work_id=work, brief=deepcopy(BRIEF if brief is None else brief),
                        inspiration_keys=[], creative=creative, references=references, expected_auth_version=1)


def test_archive_uses_valid_review_evidence_stable_identity_and_no_model_or_render_side_effects(ledger):
    db = ledger
    source = reviewed(db)
    operations, authorizations = db.audit_operations("s", SCOPE), db.audit_authorizations("s", SCOPE)
    versions = db.db.execute("SELECT * FROM versions").fetchall()
    asset = archive(db)
    assert asset["asset_id"] == "asset:" + hashlib.sha256(canonical_json(["s", "v"]).encode()).hexdigest()
    assert asset["source_kind"] == "reviewed_work"
    assert (asset["source_version_id"], asset["work_id"], asset["job_id"], asset["sha256"], asset["mime"]) == (
        "v", "work-v", "job-v", source["sha256"], source["mime"],
    )
    assert "温暖的光线" in asset["summary"] and "清晰构图" in asset["summary"]
    assert BRIEF["prompt"] not in asset["summary"]
    assert len(asset["summary"]) <= 1000 and len(asset["description"]) <= 2000
    assert "data" not in asset
    assert archive(db, roles=["composition", "palette"], tags=["warm", "window"]) == asset
    assert db.audit_operations("s", SCOPE) == operations
    assert db.audit_authorizations("s", SCOPE) == authorizations
    assert [tuple(row) for row in db.db.execute("SELECT * FROM versions")] == [tuple(row) for row in versions]
    assert db.db.execute("SELECT count(*) FROM asset_catalog").fetchone()[0] == 1
    assert db.budget_usage("s", SCOPE)["share"]["total"] == 0


@pytest.mark.parametrize("changes", [{"roles": ["style"]}, {"tags": ["other"]}, {"reason": "other reason"}])
def test_conflicting_archive_content_cannot_rewrite_existing_asset(ledger, changes):
    reviewed(ledger)
    asset = archive(ledger)
    with error("REQUEST_CONFLICT"):
        archive(ledger, **changes)
    assert ledger.get_asset("s", SCOPE, asset["asset_id"]) == asset


@pytest.mark.parametrize("state,decision,selected", [
    ("absent", "accept", None), ("reserved", "accept", None), ("submitting", "accept", None),
    ("unknown", "accept", None), ("failed", "accept", None), ("succeeded", "discard", None),
    ("succeeded", "revise", None), ("succeeded", "accept", "unseen"),
])
def test_finished_best_or_unsuccessful_or_unseen_review_is_not_archive_acceptance(ledger, state, decision, selected):
    reviewed(ledger, state=state, decision=decision, selected=selected)
    assert not ledger.version_accepted("s", SCOPE, "v")
    with error("VERSION_NOT_ACCEPTED"):
        archive(ledger)
    assert ledger.list_assets("s", SCOPE) == []


@pytest.mark.parametrize("tamper", ["evidence", "digest", "auth", "work"])
def test_v2_archive_shares_acceptance_proof_checks(ledger, tamper):
    reviewed(ledger, contract=2)
    assert ledger.version_accepted("s", SCOPE, "v")
    if tamper == "evidence":
        ledger.db.execute("UPDATE operations SET evidence='unrelated' WHERE request_id='review-v'")
    elif tamper == "digest":
        ledger.db.execute("UPDATE review_snapshots SET input_digest='bad'")
    elif tamper == "auth":
        ledger.db.execute("UPDATE operations SET auth_version=0 WHERE request_id='review-v'")
    else:
        ledger.create_work("s", SCOPE, "different")
        ledger.db.execute("UPDATE operations SET work_id='different' WHERE request_id='review-v'")
    assert not ledger.version_accepted("s", SCOPE, "v")
    with error("VERSION_NOT_ACCEPTED"):
        archive(ledger)


def test_candidate_and_detail_shapes_selection_order_restart_and_normal_duplicate_validation(ledger):
    db = ledger
    assets = []
    for identifier in ("one", "two", "three"):
        reviewed(db, identifier)
        assets.append(archive(db, identifier))
    candidates = db.list_assets("s", SCOPE)
    assert all(row.keys() == SUMMARY_KEYS for row in candidates)
    selected_ids = [assets[2]["asset_id"], assets[0]["asset_id"]]
    details = db.select_assets("s", SCOPE, selected_ids, expected_auth_version=1)
    assert [item["asset_id"] for item in details] == selected_ids
    assert all(item.keys() == DETAIL_KEYS for item in details)
    assert len(json.dumps(details, ensure_ascii=False, separators=(",", ":"))) <= 6000
    choice = refs(assets[0])
    assert validate_references(choice, candidates + details) == choice
    selected_copy = db.selected_assets("s", SCOPE)
    selected_copy[0]["tags"].append("caller mutation")
    assert db.selected_assets("s", SCOPE) == details
    reopened = ArtistStore(db.path)
    try:
        assert reopened.selected_assets("s", SCOPE) == details
        assert reopened.select_assets("s", SCOPE, [], expected_auth_version=1) == []
        assert reopened.selected_assets("s", SCOPE) == []
        assert reopened.db.execute("SELECT count(*) FROM asset_selection").fetchone()[0] == 2
    finally:
        reopened.close()


def test_query_is_deterministic_bounded_and_prefers_tag_matches(ledger):
    for identifier, tags, reason in (("exact", ["warm"], "warm"), ("partial", ["warmth"], "warm"),
                                     ("text", ["other"], "warm composition"), ("unrelated", [], "cold light")):
        reviewed(ledger, identifier, review_reason=reason)
        archive(ledger, identifier, tags=tags)
    expected = [archive(ledger, identifier, tags=tags)["asset_id"]
                for identifier, tags in (("exact", ["warm"]), ("partial", ["warmth"]), ("text", ["other"]))]
    results = ledger.list_assets("s", SCOPE, query=" ＷＡＲＭ ")
    assert [item["asset_id"] for item in results] == expected
    assert ledger.list_assets("s", SCOPE, query="warm", limit=2) == results[:2]
    assert ledger.list_assets("s", SCOPE, query="does-not-exist") == []
    empty = ledger.list_assets("s", SCOPE)
    assert [row["asset_id"] for row in empty] == sorted(row["asset_id"] for row in empty)
    for query, limit in (("x" * 501, 8), ("", 0), ("", 9), ("", True)):
        with error():
            ledger.list_assets("s", SCOPE, query=query, limit=limit)


def test_reference_snapshot_preserves_evidence_and_replay_after_deactivation(ledger):
    db = ledger
    reviewed(db)
    asset = archive(db)
    choice = refs(asset)
    saved = plan(db, choice)
    frozen = saved["brief"]["references"]
    assert frozen == [{**choice[0], **{key: asset[key] for key in (
        "summary", "source_kind", "source_version_id", "sha256", "mime")}, "usage": "text_context"}]
    prompt = reference_prompt(BRIEF["prompt"], frozen)
    assert "仅文字灵感，未输入参考图" in prompt
    assert asset["summary"] in prompt and choice[0]["reason"] in prompt
    db.select_assets("s", SCOPE, [asset["asset_id"]], expected_auth_version=1)
    db.deactivate_asset("s", SCOPE, asset["asset_id"], expected_auth_version=1)
    db.deactivate_asset("s", SCOPE, asset["asset_id"], expected_auth_version=1)
    assert db.list_assets("s", SCOPE) == [] and db.selected_assets("s", SCOPE) == []
    assert db.get_asset("s", SCOPE, asset["asset_id"])["active"] == 0
    assert db.get_version("s", SCOPE, "v")["data"] == b"v"
    assert plan(db, choice) == saved
    assert reference_prompt(BRIEF["prompt"], saved["brief"]["references"]) == prompt
    db.update_artist_work("s", SCOPE, "next", changes={"status": "finished"})
    with error("NOT_FOUND"):
        plan(db, choice, work="later")
    with error("NOT_FOUND"):
        db.select_assets("s", SCOPE, [asset["asset_id"]], expected_auth_version=1)
    assert archive(db)["active"] == 0  # Idempotency does not reactivate.
    reopened = ArtistStore(db.path)
    try:
        assert reopened.get_artist_work("s", SCOPE, "next")["brief"] == saved["brief"]
        assert plan(reopened, choice) is not None
    finally:
        reopened.close()


def test_plain_old_briefs_and_empty_reference_lists_remain_unchanged(ledger):
    saved = plan(ledger)
    assert saved["brief"] == BRIEF and "references" not in saved["brief"]
    assert plan(ledger, []) == saved
    assert reference_prompt("  原文\n  ", []) == "  原文\n  "


def test_reference_choice_immutability_and_model_cannot_supply_frozen_metadata(ledger):
    reviewed(ledger)
    asset = archive(ledger)
    choice = refs(asset)
    saved = plan(ledger, choice)
    for changed in ([], refs(asset, reason="changed"), refs(asset, role="composition")):
        with error("REQUEST_CONFLICT"):
            plan(ledger, changed)
    ledger.update_artist_work("s", SCOPE, "next", changes={"status": "finished"})
    with error():
        plan(ledger, saved["brief"]["references"], work="forged")
    with error():
        plan(ledger, brief={**BRIEF, "references": saved["brief"]["references"]})


def test_cross_scope_and_same_hash_do_not_grant_asset_access(ledger):
    db = ledger
    reviewed(db, data=b"same image")
    asset = archive(db)
    db.create_space("other", OTHER, binding_id="asset-test-binding")
    db.grant("other", OTHER, Delegation("explicit-other", "admin", continuous=True))
    db.set_limits("other", OTHER, Limits(think=10, render=10, share=0, max_inflight=10))
    reviewed(db, data=b"same image", space="other", scope=OTHER)
    other_asset = db.archive_version("other", OTHER, version_id="v", roles=["palette"], tags=[], reason="reuse",
                                     expected_auth_version=1)
    assert other_asset["sha256"] == asset["sha256"] and other_asset["asset_id"] != asset["asset_id"]
    for call in (
        lambda: db.get_asset("s", OTHER, asset["asset_id"]),
        lambda: db.get_asset("other", OTHER, asset["asset_id"]),
        lambda: db.select_assets("other", OTHER, [asset["asset_id"]], expected_auth_version=1),
        lambda: db.deactivate_asset("other", OTHER, asset["asset_id"], expected_auth_version=1),
        lambda: db.plan_work("other", OTHER, work_id="steal", brief=BRIEF, inspiration_keys=[],
                             references=refs(asset), expected_auth_version=1),
    ):
        with error("NOT_FOUND"):
            call()


def test_revocation_keeps_history_readable_but_blocks_all_new_asset_writes(ledger):
    db = ledger
    reviewed(db)
    asset = archive(db)
    details = db.select_assets("s", SCOPE, [asset["asset_id"]], expected_auth_version=1)
    db.revoke("s", SCOPE, source_ref="withdrawn", granted_by="admin")
    assert db.get_asset("s", SCOPE, asset["asset_id"]) == asset
    assert db.selected_assets("s", SCOPE) == details
    for auth, code in ((1, "AUTH_CHANGED"), (2, "AUTH_REQUIRED")):
        for call in (
            lambda: archive(db, auth=auth),
            lambda: db.select_assets("s", SCOPE, [asset["asset_id"]], expected_auth_version=auth),
            lambda: db.deactivate_asset("s", SCOPE, asset["asset_id"], expected_auth_version=auth),
        ):
            with error(code):
                call()
    with error("AUTH_REQUIRED"):
        db.list_assets("s", SCOPE)
    db.grant("s", SCOPE, Delegation("new-consent", "admin", continuous=True))
    with error("VERSION_NOT_ACCEPTED"):
        archive(db, auth=3)


@pytest.mark.parametrize("roles,tags,reason", [
    ([], [], "reason"), (["unknown"], [], "reason"), (["style", "style"], [], "reason"),
    (["style"], [str(i) for i in range(9)], "reason"), (["style"], ["x" * 81], "reason"),
    (["style"], ["same", "same"], "reason"), (["style"], [], "x" * 501), (["style"], [], ""),
])
def test_archive_roles_tags_and_reason_are_bounded(ledger, roles, tags, reason):
    reviewed(ledger)
    with error():
        archive(ledger, roles=roles, tags=tags, reason=reason)
    assert ledger.list_assets("s", SCOPE) == []


def test_reference_validation_accepts_summary_detail_duplicates_but_rejects_invalid_choices():
    available = [{"asset_id": "asset", "roles": ["style", "palette"], "summary": "summary"},
                 {"asset_id": "asset", "roles": ["style", "palette"], "description": "detail"}]
    choice = [{"asset_id": "asset", "role": "palette", "reason": "inspiration"}]
    result = validate_references(choice, available)
    result[0]["reason"] = "changed"
    assert choice[0]["reason"] == "inspiration"
    for invalid in (
        choice * 4, choice * 2, [{**choice[0], "role": "character"}], [{**choice[0], "asset_id": "missing"}],
        [{**choice[0], "reason": "x" * 501}], [{**choice[0], "source_version_id": "forged"}],
    ):
        with error():
            validate_references(invalid, available)
    assert validate_references(choice, available) == choice


def test_detail_budget_is_atomic_and_description_summary_are_bounded(ledger):
    db = ledger
    ids = []
    for i in range(3):
        reviewed(db, str(i), review_reason="评" * 1800, strengths=["优" * 800])
        asset = archive(db, str(i))
        assert len(asset["summary"]) <= 1000 and len(asset["description"]) <= 2000
        ids.append(asset["asset_id"])
    previous = db.select_assets("s", SCOPE, ids[:1], expected_auth_version=1)
    with error("ASSET_DETAILS_TOO_LONG"):
        db.select_assets("s", SCOPE, ids, expected_auth_version=1)
    assert db.selected_assets("s", SCOPE) == previous


def test_source_tampering_blocks_archive_selection_and_new_references_but_retains_history(ledger):
    reviewed(ledger)
    asset = archive(ledger)
    ledger.db.execute("UPDATE versions SET data=? WHERE version_id='v'", (b"tampered image",))
    with error("ARTIFACT_INVALID"):
        archive(ledger)
    with error("ARTIFACT_INVALID"):
        ledger.select_assets("s", SCOPE, [asset["asset_id"]], expected_auth_version=1)
    with error("ARTIFACT_INVALID"):
        plan(ledger, refs(asset))
    assert ledger.get_asset("s", SCOPE, asset["asset_id"]) == asset


def test_asset_count_quota_counts_inactive_and_preserves_existing_entries(ledger):
    for i in range(101):
        reviewed(ledger, str(i))
        if i < 100:
            asset = archive(ledger, str(i))
            ledger.deactivate_asset("s", SCOPE, asset["asset_id"], expected_auth_version=1)
    with error("ASSET_QUOTA"):
        archive(ledger, "100")
    assert ledger.db.execute("SELECT count(*) FROM asset_catalog").fetchone()[0] == 100
    assert ledger.db.execute("SELECT count(*) FROM versions").fetchone()[0] == 101


def test_real_source_byte_quota_allows_exact_64_mib_and_rejects_one_more_byte(ledger):
    payload = b"s" * (8 * 1024 * 1024)
    for i in range(8):
        reviewed(ledger, str(i), data=payload)
        asset = archive(ledger, str(i))
    ledger.deactivate_asset("s", SCOPE, asset["asset_id"], expected_auth_version=1)
    reviewed(ledger, "overflow", data=b"x")
    with error("ASSET_QUOTA"):
        archive(ledger, "overflow")
    assert ledger.db.execute("SELECT sum(size_bytes) FROM asset_catalog").fetchone()[0] == 64 * 1024 * 1024


def test_concurrent_archive_is_idempotent_and_creates_one_catalogue_entry(ledger):
    reviewed(ledger)
    barrier = Barrier(2)

    def worker(_):
        connection = ArtistStore(ledger.path)
        try:
            barrier.wait(timeout=5)
            return archive(connection)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assets = list(pool.map(worker, range(2)))
    assert assets[0] == assets[1]
    assert ledger.db.execute("SELECT count(*) FROM asset_catalog").fetchone()[0] == 1


def test_final_reference_prompt_limit_includes_creative_and_is_not_truncated(ledger):
    reviewed(ledger)
    asset = archive(ledger)
    saved = plan(ledger, refs(asset))
    frozen = saved["brief"]["references"]
    suffix_length = len(reference_prompt("", frozen))
    assert len(reference_prompt("x" * (4000 - suffix_length), frozen)) == 4000
    with error("PROMPT_TOO_LONG"):
        reference_prompt("x" * (4001 - suffix_length), frozen)
    ledger.update_artist_work("s", SCOPE, "next", changes={"status": "finished"})
    with error("PROMPT_TOO_LONG"):
        plan(ledger, refs(asset), work="too-long", brief={**BRIEF, "prompt": "x" * 3999})
    profile = {"profile_id": "p", "revision": 1,
               "data": {"title": "watercolour", "palette": "blue", "technique": "soft",
                        "stable": {"character": "cat"}, "variable": {"light": "warm"}, "source": "config"}}
    ledger.configure_creative("s", SCOPE, profiles=[profile], series=[], expected_auth_version=1)
    creative = {"profile_id": "p", "profile_revision": 1, "series_id": None, "changes": {}, "reason": "study"}
    together = plan(ledger, refs(asset), work="combined", creative=creative)
    prompt = reference_prompt(render_prompt(BRIEF["prompt"], together["brief"]["creative"]),
                              together["brief"]["references"])
    assert "character：cat" in prompt and "仅文字灵感，未输入参考图" in prompt
    assert len(prompt) <= 4000


def make_schema_4(path):
    db = sqlite3.connect(path)
    for statements in (store_module._SCHEMA, store_module._SCHEMA_2, store_module._SCHEMA_3, store_module._SCHEMA_4):
        for statement in statements:
            db.execute(statement)
    db.execute("INSERT INTO spaces VALUES('s',?,'old-binding',1,1)", (SCOPE.snapshot(),))
    db.execute("INSERT INTO grants VALUES('s',1,1,'consent','adapter','then')")
    db.execute("INSERT INTO works VALUES('s','w','then')")
    db.execute("INSERT INTO artist_works(space_id,work_id,status,brief,inspiration_keys) "
               "VALUES('s','w','finished',?,'[]')", (canonical_json(BRIEF),))
    digest = hashlib.sha256(b"image").hexdigest()
    db.execute("INSERT INTO versions VALUES('s','v','w','render','job',?,?,'image/png',NULL,'then')", (b"image", digest))
    context = {"current_work": {"work_id": "w", "brief": BRIEF, "best_version": None},
               "current_version": {"version_id": "v", "sha256": digest, "mime": "image/png"},
               "review_contract": 2,
               "review_inputs": [{"version_id": "v", "roles": ["current"], "sha256": digest, "mime": "image/png"}]}
    input_digest = hashlib.sha256(canonical_json(context).encode()).hexdigest()
    db.execute("INSERT INTO review_snapshots VALUES('s','v',1,?,?)", (canonical_json(context), input_digest))
    params = {"task": "review", "version_id": "v", "context": context, "review_contract": 2, "input_digest": input_digest}
    review = {"decision": "accept", "reason": "historically accepted", "strengths": ["lighting"], "issues": []}
    for index, state in enumerate(("reserved", "unknown", "succeeded"), 1):
        db.execute("""INSERT INTO operations VALUES(?,'s','w',?,'think','think',?,?,'old-binding',1,'idle',NULL,
                   ?,'2026-10-31',NULL,?,'then','then')""",
                   (index, state, canonical_json(params), SCOPE.snapshot(), state,
                    canonical_json(review) if state == "succeeded" else None))
    db.execute("INSERT INTO reviews VALUES(1,'s','w','v','succeeded',?,'then')", (canonical_json(review),))
    db.execute("PRAGMA user_version=4")
    db.commit()
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    before = {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    db.close()
    return before


def test_schema_4_to_5_preserves_old_data_acceptance_and_snapshots_and_restart(tmp_path):
    path = tmp_path / "old.sqlite3"
    before = make_schema_4(path)
    for _ in range(2):
        db = ArtistStore(path)
        try:
            assert db.db.execute("PRAGMA user_version").fetchone()[0] == store_module.SCHEMA_VERSION
            for table, rows in before.items():
                assert [tuple(row) for row in db.db.execute(f"SELECT * FROM {table}")] == rows
            assert db.version_accepted("s", SCOPE, "v")
            assert db.list_assets("s", SCOPE) == [] and db.selected_assets("s", SCOPE) == []
        finally:
            db.close()
    db = ArtistStore(path)
    try:
        snapshot = db.get_review_snapshot("s", SCOPE, "v")
        assert db.freeze_review_snapshot("s", SCOPE, version_id="v", context=snapshot["context"],
                                         expected_auth_version=1) == snapshot
        assert archive(db)["summary"].startswith("评审理由：historically accepted")
    finally:
        db.close()


def test_schema_5_migration_failure_rolls_back_all_new_tables_and_version(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite3"
    before = make_schema_4(path)
    with monkeypatch.context() as patch:
        patch.setattr(store_module, "_SCHEMA_5", (*store_module._SCHEMA_5, "CREATE TABLE works(bad TEXT)"))
        with pytest.raises(sqlite3.DatabaseError):
            ArtistStore(path)
    db = sqlite3.connect(path)
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} == set(before)
        for table, rows in before.items():
            assert db.execute(f"SELECT * FROM {table}").fetchall() == rows
    finally:
        db.close()
    ArtistStore(path).close()
