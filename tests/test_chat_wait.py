"""Chat interrupts only a proven successful wait, using the existing ledger."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
import asyncio

import pytest

from image_plugin_under_test.policy import Delegation, Limits, StoreError, canonical_json
from image_plugin_under_test.store import ArtistStore, SCHEMA_VERSION
from image_plugin_under_test.thinking import Thinking
from test_artist_flow import SCOPE as FLOW_SCOPE, studio as studio_fixture
from test_artist_state import SCOPE, ledger as ledger_fixture


studio = studio_fixture
ledger = ledger_fixture


def successful_wait(db, *, finish=True):
    """Persist exactly the pre-fix plan/claim layout, without schema changes."""
    db.enqueue_wake("s", SCOPE, event_id="idle-wait", kind="idle")
    batch = db.claim_wake("s", SCOPE, token="wait")
    db.reserve("s", SCOPE, request_id="wait-plan", action="think",
               params={"task": "plan"}, trigger_ref=str(batch["through"]),
               expected_auth_version=batch["auth_version"])
    db.mark_submitting("s", SCOPE, "wait-plan")
    db.settle("s", SCOPE, "wait-plan", outcome="succeeded", evidence=canonical_json({
        "ideas": [], "decision": {"action": "wait", "reason": "rest", "delay_seconds": 86400},
    }))
    if finish:
        db.finish_wake("s", SCOPE, token="wait", retry_after=86400)
    return batch


@pytest.mark.parametrize("before_finish", [False, True])
def test_chat_interrupts_wait_on_either_side_of_finish(ledger, before_finish):
    db, _ = ledger
    batch = successful_wait(db, finish=not before_finish)
    db.enqueue_wake("s", SCOPE, event_id="new-chat", kind="chat", source_ref="verified-message")
    if before_finish:
        db.finish_wake("s", SCOPE, token="wait", retry_after=86400)
    next_batch = db.claim_wake("s", SCOPE, token="chat")
    assert next_batch is not None and next_batch["through"] > batch["through"]
    assert [e["event_id"] for e in next_batch["events"]] == ["new-chat"]
    assert db.claim_wake("s", SCOPE, token="chat") == next_batch
    assert db.claim_wake("s", SCOPE, token="competitor") is None


def test_idle_and_completion_cannot_interrupt_wait(ledger):
    db, _ = ledger
    successful_wait(db)
    db.enqueue_wake("s", SCOPE, event_id="completion", kind="completion")
    db.enqueue_wake("s", SCOPE, event_id="idle", kind="idle")
    assert db.claim_wake("s", SCOPE, token="early") is None


@pytest.mark.parametrize("ahead", [63, 64])
def test_chat_interrupt_requires_chat_in_first_batch_without_skipping_backlog(ledger, ahead):
    db, clock = ledger
    successful_wait(db)
    backlog = [f"completion-{n}" for n in range(ahead)]
    for event_id in backlog:
        db.enqueue_wake("s", SCOPE, event_id=event_id, kind="completion")
    db.enqueue_wake("s", SCOPE, event_id="chat", kind="chat", source_ref="verified-message")
    batch = db.claim_wake("s", SCOPE, token="early")
    if ahead == 63:
        assert [e["event_id"] for e in batch["events"]] == backlog + ["chat"]
    else:
        assert batch is None
        clock.now += timedelta(days=1)
        batch = db.claim_wake("s", SCOPE, token="after-wait")
        assert [e["event_id"] for e in batch["events"]] == backlog
        db.finish_wake("s", SCOPE, token="after-wait")
        next_batch = db.claim_wake("s", SCOPE, token="next-batch")
        assert [e["event_id"] for e in next_batch["events"]] == ["chat"]


def test_old_wait_and_pending_chat_recover_after_reopen_and_new_grant(ledger):
    db, clock = ledger
    batch = successful_wait(db)
    db.enqueue_wake("s", SCOPE, event_id="pending-chat", kind="chat", source_ref="original-message")
    before = db.audit_operations("s", SCOPE)
    db.revoke("s", SCOPE, source_ref="operator-paused", granted_by="admin")
    with pytest.raises(StoreError, check=lambda exc: exc.code == "AUTH_REQUIRED"):
        db.claim_wake("s", SCOPE, token="revoked")
    db.grant("s", SCOPE, Delegation("renewed-consent", "admin", continuous=True))
    reopened = ArtistStore(db.path, clock=clock)
    try:
        reopened.recover_artist("s", SCOPE, offline=True)
        next_batch = reopened.claim_wake("s", SCOPE, token="after-upgrade")
        assert next_batch is not None and next_batch["auth_version"] > batch["auth_version"]
        assert next_batch["events"][0]["source_ref"] == "original-message"
        assert reopened.audit_operations("s", SCOPE) == before
        assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 6
    finally:
        reopened.close()


def test_later_failed_claim_cannot_borrow_old_successful_wait(ledger):
    db, clock = ledger
    successful_wait(db)
    clock.now += timedelta(days=1)
    db.enqueue_wake("s", SCOPE, event_id="later", kind="idle")
    db.claim_wake("s", SCOPE, token="failed")
    db.finish_wake("s", SCOPE, token="failed", acknowledge=False, retry_after=300)
    db.enqueue_wake("s", SCOPE, event_id="new-chat", kind="chat", source_ref="verified-message")
    assert db.claim_wake("s", SCOPE, token="early") is None
    clock.now += timedelta(seconds=300)
    assert db.claim_wake("s", SCOPE, token="due") is not None


@pytest.mark.parametrize("mutation", [
    "wrong-task", "not-wait", "bad-json", "wrong-through", "unknown", "wrong-auth", "wrong-binding",
])
def test_unproven_wait_keeps_backoff(ledger, mutation):
    db, _ = ledger
    successful_wait(db)
    # Synthetic corruption/uncertainty only; never read an existing state file.
    column, value = {
        "wrong-task": ("params_json", canonical_json({"task": "inspire"})),
        "not-wait": ("evidence", canonical_json({"decision": {"action": "create"}})),
        "bad-json": ("evidence", "not-json"),
        "wrong-through": ("trigger_ref", "unrelated"),
        "unknown": ("state", "unknown"),
        "wrong-auth": ("auth_version", 99),
        "wrong-binding": ("binding_id", "unrelated-service"),
    }[mutation]
    db.db.execute(f"UPDATE operations SET {column}=? WHERE request_id='wait-plan'", (value,))
    db.enqueue_wake("s", SCOPE, event_id="chat", kind="chat", source_ref="verified-message")
    assert db.claim_wake("s", SCOPE, token="early") is None


@pytest.mark.parametrize("last_state", ["released", "recovered", "active"])
def test_latest_claim_by_creation_order_controls_wait_proof(ledger, last_state):
    db, _ = ledger
    successful_wait(db)
    # A conservative ambiguity probe: a later claim has a smaller through. An
    # aggregate max(through) must never select the older finished wait instead.
    db.db.execute("INSERT INTO artist_claims VALUES(?,?,?,?,?)", ("s", "later-claim", 0, last_state, 1))
    db.enqueue_wake("s", SCOPE, event_id="chat", kind="chat", source_ref="verified-message")
    assert db.claim_wake("s", SCOPE, token="early") is None


def test_chat_interrupt_claim_is_atomic_between_connections(ledger):
    db, clock = ledger
    successful_wait(db)
    db.enqueue_wake("s", SCOPE, event_id="chat", kind="chat", source_ref="verified-message")
    barrier = Barrier(2)

    def claim(token):
        other = ArtistStore(db.path, clock=clock)
        try:
            barrier.wait(timeout=5)
            return other.claim_wake("s", SCOPE, token=token)
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(batch is not None for batch in pool.map(claim, ["one", "two"])) == 1


async def wait_plan(request):
    return {"ideas": [], "decision": {"action": "wait", "reason": "rest", "delay_seconds": 86400}}


@pytest.mark.parametrize("arrival", ["after-finish", "during-model", "before-finish"])
async def test_runtime_chat_interrupts_wait_without_replaying_original(studio, monkeypatch, arrival):
    runtime, store, _, model, renderer, sender = studio
    entered, release = asyncio.Event(), asyncio.Event()

    async def waiting(request):
        if arrival == "during-model":
            entered.set()
            await release.wait()
        return await wait_plan(request)

    runtime.thinking = Thinking(waiting)
    if arrival == "before-finish":
        finish = store.finish_wake

        def racing_finish(*args, **kwargs):
            store.enqueue_wake("s", FLOW_SCOPE, event_id="chat:late", kind="chat",
                               text="new inspiration", source_ref="late")
            return finish(*args, **kwargs)

        monkeypatch.setattr(store, "finish_wake", racing_finish)
    task = asyncio.create_task(runtime.tick())
    if arrival == "during-model":
        await asyncio.wait_for(entered.wait(), 2)
        await runtime.on_chat(message_id="late", text="new inspiration")
        release.set()
    assert (await task)["state"] == "waiting"
    if arrival == "after-finish":
        await runtime.on_chat(message_id="late", text="new inspiration")
    runtime.thinking = Thinking(model)
    assert (await runtime.run_once())["state"] == "planned"
    assert [e["source_ref"] for e in model.calls[0]["context"]["events"] if e["kind"] == "chat"] == ["late"]
    assert store.budget_usage("s", FLOW_SCOPE)["think"]["consumed"] == 2
    assert len(store.list_artist_works("s", FLOW_SCOPE)) == 1
    assert renderer.calls == sender.calls == []


@pytest.mark.parametrize("failure", ["error", "unknown", "cancel"])
async def test_chat_never_interrupts_failed_or_cancelled_model_backoff(studio, failure):
    runtime, store, clock, _, renderer, sender = studio
    entered = asyncio.Event()
    calls = []

    async def failing(request):
        calls.append(request)
        entered.set()
        if failure == "cancel":
            await asyncio.Event().wait()
        if failure == "unknown":
            raise ConnectionError("synthetic lost model response")
        raise RuntimeError("synthetic known model failure")

    runtime.thinking = Thinking(failing)
    task = asyncio.create_task(runtime.tick())
    await asyncio.wait_for(entered.wait(), 2)
    if failure == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert (await task)["reason"] == ("THINK_UNKNOWN" if failure == "unknown" else "THINK_FAILED")
    await runtime.on_chat(message_id="late", text="new inspiration")
    assert (await runtime.run_once())["state"] == "idle"
    clock.advance(299)
    assert (await runtime.run_once())["state"] == "idle" and len(calls) == 1
    if failure != "error":
        clock.advance(1)
        assert (await runtime.run_once())["reason"] == "THINK_UNKNOWN" and len(calls) == 1
        assert store.budget_usage("s", FLOW_SCOPE)["think"]["held"] == 1
    assert renderer.calls == sender.calls == []


async def test_interrupted_wait_still_checks_current_budget(studio):
    runtime, store, _, model, renderer, sender = studio
    runtime.thinking = Thinking(wait_plan)
    assert (await runtime.tick())["state"] == "waiting"
    store.set_limits("s", FLOW_SCOPE, Limits(think=1, render=2, share=1, max_inflight=2, max_revisions=1))
    await runtime.on_chat(message_id="late", text="new inspiration")
    runtime.thinking = Thinking(model)
    assert (await runtime.run_once())["reason"] == "BUDGET_EXCEEDED"
    await runtime.on_chat(message_id="later", text="another inspiration")
    assert (await runtime.run_once())["state"] == "idle"
    assert model.calls == renderer.calls == sender.calls == []


@pytest.mark.parametrize("new_state", ["failed", "unknown"])
def test_newer_failed_or_unknown_operation_cannot_borrow_old_wait(ledger, new_state):
    db, _ = ledger
    batch = successful_wait(db)
    db.reserve("s", SCOPE, request_id="newer-plan", action="think", params={"task": "plan"},
               trigger_ref=str(batch["through"]), expected_auth_version=batch["auth_version"])
    db.mark_submitting("s", SCOPE, "newer-plan")
    if new_state == "failed":
        db.settle("s", SCOPE, "newer-plan", outcome="failed", evidence="synthetic known failure")
    else:
        db.mark_unknown("s", SCOPE, "newer-plan", reason="synthetic lost receipt")
    before = db.audit_operations("s", SCOPE)
    backoff = db.db.execute("SELECT backoff_until FROM artist_state WHERE space_id='s'").fetchone()[0]
    db.enqueue_wake("s", SCOPE, event_id="new-chat", kind="chat", source_ref="verified-message")
    assert db.claim_wake("s", SCOPE, token="early") is None
    assert db.audit_operations("s", SCOPE) == before
    assert db.db.execute("SELECT backoff_until FROM artist_state WHERE space_id='s'").fetchone()[0] == backoff
    assert before[0]["state"] == "succeeded" and before[1]["state"] == new_state


async def test_regranted_runtime_uses_epoch_three_for_new_think_not_old_wait(studio):
    from image_plugin_under_test.artist import Artist

    runtime, store, clock, model, renderer, sender = studio
    old_calls = []

    async def initial_wait(request):
        old_calls.append(request)
        return await wait_plan(request)

    runtime.thinking = Thinking(initial_wait)
    assert (await runtime.tick())["state"] == "waiting"
    assert await runtime.on_chat(message_id="original-pending-chat", text="new inspiration")
    old_operation = store.audit_operations("s", FLOW_SCOPE)[0]
    assert old_operation["auth_version"] == 1
    store.revoke("s", FLOW_SCOPE, source_ref="operator-paused", granted_by="admin")
    store.grant("s", FLOW_SCOPE, Delegation("renewed-consent", "admin", continuous=True))
    reopened = ArtistStore(store.path, clock=clock)
    try:
        restored = Artist(reopened, "s", FLOW_SCOPE, Thinking(model), renderer, sender)
        await restored.recover(offline=True)
        assert (await restored.run_once())["state"] == "planned"
        operations = reopened.audit_operations("s", FLOW_SCOPE)
        assert len(operations) == 2 and [op["auth_version"] for op in operations] == [1, 3]
        assert operations[0] == old_operation
        assert operations[1]["request_id"] != old_operation["request_id"]
        assert operations[1]["state"] == "succeeded" and operations[1]["params"]["task"] == "plan"
        assert len(old_calls) == len(model.calls) == 1
        assert [e["source_ref"] for e in model.calls[0]["context"]["events"]] == ["original-pending-chat"]
        assert reopened.budget_usage("s", FLOW_SCOPE)["think"]["consumed"] == 2
        assert renderer.calls == sender.calls == []
    finally:
        reopened.close()
