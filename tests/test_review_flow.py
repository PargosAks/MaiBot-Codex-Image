"""Review actual version sets through the artist loop, without model/network effects."""

from dataclasses import replace
import hashlib

from image_plugin_under_test.artist import stable_id
from image_plugin_under_test.policy import Limits
from image_plugin_under_test.thinking import Thinking
from test_artist_flow import SCOPE, step, studio  # noqa: F401


async def test_three_distinct_versions_reach_review_and_old_best_is_shared(studio):  # noqa: F811
    runtime, store, _, model, renderer, sender = studio
    original_submit = renderer.submit
    seen = []

    async def submit(**kwargs):
        result = await original_submit(**kwargs)
        data = result.image + bytes([len(renderer.calls)])
        result = replace(result, image=data, sha256=hashlib.sha256(data).hexdigest())
        renderer.jobs[result.request_id] = result
        return result

    async def decide(request):
        if request["task"] != "review":
            return await model(request)
        seen.append(request)
        inputs = request["images"]
        answer = {
            "decision": "revise" if len(seen) < 3 else "accept", "reason": "Synthetic comparison",
            "strengths": ["composition"], "issues": ["lighting"] if len(seen) < 3 else [],
            "best_version_id": next(i["version_id"] for i in inputs if "previous_best" in i["roles"]),
            "comparison": {"improvements": [], "regressions": ["lost detail"], "preserved": ["subject"],
                           "evidence_version_ids": [i["version_id"] for i in inputs]},
        }
        if answer["decision"] == "revise":
            answer.update(edit_prompt="Improve lighting", preserve="Keep subject")
        return answer

    renderer.submit = submit
    runtime.thinking = Thinking(decide)
    assert (await runtime.tick())["state"] == "planned"
    for _ in range(2):
        assert (await step(studio))["state"] == "review_pending"
        assert (await step(studio))["state"] == "planned"
    assert (await step(studio))["state"] == "review_pending"
    assert (await step(studio))["state"] == "finished"
    assert len(seen[-1]["images"]) == 3
    assert [i["roles"] for i in seen[-1]["images"]] == [["current"], ["parent"], ["previous_best"]]
    assert len({i["data"] for i in seen[-1]["images"]}) == 3
    work = store.list_artist_works("s", SCOPE)[0]
    assert work["best_version"] == seen[0]["images"][0]["version_id"]
    model.share = True
    assert (await step(studio))["state"] == "shared"
    assert sender.calls[0][1]["data"] == seen[0]["images"][0]["data"]


async def test_reserved_legacy_review_keeps_original_contract(studio):  # noqa: F811
    runtime, store, _, model, _, _ = studio
    await runtime.tick()
    await step(studio)
    work = store.list_artist_works("s", SCOPE)[0]
    version = store.list_versions("s", SCOPE, work["work_id"])[0]
    context = await runtime._context([])
    context.update(
        current_work={k: work[k] for k in ("work_id", "brief", "best_version")},
        current_version={k: version[k] for k in ("version_id", "sha256", "mime")},
        versions=[{k: version[k] for k in ("version_id", "parent_id", "sha256", "mime")}],
    )
    request_id = stable_id("review", "s", "version", work["work_id"], version["version_id"]) + ":0"
    store.reserve("s", SCOPE, request_id=request_id, action="think", work_id=work["work_id"],
                  params={"task": "review", "context": context, "version_id": version["version_id"]})
    assert (await step(studio))["state"] == "finished"
    calls = [c for c in model.calls if c["task"] == "review"]
    assert len(calls) == 1 and "image" in calls[0] and "images" not in calls[0]
    assert store.get_review_snapshot("s", SCOPE, version["version_id"]) is None
    assert store.get_operation("s", SCOPE, request_id)["state"] == "succeeded"


async def test_revision_limit_does_not_approve_image_for_sharing(studio):  # noqa: F811
    runtime, store, _, model, _, sender = studio
    store.set_limits("s", SCOPE, Limits(think=100, render=20, share=20, max_inflight=20, max_revisions=0))
    model.revise = 1
    await runtime.tick()
    await step(studio)
    assert (await step(studio))["state"] == "planned"
    assert (await step(studio))["reason"] == "REVISION_LIMIT"
    model.share = True
    assert (await step(studio)) == {"state": "blocked", "reason": "REVIEW_REQUIRED"}
    assert sender.calls == []


async def test_accepted_image_still_waits_for_unknown_render_before_sharing(studio):  # noqa: F811
    runtime, store, _, model, renderer, sender = studio
    renderer.unknown_artifact = True
    await runtime.tick()
    await step(studio)
    assert (await step(studio))["state"] == "finished"
    model.share = True
    await step(studio)
    assert sender.calls == [] and store.budget_usage("s", SCOPE)["render"]["held"] == 1
    renderer.complete()
    await step(studio)  # reconcile first, then allow planning on the next wake
    assert (await step(studio))["state"] == "shared"


async def test_unavailable_parent_does_not_trigger_another_render(studio, monkeypatch):  # noqa: F811
    runtime, store, _, model, renderer, _ = studio
    model.revise = 1
    await runtime.tick()
    await step(studio)
    await step(studio)
    await step(studio)
    work = store.list_artist_works("s", SCOPE)[0]
    parent = store.list_versions("s", SCOPE, work["work_id"])[0]["version_id"]
    original = store.get_version

    def corrupt(*args):
        result = original(*args)
        return {**result, "data": b"corrupted"} if result["version_id"] == parent else result

    monkeypatch.setattr(store, "get_version", corrupt)
    calls = len(model.calls)
    assert (await step(studio)) == {"state": "blocked", "reason": "ARTIFACT_INVALID"}
    assert len(renderer.calls) == 2 and len(model.calls) == calls
