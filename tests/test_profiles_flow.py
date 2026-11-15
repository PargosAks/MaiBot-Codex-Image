"""Configured private profiles influence frozen plans, never identity or authority."""

from copy import deepcopy

import pytest

from image_plugin_under_test.domain.policy import StoreError
from image_plugin_under_test.creation.thinking import Thinking
from support.artist_fakes import SCOPE
from support.artist_flow import step, studio as studio
from support.catalog_examples import PROFILES, SERIES, CHOICE


def configure(store, profiles=None, series=None):
    store.configure_creative("s", SCOPE, profiles=profiles if profiles is not None else deepcopy(PROFILES),
                             series=series if series is not None else deepcopy(SERIES),
                             expected_auth_version=store.get_space("s", SCOPE)["auth_version"])


async def use_profile(runtime, model):
    async def decide(request):
        result = await model(request)
        if request["task"] == "plan" and result["decision"]["action"] == "create":
            result["decision"]["creative"] = deepcopy(CHOICE)
        return result
    runtime.thinking = Thinking(decide)


async def test_profile_snapshot_reaches_render_and_review_after_configuration_changes(studio):  # noqa: F811
    runtime, store, _, model, renderer, _ = studio
    configure(store)
    await use_profile(runtime, model)
    assert (await runtime.tick())["state"] == "planned"
    work = store.list_artist_works("s", SCOPE)[0]
    frozen = deepcopy(work["brief"])
    new = deepcopy(PROFILES)
    new[0]["revision"] = 2
    new[0]["data"]["palette"] = "cold blue"
    configure(store, new, [])
    assert (await step(studio))["state"] == "review_pending"
    operation = next(op for op in store.audit_operations("s", SCOPE) if op["action"] == "submit")
    assert "warm amber" in operation["params"]["prompt"]
    assert "cold blue" not in operation["params"]["prompt"]
    assert "moonlight" in operation["params"]["prompt"]
    assert (await step(studio))["state"] == "finished"
    review = next(c for c in model.calls if c["task"] == "review")
    assert review["context"]["current_work"]["brief"] == frozen
    assert len(renderer.calls) == 1


async def test_profile_prompt_overflow_fails_plan_and_retries_with_shorter_body(studio):  # noqa: F811
    runtime, store, _, model, renderer, sender = studio
    configure(store)
    suffix = "\n\n本作品固定创作方向：\n" + runtime.creative_direction

    async def decide(request):
        result = await model(request)
        decision = result["decision"]
        if decision["action"] == "create":
            decision["creative"] = deepcopy(CHOICE)
            if len(model.calls) == 1:
                decision["brief"]["prompt"] = "画" * (4000 - len(suffix))
                result["ideas"] = [{"summary": "超长方案不应留下灵感", "source_ids": []}]
        return result

    runtime.thinking = Thinking(decide)
    assert (await runtime.tick()) == {"state": "blocked", "reason": "PROMPT_TOO_LONG"}
    (failed,) = store.audit_operations("s", SCOPE)
    assert (failed["action"], failed["state"], failed["evidence"]) == ("think", "failed", "PROMPT_TOO_LONG")
    assert store.list_inspirations("s", SCOPE) == store.list_artist_works("s", SCOPE) == []
    assert renderer.calls == sender.calls == []
    assert (await runtime.run_once())["state"] == "idle" and len(model.calls) == 1

    assert (await step(studio))["state"] == "planned" and len(model.calls) == 2
    assert store.get_operation("s", SCOPE, failed["request_id"]) == failed
    (work,) = store.list_artist_works("s", SCOPE)
    assert work["brief"]["creative"]["profile"] == PROFILES[0]["data"]
    assert (await step(studio))["state"] == "review_pending"
    (render,) = renderer.calls
    assert "warm amber" in render["prompt"] and len(render["prompt"]) <= 4000
    assert not sender.calls


async def test_model_cannot_change_stable_profile_attributes(studio):  # noqa: F811
    runtime, store, _, model, renderer, sender = studio
    configure(store)

    async def invalid(request):
        answer = await model(request)
        answer["decision"]["creative"] = {**deepcopy(CHOICE), "changes": {"subject": "different person"}}
        return answer

    runtime.thinking = Thinking(invalid)
    assert (await runtime.tick()) == {"state": "blocked", "reason": "THINK_FAILED"}
    assert store.list_artist_works("s", SCOPE) == []
    assert renderer.calls == sender.calls == []


async def test_closed_series_is_not_reopened_by_same_host_configuration(studio):  # noqa: F811
    runtime, store, _, _, renderer, sender = studio
    configure(store)

    async def close(request):
        assert request["context"]["series"][0]["series_id"] == "windows"
        return {"ideas": [], "decision": {"action": "close_series", "series_id": "windows", "reason": "Study complete"}}

    runtime.thinking = Thinking(close)
    assert (await runtime.tick())["state"] == "series_closed"
    configure(store)
    assert store.list_series("s", SCOPE) == []
    assert renderer.calls == sender.calls == []


def test_configuration_cannot_rewrite_an_existing_profile_revision(studio):  # noqa: F811
    _, store, *_ = studio
    configure(store)
    changed = deepcopy(PROFILES)
    changed[0]["data"]["palette"] = "replacement"
    with pytest.raises(StoreError):
        configure(store, changed)
    assert store.list_profiles("s", SCOPE) == PROFILES
