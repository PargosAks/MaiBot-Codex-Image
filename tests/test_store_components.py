"""SQLite regressions for the shared ArtistStore components."""

from contextlib import closing
import sqlite3

import pytest

from image_plugin_under_test.domain import store as store_module
from image_plugin_under_test.domain.policy import Delegation, Limits, Scope, StoreError, canonical_json
from image_plugin_under_test.domain.store import ArtistStore


SPACE = "画室"
SCOPE = Scope("qq", "画室会话", "画师")
WORK = "窗边"
VERSION = "初稿"
JOB = "local-job"
LIMITS = Limits(think=5, render=5, share=5, max_inflight=5, max_revisions=2)
BRIEF = {
    "intent": "练习光影", "subject": "窗户", "mood": "安静", "prompt": "窗边光影",
    "completion_criteria": "看得出窗户",
}


def authorize(store, space=SPACE, scope=SCOPE):
    store.create_space(space, scope, binding_id="local-fixture")
    store.grant(space, scope, Delegation("明确授权", "管理员", True))
    store.set_limits(space, scope, LIMITS)


@pytest.fixture
def ledger(tmp_path):
    with closing(ArtistStore(tmp_path / "artist.sqlite3")) as store:
        authorize(store)
        store.set_global_limits(LIMITS)
        yield store


def bind_render_job(store, space, scope, *, request_id, work_id, job_id):
    store.reserve(space, scope, request_id=request_id, action="submit", work_id=work_id,
                  params={"prompt": "窗边光影"})
    store.mark_submitting(space, scope, request_id)
    store.settle(space, scope, request_id, outcome="succeeded", evidence="本地模拟回执", job_id=job_id)


def render(store):
    bind_render_job(store, SPACE, SCOPE, request_id="render", work_id=WORK, job_id=JOB)
    return store.record_version(
        SPACE, SCOPE, work_id=WORK, version_id=VERSION, request_id="render", job_id=JOB,
        data=b"synthetic-image", mime="image/png",
    )


def finish_review(store, version):
    review = {"decision": "accept", "reason": "符合创作目标"}
    store.reserve(
        SPACE, SCOPE, request_id="review", action="think", work_id=WORK,
        params={"task": "review", "version_id": VERSION, "context": {
            "current_work": {"work_id": WORK},
            "current_version": {key: version[key] for key in ("version_id", "sha256", "mime")},
        }},
    )
    store.mark_submitting(SPACE, SCOPE, "review")
    store.settle(SPACE, SCOPE, "review", outcome="succeeded", evidence=canonical_json(review))
    store.record_review(SPACE, SCOPE, version_id=VERSION, request_id="review", review=review)
    store.update_artist_work(SPACE, SCOPE, WORK, changes={"status": "finished", "best_version": VERSION})


@pytest.mark.parametrize("bound_version", [VERSION, "unrelated-version", None])
def test_post_review_progress_attributes_share_without_work_id(ledger, bound_version):
    ledger.plan_work(SPACE, SCOPE, work_id=WORK, inspiration_keys=[], brief=BRIEF)
    finish_review(ledger, render(ledger))
    ledger.reserve(SPACE, SCOPE, request_id="share", action="share",
                   params={"job_id": JOB, "version_id": bound_version})
    ledger.mark_submitting(SPACE, SCOPE, "share")
    ledger.mark_unknown(SPACE, SCOPE, "share", reason="模拟发送未确认")

    # 新连接读取已有 schema 10、中文作品和历史 contract 1 评审。
    with closing(ArtistStore(ledger.path)) as reopened:
        assert reopened.version_accepted(SPACE, SCOPE, VERSION)
        assert reopened.get_operation(SPACE, SCOPE, "share")["work_id"] is None
        assert reopened.schedule_progress_wake(SPACE, SCOPE) is (bound_version != VERSION)


@pytest.mark.parametrize(("case", "expected_wake"), [
    ("unique", False),
    ("same-work-twice", False),
    ("other-work-job", True),
    ("ambiguous", True),
    ("foreign-share", True),
    ("foreign-job-reuse", False),
])
def test_post_review_progress_resolves_historical_job_only_share(tmp_path, case, expected_wake):
    path = tmp_path / "old-share.sqlite3"
    share_space, share_scope, share_job = SPACE, SCOPE, JOB
    with closing(ArtistStore(path)) as store:
        authorize(store)
        store.set_global_limits(LIMITS)
        store.plan_work(SPACE, SCOPE, work_id=WORK, inspiration_keys=[], brief=BRIEF)
        finish_review(store, render(store))
        if case in {"same-work-twice", "other-work-job", "ambiguous"}:
            other_work = WORK if case == "same-work-twice" else "另一件作品"
            other_job = "another-job" if case == "other-work-job" else JOB
            store.create_work(SPACE, SCOPE, other_work)
            # 原始渲染回执可能已绑定 job，但另一作品的版本还没落库。
            bind_render_job(store, SPACE, SCOPE, request_id="other-render", work_id=other_work, job_id=other_job)
            share_job = other_job
        if case in {"foreign-share", "foreign-job-reuse"}:
            other_space, other_scope = "另一画室", Scope("qq", "另一会话", "另一画师")
            authorize(store, other_space, other_scope)
            store.create_work(other_space, other_scope, "另一件作品")
            bind_render_job(store, other_space, other_scope, request_id="other-render", work_id="另一件作品", job_id=JOB)
            if case == "foreign-share":
                share_space, share_scope = other_space, other_scope

        # 旧公开 API 只接收 job_id，故意不写 work_id 或 version_id。
        store.reserve(share_space, share_scope, request_id="old-share", action="share", params={"job_id": share_job})
        store.mark_submitting(share_space, share_scope, "old-share")
        store.mark_unknown(share_space, share_scope, "old-share", reason="模拟发送未确认")
        prior = store.get_operation(share_space, share_scope, "old-share")
        usage = store.budget_usage(share_space, share_scope)

    with closing(ArtistStore(path)) as reopened:
        assert prior["state"] == "unknown" and prior["work_id"] is None
        assert prior["params"] == {"job_id": share_job}
        assert usage["share"]["held"] == 1
        assert reopened.version_accepted(SPACE, SCOPE, VERSION)
        assert reopened.schedule_progress_wake(SPACE, SCOPE) is expected_wake
        assert reopened.get_operation(share_space, share_scope, "old-share") == prior
        assert reopened.budget_usage(share_space, share_scope) == usage


def test_share_dispatch_rechecks_legacy_lock_without_counting_its_own_attempt(ledger, tmp_path):
    ledger.create_work(SPACE, SCOPE, WORK)
    render(ledger)
    ledger.reserve(SPACE, SCOPE, request_id="share", action="share",
                   params={"job_id": JOB, "version_id": VERSION})
    ledger.mark_submitting(SPACE, SCOPE, "share")
    prior = ledger.validate_submitting(SPACE, SCOPE, "share")

    source = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(source)) as legacy, legacy:
        legacy.execute("CREATE TABLE delivery(job TEXT PRIMARY KEY, state TEXT NOT NULL)")
        legacy.execute("INSERT INTO delivery VALUES(?, 'sending')", (JOB,))
    with closing(ArtistStore(ledger.path)) as other:
        other.migrate_legacy_deliveries(source, tmp_path / "backup.sqlite3", offline=True)

    with pytest.raises(StoreError, check=lambda exc: exc.code == "LEGACY_LOCK"):
        ledger.validate_submitting(SPACE, SCOPE, "share")
    assert ledger.get_operation(SPACE, SCOPE, "share") == prior
    assert ledger.budget_usage(SPACE, SCOPE)["share"]["held"] == 1


@pytest.mark.parametrize("version", range(1, 11))
def test_upgrade_historical_chinese_ledger_preserves_unknown_attempt(version, tmp_path):
    path = tmp_path / "历史台账.sqlite3"
    timestamp = "2026-11-10T00:00:00+00:00"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        for step in range(1, version + 1):
            for statement in getattr(store_module, "_SCHEMA" if step == 1 else f"_SCHEMA_{step}"):
                db.execute(statement)
        db.execute(f"PRAGMA user_version={version}")
        db.execute("INSERT INTO spaces VALUES(?,?,?,1,1)", (SPACE, SCOPE.snapshot(), "本地渲染器"))
        db.execute("INSERT INTO grants VALUES(?,1,1,?,?,?)", (SPACE, "明确授权", "管理员", timestamp))
        db.execute("INSERT INTO works VALUES(?,?,?)", (SPACE, WORK, timestamp))
        db.execute(
            """INSERT INTO operations(id,space_id,work_id,request_id,action,category,params_json,
            scope_snapshot,binding_id,auth_version,trigger,state,budget_day,evidence,created_at,updated_at)
            VALUES(17,?,?,?,'submit','render',?,?,?,1,'chat','unknown','2026-11-10',?,?,?)""",
            (SPACE, WORK, "未确认绘图", canonical_json({"prompt": "窗边光影"}), SCOPE.snapshot(),
             "本地渲染器", "模拟超时", timestamp, timestamp),
        )
    before = path.read_bytes()

    with closing(ArtistStore(path)) as upgraded:
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 10
        assert upgraded.db.execute("PRAGMA foreign_key_check").fetchall() == []
        operation = upgraded.get_operation(SPACE, SCOPE, "未确认绘图")
        assert (operation["id"], operation["state"], operation["evidence"]) == (17, "unknown", "模拟超时")
        assert operation["params"] == {"prompt": "窗边光影"}
        assert upgraded.audit_authorizations(SPACE, SCOPE)[0]["source_ref"] == "明确授权"
        assert upgraded.budget_usage(SPACE, SCOPE)["render"]["held"] == 1
    if version == 10:
        assert path.read_bytes() == before
