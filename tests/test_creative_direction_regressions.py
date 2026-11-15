"""长方向在 Host 规划、原图评审及纯推进批次中的回归。"""

from base64 import b64decode
from hashlib import sha256
from types import SimpleNamespace
import json

import pytest

from image_plugin_under_test.creation.artist import Artist
from image_plugin_under_test.creation.thinking import Thinking
from image_plugin_under_test.domain.policy import StoreError, canonical_json
from image_plugin_under_test.host.host_adapters import HostThinking
from protocol_fixture import PNG
from support.artist_fakes import BRIEF, SCOPE
from support.artist_flow import finished, step, studio as studio
from support.catalog_examples import CHOICE, PROFILES, SERIES


DIRECTION = "雨夜暖光" * 500
DIRECTION_SUFFIX = "\n\n本作品固定创作方向：\n" + DIRECTION
HOST_LIMIT = 512 * 1024


def _with_direction(brief):
    return dict(brief, prompt=brief["prompt"] + DIRECTION_SUFFIX,
                completion_criteria=brief["completion_criteria"] + DIRECTION_SUFFIX)


def _assert_history_projection(context, history):
    visible = [work for work in context["works"] if work["status"] in {"finished", "abandoned"}]
    for summary, original in zip(visible, history[-20:], strict=True):
        assert {key: summary[key] for key in ("work_id", "status", "best_version")} == {
            key: original[key] for key in ("work_id", "status", "best_version")
        }
        assert set(summary["brief"]) == {"intent", "subject", "mood"}
        for key, value in summary["brief"].items():
            assert 0 < len(value) <= 200 and original["brief"][key].startswith(value)
        assert 0 < len(summary["reason"]) <= 200 and original["reason"].startswith(summary["reason"])


async def test_long_history_fits_host_plan_and_original_image_review(studio):
    seed_runtime, store, clock, _, renderer, sender = studio
    seed_runtime.idle_seconds = 3600
    proposal = dict(BRIEF, intent="记录" * 130, subject="雨窗" * 130, mood="静谧" * 130,
                    prompt="水彩" * 600, completion_criteria="自然" * 600)
    full_brief = _with_direction(proposal)

    def add_abandoned(index):
        work_id = f"old-{index}"
        store.plan_work("s", SCOPE, work_id=work_id, brief=full_brief, inspiration_keys=[])
        # 历史弃稿只需要终态；留一件真正走完合成绘图和评审的作品来校验 best_version 与冻结记录。
        store.update_artist_work("s", SCOPE, work_id, changes={"status": "abandoned", "reason": "弃稿理由" * 65})

    add_abandoned(0)
    store.plan_work("s", SCOPE, work_id="old-reviewed", brief=full_brief, inspiration_keys=[])
    assert (await seed_runtime.tick())["state"] == "review_pending"
    assert (await step(studio))["state"] == "finished"
    seed_version = store.get_artist_work("s", SCOPE, "old-reviewed")["best_version"]
    seed_snapshot = store.get_review_snapshot("s", SCOPE, seed_version)
    assert seed_snapshot["context"]["current_work"]["brief"] == full_brief
    store.update_artist_work("s", SCOPE, "old-reviewed", changes={"reason": "完成理由" * 65})
    for index in range(1, 20):
        add_abandoned(index)
    history = store.list_artist_works("s", SCOPE)
    # Host 使用 ASCII JSON；旧版重复转义中文长 brief，仅最近二十份就已超过边界。
    assert len(canonical_json([work["brief"] for work in history[-20:]]).encode("utf-8")) > HOST_LIMIT

    host_calls = []

    async def host_receipt(method, *, payload, timeout_ms):
        host_calls.append((method, payload))
        args = payload["args"]
        context = json.loads(args["prompt"][1]["content"][0]["text"])
        if args["task_name"] == "vlm":
            answer = {
                "decision": "accept", "reason": "合成原图符合标准", "strengths": [], "issues": [],
                "comparison": {"improvements": [], "regressions": [], "preserved": [],
                               "evidence_version_ids": [context["current_version"]["version_id"]]},
            }
        else:
            active = next((work for work in context["works"] if work["status"] == "planned"), None)
            decision = {"action": "continue", "work_id": active["work_id"], "reason": "继续原作"} if active else {
                "action": "create", "reason": "试画新主题", "brief": proposal, "inspiration_keys": [],
            }
            answer = {"ideas": [], "decision": decision}
        return {"success": True, "response": json.dumps(answer)}

    thinking = Thinking(HostThinking(SimpleNamespace(call_host_method=host_receipt), plan_task="utils", review_task="vlm"))
    artist = Artist(store, "s", SCOPE, thinking, renderer, sender, creative_direction=DIRECTION, idle_seconds=3600)
    assert await artist.on_chat(message_id="new-theme", text="试画一张新的雨窗水彩")
    planned = await artist.run_once()
    assert planned["state"] == "planned"
    work_id = planned["work_id"]
    assert store.get_artist_work("s", SCOPE, work_id)["brief"] == full_brief

    # 更换公共方向后，续作上下文和原图评审仍保留计划时的完整 brief。
    replacement = "晴日远景" * 500
    artist = Artist(store, "s", SCOPE, thinking, renderer, sender, creative_direction=replacement, idle_seconds=3600)
    clock.advance()
    assert (await artist.tick())["state"] == "review_pending"
    clock.advance()
    reviewed = await artist.tick()
    assert reviewed["state"] == "finished"

    assert [payload["args"]["task_name"] for _, payload in host_calls] == ["utils", "utils", "vlm"]
    contexts = []
    for method, payload in host_calls:
        assert (method, payload["capability"]) == ("cap.call", "llm.generate")
        assert len(canonical_json(payload).encode("utf-8")) < HOST_LIMIT
        content = payload["args"]["prompt"][1]["content"]
        context = json.loads(content[0]["text"])
        _assert_history_projection(context, history)
        contexts.append(context)
    new_plan, continuation, review = contexts
    assert new_plan["creative_direction"] == DIRECTION
    assert continuation["creative_direction"] == replacement
    for context in (continuation, review):
        assert next(work["brief"] for work in context["works"] if work["work_id"] == work_id) == full_brief
    assert "creative_direction" not in review
    assert review["current_work"]["brief"] == full_brief
    assert renderer.calls[-1]["prompt"] == full_brief["prompt"]

    _, label, image = host_calls[-1][1]["args"]["prompt"][1]["content"]
    version_id = reviewed["best_version"]
    image_label = json.loads(label["text"])
    assert image_label["version_id"] == version_id and "current" in image_label["roles"]
    header, encoded = image["image_url"]["url"].split(",", 1)
    assert header == "data:image/png;base64" and b64decode(encoded, validate=True) == PNG
    snapshot = store.get_review_snapshot("s", SCOPE, version_id)
    assert snapshot["context"] == review
    assert snapshot["input_digest"] == sha256(canonical_json(review).encode("utf-8")).hexdigest()
    assert store.get_review_snapshot("s", SCOPE, seed_version) == seed_snapshot
    assert store.list_artist_works("s", SCOPE)[:-1] == history
    assert store.get_artist_work("s", SCOPE, work_id)["brief"] == full_brief
    assert len(renderer.calls) == 2 and not sender.calls


@pytest.mark.parametrize(
    ("profile_overlay", "with_chat"),
    [
        pytest.param(True, False, id="profile-overflow"),
        pytest.param(False, False, id="direction-overflow"),
        pytest.param(False, True, id="direction-overflow-with-chat"),
    ],
)
async def test_create_suggestion_obeys_wake_batch_boundary(studio, profile_overlay, with_chat):
    artist, store, clock, _, renderer, sender = studio
    artist.idle_seconds = 3600
    artist.creative_direction = DIRECTION
    await finished(studio)
    works = store.list_artist_works("s", SCOPE)
    operations = store.audit_operations("s", SCOPE)
    usage = store.budget_usage("s", SCOPE)["think"]

    if profile_overlay:
        # 此用例加完方向刚好合法，超限来自落库前叠加的创作档案。
        auth = store.get_space("s", SCOPE)["auth_version"]
        store.configure_creative("s", SCOPE, profiles=PROFILES, series=SERIES, expected_auth_version=auth)
        brief = dict(BRIEF, prompt="画" * (4000 - len(DIRECTION_SUFFIX)))
        frozen_prompt = _with_direction(brief)["prompt"]
        assert len(frozen_prompt) == 4000
        with pytest.raises(StoreError) as error:
            store.preview_plan_prompt("s", SCOPE, prompt=frozen_prompt, creative=CHOICE, expected_auth_version=auth)
        assert error.value.code == "PROMPT_TOO_LONG"
    else:
        # 没有档案：模型正文恰好满足原有上限，只有追加方向才会超限。
        brief = dict(BRIEF, prompt="画" * 4000)
        assert len(_with_direction(brief)["prompt"]) > len(brief["prompt"]) == 4000

    calls = []

    async def suggest_create(request):
        calls.append(request)
        decision = {"action": "create", "reason": "建议画下一张", "brief": brief, "inspiration_keys": []}
        if profile_overlay:
            decision["creative"] = CHOICE
        return {"ideas": [], "decision": decision}

    artist.thinking = Thinking(suggest_create)
    clock.advance(15)
    if with_chat:
        # 先让调度器入队推进事件，再入队聊天，保证同一批次确实同时携带两者。
        assert store.schedule_progress_wake("s", SCOPE)
        assert await artist.on_chat(message_id="next-theme", text="再画一张雨窗水彩")
    result = await artist.tick()
    (call,) = calls
    assert call["task"] == "plan"
    events = call["context"]["events"]
    assert [event["kind"] for event in events] == (["completion", "chat"] if with_chat else ["completion"])
    event = events[0]
    assert event["kind"] == "completion" and event["event_id"].startswith("progress:post-review:")
    (operation,) = store.audit_operations("s", SCOPE)[len(operations):]

    if with_chat:
        assert result == {"state": "blocked", "reason": "THINK_FAILED"}
        assert (operation["state"], operation["evidence"]) == ("failed", "THINK_FAILED")
    else:
        assert result == {"state": "waiting", "reason": "PROGRESS_EXISTING_WORK_ONLY"}
        assert operation["state"] == "succeeded"
        decision = json.loads(operation["evidence"])["decision"]
        assert decision["action"] == "create"
        if not profile_overlay:
            assert decision["brief"]["prompt"] == brief["prompt"]

        # 越过失败批次的 300 秒退避，同时仍未到下一轮 idle，确认纯推进没有再消费。
        assert await step(studio, 300) == {"state": "idle"}
        assert calls == [call]
        assert store.audit_operations("s", SCOPE) == [*operations, operation]
    assert store.budget_usage("s", SCOPE)["think"] == dict(
        usage, consumed=usage["consumed"] + 1, total=usage["total"] + 1,
    )
    assert store.list_artist_works("s", SCOPE) == works
    assert len(renderer.calls) == 1 and not sender.calls
