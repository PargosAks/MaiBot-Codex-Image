"""Stage 3 independent acceptance using fixed public Host code and real SQLite.

External Host contract: set CODEX_IMAGE_TEST_HOST_REPO to a public MaiBot Git
repository containing HOST_REV, then run pytest tests/test_artist_studio_e2e.py.
Without that variable only this external contract is skipped. An explicitly
configured repository lacking the fixed object fails; no moving checkout is used.
Only public Host methods are AST-loaded; model answers, PNG bytes, renderer and platform
receipts are synthetic. This is not a live adapter/model/deployment acceptance.
Private WebUI APIs and planner/replyer hooks run through actual Runner methods.
WebUI authentication and browser rendering are outside this isolated acceptance.
"""

from copy import deepcopy
from threading import Event as ThreadEvent
import asyncio
import base64
import hashlib
import json

import pytest

from image_plugin_under_test.domain.policy import Limits
from image_plugin_under_test.workspace.studio import Studio
from image_plugin_under_test.host.studio_chat import is_studio_status_message
from protocol_fixture import PNG
from support.catalog_examples import PROFILES
from support import host_chain as host_fixture
from support.studio_acceptance_host import (
    public_host as public_host,
    replyer_host as replyer_host,
    planner_host as planner_host,
    build_planner_request,
)
from support.studio_acceptance import (
    SCOPE,
    SPACE,
    source_snapshot as source_snapshot,
    studio_chain as studio_chain,
    state_snapshot,
)


def assert_projection(h):
    view = Studio(h.data_paths.data_dir, scopes={SPACE: SCOPE}, clock=h.clock, enabled=True)
    summary = view.summary(SPACE)
    ledger = h.operations()
    assert summary["read_only"] and summary["authorization"]["authorized"]
    for category in ("think", "render", "share"):
        usage = h.runtime.store.budget_usage(SPACE, SCOPE)[category]
        assert summary["budget"][category]["held"] == usage["held"]
        assert summary["budget"][category]["consumed"] == usage["consumed"]
    tasks = view.records(SPACE, "tasks", limit=50)["items"]
    assert {row["request_id"]: row["state"] for row in tasks} == {
        row["request_id"]: row["state"] for row in ledger
    }
    assert view.records(SPACE, "profiles")["items"][0]["data"] == PROFILES[0]["data"]
    versions = view.records(SPACE, "versions")["items"]
    assert summary["counts"]["versions"] == len(versions)
    for version in versions:
        assert version["sha256"] == hashlib.sha256(PNG).hexdigest()
        assert view.image(SPACE, version["version_id"], thumbnail=False)["sha256"] == version["sha256"]
    return summary


async def assert_public_reads(h):
    """Read through the four WebUI API registrations and the planner hook."""
    expected = assert_projection(h)
    spaces = await h.api("studio_spaces", offset=0, limit=20)
    assert spaces["items"] == [{"space_id": SPACE, "authorized": True, "state_exists": True}]
    # Sharing permission is an administrative projection, not private context
    # automatically disclosed to the chat planner/replyer.
    assert "sharing" not in expected
    assert await h.api("studio_summary", space_id=SPACE) == {
        **expected, "sharing": {"mode": "private", "allowed": True},
    }
    for section in ("works", "tasks", "versions", "reviews", "profiles", "series", "assets"):
        result = await h.api("studio_records", space_id=SPACE, section=section, offset=0, limit=50, work_id="")
        view = Studio(h.data_paths.data_dir, scopes={SPACE: SCOPE}, clock=h.clock)
        assert result == view.records(SPACE, section, limit=50)
    versions = await h.api("studio_records", space_id=SPACE, section="versions")
    for version in versions["items"]:
        original = await h.api("studio_image", space_id=SPACE, version_id=version["version_id"],
                               thumbnail=False, offset=0)
        assert base64.b64decode(original["chunk_base64"], validate=True) == PNG
        assert original["next_offset"] is None and original["total_bytes"] == len(PNG)
        thumbnail = await h.api("studio_image", space_id=SPACE, version_id=version["version_id"],
                                thumbnail=True, offset=0)
        assert thumbnail["data_url"].startswith("data:image/jpeg;base64,")
    # Reading an ordinary status question through real receive_message must not
    # queue a new inspiration/wake; planner reading must not consume any quota.
    text = "现在在画什么"
    assert is_studio_status_message({"processed_plain_text": text})
    await h.message(text, message_id="stage3-read-only-question")
    assert_chat_matches(await h.query(text), expected)
    return expected


def assert_chat_matches(data, summary):
    assert data is not None
    for key in ("space_id", "day", "read_only", "state_exists", "budget", "counts", "waiting",
                "current_work", "recent_works"):
        assert data[key] == summary[key]
    for key in ("authorized", "version"):
        assert data["authorization"][key] == summary["authorization"][key]


@pytest.mark.parametrize("chat,revise", [(True, True), (False, False)],
                         ids=["chat-refine-share", "idle-accept-share"])
async def test_host_catalogue_review_share_and_readonly_projection(studio_chain, chat, revise):
    h = studio_chain
    await h.completed_work(chat=chat, revise=revise)
    await h.share_work()
    assert h.plugin.journal.db.execute("SELECT state FROM delivery").fetchone()[0] == "sent"
    before = state_snapshot(h)
    for _ in range(3):
        await assert_public_reads(h)
    assert state_snapshot(h) == before
    await h.advance()
    await h.settled()
    assert len(h.sends) == 1 and len(h.renderer.calls) == (2 if revise else 1)


async def test_host_unknown_send_is_visible_and_reload_does_not_replay(studio_chain):
    h = studio_chain
    await h.completed_work(chat=False, revise=False)
    await h.share_work(unknown=True)
    before = state_snapshot(h)
    for _ in range(3):
        summary = await assert_public_reads(h)
        assert "SEND_UNKNOWN" in {row["code"] for row in summary["waiting"]}
        assert summary["budget"]["share"]["held"] == 1
    assert state_snapshot(h) == before
    await h.plugin.on_config_update("plugin", {}, "stage3-synthetic-reload")
    await h.advance()
    await h.settled()
    assert len(h.sends) == len(h.renderer.calls) == 1
    assert h.runtime.store.budget_usage(SPACE, SCOPE)["share"]["held"] == 1
    assert h.plugin.journal.db.execute("SELECT state FROM delivery").fetchone()[0] == "send_unknown"


async def test_host_zero_budget_prevents_catalogue_and_effects(studio_chain):
    h = studio_chain
    await h.load()
    await h.runtime.db("set_limits", SPACE, SCOPE, Limits(think=0, render=0, share=0, max_inflight=0))
    h.gate.set()
    await asyncio.sleep(.15)
    await h.settled()
    before = state_snapshot(h)
    view = Studio(h.data_paths.data_dir, scopes={SPACE: SCOPE}, clock=h.clock)
    for _ in range(3):
        summary = await h.api("studio_summary", space_id=SPACE)
        assert_chat_matches(await h.query(), summary)
        assert "SPACE_BUDGET_EXHAUSTED" in {row["code"] for row in summary["waiting"]}
        assert not view.records(SPACE, "profiles")["items"]
        assert not view.records(SPACE, "works")["items"]
    assert state_snapshot(h) == before
    await h.advance()
    await h.settled()
    assert not h.operations() and not h.renderer.calls and not h.sends
    assert all(name != "llm.generate" for name, _ in h.calls)


@pytest.mark.parametrize("category", ["think", "render"])
async def test_unknown_model_or_render_query_and_reload_never_replays(studio_chain, monkeypatch, category):
    h = studio_chain
    await h.load()
    if category == "think":
        h.lose_model = True
        h.gate.set()
    else:
        await h.catalogue()
        await h.advance("planned")

        async def lost_render(**kwargs):
            h.renderer.calls.append(deepcopy(kwargs))
            raise OSError("synthetic renderer response lost")

        monkeypatch.setattr(h.renderer, "submit", lost_render)
        await h.advance("creating")
    await h.until(lambda: any(op["category"] == category and op["state"] == "unknown" for op in h.operations()))
    await h.settled()
    operation = next(op for op in h.operations() if op["state"] == "unknown")
    calls = sum(name == "llm.generate" for name, _ in h.calls)
    before = state_snapshot(h)
    for _ in range(3):
        summary = await h.api("studio_summary", space_id=SPACE)
        assert category.upper() + "_UNKNOWN" in {row["code"] for row in summary["waiting"]}
        assert summary["budget"][category]["held"] == 1
        assert_chat_matches(await h.query(), summary)
        await h.message("现在在画什么", message_id="stage3-unknown-status")
    assert state_snapshot(h) == before
    await h.plugin.on_config_update("plugin", {}, "stage3-unknown-reload")
    await h.message("synthetic new idea cannot unlock an unknown call", message_id="stage3-new-wake")
    await h.settled()
    await h.advance()
    later_models = [args for name, args in h.calls if name == "llm.generate"][calls:]
    if category == "think":
        assert not later_models
    else:
        # Fresh chat can legitimately inspire future work while the old render
        # is unknown; it cannot replan, redraw or unlock that operation.
        assert all("不得规划或覆盖" in args["prompt"][0]["content"] for args in later_models)
    assert len(h.renderer.calls) == int(category == "render") and not h.sends
    assert next(op for op in h.operations() if op["request_id"] == operation["request_id"])["state"] == "unknown"
    assert h.runtime.store.budget_usage(SPACE, SCOPE)[category]["held"] == 1


async def test_foreign_private_chat_and_creative_request_do_not_get_studio_context(studio_chain):
    h = studio_chain
    await h.load()
    # Keep the genuine worker gated: denial must not open a new stateful path.
    before = state_snapshot(h)
    assert await h.query(session_id="unconfigured-foreign-stream") is None
    assert await h.query("现在在画什么？再帮我画一只猫") is None
    assert not is_studio_status_message({"processed_plain_text": "现在在画什么？再帮我画一只猫"})
    assert state_snapshot(h) == before


async def test_autonomous_profile_revision_is_visible_without_changing_finished_work(studio_chain):
    h = studio_chain
    work = await h.completed_work(chat=False, revise=False)
    before = deepcopy(h.renderer.calls), deepcopy(h.sends)
    thoughts = sum(op["category"] == "think" for op in h.operations())
    h.revise_catalogue = True
    await h.advance()
    profiles = h.runtime.store.list_profiles(SPACE, SCOPE)
    assert {profile["revision"] for profile in profiles} == {1, 2}
    assert h.runtime.store.get_artist_work(SPACE, SCOPE, work["work_id"])["brief"] == work["brief"]
    assert (h.renderer.calls, h.sends) == before
    assert sum(op["category"] == "think" for op in h.operations()) == thoughts + 1
    snapshot = state_snapshot(h)
    result = await h.api("studio_records", space_id=SPACE, section="profiles", offset=0, limit=50)
    assert {row["revision"]: row["data"]["palette"] for row in result["items"]} == {
        1: "warm amber", 2: "cold blue",
    }
    summary = await h.api("studio_summary", space_id=SPACE)
    assert_chat_matches(await h.query(), summary)
    assert state_snapshot(h) == snapshot


def assert_replyer_work_matches_ledger(h, data, work_id, *, share_state):
    """Assert user-facing facts against independent raw ledger/receipt evidence."""
    store = h.runtime.store
    work = store.get_artist_work(SPACE, SCOPE, work_id)
    versions = store.db.execute("SELECT job_id FROM versions WHERE space_id=? AND work_id=?",
                                (SPACE, work_id)).fetchall()
    latest_review = store.db.execute("SELECT review FROM reviews WHERE space_id=? AND work_id=? ORDER BY id DESC LIMIT 1",
                                     (SPACE, work_id)).fetchone()
    expected = {"work_id": work_id, "subject": work["brief"]["subject"], "status": work["status"],
                "generated": bool(versions), "version_count": len(versions), "share_state": share_state,
                "review_state": json.loads(latest_review[0])["decision"] if latest_review else "not_generated"}
    recent = next(item for item in data["recent_works"] if item["work_id"] == work_id)
    assert {key: recent[key] for key in expected} == expected
    if work["status"] in {"planned", "deferred", "creating", "review_pending"}:
        assert data["current_work"] == recent
    else:
        assert data["current_work"] is None
    shares = [operation for operation in h.operations() if operation["category"] == "share"]
    receipts = [h.plugin.journal.db.execute("SELECT state FROM delivery WHERE job=?", (version["job_id"],)).fetchone()
                for version in versions]
    if share_state == "not_shared":
        assert not shares and not any(receipts)
    elif share_state == "sent":
        assert shares[-1]["state"] == "succeeded" and receipts[-1][0] == "sent"
    else:
        assert share_state == "send_unknown"
        assert shares[-1]["state"] == "unknown" and receipts[-1][0] == "send_unknown"


@pytest.mark.parametrize("stage", ["planned", "rendered", "sent", "send_unknown"])
async def test_actual_reply_target_gets_work_facts_from_real_ledger(studio_chain, replyer_host, stage):
    h = studio_chain
    if stage == "planned":
        await h.load()
        await h.catalogue()
        await h.advance("planned")
        work = h.runtime.store.list_artist_works(SPACE, SCOPE)[0]
    else:
        work = await h.completed_work(chat=False, revise=False)
        if stage != "rendered":
            await h.share_work(unknown=stage == "send_unknown")
    message_id = "stage3-full-received-platform-message-" + stage
    before = state_snapshot(h)
    await h.message("窗边的小猫画了吗？", message_id=message_id)
    assert state_snapshot(h) == before
    original, actual = await h.replyer(replyer_host, message_id=message_id, tool_args={
        "reply_message_id": "model-forged-target", "facts": "模型伪造：已全部送达", "retry": True,
    })
    assert len(actual) == len(original) + 2
    assert type(actual[-2]).__name__ == "SystemMessageItem"
    assert type(actual[-1]).__name__ == "UserMessageItem"
    projection = json.loads(actual[-1].parts[0].text)
    assert projection["source"] == "codex_image_studio_readonly"
    summary = await h.api("studio_summary", space_id=SPACE)
    assert_chat_matches(projection["data"], summary)
    assert_replyer_work_matches_ledger(h, projection["data"], work["work_id"],
                                       share_state=stage if stage in {"sent", "send_unknown"} else "not_shared")
    # A duplicate receive cannot rearm the consumed target, nor retry its reply.
    await h.message("窗边的小猫画了吗？", message_id=message_id)
    original, repeated = await h.replyer(replyer_host, message_id=message_id)
    assert repeated == original
    assert state_snapshot(h) == before


async def test_replyer_unmatched_targets_never_leak_or_consume_real_target(studio_chain, replyer_host):
    h = studio_chain
    work = await h.completed_work(chat=False, revise=False)
    await h.share_work(unknown=True)
    message_id = "stage3-received-query-full-platform-id"
    await h.message("窗边的小猫画了吗？", message_id=message_id)
    await h.message("窗边的小猫画了吗？", owner="synthetic-foreign-owner", message_id="foreign-owner-message")
    before = state_snapshot(h)
    calls = len(h.calls)
    forged = {"reply_message_id": message_id, "session_id": host_fixture.STREAM,
              "user_id": host_fixture.OWNER, "query": "窗边的小猫画了吗？", "authorized": True}
    for target, session in (("unreceived-message", host_fixture.STREAM), ("mshort", host_fixture.STREAM),
                            (None, host_fixture.STREAM), ("foreign-owner-message", host_fixture.STREAM),
                            (message_id, "foreign-private-session")):
        original, actual = await h.replyer(replyer_host, message_id=target, session_id=session, tool_args=forged)
        assert actual == original  # No facts or authority text enters the actual model request.
        assert len(h.calls) == calls  # Misses do not even consult Host lookup/ledger.
    original, actual = await h.replyer(replyer_host, message_id=message_id, tool_args=forged)
    assert len(actual) == len(original) + 2  # Earlier misses did not consume the valid note.
    data = json.loads(actual[-1].parts[0].text)["data"]
    assert_replyer_work_matches_ledger(h, data, work["work_id"], share_state="send_unknown")
    assert state_snapshot(h) == before


@pytest.mark.parametrize("case", ["status-with-real-user-tails", "older-query-is-not-current",
                                   "newer-nonquery-omitted-from-selection"])
async def test_real_planner_constructor_with_tail_reminders(studio_chain, planner_host, replyer_host, case):
    h = studio_chain
    work = await h.completed_work(chat=False, revise=False)
    query = case == "status-with-real-user-tails"
    messages = [("stage3-first-query-platform-id", "现在在画什么")]
    if query:
        messages.append(("stage3-latest-query-platform-id", "窗边的小猫画了吗？"))
    for message_id, text in messages:
        await h.message(text, message_id=message_id)
    if not query:
        claims = h.runtime.store.db.execute("SELECT count(*) FROM artist_claims").fetchone()[0]
        unrelated = ("stage3-latest-unrelated-platform-id", "synthetic unrelated latest message")
        await h.message(unrelated[1], message_id=unrelated[0])
        await h.until(lambda: h.runtime.store.db.execute("SELECT count(*) FROM artist_claims").fetchone()[0] > claims)
        await h.settled()
        if case == "older-query-is-not-current":
            messages.append(unrelated)
    ci, namespace = planner_host.context, planner_host.namespace
    original, history_count = build_planner_request(h, planner_host, messages)
    args = {"session_id": host_fixture.STREAM, "item_schema_version": ci.CONTEXT_ITEM_SCHEMA_VERSION,
            "items": namespace["serialize_prompt_items"](original), "tool_definitions": [],
            "selected_history_count": history_count, "built_message_count": len(original),
            "selection_reason": "synthetic history selection"}
    before = state_snapshot(h)
    calls = len(h.calls)
    envelope = h.host.Envelope(request_id=1, message_type="request", method="plugin.invoke_hook",
                               plugin_id=host_fixture.PLUGIN_ID, payload={
                                   "component_name": "codex_image_studio_chat", "args": args})
    response = await h.runner._handle_hook_invoke(envelope)
    assert response.error is None and response.payload["success"] is True
    modified = response.payload.get("modified_kwargs")
    if query:
        assert modified is not None, "STUDIO-R1-01: genuine planner user-role reminders must not hide the latest IM query"
        actual = namespace["deserialize_prompt_items"](
            modified["items"], item_schema_version=modified["item_schema_version"], original_items=original)
        assert len(actual) == len(original) + 2
        assert all(actual[index] is item for index, item in enumerate(original))
        data = json.loads(actual[-1].parts[0].text)["data"]
        assert_replyer_work_matches_ledger(h, data, work["work_id"], share_state="not_shared")
    else:
        assert modified is None
        assert len(h.calls) == calls
        # The same newer nonquery must also supersede the old formal reply
        # target, independently of which items the planner chose to include.
        initial, actual = await h.replyer(replyer_host, message_id=messages[0][0])
        assert actual == initial
        assert len(h.calls) == calls
    assert state_snapshot(h) == before


@pytest.mark.parametrize("stage", ["planner", "replyer"])
async def test_new_ingress_invalidates_old_snapshot_while_lifecycle_is_contended(
        studio_chain, planner_host, replyer_host, monkeypatch, stage):
    h = studio_chain
    await h.load()  # Keep the synthetic worker gated; ordinary ingress still uses the real store.
    old_id, new_id = "stage3-lock-old-query-" + stage, "stage3-lock-new-ordinary-" + stage
    new_text = "synthetic ordinary message received while the old read holds lifecycle"
    await h.message("现在在画什么", message_id=old_id)
    before = state_snapshot(h)
    loop, lifecycle = asyncio.get_running_loop(), h.plugin.lifecycle
    snapshot_captured, ingress_waiting, read_completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_snapshot, release_ingress = ThreadEvent(), asyncio.Event()
    summaries = []
    real_summary, real_read = Studio.summary, h.plugin.studio_chat.read

    class ContendedLifecycle:
        async def __aenter__(self):
            if lifecycle.locked():
                # Hold the contender even after the read releases the lock.
                # Otherwise the old observer might catch up before the final
                # freshness check, masking note_ingress being inside lifecycle.
                ingress_waiting.set()
                await release_ingress.wait()
            await lifecycle.acquire()
            return self

        async def __aexit__(self, *args):
            lifecycle.release()

    def captured_summary(view, space_id):
        summary = real_summary(view, space_id)
        summaries.append(summary)
        loop.call_soon_threadsafe(snapshot_captured.set)
        assert release_snapshot.wait(5), "Snapshot release event was not signalled"
        return summary

    async def completed_read(space_id, scope):
        snapshot = await real_read(space_id, scope)
        read_completed.set()  # Distinguish supersession from timeout/cancellation or read failure.
        return snapshot

    async def old_request():
        if stage == "replyer":
            return await h.replyer(replyer_host, message_id=old_id)
        original, count = build_planner_request(h, planner_host, [(old_id, "现在在画什么")])
        namespace = planner_host.namespace
        envelope = h.host.Envelope(request_id=1, message_type="request", method="plugin.invoke_hook",
                                   plugin_id=host_fixture.PLUGIN_ID, payload={
                                       "component_name": "codex_image_studio_chat", "args": {
                                           "session_id": host_fixture.STREAM, "item_schema_version": 1,
                                           "items": namespace["serialize_prompt_items"](original),
                                           "tool_definitions": [], "selected_history_count": count,
                                           "built_message_count": len(original),
                                           "selection_reason": "synthetic contended-read selection"}})
        response = await h.runner._handle_hook_invoke(envelope)
        assert response.error is None and response.payload["success"] is True
        modified = response.payload.get("modified_kwargs")
        actual = namespace["deserialize_prompt_items"](
            modified["items"], item_schema_version=modified["item_schema_version"], original_items=original,
        ) if modified is not None else original
        return original, actual

    monkeypatch.setattr(h.plugin, "lifecycle", ContendedLifecycle())
    monkeypatch.setattr(Studio, "summary", captured_summary)
    monkeypatch.setattr(h.plugin.studio_chat, "read", completed_read)
    pending = []
    try:
        old_task = asyncio.create_task(old_request())
        pending.append(old_task)
        await asyncio.wait_for(snapshot_captured.wait(), 5)
        assert lifecycle.locked() and not old_task.done()
        new_task = asyncio.create_task(h.message(new_text, message_id=new_id))
        pending.append(new_task)
        await asyncio.wait_for(ingress_waiting.wait(), 5)
        assert h.received[-1].message_id == new_id
        assert h.hook_calls[-1]["message"]["message_id"] == new_id
        assert not new_task.done() and not old_task.done()
        release_snapshot.set()
        original, actual = await asyncio.wait_for(old_task, 5)
        assert read_completed.is_set() and len(summaries) == 1 and summaries[0]["read_only"] is True
        assert not new_task.done(), "New ordinary observer must remain blocked until stale-result assertion"
        assert actual == original, "Old query facts crossed a newer Host ingress pending on lifecycle"
        assert all(actual[index] is item for index, item in enumerate(original))
        assert state_snapshot(h) == before

        release_ingress.set()
        message = await asyncio.wait_for(new_task, 5)
        assert h.received[-1] is message and message.processed_plain_text == new_text
        wakes = h.runtime.store.db.execute(
            "SELECT event_id,kind,text,source_ref FROM artist_wakes WHERE space_id=?", (SPACE,),
        ).fetchall()
        assert [tuple(row) for row in wakes] == [("chat:" + new_id, "chat", new_text, new_id)]
        assert not h.operations() and not h.renderer.calls and not h.sends
        assert all(name != "llm.generate" for name, _ in h.calls)
    finally:
        release_snapshot.set()
        release_ingress.set()
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 5)
