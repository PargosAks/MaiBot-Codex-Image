"""Configured private profiles influence frozen plans, never identity or authority."""

from copy import deepcopy

import pytest

from image_plugin_under_test.policy import StoreError
from image_plugin_under_test.thinking import Thinking
from test_artist_flow import SCOPE, step, studio  # noqa: F401


PROFILES = [{"profile_id": "rain", "revision": 1, "data": {
    "title": "Rain studies", "palette": "warm amber", "technique": "watercolor",
    "stable": {"subject": "cat by window"}, "variable": {"light": "soft"}, "source": "configured",
}}]
SERIES = [{"series_id": "windows", "title": "Window series", "goal": "Explore changing weather",
           "profile_id": "rain", "profile_revision": 1}]
CHOICE = {"profile_id": "rain", "profile_revision": 1, "series_id": "windows",
          "changes": {"light": "moonlight"}, "reason": "Continue the weather study"}


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
