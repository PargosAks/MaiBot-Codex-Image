"""End-to-end simulated artist cycles over real SQLite, with no Host/network."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import asyncio
import hashlib

import pytest

from image_plugin_under_test.artist import Artist, RenderResult, render_digest
from image_plugin_under_test.policy import Delegation, Limits, Scope, StoreError
from image_plugin_under_test.store import ArtistStore
from image_plugin_under_test.thinking import Thinking
from protocol_fixture import PNG


SCOPE = Scope("synthetic", "studio", "delegating-owner")
BRIEF = {
    "intent": "记录雨夜气氛",
    "subject": "窗边的小猫",
    "mood": "安静",
    "prompt": "画一只在雨夜窗边的小猫",
    "completion_criteria": "主体清楚且保留柔和灯光",
}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 31, 2, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds=300):
        self.now += timedelta(seconds=seconds)


class Model:
    def __init__(self):
        self.calls = []
        self.share = False
        self.revise = 0
        self.review_errors = 0
        self.defer = False
        self.keep_first = False
        self.plan_action = None
        self.hook = None

    async def __call__(self, request):
        self.calls.append(request)
        if self.hook:
            await self.hook(request)
        context = request["context"]
        ideas = [
            {"summary": e["text"], "source_ids": [e["event_id"]]}
            for e in context.get("events", [])
            if e["kind"] == "chat"
        ][:8]
        if request["task"] == "inspire":
            return {"ideas": ideas}
        if request["task"] == "review":
            if context.get("review_contract") == 2:
                assert all(item["data"] == PNG and item["mime"] == "image/png" for item in request["images"])
            else:
                assert request["image"] == PNG
                assert request["mime"] == "image/png"
            if self.review_errors:
                self.review_errors -= 1
                raise RuntimeError("synthetic model failure")
            result = {
                "decision": "accept",
                "reason": "已达到完成标准",
                "strengths": ["主体清楚"],
                "issues": [],
            }
            if self.revise:
                self.revise -= 1
                result.update(
                    decision="revise",
                    issues=["背景过亮"],
                    edit_prompt="降低背景亮度",
                    preserve="保留小猫姿态",
                )
            if self.keep_first:
                result["best_version_id"] = context["versions"][0]["version_id"]
            if context.get("review_contract") == 2:
                result["comparison"] = {
                    "improvements": [], "regressions": list(result["issues"]), "preserved": ["小猫姿态"],
                    "evidence_version_ids": [item["version_id"] for item in request["images"]],
                }
            return result
        works = context["works"]
        active = next((w for w in works if w["status"] not in {"finished", "abandoned"}), None)
        if active:
            decision = {"action": "continue", "work_id": active["work_id"], "reason": "现在适合继续"}
            if self.plan_action == "wait":
                decision = {"action": "wait", "reason": "先休息", "delay_seconds": 600}
            elif self.plan_action == "abandon":
                decision["action"] = "abandon"
            if self.defer:
                self.defer = False
                decision.update(action="defer", delay_seconds=600)
        elif not works:
            decision = {
                "action": "create",
                "reason": "想画雨夜",
                "brief": dict(BRIEF),
                "inspiration_keys": [],
            }
            if not ideas:
                ideas = [{"summary": "自主练习雨夜暖光", "source_ids": []}]
        elif self.share and any(w["status"] == "finished" for w in works):
            decision = {"action": "share", "reason": "想分享完成的画", "work_id": works[-1]["work_id"]}
        else:
            decision = {"action": "wait", "reason": "先收藏，暂不分享"}
        return {"ideas": ideas, "decision": decision}


class Renderer:
    simulation = True
    supports_edit = True

    def __init__(self):
        self.calls = []
        self.jobs = {}
        self.pending = False
        self.unknown_artifact = False
        self.lose_receipt = False
        self.lookup_missing = False
        self.corrupt = False
        self.before_return = None

    async def submit(self, *, scope, request_id, prompt, source):
        self.calls.append({"scope": scope, "request_id": request_id, "prompt": prompt, "source": source})
        params = {"prompt": prompt}
        if source:
            params.update(source_job_id=source["job_id"], source_sha256=source["sha256"])
        result = RenderResult(
            request_id,
            render_digest("edit" if source else "submit", params),
            "mock_job_" + str(len(self.calls)),
            "running" if self.pending else "completed",
            None if self.pending else PNG,
            sha256=hashlib.sha256(PNG).hexdigest(),
        )
        if self.unknown_artifact:
            result = replace(result, state="outcome_unknown", image=PNG)
        self.jobs[request_id] = result
        if self.before_return:
            await self.before_return()
        if self.lose_receipt:
            raise TimeoutError("response lost after acceptance")
        return self._response(result)

    def _response(self, result):
        return replace(result, sha256="0" * 64) if self.corrupt else result

    async def status(self, *, scope, job_id):
        assert scope == SCOPE
        return self._response(next(r for r in self.jobs.values() if r.job_id == job_id))

    async def lookup(self, *, scope, request_id):
        assert scope == SCOPE
        return None if self.lookup_missing else self._response(self.jobs.get(request_id))

    def complete(self):
        self.jobs = {
            k: replace(r, state="completed", image=PNG, sha256=hashlib.sha256(PNG).hexdigest())
            for k, r in self.jobs.items()
        }


class Sender:
    simulation = True

    def __init__(self):
        self.calls = []
        self.lose_receipt = False

    async def send(self, *, scope, version):
        self.calls.append((scope, version))
        if self.lose_receipt:
            raise ConnectionError("send may already have happened")
        return True


@pytest.fixture
def studio(tmp_path):
    clock, model, renderer, sender = Clock(), Model(), Renderer(), Sender()
    store = ArtistStore(tmp_path / "artist.sqlite3", clock=clock)
    store.create_space("s", SCOPE, binding_id="simulation-service")
    store.grant("s", SCOPE, Delegation("explicit-host-delegation", "admin", continuous=True))
    limits = Limits(think=100, render=20, share=20, max_inflight=20, max_revisions=2)
    store.set_global_limits(limits)
    store.set_limits("s", SCOPE, limits)
    runtime = Artist(store, "s", SCOPE, Thinking(model), renderer, sender)
    yield runtime, store, clock, model, renderer, sender
    store.close()


async def step(studio, seconds=300):
    runtime, _, clock, *_ = studio
    clock.advance(seconds)
    return await runtime.tick()


async def finished(studio):
    assert (await studio[0].tick())["state"] == "planned"
    assert (await step(studio))["state"] == "review_pending"
    assert (await step(studio))["state"] == "finished"


async def test_same_theme_from_chat_and_idle_keeps_both_origins(studio):
    runtime, store, _, _, renderer, _ = studio
    assert (await runtime.tick())["state"] == "planned"
    await runtime.on_chat(message_id="same-theme", text="自主练习雨夜暖光")
    assert (await runtime.run_once())["state"] == "review_pending"
    ideas = store.list_inspirations("s", SCOPE)
    assert len(ideas) == 2 and {i["origin"] for i in ideas} == {"chat", "idle"}
    assert next(i for i in ideas if i["origin"] == "chat")["source_refs"] == ["same-theme"]
    assert next(i for i in ideas if i["origin"] == "idle")["source_refs"] == []
    assert len(renderer.calls) == 1


async def test_consumed_inspiration_is_not_offered_to_next_plan(studio):
    runtime, store, _, _, renderer, _ = studio
    store.remember_inspiration("s", SCOPE, key="used", summary="雨夜", source_refs=[], origin="idle")
    store.plan_work("s", SCOPE, work_id="old", brief=BRIEF, inspiration_keys=["used"])
    store.update_artist_work("s", SCOPE, "old", changes={"status": "abandoned"})

    async def plan(request):
        assert request["context"]["inspirations"] == []
        return {
            "ideas": [],
            "decision": {
                "action": "create",
                "reason": "新的主题",
                "brief": dict(BRIEF),
                "inspiration_keys": [],
            },
        }

    runtime.thinking = Thinking(plan)
    assert (await runtime.tick())["state"] == "planned"
    assert store.list_inspirations("s", SCOPE)[0]["consumed_by"] == "old"
    assert renderer.calls == []


async def test_invalid_action_does_not_poison_cached_plan(studio):
    runtime, store, _, model, renderer, _ = studio
    assert (await runtime.tick())["state"] == "planned"

    async def invalid_plan(request):
        return {
            "ideas": [],
            "decision": {
                "action": "create",
                "reason": "不应覆盖已有计划",
                "brief": dict(BRIEF),
                "inspiration_keys": [],
            },
        }

    runtime.thinking = Thinking(invalid_plan)
    assert (await step(studio)) == {"state": "blocked", "reason": "THINK_FAILED"}
    assert len(store.list_artist_works("s", SCOPE)) == 1 and renderer.calls == []
    runtime.thinking = Thinking(model)
    assert (await step(studio))["state"] == "review_pending"
    assert len(renderer.calls) == 1


@pytest.mark.parametrize("failure", ["timeout", "connection"])
async def test_uncertain_thinking_keeps_hold_and_does_not_replay_after_reopen(studio, failure):
    runtime, store, clock, _, renderer, sender = studio
    calls = []

    async def unconfirmed(request):
        calls.append(request)
        if failure == "connection":
            raise ConnectionError("simulated lost response")
        await asyncio.Event().wait()

    runtime.thinking = Thinking(unconfirmed)
    runtime.timeout = 0.01
    assert (await runtime.tick())["reason"] == "THINK_UNKNOWN"
    assert store.audit_operations("s", SCOPE)[0]["state"] == "unknown"
    reopened = ArtistStore(store.path, clock=clock)
    try:
        next_runtime = Artist(reopened, "s", SCOPE, Thinking(unconfirmed), renderer, sender)
        clock.advance(86400)
        assert (await next_runtime.run_once())["reason"] == "THINK_UNKNOWN"
        assert len(calls) == 1
        assert len(reopened.audit_operations("s", SCOPE)) == 1
        assert reopened.budget_usage("s", SCOPE)["think"] == {"held": 1, "consumed": 0, "total": 1}
        assert renderer.calls == sender.calls == []
    finally:
        reopened.close()


@pytest.mark.parametrize("finished_unknown", [False, True])
async def test_readonly_reconciliation_precedes_exhausted_inspiration_budget(studio, finished_unknown):
    runtime, store, _, model, renderer, sender = studio
    if finished_unknown:
        renderer.unknown_artifact = True
        await finished(studio)
    else:
        renderer.pending = True
        assert (await runtime.tick())["state"] == "planned"
        assert (await step(studio))["state"] == "creating"
    request_id = next(iter(renderer.jobs))
    allowance = len(model.calls)
    store.set_limits(
        "s", SCOPE, Limits(think=allowance, render=20, share=20, max_inflight=20, max_revisions=2)
    )
    renderer.complete()
    await runtime.on_chat(message_id="budget-late-chat", text="等有空再画落叶")
    assert (await runtime.run_once())["reason"] == "BUDGET_EXCEEDED"
    assert store.get_operation("s", SCOPE, request_id)["state"] == "succeeded"
    work = store.list_artist_works("s", SCOPE)[0]
    assert work["status"] == ("finished" if finished_unknown else "review_pending")
    assert len(model.calls) == allowance
    store.set_limits("s", SCOPE, Limits(think=100, render=20, share=20, max_inflight=20, max_revisions=2))
    studio[2].advance(300)
    await runtime.run_once()
    assert any(i["source_refs"] == ["budget-late-chat"] for i in store.list_inspirations("s", SCOPE))
    assert len(renderer.calls) == 1 and sender.calls == []


@pytest.mark.parametrize("chat", [False, True])
async def test_chat_and_idle_share_one_full_flow_without_automatic_sharing(studio, chat):
    runtime, store, _, model, renderer, sender = studio
    if chat:
        assert await runtime.on_chat(message_id="real-message", text="雨夜的暖灯很好看")
        assert not await runtime.on_chat(message_id="real-message", text="雨夜的暖灯很好看")
    await finished(studio)
    ideas = store.list_inspirations("s", SCOPE)
    assert len(ideas) == 1 and ideas[0]["origin"] == ("chat" if chat else "idle")
    assert len(renderer.calls) == 1 and sender.calls == []
    assert (await step(studio))["state"] == "waiting"
    assert sender.calls == []
    assert next(c for c in model.calls if c["task"] == "review")["images"][0]["data"] == PNG
    model.share = True
    assert (await step(studio))["state"] == "shared"
    assert (await step(studio))["state"] == "share_recorded"
    assert len(sender.calls) == 1 and sender.calls[0][0] == SCOPE


async def test_new_chat_during_claim_is_not_lost_and_second_runtime_cannot_claim(studio):
    runtime, store, _, model, renderer, sender = studio
    started, release = asyncio.Event(), asyncio.Event()

    async def hook(request):
        if len(model.calls) == 1:
            started.set()
            await release.wait()

    model.hook = hook
    await runtime.on_chat(message_id="one", text="第一份灵感")
    task = asyncio.create_task(runtime.run_once())
    await started.wait()
    await runtime.on_chat(message_id="two", text="第二份灵感")
    second = Artist(store, "s", SCOPE, Thinking(model), renderer, sender)
    assert (await second.run_once())["state"] == "idle"
    release.set()
    assert (await task)["state"] == "planned"
    assert (await second.run_once())["state"] == "review_pending"
    assert {i["summary"] for i in store.list_inspirations("s", SCOPE)} == {"第一份灵感", "第二份灵感"}
    assert len(renderer.calls) == 1


async def test_running_work_collects_new_ideas_without_overwriting_plan(studio):
    runtime, store, _, model, renderer, _ = studio
    renderer.pending = True
    await runtime.tick()
    assert (await step(studio))["state"] == "creating"
    await runtime.on_chat(message_id="later", text="下一幅可以画落叶")
    await runtime.run_once()
    work = store.list_artist_works("s", SCOPE)[0]
    assert work["brief"] == BRIEF and len(renderer.calls) == 1
    assert any(c["task"] == "inspire" for c in model.calls)
    assert any(i["summary"] == "下一幅可以画落叶" for i in store.list_inspirations("s", SCOPE))


async def test_deferred_plan_waits_until_due_instead_of_timer_forcing_draw(studio):
    await studio[0].tick()
    studio[3].defer = True
    assert (await step(studio))["state"] == "deferred"
    assert (await step(studio))["state"] == "waiting"
    assert studio[4].calls == []
    assert (await step(studio))["state"] == "review_pending"


async def test_failed_review_keeps_image_and_recovers_after_persistent_backoff(studio):
    runtime, store, _, model, renderer, _ = studio
    await runtime.tick()
    await step(studio)
    model.review_errors = 1
    assert (await step(studio)) == {"state": "blocked", "reason": "THINK_FAILED"}
    assert store.list_artist_works("s", SCOPE)[0]["status"] == "review_pending"
    calls = len(model.calls)
    assert (await runtime.run_once())["state"] == "idle"
    assert len(model.calls) == calls
    assert (await step(studio))["state"] == "finished"
    assert len(renderer.calls) == 1


async def test_edit_uses_original_bytes_and_can_keep_older_best_version(studio):
    runtime, store, _, model, renderer, _ = studio
    model.revise, model.keep_first = 1, True
    await runtime.tick()
    await step(studio)
    assert (await step(studio))["reason"] == "revision"
    assert (await step(studio))["state"] == "review_pending"
    assert (await step(studio))["state"] == "finished"
    work = store.list_artist_works("s", SCOPE)[0]
    versions = store.list_versions("s", SCOPE, work["work_id"])
    assert len(versions) == 2 and versions[1]["parent_id"] == versions[0]["version_id"]
    assert work["best_version"] == versions[0]["version_id"]
    assert renderer.calls[1]["source"]["data"] == PNG
    assert renderer.calls[1]["source"]["sha256"] == versions[0]["sha256"]
    assert "保留" in renderer.calls[1]["prompt"]


async def test_revision_limit_finishes_without_a_new_work_or_extra_generation(studio):
    studio[3].revise = 10
    await studio[0].tick()
    states = [await step(studio) for _ in range(8)]
    assert any(s == {"state": "finished", "reason": "REVISION_LIMIT"} for s in states)
    assert len(studio[4].calls) == 3
    assert len(studio[1].list_artist_works("s", SCOPE)) == 1


async def test_edit_unsupported_does_not_fall_back_to_text_generation(studio):
    studio[3].revise = 1
    studio[4].supports_edit = False
    await studio[0].tick()
    await step(studio)
    await step(studio)
    assert (await step(studio))["reason"] == "EDIT_UNSUPPORTED"
    assert len(studio[4].calls) == 1


@pytest.mark.parametrize("found", [True, False])
async def test_lost_submit_response_only_looks_up_original_request(studio, found):
    runtime, store, clock, model, renderer, sender = studio
    renderer.lose_receipt, renderer.lookup_missing = True, not found
    await runtime.tick()
    assert (await step(studio))["reason"] == "SUBMIT_UNCONFIRMED"
    await runtime.recover(offline=True)
    reopened = ArtistStore(store.path, clock=clock)
    try:
        recovered = Artist(reopened, "s", SCOPE, Thinking(model), renderer, sender)
        clock.advance()
        result = await recovered.tick()
        assert result["state"] == ("review_pending" if found else "blocked")
        if not found:
            assert result["reason"] == "LOOKUP_NOT_FOUND"
            assert reopened.budget_usage("s", SCOPE)["render"]["held"] == 1
        assert len(renderer.calls) == 1
    finally:
        reopened.close()


async def test_available_image_with_unknown_turn_can_be_reviewed_but_not_revised_yet(studio):
    runtime, store, _, model, renderer, _ = studio
    renderer.unknown_artifact = True
    model.revise = 1
    await runtime.tick()
    assert (await step(studio))["state"] == "review_pending"
    assert (await step(studio))["reason"] == "RENDER_UNRESOLVED"
    assert store.budget_usage("s", SCOPE)["render"]["held"] == 1
    review_count = len([c for c in model.calls if c["task"] == "review"])
    renderer.complete()
    assert (await step(studio))["reason"] == "revision"
    assert len([c for c in model.calls if c["task"] == "review"]) == review_count
    assert len(renderer.calls) == 1


async def test_corrupt_artifact_refetches_same_job_without_regenerating(studio):
    renderer = studio[4]
    renderer.corrupt = True
    await studio[0].tick()
    assert (await step(studio))["reason"] == "ARTIFACT_INVALID"
    renderer.corrupt = False
    assert (await step(studio))["state"] == "review_pending"
    assert len(renderer.calls) == 1


async def test_unknown_send_is_not_replayed_after_restart(studio):
    await finished(studio)
    runtime, store, clock, model, renderer, sender = studio
    model.share, sender.lose_receipt = True, True
    assert (await step(studio))["state"] == "share_unknown"
    await runtime.recover(offline=True)
    restored = Artist(store, "s", SCOPE, Thinking(model), renderer, sender)
    clock.advance()
    assert (await restored.tick()) == {"state": "share_recorded", "outcome": "unknown"}
    assert len(sender.calls) == 1 and len(renderer.calls) == 1


async def test_authorization_change_during_model_call_discards_old_advice(studio):
    runtime, store, _, model, renderer, sender = studio

    async def hook(request):
        store.revoke("s", SCOPE, source_ref="revocation", granted_by="admin")
        store.grant("s", SCOPE, Delegation("new-consent", "admin", continuous=True))

    model.hook = hook
    assert (await runtime.tick())["reason"] == "AUTH_CHANGED"
    assert store.list_artist_works("s", SCOPE) == []
    assert renderer.calls == sender.calls == []


async def test_cancelled_submit_is_joined_and_recovered_without_duplicate(studio):
    runtime, store, _, _, renderer, _ = studio
    entered = asyncio.Event()

    async def hold():
        entered.set()
        await asyncio.Event().wait()

    await runtime.tick()
    renderer.before_return = hold
    task = asyncio.create_task(step(studio))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.budget_usage("s", SCOPE)["render"]["held"] == 1
    assert (await step(studio))["state"] == "review_pending"
    assert len(renderer.calls) == 1


def test_live_adapters_are_not_accepted_by_stage2_runtime(studio):
    _, store, _, model, renderer, sender = studio
    renderer.simulation = False
    with pytest.raises(StoreError, match="simulated"):
        Artist(store, "s", SCOPE, Thinking(model), renderer, sender)


@pytest.mark.parametrize("decision", ["wait", "abandon"])
async def test_planned_work_can_rest_or_be_abandoned_without_drawing(studio, decision):
    await studio[0].tick()
    studio[3].plan_action = decision
    assert (await step(studio))["state"] == ("waiting" if decision == "wait" else "abandoned")
    assert studio[4].calls == []
    if decision == "wait":
        count = len(studio[3].calls)
        assert (await step(studio))["state"] == "idle"
        assert len(studio[3].calls) == count


async def test_lower_budget_stops_preexisting_plan_before_render(studio):
    await studio[0].tick()
    studio[1].set_limits("s", SCOPE, Limits(think=100, render=0, max_inflight=20))
    assert (await step(studio))["reason"] == "BUDGET_EXCEEDED"
    assert studio[4].calls == []
    assert studio[1].list_artist_works("s", SCOPE)[0]["status"] == "planned"


async def test_mismatched_lookup_receipt_does_not_bind_unrelated_job(studio):
    runtime, store, _, _, renderer, _ = studio
    renderer.lose_receipt = True
    await runtime.tick()
    await step(studio)
    key, receipt = next(iter(renderer.jobs.items()))
    renderer.jobs[key] = replace(receipt, request_digest="wrong-digest")
    assert (await step(studio))["reason"] == "RECEIPT_MISMATCH"
    operation = store.get_operation("s", SCOPE, key)
    assert operation["state"] == "unknown" and operation["job_id"] is None
    assert store.list_versions("s", SCOPE, store.list_artist_works("s", SCOPE)[0]["work_id"]) == []
    assert len(renderer.calls) == 1


async def test_persisted_image_corruption_cannot_be_reviewed_or_shared(studio):
    runtime, store, _, model, renderer, sender = studio
    await runtime.tick()
    await step(studio)
    store.db.execute("UPDATE versions SET data=?", (b"synthetic corruption",))
    assert (await step(studio))["reason"] == "ARTIFACT_INVALID"
    assert not any(c["task"] == "review" for c in model.calls)
    assert len(renderer.calls) == 1 and sender.calls == []


async def test_no_idle_backlog_replay_after_long_restart(studio):
    runtime, store, clock, model, _, _ = studio
    await finished(studio)
    await step(studio)
    count = len(model.calls)
    clock.advance(30 * 86400)
    await runtime.recover(offline=True)
    assert (await runtime.tick())["state"] == "waiting"
    assert len(model.calls) == count + 1
    assert (await runtime.tick())["state"] == "idle"
    assert len(store.list_artist_works("s", SCOPE)) == 1
