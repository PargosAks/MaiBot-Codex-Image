"""构造已记录原图及其提交回执，用于渲染和引用运输测试。"""

import hashlib

from image_plugin_under_test.domain.policy import Scope
from protocol_fixture import PNG


SCOPE = Scope("synthetic", "studio", "owner")


OTHER = Scope("synthetic", "elsewhere", "another-owner")


BINDING = "isolated-service"


PROMPT = "保留有意的空格  "


HASH = hashlib.sha256(PNG).hexdigest()


BRIEF = dict(intent="practice", subject="cat", mood="quiet", prompt=PROMPT, completion_criteria="clear")


def attempt(db, *, action="submit", request_id="r", state="submitting"):
    params = {"prompt": PROMPT}
    if action == "edit":
        params.update(source_job_id="original", source_sha256=HASH)
    db.reserve("s", SCOPE, request_id=request_id, action=action, params=params, work_id="work")
    db.update_artist_work("s", SCOPE, "work", changes={"pending_request": request_id, "status": "creating"})
    if state != "reserved":
        db.mark_submitting("s", SCOPE, request_id)
    if state == "unknown":
        db.mark_unknown("s", SCOPE, request_id, reason="synthetic lost receipt")
    elif state in {"accepted", "succeeded", "failed"}:
        db.mark_accepted("s", SCOPE, request_id, job_id="job")
        if state != "accepted":
            db.settle("s", SCOPE, request_id, outcome=state, evidence="synthetic terminal")
    return db.get_operation("s", SCOPE, request_id)


def parent(db):
    attempt(db, request_id="original-request")
    db.settle("s", SCOPE, "original-request", outcome="succeeded", evidence="synthetic", job_id="original")
    version = db.record_version(
        "s",
        SCOPE,
        work_id="work",
        version_id="parent",
        request_id="original-request",
        job_id="original",
        data=PNG,
        mime="image/png",
    )
    db.update_artist_work("s", SCOPE, "work", changes={"parent_version": "parent", "edit_prompt": PROMPT})
    return {key: version[key] for key in ("job_id", "sha256", "data", "mime")}
