"""Stage 2 simulations over real plugin hooks, policy, SQLite and Artist flows.

The fixed public Host Runner/Dispatcher and SDK are reused from thought_host.
All model choices, image bytes and send receipts are synthetic; schedulers stay
gated throughout. No running Host, MCP or QQ is used.
"""

from copy import deepcopy
import json

import pytest

from image_plugin_under_test.domain.policy import Limits, Scope
from image_plugin_under_test.creation.thinking import Thinking
from protocol_fixture import PNG
from test_artist_flow import BRIEF, Model, SCOPE, step, studio as studio
from test_group_thought_host import (
    OTHER_STREAM, after, before, counts, host_chain as host_chain, output,
    public_host as public_host, receipt, thought_host as thought_host,
)
from test_host_integration import Harness, OWNER, PLATFORM, STREAM


HOST_SCOPE = Scope(PLATFORM, STREAM, OWNER)
OTHER_SCOPE = Scope(PLATFORM, OTHER_STREAM, OWNER)
NOTE = {"note_id": "gh_" + "1" * 64, "summary": "雨夜中亮着暖灯的便利店"}


class AdoptionModel(Model):
    """Script the choices, using only inspiration keys the Artist actually offers."""

    def __init__(self):
        super().__init__()
        self.wait = False
        self.force_create = False
        self.plans = []

    def plan(self, context):
        self.plans.append(deepcopy(context))
        notes = [idea for idea in context["inspirations"] if idea["origin"] == "group_thought"]
        active = next((work for work in context["works"]
                       if work["status"] not in {"finished", "abandoned"}), None)
        finished = [work for work in context["works"] if work["status"] == "finished"]
        if self.wait:
            decision = {"action": "wait", "reason": "先保留构思，暂时不画", "delay_seconds": 300}
        elif active and not self.force_create:
            decision = {"action": "continue", "work_id": active["work_id"], "reason": "继续原计划"}
        elif self.share and finished:
            decision = {"action": "share", "work_id": finished[-1]["work_id"], "reason": "愿意分享这幅画"}
        elif notes:
            chosen = notes[0]
            brief = {**BRIEF, "subject": chosen["summary"], "prompt": "用水彩描绘：" + chosen["summary"]}
            decision = {"action": "create", "reason": "这条构思适合画成水彩", "brief": brief,
                        "inspiration_keys": [chosen["key"]]}
        else:
            decision = {"action": "wait", "reason": "没有想采用的新构思"}
        return {"ideas": [], "decision": decision}

    async def __call__(self, request):
        if request["task"] == "plan":
            self.calls.append(deepcopy(request))
            return self.plan(request["context"])
        return await super().__call__(request)


@pytest.fixture
async def adoption_host(thought_host):
    h = thought_host
    h.adoption_model = AdoptionModel()

    async def capability(plugin_id, name, args):
        if name == "llm.generate":
            messages = args["prompt"]
            content = messages[1]["content"]
            if len(content) == 1 and "不得规划或覆盖" not in messages[0]["content"]:
                assert args["task_name"] == "utils"
                h.calls.append((name, deepcopy(args)))
                answer = h.adoption_model.plan(json.loads(content[0]["text"]))
                return {"success": True, "response": json.dumps(answer, ensure_ascii=False),
                        "model": "synthetic-group-adoption"}
        # Retain the existing synthetic multimodal review and raw Host receipt
        # handlers; they assert actual PNG bytes, routing and return_details.
        return await Harness.capability(h, plugin_id, name, args)

    for name in ("llm.generate", "send.image"):
        h.capabilities.register_capability(name, capability)
    await h.load(group=True)
    h.plugin.group_thought_runtime.clock = lambda: h.clock().timestamp()
    assert not h.gate.is_set()
    return h


def links(store, space="studio"):
    with store.lock:
        return [dict(row) for row in store.db.execute(
            "SELECT * FROM group_thought_links WHERE space_id=? ORDER BY note_id", (space,))]


def group_ideas(store, space="studio", scope=HOST_SCOPE):
    return [idea for idea in store.list_inspirations(space, scope) if idea["origin"] == "group_thought"]


async def remember(h, *, session=STREAM, tags=None):
    token, _ = await before(h, session)
    assert token
    items = output(h, token, tags)
    await after(h, items, session)
    result = await receipt(h, token, tags, session=session)
    assert result["success"] and result["structured_content"] == {"recorded": True}
    return items


async def sync(h, space="studio"):
    return await h.plugin.group_thought_runtime.sync(space)


async def advance(h, *, space="studio", seconds=300):
    h.clock.advance(seconds)
    await sync(h, space)
    return await h.runtime.artists[space].tick()


async def reload_host(h):
    assert not h.gate.is_set()  # No worker may still own a model/send operation.
    await h.plugin.on_unload()
    await h.load(group=True)
    h.plugin.group_thought_runtime.clock = lambda: h.clock().timestamp()


@pytest.mark.parametrize("wake", ["chat", "idle"])
async def test_public_hooks_adopt_review_edit_original_and_share_once(adoption_host, wake):
    h = adoption_host
    limits = Limits(think=30, render=2, share=1, max_inflight=10, max_revisions=1)
    h.runtime.store.set_global_limits(limits)
    h.runtime.store.set_limits("studio", HOST_SCOPE, limits)
    # Other members are real ingress participants, never substituted for OWNER.
    await h.message("雨夜适合躲雨", owner="member-one", message_id="member-one-message")
    await h.message("便利店的暖灯很好看", owner="member-two", message_id="member-two-message")
    assert len(h.received) == 2
    assert not h.runtime.store.db.execute("SELECT 1 FROM artist_wakes WHERE kind='chat'").fetchone()
    await remember(h)
    assert counts(h) == (1, 1)
    assert h.runtime.signals["studio"].is_set()
    assert not h.adoption_model.plans and not h.renderer.calls and not h.sends
    assert not h.runtime.store.audit_operations("studio", HOST_SCOPE)
    assert await sync(h) is True
    idea, = group_ideas(h.runtime.store)
    note_id = idea["key"].removeprefix("group-thought:")
    assert note_id.startswith("gh_") and len(note_id) == 67
    assert idea["source_refs"] == [note_id]
    assert links(h.runtime.store)[0]["adopted_by"] is None
    assert not h.adoption_model.plans and not h.renderer.calls and not h.sends
    if wake == "chat":
        await h.message("你今天想画什么？", message_id="authorized-owner-message")
        planned = await h.runtime.artists["studio"].run_once()
    else:
        planned = await h.runtime.artists["studio"].tick()
    assert planned["state"] == "planned"
    work = h.runtime.store.get_artist_work("studio", HOST_SCOPE, planned["work_id"])
    assert work["inspiration_keys"] == [idea["key"]]
    assert work["brief"]["subject"] == idea["summary"]
    assert links(h.runtime.store)[0]["adopted_by"] == work["work_id"]
    assert group_ideas(h.runtime.store) == []
    context = h.adoption_model.plans[0]
    assert any(item["key"] == idea["key"] for item in context["inspirations"])
    chat_ids = [event["source_ref"] for event in context["events"] if event["kind"] == "chat"]
    assert chat_ids == (["authorized-owner-message"] if wake == "chat" else [])
    assert not h.renderer.calls and not h.sends
    assert (await advance(h))["state"] == "review_pending"
    assert (await advance(h))["reason"] == "revision"
    assert (await advance(h))["state"] == "review_pending"
    assert (await advance(h))["state"] == "finished"
    versions = h.runtime.store.list_versions("studio", HOST_SCOPE, work["work_id"])
    assert len(versions) == len(h.renderer.calls) == len(h.reviews) == 2
    original, edited = versions
    assert edited["parent_id"] == original["version_id"]
    assert h.renderer.calls[0]["source"] is None
    assert h.renderer.calls[1]["source"] == {
        "job_id": original["job_id"], "sha256": original["sha256"], "data": PNG, "mime": "image/png"}
    assert all(call["scope"] == HOST_SCOPE for call in h.renderer.calls)
    assert not h.sends  # Acceptance alone does not authorize a share decision.
    h.adoption_model.share = True
    assert (await advance(h))["state"] == "shared"
    assert (await advance(h))["state"] == "share_recorded"
    assert len(h.sends) == 1 and h.sends[0]["stream_id"] == STREAM
    usage = h.runtime.store.budget_usage("studio", HOST_SCOPE)
    assert usage["render"] == {"held": 0, "consumed": 2, "total": 2}
    assert usage["share"] == {"held": 0, "consumed": 1, "total": 1}


@pytest.mark.parametrize("wake", ["chat", "idle"])
async def test_model_can_wait_then_adopt_same_note_on_either_wake(adoption_host, wake):
    h = adoption_host
    await remember(h)
    await sync(h)
    idea, = group_ideas(h.runtime.store)
    h.adoption_model.wait = True
    if wake == "chat":
        await h.message("聊聊今晚的雨", message_id="owner-wait")
        result = await h.runtime.artists["studio"].run_once()
    else:
        result = await h.runtime.artists["studio"].tick()
    assert result["state"] == "waiting"
    assert not h.runtime.store.list_artist_works("studio", HOST_SCOPE)
    assert links(h.runtime.store)[0]["adopted_by"] is None
    assert group_ideas(h.runtime.store)[0]["key"] == idea["key"]
    assert not h.renderer.calls and not h.sends and not h.reviews
    h.adoption_model.wait = False
    assert (await advance(h))["state"] == "planned"
    work, = h.runtime.store.list_artist_works("studio", HOST_SCOPE)
    assert work["inspiration_keys"] == [idea["key"]]
    assert all(any(note["key"] == idea["key"] for note in context["inspirations"])
               for context in h.adoption_model.plans)


async def test_same_topic_in_two_groups_never_crosses_adoption_scope(adoption_host):
    h = adoption_host
    first = await remember(h)
    await after(h, first)  # Same Host response replay cannot add another source.
    await remember(h, session=OTHER_STREAM)
    assert counts(h) == (2, 2)
    await sync(h)
    await sync(h, "other")
    a, = group_ideas(h.runtime.store)
    b, = group_ideas(h.runtime.store, "other", OTHER_SCOPE)
    assert a["summary"] == b["summary"] and a["key"] != b["key"]
    assert (await h.runtime.artists["studio"].run_once())["state"] == "planned"
    assert (await h.runtime.artists["other"].run_once())["state"] == "planned"
    for space, scope, own, foreign in (("studio", HOST_SCOPE, a, b), ("other", OTHER_SCOPE, b, a)):
        work, = h.runtime.store.list_artist_works(space, scope)
        assert work["inspiration_keys"] == [own["key"]]
        context = next(context for context in h.adoption_model.plans
                       if any(idea["key"] == own["key"] for idea in context["inspirations"]))
        assert foreign["key"] not in {idea["key"] for idea in context["inspirations"]}
        assert links(h.runtime.store, space)[0]["adopted_by"] == work["work_id"]
    assert not h.renderer.calls and not h.sends


@pytest.mark.parametrize("invalid_replan", [False, True])
async def test_later_note_cannot_overwrite_existing_plan(adoption_host, invalid_replan):
    h = adoption_host
    await remember(h)
    await sync(h)
    assert (await h.runtime.artists["studio"].run_once())["state"] == "planned"
    original, = h.runtime.store.list_artist_works("studio", HOST_SCOPE)
    h.clock.advance(601)
    await remember(h, tags=["forest", "mist"])
    await sync(h)
    pending, = group_ideas(h.runtime.store)
    h.adoption_model.force_create = invalid_replan
    result = await h.runtime.artists["studio"].run_once()
    assert result["state"] == ("blocked" if invalid_replan else "review_pending")
    current, = h.runtime.store.list_artist_works("studio", HOST_SCOPE)
    assert current["work_id"] == original["work_id"]
    assert current["brief"] == original["brief"] and current["inspiration_keys"] == original["inspiration_keys"]
    assert next(row for row in links(h.runtime.store) if row["inspiration_key"] == pending["key"])["adopted_by"] is None
    assert len(h.renderer.calls) == (0 if invalid_replan else 1) and not h.sends


@pytest.fixture
def adoption_studio(studio):
    artist, store, _, _, _, _ = studio
    model = AdoptionModel()
    artist.thinking = Thinking(model)
    version = store.get_space("s", SCOPE)["auth_version"]
    assert store.sync_group_thoughts("s", SCOPE, group_id="synthetic-group", thoughts=[NOTE],
                                     enabled=True, expected_auth_version=version) is True
    assert store.sync_group_thoughts("s", SCOPE, group_id="synthetic-group", thoughts=[NOTE],
                                     enabled=True, expected_auth_version=version) is False
    store.configure_share_policy("s", SCOPE, group_id="synthetic-group", mode="original_group",
                                 expected_auth_version=version)
    return (*studio[:3], model, *studio[4:])


@pytest.mark.parametrize("category", ["think", "render", "share"])
async def test_imported_note_never_bypasses_exhausted_budget(adoption_studio, category):
    artist, store, _, model, renderer, sender = adoption_studio
    if category != "think":
        assert (await artist.run_once())["state"] == "planned"
    if category == "share":
        assert (await step(adoption_studio))["state"] == "review_pending"
        assert (await step(adoption_studio))["state"] == "finished"
        model.share = True
    limits = {"think": 100, "render": 20, "share": 20, "max_inflight": 20, "max_revisions": 1}
    limits[category] = 0
    store.set_limits("s", SCOPE, Limits(**limits))
    before_effects = len(renderer.calls), len(sender.calls)
    result = await step(adoption_studio)
    assert result == {"state": "blocked", "reason": "BUDGET_EXCEEDED"}
    assert (len(renderer.calls), len(sender.calls)) == before_effects
    if category == "think":
        assert not model.calls and links(store, "s")[0]["adopted_by"] is None
    else:
        work, = store.list_artist_works("s", SCOPE)
        assert links(store, "s")[0]["adopted_by"] == work["work_id"]


async def test_group_work_revision_limit_cannot_create_extra_generation(adoption_studio):
    artist, store, _, model, renderer, sender = adoption_studio
    store.set_limits("s", SCOPE, Limits(think=100, render=20, share=1, max_inflight=20, max_revisions=1))
    model.revise = 10  # Model persistently asks for more; policy allows one edit.
    assert (await artist.run_once())["state"] == "planned"
    results = [await step(adoption_studio) for _ in range(6)]
    assert any(result == {"state": "finished", "reason": "REVISION_LIMIT"} for result in results)
    assert len(renderer.calls) == 2 and renderer.calls[1]["source"]["data"] == PNG
    assert len(store.list_artist_works("s", SCOPE)) == 1 and not sender.calls


async def test_host_send_unknown_preserves_receipt_and_is_not_replayed_on_reload(adoption_host):
    h = adoption_host
    h.revise = False
    await remember(h)
    await sync(h)
    assert (await h.runtime.artists["studio"].run_once())["state"] == "planned"
    assert (await advance(h))["state"] == "review_pending"
    assert (await advance(h))["state"] == "finished"
    h.adoption_model.share = True
    h.lose_send = True
    assert (await advance(h))["state"] == "share_unknown"
    delivery = tuple(h.plugin.journal.db.execute("SELECT * FROM delivery").fetchone())
    assert h.plugin.journal.db.execute("SELECT state FROM delivery").fetchone()[0] == "send_unknown"
    source_links = links(h.runtime.store)
    usage = h.runtime.store.budget_usage("studio", HOST_SCOPE)
    assert usage["share"] == {"held": 1, "consumed": 0, "total": 1}
    await reload_host(h)
    assert await sync(h) is False
    assert h.runtime.store.budget_usage("studio", HOST_SCOPE) == usage
    for _ in range(2):
        assert (await advance(h)) == {"state": "share_recorded", "outcome": "unknown"}
    assert len(h.sends) == 1 and len(h.renderer.calls) == 1
    assert tuple(h.plugin.journal.db.execute("SELECT * FROM delivery").fetchone()) == delivery
    assert links(h.runtime.store) == source_links and group_ideas(h.runtime.store) == []
