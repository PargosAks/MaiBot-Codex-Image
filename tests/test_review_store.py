"""Frozen review inputs and acceptance proofs using only synthetic local data."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
import hashlib
import json
import sqlite3

import pytest

from image_plugin_under_test import store as store_module
from image_plugin_under_test.policy import Delegation, Limits, Scope, StoreError, canonical_json
from image_plugin_under_test.store import SCHEMA_VERSION, ArtistStore


SCOPE = Scope("test", "review-stream", "review-owner")
OTHER = Scope("test", "other-stream", "other-owner")
BRIEF = {
    "intent": "study light", "subject": "window", "mood": "quiet", "prompt": "soft light",
    "completion_criteria": "recognizable window and warm lighting",
}
LIMITS = Limits(think=100, render=100, share=0, max_inflight=100, max_revisions=20)


def error(code):
    return pytest.raises(StoreError, check=lambda exc: exc.code == code)


@pytest.fixture
def ledger(tmp_path):
    db = ArtistStore(tmp_path / "review.sqlite3")
    db.create_space("s", SCOPE, binding_id="synthetic-review-binding")
    db.grant("s", SCOPE, Delegation("explicit-consent", "trusted-adapter", continuous=True))
    db.set_global_limits(LIMITS)
    db.set_limits("s", SCOPE, LIMITS)
    db.plan_work("s", SCOPE, work_id="w", brief=BRIEF, inspiration_keys=[])
    yield db
    db.close()


def image(db, identifier, *, parent=None, work="w", space="s", scope=SCOPE):
    params = {"prompt": "synthetic image"}
    if parent is not None:
        source = db.get_version(space, scope, parent)
        params.update(source_job_id=source["job_id"], source_sha256=source["sha256"])
    request, job = "render-" + identifier, "job-" + identifier
    db.reserve(space, scope, request_id=request, action="edit" if parent else "submit",
               work_id=work, params=params)
    db.mark_submitting(space, scope, request)
    db.settle(space, scope, request, outcome="succeeded", evidence="synthetic complete", job_id=job)
    return db.record_version(
        space, scope, version_id=identifier, work_id=work, request_id=request, job_id=job,
        data=("synthetic image " + identifier).encode(), mime="image/png", parent_id=parent,
    )


def context_for(db, current="v3", *, contract=2):
    version = db.get_version("s", SCOPE, current)
    work = db.get_artist_work("s", SCOPE, version["work_id"])
    context = {
        "persona": "patient artist", "events": [], "inspirations": [], "works": [],
        "current_work": {key: work[key] for key in ("work_id", "brief", "best_version")},
        "current_version": {key: version[key] for key in ("version_id", "sha256", "mime")},
        "versions": [
            {key: row[key] for key in ("version_id", "parent_id", "sha256", "mime")}
            for row in db.list_versions("s", SCOPE, version["work_id"])
        ],
    }
    if contract == 2:
        roles = [("current", current), ("parent", version["parent_id"]),
                 ("previous_best", work["best_version"])]
        inputs = {}
        for role, identifier in roles:
            if identifier is None:
                continue
            if identifier not in inputs:
                row = db.get_version("s", SCOPE, identifier)
                inputs[identifier] = {"version_id": identifier, "roles": [],
                                      "sha256": row["sha256"], "mime": row["mime"]}
            inputs[identifier]["roles"].append(role)
        context.update(review_contract=2, review_inputs=list(inputs.values()))
    return context


@pytest.fixture
def comparison(ledger):
    image(ledger, "v1")
    image(ledger, "v2", parent="v1")
    image(ledger, "v3", parent="v2")
    # The next edit's parent is deliberately different from v3's actual parent.
    ledger.update_artist_work("s", SCOPE, "w", changes={
        "best_version": "v1", "parent_version": "v1", "pending_request": "render-v3",
        "status": "review_pending",
    })
    return ledger, context_for(ledger)


def freeze(db, context, *, version="v3", auth=1):
    return db.freeze_review_snapshot(
        "s", SCOPE, version_id=version, context=context, expected_auth_version=auth,
    )


def accept(db, context, *, selected=None, request="review", state="succeeded", params=None):
    review = {"decision": "accept", "reason": "meets brief", "strengths": ["lighting"], "issues": []}
    if selected is not None:
        review["best_version_id"] = selected
    version = context["current_version"]["version_id"]
    if params is None:
        params = {"task": "review", "version_id": version, "context": deepcopy(context)}
        if context.get("review_contract") == 2:
            params.update(review_contract=2,
                          input_digest=hashlib.sha256(canonical_json(context).encode()).hexdigest())
    db.reserve("s", SCOPE, request_id=request, action="think", work_id="w", params=params)
    if state != "reserved":
        db.mark_submitting("s", SCOPE, request)
    if state in {"succeeded", "failed"}:
        db.settle("s", SCOPE, request, outcome=state, evidence=canonical_json(review))
    elif state == "unknown":
        db.mark_unknown("s", SCOPE, request, reason="synthetic lost response")
    elif state == "accepted":
        db.mark_accepted("s", SCOPE, request)
    if state not in {"reserved", "failed"}:
        db.record_review("s", SCOPE, version_id=version, request_id=request, review=review)
    return review


def test_snapshot_is_metadata_only_immutable_and_reusable_after_best_changes(comparison):
    db, context = comparison
    snapshot = freeze(db, context)
    assert snapshot == {
        "space_id": "s", "version_id": "v3", "auth_version": 1, "context": context,
        "input_digest": hashlib.sha256(canonical_json(context).encode()).hexdigest(),
    }
    assert [row["roles"] for row in snapshot["context"]["review_inputs"]] == [
        ["current"], ["parent"], ["previous_best"],
    ]
    assert db.db.execute("SELECT context FROM review_snapshots").fetchone()[0] == canonical_json(context)
    assert freeze(db, dict(reversed(list(context.items())))) == snapshot
    snapshot["context"]["review_inputs"][0]["roles"].append("parent")
    assert db.get_review_snapshot("s", SCOPE, "v3")["context"] == context
    db.update_artist_work("s", SCOPE, "w", changes={"best_version": "v2"})
    assert freeze(db, context)["context"] == context
    with error("REQUEST_CONFLICT"):
        freeze(db, context_for(db))
    changed = deepcopy(context)
    changed["persona"] = "changed"
    with error("REQUEST_CONFLICT"):
        freeze(db, changed)
    assert db.db.execute("SELECT count(*) FROM review_snapshots").fetchone()[0] == 1


def test_snapshot_and_acceptance_survive_restart_without_new_operations(comparison):
    db, context = comparison
    saved = freeze(db, context)
    accept(db, context, selected="v2")
    before = db.audit_operations("s", SCOPE)
    reopened = ArtistStore(db.path)
    try:
        assert reopened.get_review_snapshot("s", SCOPE, "v3") == saved
        assert freeze(reopened, context) == saved
        assert reopened.version_accepted("s", SCOPE, "v2", expected_auth_version=1)
        assert not reopened.version_accepted("s", SCOPE, "v3")
        assert reopened.audit_operations("s", SCOPE) == before
    finally:
        reopened.close()


def test_first_image_and_overlapping_roles_are_deduplicated(ledger):
    image(ledger, "first")
    context = context_for(ledger, "first")
    assert context["review_inputs"][0]["roles"] == ["current"]
    freeze(ledger, context, version="first")
    ledger.update_artist_work("s", SCOPE, "w", changes={"best_version": "first"})
    image(ledger, "child", parent="first")
    context = context_for(ledger, "child")
    assert [row["roles"] for row in context["review_inputs"]] == [
        ["current"], ["parent", "previous_best"],
    ]
    freeze(ledger, context, version="child")
    image(ledger, "best-current", parent="child")
    ledger.update_artist_work("s", SCOPE, "w", changes={"best_version": "best-current"})
    context = context_for(ledger, "best-current")
    assert context["review_inputs"][0]["roles"] == ["current", "previous_best"]
    freeze(ledger, context, version="best-current")


@pytest.mark.parametrize("mutation,code", [
    (lambda c: c.update(review_contract=1), "INVALID_ARGUMENT"),
    (lambda c: c.update(review_contract=True), "INVALID_ARGUMENT"),
    (lambda c: c.update(review_inputs=[]), "INVALID_ARGUMENT"),
    (lambda c: c["review_inputs"].append(deepcopy(c["review_inputs"][0])), "INVALID_ARGUMENT"),
    (lambda c: c["review_inputs"].pop(), "INVALID_ARGUMENT"),
    (lambda c: c["review_inputs"][0]["roles"].append("current"), "INVALID_ARGUMENT"),
    (lambda c: c["review_inputs"][1].update(roles=["current"]), "INVALID_ARGUMENT"),
    (lambda c: c["review_inputs"][1].update(roles=["unknown"]), "INVALID_ARGUMENT"),
    (lambda c: c["review_inputs"][1].update(roles=[]), "INVALID_ARGUMENT"),
    (lambda c: c["review_inputs"][1].update(roles=[{}]), "INVALID_ARGUMENT"),
    (lambda c: c["review_inputs"][1].update(version_id="v3"), "INVALID_ARGUMENT"),
    (lambda c: c["review_inputs"][1].update(sha256="0" * 64), "ARTIFACT_INVALID"),
    (lambda c: c["review_inputs"][1].update(mime="image/jpeg"), "ARTIFACT_INVALID"),
    (lambda c: c["review_inputs"][0].update(data="base64 is not metadata"), "INVALID_ARGUMENT"),
    (lambda c: c["current_version"].update(version_id="v2"), "INVALID_ARGUMENT"),
    (lambda c: c["current_version"].update(sha256="0" * 64), "INVALID_ARGUMENT"),
    (lambda c: c["current_work"].update(work_id="other-work"), "NOT_FOUND"),
    (lambda c: c["current_work"].update(best_version="v2"), "REQUEST_CONFLICT"),
    (lambda c: c["current_work"]["brief"].update(prompt="changed brief"), "REQUEST_CONFLICT"),
])
def test_invalid_snapshot_never_partially_freezes(comparison, mutation, code):
    db, context = comparison
    mutation(context)
    with error(code):
        freeze(db, context)
    assert db.get_review_snapshot("s", SCOPE, "v3") is None


def test_snapshot_checks_parent_from_version_and_every_image_bytes(comparison):
    db, context = comparison
    wrong = deepcopy(context)
    wrong["review_inputs"][1]["roles"] = ["previous_best"]
    wrong["review_inputs"][2]["roles"] = ["parent"]
    with error("INVALID_ARGUMENT"):
        freeze(db, wrong)
    db.db.execute("UPDATE versions SET data=? WHERE version_id='v2'", (b"tampered",))
    with error("ARTIFACT_INVALID"):
        freeze(db, context)


def test_snapshot_rejects_other_work_and_owner_even_with_matching_bytes(comparison):
    db, context = comparison
    db.create_work("s", SCOPE, "other-work")
    foreign = image(db, "other-version", work="other-work")
    db.db.execute("UPDATE versions SET data=?,sha256=? WHERE version_id='other-version'",
                  (db.get_version("s", SCOPE, "v2")["data"], context["review_inputs"][1]["sha256"]))
    foreign["sha256"] = context["review_inputs"][1]["sha256"]
    context["review_inputs"][1].update(version_id=foreign["version_id"], sha256=foreign["sha256"])
    with error("NOT_FOUND"):
        freeze(db, context)
    for method in (
        lambda: db.get_review_snapshot("s", OTHER, "v3"),
        lambda: db.freeze_review_snapshot("s", OTHER, version_id="v3", context=context,
                                         expected_auth_version=1),
        lambda: db.version_accepted("s", OTHER, "v3"),
    ):
        with error("NOT_FOUND"):
            method()


def test_snapshot_is_readable_after_revocation_but_cannot_be_reused(comparison):
    db, context = comparison
    saved = freeze(db, context)
    accept(db, context)
    db.revoke("s", SCOPE, source_ref="withdrawn", granted_by="trusted-adapter")
    assert db.get_review_snapshot("s", SCOPE, "v3") == saved
    assert not db.version_accepted("s", SCOPE, "v3")
    with error("AUTH_CHANGED"):
        freeze(db, context)
    with error("AUTH_REQUIRED"):
        freeze(db, context, auth=2)
    db.grant("s", SCOPE, Delegation("new-consent", "trusted-adapter", continuous=True))
    with error("AUTH_CHANGED"):
        freeze(db, context, auth=3)
    with error("AUTH_CHANGED"):
        db.version_accepted("s", SCOPE, "v3", expected_auth_version=1)
    assert not db.version_accepted("s", SCOPE, "v3", expected_auth_version=3)
    assert db.get_review_snapshot("s", SCOPE, "missing") is None


def test_concurrent_freezes_have_one_immutable_winner(comparison):
    db, context = comparison
    barrier = Barrier(2)

    def worker(persona):
        connection = ArtistStore(db.path)
        candidate = deepcopy(context)
        candidate["persona"] = persona
        try:
            barrier.wait(timeout=5)
            try:
                return freeze(connection, candidate)["context"]["persona"]
            except StoreError as exc:
                return exc.code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(worker, ["first", "second"]))
    assert result.count("REQUEST_CONFLICT") == 1
    saved = db.get_review_snapshot("s", SCOPE, "v3")
    assert saved["context"]["persona"] in result
    assert db.db.execute("SELECT count(*) FROM review_snapshots").fetchone()[0] == 1


@pytest.mark.parametrize("selected", [None, "v1", "v2", "v3"])
def test_v2_accepts_only_explicitly_selected_visible_image(comparison, selected):
    db, context = comparison
    freeze(db, context)
    accept(db, context, selected=selected)
    for identifier in ("v1", "v2", "v3"):
        assert db.version_accepted("s", SCOPE, identifier) == (identifier == (selected or "v3"))
    db.update_artist_work("s", SCOPE, "w", changes={"best_version": "v3", "status": "finished"})
    assert db.version_accepted("s", SCOPE, selected or "v3")


@pytest.mark.parametrize("state", ["reserved", "submitting", "accepted", "unknown", "failed"])
def test_only_succeeded_think_can_certify_acceptance(comparison, state):
    db, context = comparison
    freeze(db, context)
    review = accept(db, context, state=state)
    if state in {"reserved", "failed"}:
        with error("NOT_FOUND"):
            db.record_review("s", SCOPE, version_id="v3", request_id="review", review=review)
    assert not db.version_accepted("s", SCOPE, "v3")


@pytest.mark.parametrize("selected", [None, "v1", "v3"])
def test_v1_accepts_actual_current_only_not_unseen_catalogue_version(comparison, selected):
    db, _ = comparison
    context = context_for(db, contract=1)
    accept(db, context, selected=selected)
    assert not db.version_accepted("s", SCOPE, "v1")
    assert db.version_accepted("s", SCOPE, "v3") == (selected in {None, "v3"})
    assert db.get_review_snapshot("s", SCOPE, "v3") is None


def test_finished_and_best_without_review_are_not_acceptance(comparison):
    db, _ = comparison
    db.update_artist_work("s", SCOPE, "w", changes={"status": "finished", "best_version": "v3"})
    assert not db.version_accepted("s", SCOPE, "v3")
    assert not db.version_accepted("s", SCOPE, "v1")


@pytest.mark.parametrize("field,value", [
    ("task", "plan"), ("version_id", "v2"), ("review_contract", 1),
    ("review_contract", True), ("input_digest", "0" * 64),
])
def test_mismatched_operation_parameters_are_not_acceptance(comparison, field, value):
    db, context = comparison
    freeze(db, context)
    accept(db, context)
    params = db.get_operation("s", SCOPE, "review")["params"]
    params[field] = value
    db.db.execute("UPDATE operations SET params_json=? WHERE request_id='review'", (canonical_json(params),))
    assert not db.version_accepted("s", SCOPE, "v3")


@pytest.mark.parametrize("field", ["review_contract", "input_digest"])
def test_v2_requires_explicit_operation_contract_and_digest(comparison, field):
    db, context = comparison
    freeze(db, context)
    accept(db, context)
    params = db.get_operation("s", SCOPE, "review")["params"]
    params.pop(field)
    db.db.execute("UPDATE operations SET params_json=? WHERE request_id='review'", (canonical_json(params),))
    assert not db.version_accepted("s", SCOPE, "v3")


def test_valid_later_review_is_not_hidden_by_a_corrupt_earlier_review(comparison):
    db, context = comparison
    freeze(db, context)
    accept(db, context, request="bad-proof")
    db.db.execute("UPDATE operations SET evidence='{' WHERE request_id='bad-proof'")
    assert not db.version_accepted("s", SCOPE, "v3")
    accept(db, context, request="valid-proof")
    assert db.version_accepted("s", SCOPE, "v3")


@pytest.mark.parametrize("tampering", [
    "evidence", "noncanonical-evidence", "review-json", "params-json", "context", "digest",
    "snapshot-context", "snapshot-auth", "missing-snapshot", "image", "mime", "work", "binding", "scope",
    "auth", "unseen-selected", "discard", "v1-unseen",
])
def test_tampered_acceptance_proof_fails_closed_and_keeps_audit(comparison, tampering):
    db, context = comparison
    freeze(db, context)
    review = accept(db, context)
    if tampering in {"evidence", "noncanonical-evidence"}:
        evidence = "unrelated receipt" if tampering == "evidence" else json.dumps(review)
        db.db.execute("UPDATE operations SET evidence=? WHERE request_id='review'", (evidence,))
    elif tampering in {"review-json", "params-json"}:
        table, column = ("reviews", "review") if tampering == "review-json" else ("operations", "params_json")
        db.db.execute(f"UPDATE {table} SET {column}='{{' WHERE request_id='review'")
    elif tampering == "context":
        params = db.get_operation("s", SCOPE, "review")["params"]
        params["context"]["persona"] = "changed"
        db.db.execute("UPDATE operations SET params_json=? WHERE request_id='review'", (canonical_json(params),))
    elif tampering == "digest":
        db.db.execute("UPDATE review_snapshots SET input_digest='bad'")
    elif tampering == "snapshot-context":
        changed = deepcopy(context)
        changed["persona"] = "changed"
        db.db.execute("UPDATE review_snapshots SET context=?", (canonical_json(changed),))
    elif tampering == "snapshot-auth":
        db.db.execute("UPDATE review_snapshots SET auth_version=99")
    elif tampering == "missing-snapshot":
        db.db.execute("DELETE FROM review_snapshots")
    elif tampering == "image":
        db.db.execute("UPDATE versions SET data=? WHERE version_id='v2'", (b"tampered parent",))
    elif tampering == "mime":
        db.db.execute("UPDATE versions SET mime='image/jpeg' WHERE version_id='v2'")
    elif tampering == "work":
        db.create_work("s", SCOPE, "another-work")
        db.db.execute("UPDATE operations SET work_id='another-work' WHERE request_id='review'")
    elif tampering in {"binding", "scope", "auth"}:
        column, value = {"binding": ("binding_id", "other-binding"),
                         "scope": ("scope_snapshot", OTHER.snapshot()), "auth": ("auth_version", 0)}[tampering]
        db.db.execute(f"UPDATE operations SET {column}=? WHERE request_id='review'", (value,))
    elif tampering in {"unseen-selected", "discard"}:
        if tampering == "unseen-selected":
            image(db, "unseen", parent="v3")
            review["best_version_id"] = "unseen"
        else:
            review["decision"] = "discard"
        db.db.execute("UPDATE reviews SET review=?", (canonical_json(review),))
        db.db.execute("UPDATE operations SET evidence=? WHERE request_id='review'", (canonical_json(review),))
    else:
        # A contract downgrade cannot make an unseen selected v1 count as visible.
        params = db.get_operation("s", SCOPE, "review")["params"]
        params["context"] = context_for(db, contract=1)
        params.pop("review_contract")
        params.pop("input_digest")
        review["best_version_id"] = "v1"
        db.db.execute("UPDATE operations SET params_json=?,evidence=? WHERE request_id='review'",
                      (canonical_json(params), canonical_json(review)))
        db.db.execute("UPDATE reviews SET review=?", (canonical_json(review),))
    before = db.db.execute("SELECT * FROM operations WHERE request_id='review'").fetchone()[:]
    assert not db.version_accepted("s", SCOPE, "v3")
    assert not db.version_accepted("s", SCOPE, "v1")
    assert db.db.execute("SELECT * FROM operations WHERE request_id='review'").fetchone()[:] == before


def test_cross_space_same_identifier_cannot_inherit_acceptance(comparison):
    db, context = comparison
    freeze(db, context)
    accept(db, context)
    db.create_space("other", OTHER, binding_id="synthetic-review-binding")
    db.grant("other", OTHER, Delegation("other-consent", "trusted-adapter", continuous=True))
    db.set_limits("other", OTHER, LIMITS)
    db.create_work("other", OTHER, "w")
    image(db, "v3", space="other", scope=OTHER)
    assert db.get_review_snapshot("other", OTHER, "v3") is None
    assert not db.version_accepted("other", OTHER, "v3")
    assert db.version_accepted("s", SCOPE, "v3")


def seed_old_database(path, version):
    db = sqlite3.connect(path)
    for statement in store_module._SCHEMA:
        db.execute(statement)
    db.execute("INSERT INTO spaces VALUES('s',?,'old-binding',1,1)", (SCOPE.snapshot(),))
    db.execute("INSERT INTO grants VALUES('s',1,1,'old-consent','adapter','then')")
    db.execute("INSERT INTO works VALUES('s','w','then')")
    for index, state in enumerate(("reserved", "unknown", "succeeded"), 1):
        db.execute(
            """INSERT INTO operations VALUES(?,'s','w',?,'think','think',?,?,'old-binding',
            1,'idle',NULL,?,'2026-10-31',NULL,?,'then','then')""",
            (index, state, canonical_json({"task": "review", "version_id": "legacy"}),
             SCOPE.snapshot(), state, '{"decision":"accept"}' if state == "succeeded" else None),
        )
    if version == 2:
        for statement in store_module._SCHEMA_2:
            db.execute(statement)
        db.execute("INSERT INTO artist_works(space_id,work_id,status,brief,inspiration_keys) "
                   "VALUES('s','w','finished',?,'[]')", (canonical_json(BRIEF),))
        db.execute("INSERT INTO versions VALUES('s','legacy','w','old-render','old-job',?,?,"
                   "'image/png',NULL,'then')", (b"legacy image", hashlib.sha256(b"legacy image").hexdigest()))
        db.execute("INSERT INTO reviews VALUES(1,'s','w','legacy','succeeded',?, 'then')",
                   (canonical_json({"decision": "accept"}),))
    db.execute(f"PRAGMA user_version={version}")
    db.commit()
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    before = {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    db.close()
    return before


@pytest.mark.parametrize("version", [1, 2])
def test_migration_to_3_is_passive_preserves_old_rows_and_restart(tmp_path, version):
    path = tmp_path / "old.sqlite3"
    before = seed_old_database(path, version)
    for _ in range(2):
        db = ArtistStore(path)
        try:
            assert SCHEMA_VERSION == db.db.execute("PRAGMA user_version").fetchone()[0]
            for table, rows in before.items():
                assert [tuple(row) for row in db.db.execute(f"SELECT * FROM {table}")] == rows
            assert db.db.execute("SELECT count(*) FROM review_snapshots").fetchone()[0] == 0
            assert [op["state"] for op in db.audit_operations("s", SCOPE)] == [
                "reserved", "unknown", "succeeded",
            ]
            if version == 2:
                # Migration does not fill in missing current-image evidence.
                assert not db.version_accepted("s", SCOPE, "legacy")
        finally:
            db.close()


def test_migrated_v1_review_retains_only_its_genuine_current_acceptance(tmp_path):
    path = tmp_path / "v2-with-review.sqlite3"
    seed_old_database(path, 2)
    db = sqlite3.connect(path)
    context = {
        "current_work": {"work_id": "w"},
        "current_version": {"version_id": "legacy", "sha256": hashlib.sha256(b"legacy image").hexdigest(),
                            "mime": "image/png"},
    }
    params = {"task": "review", "version_id": "legacy", "context": context}
    db.execute("UPDATE operations SET params_json=? WHERE request_id='succeeded'", (canonical_json(params),))
    db.commit()
    before = db.execute("SELECT * FROM operations").fetchall()
    db.close()
    for _ in range(2):
        upgraded = ArtistStore(path)
        try:
            assert upgraded.version_accepted("s", SCOPE, "legacy", expected_auth_version=1)
            assert upgraded.get_review_snapshot("s", SCOPE, "legacy") is None
            assert [tuple(row) for row in upgraded.db.execute("SELECT * FROM operations")] == before
        finally:
            upgraded.close()


@pytest.mark.parametrize("version", [1, 2])
def test_migration_failure_rolls_back_all_ddl_and_version(tmp_path, monkeypatch, version):
    path = tmp_path / "old.sqlite3"
    before = seed_old_database(path, version)
    with monkeypatch.context() as patch:
        patch.setattr(store_module, "_SCHEMA_3", (*store_module._SCHEMA_3, "CREATE TABLE works(bad TEXT)"))
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
