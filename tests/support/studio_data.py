"""供工作区投影和公开 Host 注册测试使用的临时账本。"""

from datetime import datetime, timezone

import pytest

from image_plugin_under_test.domain.policy import Delegation, Limits, Scope
from image_plugin_under_test.domain.store import ArtistStore
from image_plugin_under_test.workspace.studio import Studio
from protocol_fixture import PNG


NOW = datetime(2026, 11, 2, 1, tzinfo=timezone.utc)


OWNER = Scope("synthetic", "space-a-chat", "owner")


OTHER = Scope("synthetic", "space-b-chat", "other-owner")


BRIEF = {"intent": "private-intent", "subject": "private-subject", "mood": "quiet",
         "prompt": "file:///never-open-this.png", "completion_criteria": "private-criteria"}


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
