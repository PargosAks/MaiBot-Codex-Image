"""Offline schema tests; synthetic bytes are never decoded or visually assessed."""

from copy import deepcopy
from unittest.mock import AsyncMock
import asyncio

import pytest

from image_plugin_under_test.domain.policy import StoreError
from image_plugin_under_test.creation.thinking import Thinking
from support.thinking_examples import BRIEF, CONTEXT, ACCEPT


CREATE = {"ideas": [{"summary": "花与光", "source_ids": ["chat1"]}],
              "decision": {"action": "create", "reason": "想练习", "brief": BRIEF, "inspiration_keys": ["flower"]}}
async def test_plan_and_idle_ideas():
    generate = AsyncMock(return_value=deepcopy(CREATE))
    assert await Thinking(generate).plan(dict(CONTEXT, works=[])) == CREATE
    idle = {"ideas": [{"summary": "自主主题", "source_ids": []}], "decision": {"action": "wait", "reason": "稍后画"}}
    generate.return_value = idle
    assert await Thinking(generate).plan({}) == idle
    assert generate.call_args.args[0]["task"] == "plan"
    assert "不编造经历" in generate.call_args.args[0]["instructions"]


async def test_create_freezes_direction_in_brief_without_mutating_input_or_model_reply():
    direction = "麦麦专心烤点心，优先刻画手部动作和期待的表情。"
    context = dict(deepcopy(CONTEXT), works=[], creative_direction=direction)
    answer = deepcopy(CREATE)
    original_context, original_answer = deepcopy((context, answer))

    async def generate(request):
        assert request["context"]["creative_direction"] == direction
        assert "角色和具体动作" in request["instructions"]
        request["context"]["creative_direction"] = "模型不可改写方向"
        return answer

    result = await Thinking(generate).plan(context)
    suffix = "\n\n本作品固定创作方向：\n" + direction
    brief = result["decision"]["brief"]
    assert brief["prompt"] == BRIEF["prompt"] + suffix
    assert brief["completion_criteria"] == BRIEF["completion_criteria"] + suffix
    assert (context, answer) == (original_context, original_answer)


@pytest.mark.parametrize("total_length", [4000, 4001])
async def test_create_prompt_limit_includes_full_frozen_direction(total_length):
    direction = "画" * 2000
    suffix = "\n\n本作品固定创作方向：\n" + direction
    answer = deepcopy(CREATE)
    body = "景" * (total_length - len(suffix))
    answer["decision"]["brief"]["prompt"] = body
    context = dict(CONTEXT, works=[], creative_direction=direction)
    generate = AsyncMock(return_value=answer)
    if total_length == 4000:
        result = await Thinking(generate).plan(context)
        assert result["decision"]["brief"]["prompt"] == body + suffix
    else:
        with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_DECISION"):
            await Thinking(generate).plan(context)
    generate.assert_awaited_once()


async def test_inspire_only_accumulates_ideas_and_copies_context():
    context = deepcopy(CONTEXT)
    context["creative_direction"] = "优先构思麦麦在做什么"
    context["works"][0]["status"] = "creating"
    before = deepcopy(context)
    answer = {"ideas": deepcopy(CREATE["ideas"])}

    async def generate(request):
        assert request["task"] == "inspire" and "不得规划" in request["instructions"]
        assert "构思与灵感应落实其中的优先级" in request["instructions"]
        assert request["context"]["creative_direction"] == context["creative_direction"]
        request["context"]["works"][0]["brief"]["intent"] = "changed"
        return answer

    assert await Thinking(generate).inspire(context) == answer
    assert context == before


@pytest.mark.parametrize("answer", [CREATE, {"ideas": [{"summary": "x", "source_ids": ["idle1"]}]},
                                   {"ideas": [], "owner": "forged"}, {"ideas": "bad"}])
async def test_invalid_inspire(answer):
    generate = AsyncMock(return_value=answer)
    with pytest.raises(StoreError):
        await Thinking(generate).inspire(CONTEXT)
    generate.assert_awaited_once()


@pytest.mark.parametrize("decision", [
    {"action": "wait", "reason": "休息", "delay_seconds": 60},
    {"action": "share", "reason": "分享", "work_id": "work1"},
    {"action": "defer", "reason": "稍后", "work_id": "work1", "delay_seconds": 86400},
    {"action": "abandon", "reason": "不再继续", "work_id": "work1"},
])
async def test_other_plan_actions(decision):
    answer = {"ideas": [], "decision": decision}
    context = deepcopy(CONTEXT)
    if decision["action"] == "share":
        context["works"][0].update(status="finished", best_version="v1")
    assert await Thinking(AsyncMock(return_value=answer)).plan(context) == answer


@pytest.mark.parametrize("status", [None, "planned", "deferred", "creating", "review_pending", "finished", "abandoned"])
async def test_create_requires_no_active_work(status):
    context = deepcopy(CONTEXT)
    context["works"] = [] if status is None else [dict(context["works"][0], status=status)]
    generate = AsyncMock(return_value=deepcopy(CREATE))
    if status in {"planned", "deferred", "creating", "review_pending"}:
        with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_DECISION"):
            await Thinking(generate).plan(context)
    else:
        assert await Thinking(generate).plan(context) == CREATE
    generate.assert_awaited_once()


@pytest.mark.parametrize("action,status,valid", [
    ("defer", "planned", True), ("defer", "deferred", True), ("defer", "creating", False),
    ("defer", "review_pending", False), ("defer", "finished", False), ("defer", "abandoned", False),
    ("share", "planned", False), ("share", "deferred", False), ("share", "creating", False),
    ("share", "review_pending", False), ("share", "finished", True), ("share", "abandoned", False),
    ("abandon", "planned", True), ("abandon", "deferred", True), ("abandon", "creating", True),
    ("abandon", "review_pending", True), ("abandon", "finished", False), ("abandon", "abandoned", False),
])
async def test_existing_work_action_states(action, status, valid):
    context = deepcopy(CONTEXT)
    context["works"][0].update(status=status, best_version="v1")
    decision = {"action": action, "reason": "根据当前作品状态决定", "work_id": "work1"}
    if action == "defer":
        decision["delay_seconds"] = 60
    answer = {"ideas": [], "decision": decision}
    generate = AsyncMock(return_value=answer)
    if valid:
        assert await Thinking(generate).plan(context) == answer
    else:
        with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_DECISION"):
            await Thinking(generate).plan(context)
    generate.assert_awaited_once()


@pytest.mark.parametrize("best_version", [None, "", " "])
async def test_share_requires_nonempty_best_version(best_version):
    context = deepcopy(CONTEXT)
    context["works"][0].update(status="finished", best_version=best_version)
    answer = {"ideas": [], "decision": {"action": "share", "reason": "分享", "work_id": "work1"}}
    with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_DECISION"):
        await Thinking(AsyncMock(return_value=answer)).plan(context)


async def test_create_checks_every_work_not_only_finished_candidate():
    context = deepcopy(CONTEXT)
    context["works"].insert(0, dict(context["works"][0], work_id="finished", status="finished", best_version="v1"))
    with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_DECISION"):
        await Thinking(AsyncMock(return_value=deepcopy(CREATE))).plan(context)


@pytest.mark.parametrize("status", ["planned", "deferred"])
async def test_continue_existing_plan_requires_model_decision(status):
    context = deepcopy(CONTEXT)
    context["works"][0]["status"] = status
    answer = {"ideas": [], "decision": {"action": "continue", "reason": "现在继续", "work_id": "work1"}}
    generate = AsyncMock(return_value=answer)
    assert await Thinking(generate).plan(context) == answer
    generate.assert_awaited_once()
    assert "不因定时到达就必画" in generate.call_args.args[0]["instructions"]


@pytest.mark.parametrize("status", ["creating", "review_pending", "finished", "abandoned", None])
async def test_continue_rejects_non_plannable_work(status):
    context = deepcopy(CONTEXT)
    context["works"][0]["status"] = status
    answer = {"ideas": [], "decision": {"action": "continue", "reason": "继续", "work_id": "work1"}}
    with pytest.raises(StoreError):
        await Thinking(AsyncMock(return_value=answer)).plan(context)


@pytest.mark.parametrize("change", [{"work_id": "foreign"}, {"work_id": True}, {"reason": " "},
                                    {"delay_seconds": 60}, {"brief": BRIEF}, {"owner": "x"}, {"target": "x"}])
async def test_continue_rejects_foreign_work_and_extra_fields(change):
    decision = {"action": "continue", "reason": "继续", "work_id": "work1"}
    decision.update(change)
    with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_DECISION"):
        await Thinking(AsyncMock(return_value={"ideas": [], "decision": decision})).plan(CONTEXT)


@pytest.mark.parametrize("mutation", [
    lambda a: a.update(owner="forged"),
    lambda a: a.update(ideas=[a["ideas"][0]] * 9),
    lambda a: a["ideas"][0].update(summary=" " ),
    lambda a: a["ideas"][0].update(summary="x" * 1001),
    lambda a: a["ideas"][0].update(source_ids=["chat1"] * 21),
    lambda a: a["ideas"][0].update(source_ids=["idle1"]),
    lambda a: a["ideas"][0].update(source_ids=["foreign-chat"]),
    lambda a: a["decision"].update(target="foreign"),
    lambda a: a["decision"].update(inspiration_keys=["new-idea"]),
    lambda a: a["decision"]["brief"].update(prompt="x" * 4001),
    lambda a: a["decision"]["brief"].update(completion_criteria=[]),
    lambda a: a.update(decision={"action": "wait", "reason": "x", "delay_seconds": True}),
    lambda a: a.update(decision={"action": "wait", "reason": "x", "delay_seconds": 59}),
    lambda a: a.update(decision={"action": "wait", "reason": "x", "delay_seconds": 86401}),
    lambda a: a.update(decision={"action": "defer", "reason": "x", "work_id": "work1"}),
    lambda a: a.update(decision={"action": "share", "reason": "x", "work_id": "foreign"}),
    lambda a: a.update(decision=[]),
])
async def test_invalid_plan(mutation):
    answer = deepcopy(CREATE)
    mutation(answer)
    generate = AsyncMock(return_value=answer)
    with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_DECISION"):
        await Thinking(generate).plan(dict(CONTEXT, works=[]))
    generate.assert_awaited_once()


async def test_review_preserves_bytes_and_context_without_mutation():
    context = deepcopy(CONTEXT)
    context.update(current_work=deepcopy(context["works"][0]), current_version=deepcopy(context["versions"][0]))
    context["creative_direction"] = "新方向：麦麦在天台练舞"
    before, image = deepcopy(context), b"synthetic-original-bytes"

    async def generate(request):
        assert request["task"] == "review" and request["image"] is image and request["mime"] == "image/png"
        assert request["context"] == {key: value for key, value in before.items() if key != "creative_direction"}
        assert context["creative_direction"] not in request["instructions"]
        assert "人物是否是视觉中心" in request["instructions"]
        request["context"]["versions"].append({"version_id": "foreign"})
        request["context"]["current_work"]["brief"]["prompt"] = "changed"
        return dict(ACCEPT, best_version_id="foreign")

    with pytest.raises(StoreError):
        await Thinking(generate).review(context, image=image, mime="image/png")
    assert context == before
    assert await Thinking(AsyncMock(return_value=ACCEPT)).review(context, image=image, mime="image/png") == ACCEPT


@pytest.mark.parametrize("answer", [
    dict(ACCEPT, best_version_id="other-work-version"), dict(ACCEPT, edit_prompt="change"),
    dict(ACCEPT, decision="discard", preserve="keep"), dict(ACCEPT, decision="revise"),
    dict(ACCEPT, decision="revise", issues=[], edit_prompt="提亮", preserve="主体"),
    dict(ACCEPT, decision="revise", issues=["暗"], edit_prompt="x" * 3501, preserve="主体"),
    dict(ACCEPT, decision="revise", issues=["暗"], edit_prompt="提亮", preserve="x" * 401),
    dict(ACCEPT, strengths="好"), dict(ACCEPT, scope={}), None,
])
async def test_invalid_review(answer):
    with pytest.raises(StoreError):
        await Thinking(AsyncMock(return_value=answer)).review(CONTEXT, image=b"original", mime="image/png")


async def test_revision_and_discard():
    for answer in (dict(ACCEPT, decision="revise", issues=["偏暗"], edit_prompt="提亮", preserve="主体"),
                   dict(ACCEPT, decision="discard")):
        assert await Thinking(AsyncMock(return_value=answer)).review(CONTEXT, image=b"raw", mime="image/png") == answer


@pytest.mark.parametrize("image,mime", [(b"", "image/png"), ("description", "image/png"),
                                        (bytearray(b"raw"), "image/png"), (b"raw", "text/plain")])
async def test_review_requires_original_bytes_and_image_mime(image, mime):
    generate = AsyncMock()
    with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_ARGUMENT"):
        await Thinking(generate).review(CONTEXT, image=image, mime=mime)
    generate.assert_not_awaited()


@pytest.mark.parametrize("context", [{"scope": {}}, {"secret": "private"}, {"works": [{"owner": "x"}]},
                                      {"current_version": {"version_id": "v1", "token": "private"}},
                                      {"events": [{"event_id": None}]}, {"versions": "bad"},
                                      {"inspirations": [{"key": "x", "source_refs": "bad"}]},
                                      {"works": [{"work_id": "x", "brief": ({"secret": "x"},)}]}])
async def test_forbidden_context_never_reaches_callback(context):
    generate = AsyncMock()
    with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_CONTEXT"):
        await Thinking(generate).plan(context)
    generate.assert_not_awaited()


@pytest.mark.parametrize("task", ["plan", "inspire", "review"])
@pytest.mark.parametrize("exception", [TimeoutError("response timeout"), OSError("response lost"),
                                      ConnectionError("connection lost"),
                                      StoreError("THINKING_UNKNOWN", "remote outcome unknown")])
async def test_unknown_communication_outcome_never_retries(task, exception):
    generate = AsyncMock(side_effect=exception)
    operation = getattr(Thinking(generate), task)
    with pytest.raises(StoreError, check=lambda exc: exc.code == "THINKING_UNKNOWN") as caught:
        await operation({}, **({"image": b"raw", "mime": "image/png"} if task == "review" else {}))
    if isinstance(exception, StoreError):
        assert caught.value is exception
    else:
        assert caught.value.__cause__ is exception
    generate.assert_awaited_once()


@pytest.mark.parametrize("exception,code", [(ValueError("invalid model response"), "THINKING_FAILED"),
                                           (StoreError("THINKING_FAILED", "model rejected"), "THINKING_FAILED"),
                                           (StoreError("INVALID_DECISION", "invalid decision"), "INVALID_DECISION")])
async def test_explicit_model_errors_are_not_unknown(exception, code):
    generate = AsyncMock(side_effect=exception)
    with pytest.raises(StoreError, check=lambda exc: exc.code == code):
        await Thinking(generate).plan({})
    generate.assert_awaited_once()


@pytest.mark.parametrize("task", ["plan", "inspire", "review"])
async def test_failure_and_cancellation_never_retry(task):
    for exception in (RuntimeError("offline failure"), asyncio.CancelledError()):
        generate = AsyncMock(side_effect=exception)
        expected = StoreError if isinstance(exception, Exception) else asyncio.CancelledError
        with pytest.raises(expected):
            operation = getattr(Thinking(generate), task)
            await operation({}, **({"image": b"raw", "mime": "image/png"} if task == "review" else {}))
        generate.assert_awaited_once()
