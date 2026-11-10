"""构造经过审查的作品及可验证的审查证据。"""

from image_plugin_under_test.domain.policy import Scope, canonical_json


SCOPE = Scope("test", "asset-stream", "asset-owner")


BRIEF = {"intent": "study light", "subject": "window", "mood": "quiet", "prompt": "a quiet window",
         "completion_criteria": "warm lighting"}


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
