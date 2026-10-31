"""Archive, inspect and reuse reviewed work without implicit drawing or sharing."""

from copy import deepcopy

from image_plugin_under_test.policy import Limits
from image_plugin_under_test.thinking import Thinking
from test_artist_flow import BRIEF, SCOPE, finished, step, studio  # noqa: F401


async def archive(studio):  # noqa: F811
    runtime, store, _, model, *_ = studio

    async def decide(request):
        if request["task"] != "plan":
            return await model(request)
        return {"ideas": [], "decision": {
            "action": "archive", "work_id": request["context"]["works"][0]["work_id"],
            "roles": ["palette"], "tags": ["rain", "warm"], "reason": "Reuse the reviewed palette",
        }}

    runtime.thinking = Thinking(decide)
    result = await step(studio)
    return result


async def test_archive_inspect_and_reference_are_independent_from_sharing(studio):  # noqa: F811
    runtime, store, _, model, renderer, sender = studio
    await finished(studio)
    assert (await archive(studio))["state"] == "archived"
    assert len(renderer.calls) == 1 and sender.calls == []
    candidates = store.list_assets("s", SCOPE)
    assert len(candidates) == 1 and candidates[0]["source_kind"] == "reviewed_work"
    asset_id = candidates[0]["asset_id"]

    async def inspect(request):
        assert request["context"]["assets"][0]["asset_id"] == asset_id
        return {"ideas": [], "decision": {"action": "inspect_assets", "asset_ids": [asset_id], "reason": "Compare details"}}

    runtime.thinking = Thinking(inspect)
    assert (await step(studio))["state"] == "assets_selected"
    assert len(renderer.calls) == 1 and sender.calls == []

    async def create(request):
        if request["task"] != "plan":
            return await model(request)
        if any(w["status"] == "planned" for w in request["context"]["works"]):
            work = next(w for w in request["context"]["works"] if w["status"] == "planned")
            return {"ideas": [], "decision": {"action": "continue", "work_id": work["work_id"], "reason": "Continue"}}
        assert request["context"]["asset_details"][0]["asset_id"] == asset_id
        brief = {**deepcopy(BRIEF), "intent": "A different study", "subject": "sunlit window"}
        return {"ideas": [], "decision": {"action": "create", "reason": "Explore palette", "brief": brief,
                "inspiration_keys": [], "references": [{"asset_id": asset_id, "role": "palette", "reason": "Warm contrast"}]}}

    runtime.thinking = Thinking(create)
    assert (await step(studio))["state"] == "planned"
    work = store.list_artist_works("s", SCOPE)[-1]
    assert work["brief"]["references"][0]["usage"] == "text_context"
    assert (await step(studio))["state"] == "review_pending"
    operations = [op for op in store.audit_operations("s", SCOPE) if op["action"] == "submit"]
    assert "未输入参考图" in operations[-1]["params"]["prompt"]
    assert len(renderer.calls) == 2 and sender.calls == []


async def test_finished_without_acceptance_cannot_enter_asset_library(studio):  # noqa: F811
    runtime, store, _, model, _, sender = studio
    store.set_limits("s", SCOPE, Limits(think=100, render=20, share=20, max_inflight=20, max_revisions=0))
    model.revise = 1
    await runtime.tick()
    await step(studio)
    await step(studio)
    assert (await step(studio))["state"] == "finished"
    result = await archive(studio)
    assert result["state"] == "blocked"
    assert store.list_assets("s", SCOPE) == [] and sender.calls == []


async def test_model_cannot_bind_an_asset_role_not_offered(studio):  # noqa: F811
    runtime, store, _, _, renderer, _ = studio
    await finished(studio)
    await archive(studio)
    asset_id = store.list_assets("s", SCOPE)[0]["asset_id"]

    async def invalid(request):
        return {"ideas": [], "decision": {"action": "create", "reason": "Wrong role", "brief": deepcopy(BRIEF),
                "inspiration_keys": [], "references": [{"asset_id": asset_id, "role": "character", "reason": "Not offered"}]}}

    runtime.thinking = Thinking(invalid)
    assert (await step(studio)) == {"state": "blocked", "reason": "THINK_FAILED"}
    assert len(store.list_artist_works("s", SCOPE)) == len(renderer.calls) == 1
