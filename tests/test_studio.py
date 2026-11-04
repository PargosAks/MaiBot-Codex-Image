"""Isolated SQLite projections: no Host lifecycle, model, MCP or message send."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import base64
import hashlib
import json
import sqlite3

import pytest

from image_plugin_under_test import CodexImagePlugin
from image_plugin_under_test.policy import Delegation, Limits, Scope, StoreError
from image_plugin_under_test.store import ArtistStore, SCHEMA_VERSION, _SCHEMA
from image_plugin_under_test.studio import Studio
from protocol_fixture import PNG


NOW = datetime(2026, 11, 2, 1, tzinfo=timezone.utc)
OWNER = Scope("synthetic", "space-a-chat", "owner")
OTHER = Scope("synthetic", "space-b-chat", "other-owner")
BRIEF = {"intent": "private-intent", "subject": "private-subject", "mood": "quiet",
         "prompt": "file:///never-open-this.png", "completion_criteria": "private-criteria"}


def snapshot(path):
    return {item.relative_to(path).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
            for item in path.rglob("*") if item.is_file()}


@pytest.fixture
def data(tmp_path):
    db = ArtistStore(tmp_path / "artist.sqlite3", clock=lambda: NOW)
    limits = Limits(think=8, render=8, share=8, max_inflight=8, max_revisions=2)
    db.set_global_limits(limits)
    for key, scope in (("a", OWNER), ("b", OTHER)):
        db.create_space(key, scope, binding_id="synthetic-binding")
        db.grant(key, scope, Delegation("synthetic-consent", "synthetic-admin", True))
        db.set_limits(key, scope, limits)
        db.plan_work(key, scope, work_id="work", brief=BRIEF, inspiration_keys=[])
        db.reserve(key, scope, request_id="render", action="submit", work_id="work", params={"prompt": "synthetic"})
        db.mark_submitting(key, scope, "render")
        db.mark_accepted(key, scope, "render", job_id="job-" + key)
        db.record_version(key, scope, work_id="work", version_id="v-" + key, request_id="render",
                          job_id="job-" + key, data=PNG, mime="image/png")
        db.settle(key, scope, "render", outcome="succeeded", evidence="synthetic receipt")
        db.reserve(key, scope, request_id="review", action="think", work_id="work", params={"purpose": "review"})
        db.mark_submitting(key, scope, "review")
        db.record_review(key, scope, version_id="v-" + key, request_id="review",
                         review={"decision": "accept", "reason": "private-review"})
        db.settle(key, scope, "review", outcome="succeeded", evidence="synthetic review")
    yield tmp_path, db, Studio(tmp_path, scopes={"a": OWNER}, clock=lambda: NOW)
    db.close()


def codes(summary):
    return {item["code"] for item in summary["waiting"]}


def error(code):
    return pytest.raises(StoreError, check=lambda exc: exc.code == code)


def test_all_reads_do_not_change_bytes_or_dispatch(data, monkeypatch):
    root, db, view = data
    before = snapshot(root)
    # Even accidental construction of a mutating runtime/store is forbidden.
    monkeypatch.setattr(ArtistStore, "__init__", lambda *a, **kw: pytest.fail("mutating store opened"))
    assert view.spaces()["items"] == [{"space_id": "a", "authorized": True, "state_exists": True}]
    result = view.summary("a")
    assert result["counts"] == {"works": 1, "versions": 1, "reviews": 1}
    for section in ("works", "tasks", "versions", "reviews", "profiles", "series", "assets"):
        view.records("a", section)
    assert view.image("a", "v-a")["data_url"].startswith("data:image/jpeg;base64,")
    original = view.image("a", "v-a", False)
    assert base64.b64decode(original["chunk_base64"]) == PNG
    assert snapshot(root) == before
    assert db.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


@pytest.mark.parametrize("call", [
    lambda view: view.summary("b"),
    lambda view: view.records("b", "tasks"),
    lambda view: view.records("b", "profiles"),
    lambda view: view.image("b", "v-b"),
    lambda view: view.image("a", "v-b"),
])
def test_cross_space_queries_rejected(data, call):
    with error("NOT_FOUND"):
        call(data[2])


def test_exact_owner_and_stream_required_even_when_allowlist_is_forged(data):
    root, _, _ = data
    for scope in (OTHER, Scope(OWNER.platform, OTHER.stream_id, OWNER.user_id),
                  Scope("other-platform", OWNER.stream_id, OWNER.user_id)):
        view = Studio(root, scopes={"a": scope})
        with error("NOT_FOUND"):
            view.summary("a")
        assert not view.spaces()["items"]


@pytest.mark.parametrize("offset,limit", [(-1, 10), (10001, 10), (True, 10), (0, 0), (0, 51), (0, True), (0, 1.0)])
def test_pagination_bounded_at_python_boundary(data, offset, limit):
    with error("INVALID_ARGUMENT"):
        data[2].records("a", "tasks", offset, limit)
    with error("INVALID_ARGUMENT"):
        data[2].spaces(offset, limit)


def test_pagination_stable_and_fields_bounded(data):
    _, db, view = data
    first, second = view.records("a", "tasks", limit=1), view.records("a", "tasks", offset=1, limit=1)
    assert first["has_more"] and not second["has_more"]
    assert first["items"][0]["request_id"] != second["items"][0]["request_id"]
    assert not view.records("a", "tasks", offset=10000)["items"]
    db.db.execute("UPDATE artist_works SET reason=? WHERE space_id='a'", ("x" * 20000,))
    assert len(view.records("a", "works")["items"][0]["reason"]) == 4000
    with error("INVALID_ARGUMENT"):
        view.records("a", "operations; DROP TABLE spaces")
    with error("INVALID_ARGUMENT"):
        view.records("a", "profiles", work_id="work")


def test_only_scoped_budget_with_unknown_held_across_midnight(data):
    root, db, _ = data
    db.reserve("a", OWNER, request_id="unknown-think", action="think", params={"purpose": "plan"})
    db.mark_submitting("a", OWNER, "unknown-think")
    db.mark_unknown("a", OWNER, "unknown-think", reason="secret-debug-details")
    # A foreign hold must not appear in any count or budget in this projection.
    db.reserve("b", OTHER, request_id="foreign", action="think", params={"purpose": "plan"})
    view = Studio(root, scopes={"a": OWNER}, clock=lambda: NOW + timedelta(days=1))
    result = view.summary("a")
    assert result["budget"]["think"] == {"limit": 8, "held": 1, "consumed": 0, "remaining": 7,
                                          "space_remaining": 7, "available_now": True}
    assert "THINK_UNKNOWN" in codes(result)
    assert result["global_budget"] == "availability_only"
    assert "foreign" not in json.dumps(result)


def test_send_unknown_is_read_without_legacy_recovery(data):
    root, db, view = data
    journal = sqlite3.connect(root / "delivery.sqlite3")
    journal.execute("CREATE TABLE delivery(job TEXT PRIMARY KEY,state TEXT NOT NULL)")
    journal.executemany("INSERT INTO delivery VALUES(?,?)", [("job-a", "sending"), ("job-b", "send_unknown")])
    journal.commit()
    journal.close()
    before = snapshot(root)
    assert "SEND_UNKNOWN" in codes(view.summary("a"))
    assert snapshot(root) == before
    check = sqlite3.connect(root / "delivery.sqlite3")
    assert check.execute("SELECT state FROM delivery WHERE job='job-a'").fetchone()[0] == "sending"
    check.execute("UPDATE delivery SET state='sent' WHERE job='job-a'")
    check.commit()
    check.close()
    assert "SEND_UNKNOWN" not in codes(view.summary("a"))


def test_wait_budget_revoke_and_runtime_error(data):
    root, db, _ = data
    db.set_limits("a", OWNER, Limits(think=0, render=0, share=0, max_inflight=0))
    db.db.execute("INSERT OR REPLACE INTO artist_state(space_id,next_due,backoff_until) VALUES(?,?,?)",
                  ("a", (NOW + timedelta(minutes=10)).isoformat(), (NOW + timedelta(minutes=1)).isoformat()))
    db.revoke("a", OWNER, source_ref="synthetic-revoke", granted_by="synthetic-admin")
    view = Studio(root, scopes={"a": OWNER}, clock=lambda: NOW, enabled=False, blocked={"a": True})
    assert {"AUTONOMY_DISABLED", "AUTHORIZATION_REVOKED", "SPACE_BUDGET_EXHAUSTED", "SPACE_INFLIGHT_LIMIT",
            "SCHEDULED_WAIT", "BACKOFF", "RUNTIME_BLOCKED"} <= codes(view.summary("a"))
    assert view.records("a", "versions")["items"]  # revocation does not erase read authority


def test_no_data_read_creates_no_directory(tmp_path):
    root = tmp_path / "absent"
    view = Studio(root, scopes={"a": OWNER})
    assert "STATE_NOT_CREATED" in codes(view.summary("a"))
    assert view.records("a", "works")["available"] is False
    assert not root.exists()


def test_old_schema_read_never_migrates(tmp_path):
    db = sqlite3.connect(tmp_path / "artist.sqlite3")
    for statement in _SCHEMA:
        db.execute(statement)
    db.execute("INSERT INTO spaces VALUES(?,?,?,?,?)", ("a", OWNER.snapshot(), "old-binding", 1, 0))
    db.execute("PRAGMA user_version=1")
    db.commit()
    db.close()
    before = snapshot(tmp_path)
    view = Studio(tmp_path, scopes={"a": OWNER})
    assert view.summary("a")["counts"] == {"works": 0, "versions": 0, "reviews": 0}
    assert view.records("a", "profiles")["available"] is False
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("version", [0, 10, 999])
def test_unknown_schema_explicit_rejection(tmp_path, version):
    db = sqlite3.connect(tmp_path / "artist.sqlite3")
    db.execute(f"PRAGMA user_version={version}")
    db.close()
    before = snapshot(tmp_path)
    with error("SCHEMA_UNSUPPORTED"):
        Studio(tmp_path, scopes={"a": OWNER}).summary("a")
    assert snapshot(tmp_path) == before


def test_image_validation_and_path_payload_never_opened(data):
    root, db, view = data
    outside = root / "not-an-image.txt"
    outside.write_text("never read me", encoding="utf-8")
    malicious = str(outside).encode()
    db.db.execute("UPDATE versions SET data=?,sha256=? WHERE space_id='a'",
                  (malicious, hashlib.sha256(malicious).hexdigest()))
    before = snapshot(root)
    with error("IMAGE_INVALID"):
        view.image("a", "v-a")
    assert snapshot(root) == before
    db.db.execute("UPDATE versions SET data=?,sha256=? WHERE space_id='a'", (PNG, "wrong-hash"))
    with error("IMAGE_INVALID"):
        view.image("a", "v-a")
    with error("INVALID_ARGUMENT"):
        view.image("a", "v-a", "false")


def test_linked_database_rejected_before_open(data, monkeypatch):
    root, _, view = data
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == root / "artist.sqlite3" or original(path))
    with error("UNSAFE_STATE_PATH"):
        view.summary("a")


def test_scoped_summary_for_chat_omits_full_text_and_rejects_foreign_scope(data):
    from image_plugin_under_test.studio_chat import snapshot_from_summary

    root, _, _ = data
    summary = Studio(root, scopes={"a": OWNER}, clock=lambda: NOW).summary("a")
    snapshot = snapshot_from_summary("a", OWNER, summary)
    text = json.dumps(snapshot.data, ensure_ascii=False)
    assert snapshot.scope == OWNER and snapshot.authorized is True
    assert snapshot.data["recent_works"][0]["subject"] == "private-subject"
    for value in ("private-intent", "private-review", "file:", "space-b", "job-", "synthetic-binding"):
        assert value not in text
    for key in ("b", "absent"):
        with error("NOT_FOUND"):
            Studio(root, scopes={key: OWNER}).summary(key)
    with error("INVALID_SCOPE"):
        Studio(root, scopes={"a": {"platform": "synthetic"}})
    with error("INVALID_ARGUMENT"):
        Studio(root, scopes={"*": OWNER})


def test_for_plugin_uses_config_only_never_bootstraps_runtime(data):
    root, _, _ = data
    plugin = CodexImagePlugin()
    plugin.set_plugin_config({"plugin": {"config_version": "0.9.2"}, "autonomy": {"spaces": [dict(space_id="a", platform=OWNER.platform,
                                                          stream_id=OWNER.stream_id, user_id=OWNER.user_id)]}})
    plugin._set_context(SimpleNamespace(paths=SimpleNamespace(data_dir=root)))
    view = Studio.for_plugin(plugin)
    assert view.spaces()["items"][0]["space_id"] == "a"
    assert "AUTONOMY_DISABLED" in codes(view.summary("a"))
    assert plugin.artist_runtime is None and plugin.journal is None and plugin.bridge is None


def test_recent_work_evidence_bounded_subjects_and_unknown_beats_sent(data):
    root, db, view = data
    journal = sqlite3.connect(root / "delivery.sqlite3")
    journal.execute("CREATE TABLE delivery(job TEXT PRIMARY KEY,state TEXT NOT NULL)")
    journal.execute("INSERT INTO delivery VALUES('job-a','sent')")
    journal.commit()
    summary = view.summary("a")
    work = summary["recent_works"][0]
    assert work == {"work_id": "work", "subject": "private-subject", "status": "planned", "generated": True,
                    "version_count": 1, "review_state": "accept", "share_state": "sent"}
    assert summary["current_work"] == work
    journal.execute("UPDATE delivery SET state='send_unknown'")
    journal.commit()
    assert view.summary("a")["recent_works"][0]["share_state"] == "send_unknown"
    assert "SEND_UNKNOWN" in codes(view.summary("a"))
    journal.close()
    db.update_artist_work("a", OWNER, "work", changes={"status": "finished"})
    for index in range(7):
        db.plan_work("a", OWNER, work_id=f"work-{index}", brief={**BRIEF, "subject": "鲸鱼娘" * 100}, inspiration_keys=[])
        db.update_artist_work("a", OWNER, f"work-{index}", changes={"status": "finished"})
    summary = view.summary("a")
    assert len(summary["recent_works"]) == 5 and summary["current_work"] is None
    assert all(len(item["subject"]) <= 80 for item in summary["recent_works"])
    assert all(item["generated"] is False and item["share_state"] == "not_shared" for item in summary["recent_works"])
    assert "不可猜测" in summary["coverage"]
    assert "private-intent" not in json.dumps(summary)


def test_original_chunks_bounded_for_real_host_result_limit(data):
    from io import BytesIO
    from PIL import Image
    from image_plugin_under_test.studio import IMAGE_CHUNK_BYTES
    import random

    _, db, view = data
    output = BytesIO()
    Image.frombytes("RGB", (512, 512), random.Random(1).randbytes(512 * 512 * 3)).save(output, format="PNG")
    original = output.getvalue()
    assert len(original) > IMAGE_CHUNK_BYTES * 2
    db.db.execute("UPDATE versions SET data=?,sha256=? WHERE space_id='a'",
                  (original, hashlib.sha256(original).hexdigest()))
    restored, offset = bytearray(), 0
    while offset is not None:
        chunk = view.image("a", "v-a", False, offset)
        assert len(json.dumps(chunk, ensure_ascii=True).encode()) < 524288
        restored.extend(base64.b64decode(chunk["chunk_base64"]))
        offset = chunk["next_offset"]
    assert restored == original
    with error("INVALID_ARGUMENT"):
        view.image("a", "v-a", False, len(original))


def test_large_record_page_has_bounded_host_payload_and_resumable_offset(data):
    _, db, view = data
    # Long historical fields cannot exceed the Host result budget even at 50 rows.
    for index in range(40):
        db.reserve("a", OWNER, request_id=f"page-{index}", action="think", params={})
        db.db.execute("UPDATE operations SET state='rejected' WHERE space_id='a' AND request_id=?", (f"page-{index}",))
    db.db.execute("UPDATE operations SET trigger_ref=? WHERE space_id='a'", ("不可信" * 4000,))
    # Use displayed IDs (which are historical SQLite data) to exercise the cap.
    db.db.execute("UPDATE operations SET job_id=? WHERE space_id='a'", ("长" * 4000,))
    page = view.records("a", "tasks", limit=50)
    assert len(json.dumps(page, ensure_ascii=True).encode()) < 524288
    assert page["has_more"] and 0 < len(page["items"]) < 50
    assert page["next_offset"] == len(page["items"])
    assert view.records("a", "tasks", limit=50, offset=page["next_offset"])["items"]


@pytest.mark.parametrize("category", ["think", "render", "share"])
def test_shared_category_limit_blocks_while_private_budget_remains(data, category):
    root, db, view = data
    limits = {"think": 8, "render": 8, "share": 8, "max_inflight": 8, "max_revisions": 2}
    # Existing a/b each consumed one think/render; a has 7 left locally.
    limits[category] = 2 if category != "share" else 0
    db.set_global_limits(Limits(**limits))
    result = view.summary("a")
    assert result["budget"][category]["space_remaining"] > 0
    assert result["budget"][category]["available_now"] is False
    assert result["shared_constraints"][category] is True
    assert "SHARED_" + category.upper() + "_LIMIT" in codes(result)
    assert all(type(value) is bool for value in result["shared_constraints"].values())
    text = json.dumps(result, ensure_ascii=False)
    assert "other-owner" not in text and "space-b-chat" not in text and "global_limits" not in text
    assert "共享" in next(item["text"] for item in result["waiting"] if item["code"].startswith("SHARED_"))


def test_shared_inflight_only_discloses_block_flag_not_foreign_holds(data):
    _, db, view = data
    db.set_global_limits(Limits(think=8, render=8, share=8, max_inflight=1, max_revisions=2))
    db.reserve("b", OTHER, request_id="private-foreign-hold", action="think", params={})
    result = view.summary("a")
    assert result["budget"]["think"]["held"] == 0
    assert result["budget"]["think"]["space_remaining"] == 7
    assert not result["budget"]["think"]["available_now"]
    assert result["shared_constraints"]["inflight"] is True
    assert "SHARED_INFLIGHT_LIMIT" in codes(result)
    assert "private-foreign-hold" not in json.dumps(result)


def test_studio_r1_02_long_valid_profile_and_frozen_brief_keep_independent_display_fields(data):
    from test_profiles_store import PROFILE
    from copy import deepcopy

    _, db, view = data
    profile = deepcopy(PROFILE)
    profile["data"].update(stable={f"固定{i}": "暖" * 400 for i in range(6)},
                           variable={f"变化{i}": "柔" * 400 for i in range(6)})
    db.configure_creative("a", OWNER, profiles=[profile], series=[], expected_auth_version=1)
    frozen = {**BRIEF, "subject": "鲸鱼娘", "intent": "研究海面反光", "prompt": "蓝" * 3500,
              "creative": {"profile_id": profile["profile_id"], "series_id": None, "profile": profile["data"]}}
    review = {"decision": "accept", "reason": "保留" * 300, "strengths": ["光影" * 100] * 8,
              "issues": ["细节" * 100] * 8,
              "comparison": {"improvements": ["构图" * 100] * 8, "regressions": [], "preserved": ["主体"]}}
    assert len(json.dumps(frozen, ensure_ascii=False)) > 4000
    assert len(json.dumps(profile["data"], ensure_ascii=False)) > 4000
    db.db.execute("UPDATE artist_works SET brief=? WHERE space_id='a'", (json.dumps(frozen),))
    db.db.execute("UPDATE reviews SET review=? WHERE space_id='a'", (json.dumps(review),))
    works = view.records("a", "works")["items"][0]
    assert works["subject"] == "鲸鱼娘" and works["brief"]["intent"] == "研究海面反光"
    assert works["brief"]["creative"]["profile_id"] == profile["profile_id"]
    assert works["review_state"] == "accept"
    assert "prompt" not in works["brief"] and "profile" not in works["brief"]["creative"]
    profiles = view.records("a", "profiles")["items"][0]
    assert profiles["data"]["title"] == profile["data"]["title"] and len(profiles["data"]["stable"]) == 6
    assert profiles["provenance"] is not None
    result = view.records("a", "reviews")["items"][0]["review"]
    assert result["decision"] == "accept" and len(result["reason"]) == 500
    assert len(result["strengths"]) == 8 and result["comparison"]["preserved"] == ["主体"]
    summary = view.summary("a")
    assert summary["recent_works"][0]["subject"] == "鲸鱼娘"
    assert summary["recent_works"][0]["review_state"] == "accept"


@pytest.mark.parametrize("decision", ["accept", "revise", "discard"])
async def test_studio_r1_03_real_review_contract_is_visible_in_summary_and_records(data, decision):
    from image_plugin_under_test.thinking import Thinking
    from test_thinking import ACCEPT, CONTEXT

    _, db, view = data
    answer = {**ACCEPT, "decision": decision}
    if decision == "revise":
        answer.update(issues=["synthetic lighting"], edit_prompt="synthetic adjustment", preserve="synthetic subject")

    async def generate(request):
        assert request["task"] == "review" and request["image"] == PNG
        return answer

    validated = await Thinking(generate).review(CONTEXT, image=PNG, mime="image/png")
    db.db.execute("UPDATE reviews SET review=? WHERE space_id='a'", (json.dumps(validated),))
    assert view.summary("a")["recent_works"][0]["review_state"] == decision
    assert view.summary("a")["current_work"]["review_state"] == decision
    assert view.records("a", "works")["items"][0]["review_state"] == decision
    assert view.records("a", "reviews")["items"][0]["review"]["decision"] == decision


@pytest.mark.parametrize("version", range(1, SCHEMA_VERSION + 1))
def test_each_supported_schema_read_does_not_migrate(tmp_path, version):
    from image_plugin_under_test import store as module

    db = sqlite3.connect(tmp_path / "artist.sqlite3")
    for number in range(1, version + 1):
        for statement in getattr(module, "_SCHEMA" if number == 1 else f"_SCHEMA_{number}"):
            db.execute(statement)
    db.execute("INSERT INTO spaces VALUES(?,?,?,?,?)", ("a", OWNER.snapshot(), "old-binding", 1, 0))
    db.execute(f"PRAGMA user_version={version}")
    db.commit()
    db.close()
    before = snapshot(tmp_path)
    view = Studio(tmp_path, scopes={"a": OWNER})
    assert view.summary("a")["authorization"]["authorized"] is False
    assert view.records("a", "profiles")["items"] == []
    assert snapshot(tmp_path) == before


def test_bounded_recent_works_fit_chat_context_with_all_wait_reasons(data):
    root, db, _ = data
    for category, action in (("think", "think"), ("render", "submit"), ("share", "share")):
        db.reserve("a", OWNER, request_id="unknown-" + category, action=action, work_id="work",
                   params={"job_id": "job-a"} if action == "share" else {})
        db.mark_submitting("a", OWNER, "unknown-" + category)
        db.mark_unknown("a", OWNER, "unknown-" + category, reason="synthetic")
    db.db.execute("UPDATE artist_works SET brief=? WHERE space_id='a'", (json.dumps({**BRIEF, "subject": "鲸" * 80}),))
    db.update_artist_work("a", OWNER, "work", changes={"status": "finished"})
    for index in range(4):
        db.plan_work("a", OWNER, work_id=f"recent-{index}", brief={**BRIEF, "subject": "鲸" * 80}, inspiration_keys=[])
        if index < 3:
            db.update_artist_work("a", OWNER, f"recent-{index}", changes={"status": "finished"})
    db.db.execute("INSERT OR REPLACE INTO artist_state(space_id,next_due,backoff_until) VALUES(?,?,?)",
                  ("a", (NOW + timedelta(days=100)).isoformat(), (NOW + timedelta(days=100)).isoformat()))
    db.revoke("a", OWNER, source_ref="synthetic", granted_by="synthetic")
    db.set_global_limits(Limits())
    db.set_limits("a", OWNER, Limits())
    summary = Studio(root, scopes={"a": OWNER}, enabled=False, blocked={"a": True}).summary("a")
    assert len(json.dumps({"source": "codex_image_studio_readonly", "data": summary}, ensure_ascii=True)) < 8192


def test_summary_keeps_one_snapshot_while_another_connection_updates_work(data):
    root, db, _ = data
    db.db.execute("PRAGMA journal_mode=WAL")

    class Interleaved(Studio):
        updated = False

        def _work_summary(self, read_db, space_id, row):
            if not self.updated:
                self.updated = True
                writer = sqlite3.connect(root / "artist.sqlite3")
                writer.execute("UPDATE artist_works SET brief=?,status='finished' WHERE space_id='a'",
                               (json.dumps({**BRIEF, "subject": "later-subject"}),))
                writer.commit()
                writer.close()
            return super()._work_summary(read_db, space_id, row)

    result = Interleaved(root, scopes={"a": OWNER}).summary("a")
    assert result["recent_works"][0]["subject"] == "private-subject"
    assert result["current_work"]["subject"] == "private-subject"
    assert result["current_work"]["status"] == "planned"
    fresh = Studio(root, scopes={"a": OWNER}).summary("a")
    assert fresh["current_work"] is None and fresh["recent_works"][0]["subject"] == "later-subject"
