"""Archive, inspect and reuse reviewed work without implicit drawing or sharing."""

from copy import deepcopy

import pytest

from image_plugin_under_test.domain.policy import Limits
from image_plugin_under_test.creation.thinking import Thinking
from support.artist_fakes import BRIEF, SCOPE
from support.artist_flow import finished, step, studio as studio
from support.catalog_examples import CHOICE, PROFILES, SERIES


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


@pytest.mark.parametrize("with_profile", [False, True], ids=["reference-only", "combined-overflow"])
async def test_reference_prompt_overflow_retries_without_persisting_rejected_plan(studio, with_profile):  # noqa: F811
    runtime, store, _, model, renderer, sender = studio
    await finished(studio)
    assert (await archive(studio))["state"] == "archived"
    (asset,) = store.list_assets("s", SCOPE)
    references = [{"asset_id": asset["asset_id"], "role": "palette", "reason": "Warm contrast"}]
    auth_version = store.get_space("s", SCOPE)["auth_version"]
    suffix = "\n\n本作品固定创作方向：\n" + runtime.creative_direction
    base_limit = 4000
    if with_profile:
        store.configure_creative("s", SCOPE, profiles=deepcopy(PROFILES), series=deepcopy(SERIES),
                                 expected_auth_version=auth_version)
        # Each addendum fits by itself at this length; the two together must fail.
        probe = "画"
        choices = ({"creative": CHOICE}, {"references": references})
        headroom = max(len(store.preview_plan_prompt(
            "s", SCOPE, prompt=probe, expected_auth_version=auth_version, **selection,
        )) - len(probe) for selection in choices)
        base_limit -= headroom

    old_works = store.list_artist_works("s", SCOPE)
    old_ideas = store.list_inspirations("s", SCOPE)
    attempts = 0

    async def create(request):
        nonlocal attempts
        if any(work["status"] == "planned" for work in request["context"]["works"]):
            return await model(request)
        attempts += 1
        brief = dict(BRIEF, intent="A different study", subject="sunlit window")
        if attempts == 1:
            brief["prompt"] = "画" * (base_limit - len(suffix))
        decision = {"action": "create", "reason": "Explore palette", "brief": brief,
                    "inspiration_keys": [], "references": deepcopy(references)}
        if with_profile:
            decision["creative"] = deepcopy(CHOICE)
        return {"ideas": [{"summary": f"素材方案 {attempts}", "source_ids": []}], "decision": decision}

    runtime.thinking = Thinking(create)
    assert (await step(studio)) == {"state": "blocked", "reason": "PROMPT_TOO_LONG"}
    failed = store.audit_operations("s", SCOPE)[-1]
    assert (failed["action"], failed["state"], failed["evidence"]) == ("think", "failed", "PROMPT_TOO_LONG")
    assert store.list_artist_works("s", SCOPE) == old_works
    assert store.list_inspirations("s", SCOPE) == old_ideas
    assert len(renderer.calls) == 1 and not sender.calls
    assert (await runtime.run_once())["state"] == "idle" and attempts == 1

    assert (await step(studio))["state"] == "planned" and attempts == 2
    assert store.get_operation("s", SCOPE, failed["request_id"]) == failed
    assert "素材方案 1" not in {idea["summary"] for idea in store.list_inspirations("s", SCOPE)}
    assert store.get_artist_work("s", SCOPE, old_works[0]["work_id"])["brief"] == old_works[0]["brief"]
    assert (await step(studio))["state"] == "review_pending"
    assert len(renderer.calls) == 2 and not sender.calls
    prompt = renderer.calls[-1]["prompt"]
    assert "未输入参考图" in prompt and len(prompt) <= 4000
    if with_profile:
        assert "warm amber" in prompt


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
