"""Synthetic review contracts only; no image decoding, model or network calls."""

from copy import deepcopy
from unittest.mock import AsyncMock
import asyncio
import hashlib

import pytest

from image_plugin_under_test.policy import StoreError
from image_plugin_under_test.thinking import Thinking


def review_case():
    images = []
    for version, role, mime in (("v-current", "current", "image/png"),
                                ("v-parent", "parent", "image/jpeg"),
                                ("v-best", "previous_best", "image/webp")):
        data = f"synthetic original {version}".encode()
        images.append({"version_id": version, "roles": [role], "mime": mime,
                       "sha256": hashlib.sha256(data).hexdigest(), "data": data})
    inputs = [{key: deepcopy(value) for key, value in image.items() if key != "data"} for image in images]
    versions = [{key: value for key, value in item.items() if key != "roles"} for item in inputs]
    context = {"review_contract": 2, "review_inputs": inputs, "current_version": deepcopy(versions[0]),
               "versions": versions + [{"version_id": "unseen-history"}]}
    return context, images


def review_answer(decision="accept"):
    answer = {"decision": decision, "reason": "Synthetic assessment", "strengths": ["clear subject"],
              "issues": [], "best_version_id": "v-parent",
              "comparison": {"improvements": ["clearer light"], "regressions": [],
                             "preserved": ["subject retained"], "evidence_version_ids": ["v-current", "v-parent"]}}
    if decision == "revise":
        answer.update(issues=["too dark"], edit_prompt="Brighten the background", preserve="Keep the subject")
    return answer


@pytest.mark.parametrize("decision", ["accept", "revise", "discard"])
async def test_v2_sends_all_original_bytes_in_order_with_comparison(decision):
    context, images = review_case()
    before = deepcopy((context, images))
    answer = review_answer(decision)
    generate = AsyncMock(return_value=answer)
    assert await Thinking(generate).review(context, images=images) == answer
    generate.assert_awaited_once()
    request = generate.call_args.args[0]
    assert set(request) == {"task", "instructions", "context", "images"}
    assert request["task"] == "review" and request["context"] == context and request["images"] == images
    assert "comparison" in request["instructions"] and "evidence_version_ids" in request["instructions"]
    assert (context, images) == before


@pytest.mark.parametrize("count", [1, 2, 3])
async def test_version_deduplication_allows_one_image_with_multiple_roles(count):
    context, images = review_case()
    images = images[:count]
    context["review_inputs"] = context["review_inputs"][:count]
    if count == 1:
        roles = ["current", "parent", "previous_best"]
        images[0]["roles"] = roles[:]
        context["review_inputs"][0]["roles"] = roles[:]
    elif count == 2:
        images[1]["roles"].append("previous_best")
        context["review_inputs"][1]["roles"].append("previous_best")
    answer = review_answer()
    answer["best_version_id"] = "v-current"
    answer["comparison"]["evidence_version_ids"] = ["v-current"]
    generate = AsyncMock(return_value=answer)
    assert await Thinking(generate).review(context, images=images) == answer
    assert len(generate.call_args.args[0]["images"]) == count


async def test_first_image_only_needs_current_role_and_evidence_not_best_selection():
    context, images = review_case()
    context["review_inputs"] = context["review_inputs"][:1]
    answer = review_answer()
    del answer["best_version_id"]
    answer["comparison"] = {"improvements": [], "regressions": [], "preserved": [],
                            "evidence_version_ids": ["v-current"]}
    assert await Thinking(AsyncMock(return_value=answer)).review(context, images=images[:1]) == answer


async def test_same_bytes_do_not_merge_distinct_versions():
    context, images = review_case()
    for index in (1, 2):
        images[index].update(data=images[0]["data"], sha256=images[0]["sha256"], mime=images[0]["mime"])
        context["review_inputs"][index].update(sha256=images[0]["sha256"], mime=images[0]["mime"])
    generate = AsyncMock(return_value=review_answer())
    await Thinking(generate).review(context, images=images)
    assert len(generate.call_args.args[0]["images"]) == 3


async def test_snapshot_order_is_preserved_even_when_current_is_last():
    context, images = review_case()
    context["review_inputs"].reverse()
    images.reverse()
    generate = AsyncMock(return_value=review_answer())
    await Thinking(generate).review(context, images=images)
    assert generate.call_args.args[0]["images"] == images
    assert generate.call_args.args[0]["images"][-1]["roles"] == ["current"]


@pytest.mark.parametrize("mutate", [
    lambda c: c.pop("review_contract"),
    lambda c: c.update(review_contract=True),
    lambda c: c.update(review_contract=1),
    lambda c: c.update(review_contract=3),
    lambda c: c.update(review_contract="2"),
    lambda c: c.pop("review_inputs"),
    lambda c: c.update(review_inputs=[]),
    lambda c: c.update(review_inputs=tuple(c["review_inputs"])),
    lambda c: c["review_inputs"].append(dict(c["review_inputs"][0], version_id="fourth")),
    lambda c: c["review_inputs"][1].update(version_id="v-current"),
    lambda c: c["review_inputs"][2].update(roles=["parent"]),
    lambda c: c["review_inputs"][0].update(roles=["current", "current"]),
    lambda c: c["review_inputs"][0].update(roles=[]),
    lambda c: c["review_inputs"][0].update(roles=["source"]),
    lambda c: c["review_inputs"][0].update(roles="current"),
    lambda c: c["review_inputs"][0].update(roles=[True]),
    lambda c: c["review_inputs"][0].update(roles=["previous_best"]),
    lambda c: c["current_version"].update(version_id="wrong-current"),
    lambda c: c.pop("current_version"),
    lambda c: c.update(current_version=None),
    lambda c: c["review_inputs"][0].update(version_id=" "),
    lambda c: c["review_inputs"][0].update(sha256="A" * 64),
    lambda c: c["review_inputs"][0].update(sha256="bad"),
    lambda c: c["review_inputs"][0].pop("sha256"),
    lambda c: c["review_inputs"][0].update(mime="image/gif"),
    lambda c: c["review_inputs"][0].update(mime=["image/png"]),
    lambda c: c["review_inputs"][0].update(data="not-metadata"),
    lambda c: c["review_inputs"][0].update(owner="forged"),
    lambda c: c["review_inputs"][0].update(request_id="forged"),
    lambda c: c.update(owner="forged"),
    lambda c: c.update(request_id="forged"),
    lambda c: c.update(request="forged"),
    lambda c: c.update(persona={"owner": "forged"}),
])
async def test_invalid_context_is_rejected_before_callback(mutate):
    context, images = review_case()
    mutate(context)
    generate = AsyncMock()
    with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_CONTEXT"):
        await Thinking(generate).review(context, images=images)
    generate.assert_not_awaited()


@pytest.mark.parametrize("mutate", [
    lambda images: images.pop(),
    lambda images: images.append(deepcopy(images[0])),
    lambda images: images.reverse(),
    lambda images: images[0].update(data=b"changed bytes"),
    lambda images: images[0].update(data=b""),
    lambda images: images[0].update(data=bytearray(images[0]["data"])),
    lambda images: images[0].update(data="description instead of bytes"),
    lambda images: images[0].update(data=b"x" * (8 * 1024 * 1024 + 1)),
    lambda images: images[0].update(sha256="0" * 64),
    lambda images: images[0].update(mime="image/jpeg"),
    lambda images: images[0].update(roles=["parent"]),
    lambda images: images[0].update(version_id="v-parent"),
    lambda images: images[0].update(owner="forged"),
    lambda images: images[0].update(request_id="forged"),
    lambda images: images[0].update(url="https://invalid.example/image.png"),
    lambda images: images[0].pop("data"),
    lambda images: images[0].pop("roles"),
])
async def test_invalid_images_are_not_repaired_or_dispatched(mutate):
    context, images = review_case()
    mutate(images)
    generate = AsyncMock()
    with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_ARGUMENT"):
        await Thinking(generate).review(context, images=images)
    generate.assert_not_awaited()


@pytest.mark.parametrize("images", [None, [], {}, (), "description"])
async def test_v2_requires_an_image_list(images):
    context, _ = review_case()
    generate = AsyncMock()
    with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_ARGUMENT"):
        await Thinking(generate).review(context, images=images)
    generate.assert_not_awaited()


async def test_inclusive_per_image_and_total_byte_limits():
    context, images = review_case()
    data = b"x" * (8 * 1024 * 1024)
    digest = hashlib.sha256(data).hexdigest()
    for metadata, image in zip(context["review_inputs"], images):
        metadata["sha256"] = digest
        image.update(data=data, sha256=digest)
    generate = AsyncMock(return_value=review_answer())
    await Thinking(generate).review(context, images=images)
    assert sum(len(image["data"]) for image in generate.call_args.args[0]["images"]) == 24 * 1024 * 1024


@pytest.mark.parametrize("mutate", [
    lambda a: a.pop("comparison"),
    lambda a: a.update(comparison=None),
    lambda a: a.update(comparison=[]),
    lambda a: a["comparison"].pop("improvements"),
    lambda a: a["comparison"].pop("regressions"),
    lambda a: a["comparison"].pop("preserved"),
    lambda a: a["comparison"].pop("evidence_version_ids"),
    lambda a: a["comparison"].update(improvements="better"),
    lambda a: a["comparison"].update(regressions=[1]),
    lambda a: a["comparison"].update(preserved=[" "]),
    lambda a: a["comparison"].update(evidence_version_ids=[]),
    lambda a: a["comparison"].update(evidence_version_ids="v-current"),
    lambda a: a["comparison"].update(evidence_version_ids=["unseen-history"]),
    lambda a: a["comparison"].update(evidence_version_ids=["v-current", "foreign"]),
    lambda a: a["comparison"].update(evidence_version_ids=[1]),
    lambda a: a["comparison"].update(owner="forged"),
    lambda a: a.update(best_version_id="unseen-history"),
    lambda a: a.update(best_version_id="foreign"),
    lambda a: a.update(owner="forged"),
    lambda a: a.update(edit_prompt="forbidden for accept"),
    lambda a: a.update(decision="discard", preserve="forbidden for discard"),
    lambda a: a.update(decision="revise"),
    lambda a: a.update(decision="revise", issues=[], edit_prompt="change", preserve="subject"),
    lambda a: a.update(decision="revise", issues=["dark"], edit_prompt="x" * 3501, preserve="subject"),
    lambda a: a.update(decision="revise", issues=["dark"], edit_prompt="brighten", preserve="x" * 401),
])
async def test_invalid_v2_response_is_rejected_without_fallback_or_retry(mutate):
    context, images = review_case()
    answer = review_answer()
    mutate(answer)
    before = deepcopy(answer)
    generate = AsyncMock(return_value=answer)
    with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_DECISION"):
        await Thinking(generate).review(context, images=images)
    assert answer == before
    generate.assert_awaited_once()


async def test_callback_mutation_cannot_change_callers_or_visible_version_set():
    context, images = review_case()
    before = deepcopy((context, images))

    async def generate(request):
        request["context"]["review_inputs"][0]["version_id"] = "invented"
        request["images"][0]["roles"].append("injected")
        answer = review_answer()
        answer["comparison"]["evidence_version_ids"] = ["invented"]
        return answer

    with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_DECISION"):
        await Thinking(generate).review(context, images=images)
    assert (context, images) == before


@pytest.mark.parametrize("media", [{"image": b"pseudo", "mime": "image/png"},
                                  {"image": b"pseudo"}, {"mime": "image/png"}])
async def test_v2_rejects_pseudo_single_image_even_with_valid_images(media):
    context, images = review_case()
    generate = AsyncMock()
    with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_ARGUMENT"):
        await Thinking(generate).review(context, images=images, **media)
    generate.assert_not_awaited()


async def test_v1_keeps_legacy_image_request_and_historical_best_selection():
    context = {"versions": [{"version_id": "unseen-history"}]}
    answer = review_answer()
    del answer["comparison"]
    answer["best_version_id"] = "unseen-history"
    generate = AsyncMock(return_value=answer)
    assert await Thinking(generate).review(context, image=b"legacy", mime="image/png") == answer
    request = generate.call_args.args[0]
    assert set(request) == {"task", "instructions", "context", "image", "mime"}
    assert request["image"] == b"legacy" and request["mime"] == "image/png"
    assert "comparison" not in request["instructions"]


async def test_v1_rejects_v2_images_and_does_not_accept_comparison():
    _, images = review_case()
    generate = AsyncMock(return_value=review_answer())
    with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_ARGUMENT"):
        await Thinking(generate).review({}, image=b"legacy", mime="image/png", images=images)
    generate.assert_not_awaited()
    with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_DECISION"):
        await Thinking(generate).review({}, image=b"legacy", mime="image/png")
    generate.assert_awaited_once()


@pytest.mark.parametrize("error,code", [(TimeoutError(), "THINKING_UNKNOWN"),
                                       (OSError(), "THINKING_UNKNOWN"),
                                       (StoreError("THINKING_UNKNOWN", "unknown"), "THINKING_UNKNOWN"),
                                       (RuntimeError(), "THINKING_FAILED"),
                                       (StoreError("INVALID_DECISION", "invalid"), "INVALID_DECISION"),
                                       (asyncio.CancelledError(), None)])
async def test_v2_errors_and_cancellation_do_not_retry(error, code):
    context, images = review_case()
    generate = AsyncMock(side_effect=error)
    if code is None:
        with pytest.raises(asyncio.CancelledError):
            await Thinking(generate).review(context, images=images)
    else:
        with pytest.raises(StoreError, check=lambda exc: exc.code == code):
            await Thinking(generate).review(context, images=images)
    generate.assert_awaited_once()
