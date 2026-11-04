"""Offline retained-plan repair, real SQLite and synthetic Artist/Host effects."""

from datetime import timedelta
from types import SimpleNamespace
import asyncio
import hashlib
import json

import pytest

from image_plugin_under_test.artist import Artist
from image_plugin_under_test.delivery import Deliveries
from image_plugin_under_test.host_runtime import Autonomy, HostRuntime, RuntimeLease
from image_plugin_under_test.policy import Delegation, Limits, Scope, StoreError
from image_plugin_under_test.store import ArtistStore
from image_plugin_under_test.thinking import Thinking
from test_artist_flow import Clock, Model, Renderer, Sender, SCOPE
from test_host_adapters import Context
from protocol_fixture import PNG


LIMITS = Limits(think=16, render=2, share=1, max_inflight=2, max_revisions=1)
URL = "http://127.0.0.1:9/mcp"
BINDING = "mcp:" + hashlib.sha256(URL.encode()).hexdigest()
REASON = "Confirmed obsolete acceptance persona; retain existing original plan"


class RecoveryModel(Model):
    def __init__(self):
        super().__init__()
        self.obsolete_wait = True

    async def __call__(self, request):
        if request["task"] == "plan" and self.obsolete_wait:
            self.calls.append(request)
            return {"ideas": [], "decision": {
                "action": "wait", "delay_seconds": 86400, "reason": "obsolete acceptance restriction",
            }}
        return await super().__call__(request)


async def seed(tmp_path):
    """Reproduce the actual ledger: wait, create, obsolete wait, revoked epoch2."""
    clock, model, renderer, sender = Clock(), RecoveryModel(), Renderer(), Sender()
    store = ArtistStore(tmp_path / "artist.sqlite3", clock=clock)
    store.create_space("s", SCOPE, binding_id=BINDING)
    store.grant("s", SCOPE, Delegation("original-consent", "host-config", continuous=True))
    store.set_global_limits(LIMITS)
    store.set_limits("s", SCOPE, LIMITS)
    runtime = Artist(store, "s", SCOPE, Thinking(model), renderer, sender, idle_seconds=86400)
    assert (await runtime.tick())["state"] == "waiting"
    clock.advance(10)
    model.obsolete_wait = False
    await runtime.on_chat(message_id="existing-inspiration", text="雨后暖灯窗边小猫")
    assert (await runtime.tick())["state"] == "planned"
    clock.advance(10)
    model.obsolete_wait = True
    await runtime.on_chat(message_id="existing-followup", text="安静的雨后窗边")
    assert (await runtime.tick())["state"] == "waiting"
    assert store.revoke("s", SCOPE, source_ref="acceptance-ended", granted_by="host-config") == 2
    operations = store.audit_operations("s", SCOPE)
    assert len(operations) == 3 and all(op["state"] == "succeeded" for op in operations)
    work = store.list_artist_works("s", SCOPE)[0]
    assert work["status"] == "planned" and work["pending_request"] is None
    assert renderer.calls == sender.calls == []
    model.obsolete_wait = False
    return runtime, store, clock, model, renderer, sender, work, operations[-1]["request_id"]


@pytest.fixture
async def retained(tmp_path):
    state = await seed(tmp_path)
    yield state
    state[1].close()


def repair(state, **changes):
    _, store, _, _, _, _, work, wait = state
    kwargs = dict(expected_auth_version=2, work_id=work["work_id"], last_wait_request_id=wait,
                  event_id="restore:original-plan:01", reason=REASON, offline=True)
    kwargs.update(changes)
    return store.restore_planned_wait("s", SCOPE, **kwargs)


def tables(store, names):
    return {name: [tuple(row) for row in store.db.execute(f"SELECT * FROM {name}")] for name in names}


def error(code):
    return pytest.raises(StoreError, check=lambda exc: exc.code == code)


async def test_atomic_restore_preserves_all_content_authority_limits_and_three_thinks(retained):
    _, store, clock, _, _, _, work, wait = retained
    unchanged = ("spaces", "grants", "limits", "works", "artist_works", "inspirations", "operations", "artist_claims")
    before = tables(store, unchanged)
    old = dict(store.db.execute("SELECT * FROM artist_state").fetchone())
    assert repair(retained)
    assert tables(store, unchanged) == before
    assert store.budget_usage("s", SCOPE)["think"] == {"held": 0, "consumed": 3, "total": 3}
    current = dict(store.db.execute("SELECT * FROM artist_state").fetchone())
    assert current == {**old, "backoff_until": None, "next_due": clock().isoformat()}
    row = dict(store.db.execute("SELECT * FROM artist_wakes ORDER BY id DESC LIMIT 1").fetchone())
    receipt = json.loads(row["text"])
    assert row["kind"] == "idle" and row["source_ref"] == wait
    assert receipt["request"]["work_id"] == work["work_id"]
    assert receipt["request"]["reason"] == REASON and receipt["old_auth_version"] == 1
    assert receipt["previous_backoff_until"] == old["backoff_until"]
    assert receipt["through"] == old["acked_through"]
    with error("AUTH_REQUIRED"):
        store.claim_wake("s", SCOPE, token="still-disabled")
    assert not repair(retained)
    with error("REQUEST_CONFLICT"):
        repair(retained, event_id="restore:different-key")


@pytest.mark.parametrize("edits", [0, 1])
async def test_restore_original_flow_restart_and_lifetime_counts(retained, edits):
    _, store, clock, model, renderer, sender, work, _ = retained
    assert repair(retained)
    assert store.grant("s", SCOPE, Delegation("resumed-consent", "host-config", True)) == 3
    model.share, model.revise = True, edits
    original_ops = store.audit_operations("s", SCOPE)[:3]
    original_work = store.get_artist_work("s", SCOPE, work["work_id"])
    runtime = Artist(store, "s", SCOPE, Thinking(model), renderer, sender, idle_seconds=60)
    await runtime.recover(offline=True)
    assert (await runtime.tick())["state"] == "review_pending"
    assert len(renderer.calls) == 1
    # Reopen after every successful stage. Existing receipts cannot dispatch again.
    for _ in range(6):
        reopened = ArtistStore(store.path, clock=clock)
        try:
            runtime = Artist(reopened, "s", SCOPE, Thinking(model), renderer, sender, idle_seconds=60)
            await runtime.recover(offline=True)
            assert (await runtime.tick())["state"] == "idle"
            clock.advance(60)
            result = await runtime.tick()
            if result["state"] == "shared":
                break
        finally:
            reopened.close()
    assert result["state"] == "shared"
    assert len(renderer.calls) == 1 + edits and len(sender.calls) == 1
    assert sum(call["source"] is None for call in renderer.calls) == 1
    assert sum(call["source"] is not None for call in renderer.calls) == edits
    assert store.audit_operations("s", SCOPE)[:3] == original_ops
    saved = store.get_artist_work("s", SCOPE, work["work_id"])
    assert saved["brief"] == original_work["brief"] and len(store.list_artist_works("s", SCOPE)) == 1
    assert len(store.list_versions("s", SCOPE, work["work_id"])) == 1 + edits
    assert store.budget_usage("s", SCOPE)["render"]["consumed"] == 1 + edits
    assert store.budget_usage("s", SCOPE)["share"]["consumed"] == 1
    assert store.budget_usage("s", SCOPE)["think"]["consumed"] >= 6
    reopened = ArtistStore(store.path, clock=clock)
    try:
        runtime = Artist(reopened, "s", SCOPE, Thinking(model), renderer, sender, idle_seconds=60)
        await runtime.recover(offline=True)
        clock.advance(60)
        await runtime.tick()
        assert len(renderer.calls) == 1 + edits and len(sender.calls) == 1
    finally:
        reopened.close()
    for call in model.calls[3:]:
        for event in call["context"].get("events", []):
            if event["event_id"] == "restore:original-plan:01":
                assert event["kind"] == "idle" and event["text"] == ""
    store.revoke("s", SCOPE, source_ref="acceptance-complete", granted_by="host-config")
    restored = ArtistStore(store.path, clock=clock)
    try:
        runtime = Artist(restored, "s", SCOPE, Thinking(model), renderer, sender, idle_seconds=60)
        await runtime.recover(offline=True)
        with error("AUTH_REQUIRED"):
            await runtime.tick()
    finally:
        restored.close()
    assert len(renderer.calls) == 1 + edits and len(sender.calls) == 1


async def test_repeat_never_clears_new_backoff_and_rejects_changed_epoch(retained):
    _, store, clock, _, _, _, _, _ = retained
    assert repair(retained)
    # A later failure cooldown must survive an idempotent administrative retry.
    later = (clock() + timedelta(seconds=600)).isoformat()
    store.db.execute("UPDATE artist_state SET backoff_until=?,next_due=?", (later, later))
    before = tables(store, ("artist_state", "artist_wakes"))
    assert not repair(retained)
    assert tables(store, ("artist_state", "artist_wakes")) == before
    store.grant("s", SCOPE, Delegation("new-consent", "host-config", True))
    with error("AUTH_CHANGED"):
        repair(retained)


@pytest.mark.parametrize("held", ["reserved", "submitting", "accepted", "unknown"])
@pytest.mark.parametrize("which", ["latest", "earlier"])
async def test_any_held_operation_blocks_without_mutation(retained, held, which):
    _, store, _, _, _, _, _, wait = retained
    # Synthetic mutation isolates each held boundary without changing epoch.
    request_id = wait if which == "latest" else store.audit_operations("s", SCOPE)[0]["request_id"]
    store.db.execute("UPDATE operations SET state=? WHERE request_id=?", (held, request_id))
    before = tables(store, ("operations", "artist_state", "artist_wakes"))
    with error("RECOVERY_UNSAFE"):
        repair(retained)
    assert tables(store, before) == before


@pytest.mark.parametrize("change", [
    "authorized", "active-claim", "released-claim", "wrong-through", "claim-auth", "old-operation",
    "failed", "inspire", "non-wait", "bad-evidence", "context-other-work", "context-brief",
    "absent-delay", "bad-delay", "wrong-trigger", "wrong-binding", "wrong-scope", "missing-grant",
    "not-revoked-history", "missing-backoff", "expired-backoff", "not-wait-backoff",
    "pending", "deferred", "next-check", "finished",
])
async def test_unproven_or_changed_state_is_rejected_atomically(retained, change):
    _, store, clock, _, _, _, work, wait = retained
    operation = store.get_operation("s", SCOPE, wait)
    if change == "authorized":
        store.db.execute("UPDATE spaces SET authorized=1")
    elif change in {"active-claim", "released-claim"}:
        store.db.execute("UPDATE artist_claims SET state=? WHERE through=(SELECT max(through) FROM artist_claims)",
                         ("active" if change == "active-claim" else "released",))
    elif change in {"wrong-through", "claim-auth"}:
        field = "through" if change == "wrong-through" else "auth_version"
        store.db.execute(f"UPDATE artist_claims SET {field}=99 WHERE through=(SELECT max(through) FROM artist_claims)")
    elif change == "old-operation":
        wait = store.audit_operations("s", SCOPE)[0]["request_id"]
    elif change in {"failed", "wrong-trigger", "wrong-binding", "wrong-scope"}:
        field, value = {"failed": ("state", "failed"), "wrong-trigger": ("trigger_ref", "99"),
                        "wrong-binding": ("binding_id", "other"), "wrong-scope": ("scope_snapshot", "{}")}[change]
        store.db.execute(f"UPDATE operations SET {field}=? WHERE request_id=?", (value, wait))
    elif change in {"inspire", "context-other-work", "context-brief"}:
        params = operation["params"]
        if change == "inspire":
            params["task"] = "inspire"
        elif change == "context-other-work":
            params["context"]["works"][0]["work_id"] = "different"
        else:
            params["context"]["works"][0]["brief"]["prompt"] = "different"
        store.db.execute("UPDATE operations SET params_json=? WHERE request_id=?", (json.dumps(params), wait))
    elif change in {"non-wait", "bad-evidence", "absent-delay", "bad-delay"}:
        result = json.loads(operation["evidence"])
        if change == "non-wait":
            result["decision"]["action"] = "continue"
        elif change == "absent-delay":
            del result["decision"]["delay_seconds"]
        else:
            result["decision"]["delay_seconds"] = True
        store.db.execute("UPDATE operations SET evidence=? WHERE request_id=?",
                         ("invalid" if change == "bad-evidence" else json.dumps(result), wait))
    elif change == "missing-grant":
        store.db.execute("DELETE FROM grants WHERE version=1")
    elif change == "not-revoked-history":
        store.db.execute("UPDATE grants SET authorized=1 WHERE version=2")
    elif change in {"missing-backoff", "expired-backoff", "not-wait-backoff"}:
        backoff = None if change == "missing-backoff" else (
            clock() - timedelta(seconds=1) if change == "expired-backoff" else clock() + timedelta(seconds=300)
        ).isoformat()
        store.db.execute("UPDATE artist_state SET backoff_until=?", (backoff,))
    else:
        field, value = {"pending": ("pending_request", "unknown-request"), "deferred": ("status", "deferred"),
                        "next-check": ("next_check_at", clock().isoformat()), "finished": ("status", "finished")}[change]
        store.db.execute(f"UPDATE artist_works SET {field}=?", (value,))
    before = tables(store, ("spaces", "grants", "operations", "artist_claims", "artist_state", "artist_wakes", "artist_works"))
    with error("RECOVERY_UNSAFE"):
        repair(retained, last_wait_request_id=wait)
    assert tables(store, before) == before


@pytest.mark.parametrize("changes,code", [
    ({"offline": False}, "OFFLINE_REQUIRED"), ({"offline": 1}, "OFFLINE_REQUIRED"),
    ({"expected_auth_version": None}, "INVALID_ARGUMENT"), ({"expected_auth_version": True}, "INVALID_ARGUMENT"),
    ({"expected_auth_version": 1}, "AUTH_CHANGED"), ({"reason": " "}, "INVALID_ARGUMENT"),
    ({"event_id": "e" * 201}, "INVALID_ARGUMENT"), ({"work_id": "missing"}, "NOT_FOUND"),
    ({"last_wait_request_id": "missing"}, "NOT_FOUND"),
])
async def test_explicit_inputs_and_owner_are_required(retained, changes, code):
    with error(code):
        repair(retained, **changes)
    _, store, _, _, _, _, work, wait = retained
    with error("NOT_FOUND"):
        store.restore_planned_wait("s", Scope("synthetic", "other", "other"), expected_auth_version=2,
                                  work_id=work["work_id"], last_wait_request_id=wait,
                                  event_id="other", reason=REASON, offline=True)


@pytest.mark.parametrize("changed", ["work", "wait", "reason", "event-collision"])
async def test_idempotency_rejects_changed_content(retained, changed):
    _, store, _, _, _, _, _, wait = retained
    assert repair(retained)
    if changed == "work":
        store.db.execute("UPDATE artist_works SET reason='changed'")
    elif changed == "wait":
        store.db.execute("UPDATE operations SET evidence='{}' WHERE request_id=?", (wait,))
    elif changed == "event-collision":
        store.db.execute("UPDATE artist_wakes SET kind='completion' WHERE event_id='restore:original-plan:01'")
    with error("RECOVERY_UNSAFE" if changed in {"work", "wait"} else "REQUEST_CONFLICT"):
        repair(retained, **({"reason": "different"} if changed == "reason" else {}))


async def test_shorter_interval_clamps_once_without_sliding_and_preserves_failure_backoff(retained):
    _, store, clock, _, _, _, _, _ = retained
    store.grant("s", SCOPE, Delegation("timing-test", "host-config", True))
    original = dict(store.db.execute("SELECT * FROM artist_state").fetchone())
    assert not store.schedule_wake("s", SCOPE, interval_seconds=60)
    due = (clock() + timedelta(seconds=60)).isoformat()
    assert store.db.execute("SELECT next_due FROM artist_state").fetchone()[0] == due
    for _ in range(59):
        clock.advance(1)
        assert not store.schedule_wake("s", SCOPE, interval_seconds=60)
        row = store.db.execute("SELECT * FROM artist_state").fetchone()
        assert row["next_due"] == due and row["backoff_until"] == original["backoff_until"]
    clock.advance(1)
    assert store.schedule_wake("s", SCOPE, interval_seconds=60)
    assert store.claim_wake("s", SCOPE, token="backoff-still-binding") is None
    assert store.db.execute("SELECT backoff_until FROM artist_state").fetchone()[0] == original["backoff_until"]


async def test_shorter_timer_does_not_ignore_real_work_defer(retained):
    _, store, clock, model, renderer, sender, work, _ = retained
    assert repair(retained)
    store.grant("s", SCOPE, Delegation("timing-test", "host-config", True))
    due = (clock() + timedelta(seconds=600)).isoformat()
    store.update_artist_work("s", SCOPE, work["work_id"],
                             changes={"status": "deferred", "next_check_at": due})
    runtime = Artist(store, "s", SCOPE, Thinking(model), renderer, sender, idle_seconds=60)
    assert (await runtime.tick())["state"] == "waiting"
    before = len(model.calls)
    clock.advance(60)
    assert (await runtime.tick())["state"] == "waiting"
    assert len(model.calls) == before and renderer.calls == sender.calls == []
    assert store.get_artist_work("s", SCOPE, work["work_id"])["next_check_at"] == due


@pytest.mark.parametrize("edits", [0, 1])
async def test_host_lifecycle_stop_offline_lease_regrant_real_worker_tick_and_restart(tmp_path, monkeypatch, edits):
    state = await seed(tmp_path)
    _, store, clock, model, renderer, _, work, wait = state
    original_ops = store.audit_operations("s", SCOPE)
    store.close()
    model.share, model.revise = True, edits
    ctx, effects = Context(), []
    ctx.paths = SimpleNamespace(data_dir=tmp_path)

    async def rpc(method, plugin_id, payload, timeout_ms=None):
        assert method == "cap.call"
        capability, args = payload["capability"], payload["args"]
        effects.append((capability, args))
        if capability == "chat.get_stream_by_user_id":
            return {"success": True, "stream": {
                "platform": SCOPE.platform, "stream_id": SCOPE.stream_id,
                "session_id": SCOPE.stream_id, "user_id": SCOPE.user_id, "is_group_session": False,
            }}
        if capability == "llm.generate":
            content = args["prompt"][1]["content"]
            context = json.loads(content[0]["text"])
            request = {"task": "review" if args["task_name"] == "synthetic-review" else "plan",
                       "context": context}
            if request["task"] == "review":
                assert any(item["type"] == "image_url" for item in content)
                if context.get("review_contract") == 2:
                    request["images"] = [{**item, "data": PNG, "mime": "image/png"}
                                         for item in context["versions"]]
                else:
                    request.update(image=PNG, mime="image/png")
            answer = await model(request)
            return {"success": True, "response": json.dumps(answer)}
        assert capability == "send.image"
        assert args["stream_id"] == SCOPE.stream_id and args["return_details"] is True
        return {"sent": True, "message_id": "synthetic-qq-receipt"}

    ctx.sdk._rpc_call = rpc
    budget = dict(think=16, render=2, share=1, max_inflight=2, max_revisions=1)
    config = Autonomy(enabled=False, spaces=[dict(
        space_id="s", platform=SCOPE.platform, stream_id=SCOPE.stream_id, user_id=SCOPE.user_id,
        continuous=True, consent_ref="resumed-consent", limits=budget,
    )], global_limits=budget, idle_seconds=60, tick_seconds=0.1,
        plan_task="synthetic-plan", review_task="synthetic-review")
    plugin = SimpleNamespace(
        config=SimpleNamespace(plugin=SimpleNamespace(enabled=True), autonomy=config,
                               connection=SimpleNamespace(url=URL, send_timeout_seconds=240.0,
                                                          allowed_users=[f"{SCOPE.platform}:{SCOPE.user_id}"])),
        ctx=ctx, bridge=object(), journal=Deliveries(tmp_path / "delivery.sqlite3"),
        _artist_renderer=lambda *args: renderer, group_thought_runtime=None,
    )

    async def open_store(runtime, path):
        runtime.store = await asyncio.to_thread(ArtistStore, path, clock=clock)

    monkeypatch.setattr(HostRuntime, "_open_store", open_store)
    disabled = HostRuntime(plugin)
    resumed = restarted = None
    try:
        await disabled.start()
        assert not disabled.artists and disabled.lease is not None
        with error("ARTIST_IN_USE"):
            RuntimeLease(tmp_path / "artist.lock")
        await disabled.close()
        lease = RuntimeLease(tmp_path / "artist.lock")
        offline_store = ArtistStore(tmp_path / "artist.sqlite3", clock=clock)
        try:
            before = tables(offline_store, ("spaces", "grants", "limits", "operations", "inspirations", "artist_works"))
            assert offline_store.restore_planned_wait(
                "s", SCOPE, expected_auth_version=2, work_id=work["work_id"], last_wait_request_id=wait,
                event_id="restore:host-lifecycle", reason=REASON, offline=True,
            )
            assert tables(offline_store, before) == before
        finally:
            offline_store.close()
            lease.close()
        config.enabled = True
        resumed = HostRuntime(plugin)
        await resumed.start()
        assert resumed.store.get_space("s", SCOPE)["auth_version"] == 3
        assert len(resumed.tasks) == 1

        async def settled(predicate):
            async with asyncio.timeout(5):
                while True:
                    active = resumed.store.db.execute(
                        "SELECT count(*) FROM artist_claims WHERE state='active'"
                    ).fetchone()[0]
                    if not active and predicate():
                        return
                    await asyncio.sleep(.01)

        await settled(lambda: len(renderer.calls) == 1)
        assert resumed.store.get_artist_work("s", SCOPE, work["work_id"])["status"] == "review_pending"
        for _ in range(6):
            clock.advance(60)
            # The real Host worker polls the fake clock; no manual Artist call
            # or injected chat/completion is needed to continue the old plan.
            previous = resumed.store.db.execute("SELECT acked_through FROM artist_state").fetchone()[0]
            await settled(lambda: resumed.store.db.execute("SELECT acked_through FROM artist_state").fetchone()[0] > previous)
            if any(cap == "send.image" for cap, _ in effects):
                break
        assert sum(cap == "send.image" for cap, _ in effects) == 1
        assert len(renderer.calls) == 1 + edits and not resumed.errors
        assert resumed.store.audit_operations("s", SCOPE)[:3] == original_ops
        assert resumed.store.budget_usage("s", SCOPE)["render"]["consumed"] == 1 + edits
        assert resumed.store.budget_usage("s", SCOPE)["share"]["consumed"] == 1
        assert resumed.store.get_artist_work("s", SCOPE, work["work_id"])["brief"] == work["brief"]
        assert plugin.journal.db.execute("SELECT state FROM delivery").fetchone()[0] == "sent"
        await resumed.close()
        # Start another Host over the retained state. An immediate tick must not
        # repeat a render, a review, or the confirmed SDK send.
        count = len(effects)
        restarted = HostRuntime(plugin)
        await restarted.start()
        await asyncio.sleep(.15)
        assert len(renderer.calls) == 1 + edits
        assert sum(cap == "send.image" for cap, _ in effects) == 1
        assert all(cap == "chat.get_stream_by_user_id" for cap, _ in effects[count:])
        previous = restarted.store.db.execute("SELECT acked_through FROM artist_state").fetchone()[0]
        clock.advance(60)
        async with asyncio.timeout(5):
            while restarted.store.db.execute("SELECT acked_through FROM artist_state").fetchone()[0] <= previous:
                await asyncio.sleep(.01)
        assert len(renderer.calls) == 1 + edits and sum(cap == "send.image" for cap, _ in effects) == 1
        config.enabled = False
        await restarted.close()
        restarted = HostRuntime(plugin)
        await restarted.start()
        assert not restarted.artists and not restarted.store.get_space("s", SCOPE)["authorized"]
    finally:
        await disabled.close()
        if resumed is not None:
            await resumed.close()
        if restarted is not None:
            await restarted.close()
        plugin.journal.close()
