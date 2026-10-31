"""Stage 5 recovery boundaries with actual ledger and injected side effects."""

from threading import Event
import asyncio

import pytest

from image_plugin_under_test.artist import Artist
from image_plugin_under_test.policy import Delegation, StoreError
from image_plugin_under_test.store import ArtistStore
from image_plugin_under_test.thinking import Thinking
from test_artist_flow import SCOPE, step, studio  # noqa: F401


def test_external_rendering_is_explicit_and_does_not_enable_live_sending(studio):  # noqa: F811
    _, store, _, model, renderer, sender = studio
    renderer.simulation = False
    with pytest.raises(StoreError, check=lambda exc: exc.code == "SIMULATION_REQUIRED"):
        Artist(store, "s", SCOPE, Thinking(model), renderer, sender)
    Artist(store, "s", SCOPE, Thinking(model), renderer, sender, allow_network_renderer=True)
    with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_ARGUMENT"):
        Artist(store, "s", SCOPE, Thinking(model), renderer, sender, allow_network_renderer=1)
    sender.simulation = False
    with pytest.raises(StoreError, check=lambda exc: exc.code == "SIMULATION_REQUIRED"):
        Artist(store, "s", SCOPE, Thinking(model), renderer, sender, allow_network_renderer=True)


@pytest.mark.parametrize("review", [False, True])
async def test_new_wakes_cannot_replay_unknown_thought(studio, review):  # noqa: F811
    runtime, store, clock, model, renderer, sender = studio
    if review:
        await runtime.tick()
        await step(studio)
    calls = []

    async def lost(request):
        calls.append(request)
        raise ConnectionError("Synthetic response loss")

    runtime.thinking = Thinking(lost)
    assert (await step(studio))["reason"] == "THINK_UNKNOWN"
    clock.advance(86400)
    await runtime.on_chat(message_id="new-after-loss", text="这条新消息不能解锁未知调用")
    assert (await runtime.tick())["reason"] == "THINK_UNKNOWN"
    assert len(calls) == 1
    assert store.budget_usage("s", SCOPE)["think"]["held"] == 1
    assert len(renderer.calls) == int(review) and sender.calls == []


async def test_completed_review_is_reused_after_restart_before_review_record(studio, monkeypatch):  # noqa: F811
    runtime, store, clock, model, renderer, sender = studio
    await runtime.tick()
    await step(studio)
    record = store.record_review

    def interrupted(*args, **kwargs):
        raise RuntimeError("Synthetic crash before persisting the review row")

    monkeypatch.setattr(store, "record_review", interrupted)
    with pytest.raises(RuntimeError, match="Synthetic crash"):
        await step(studio)
    assert len([c for c in model.calls if c["task"] == "review"]) == 1
    monkeypatch.setattr(store, "record_review", record)
    reopened = ArtistStore(store.path, clock=clock)
    try:
        restored = Artist(reopened, "s", SCOPE, Thinking(model), renderer, sender)
        await restored.recover(offline=True)
        clock.advance()
        await restored.on_chat(message_id="new-watermark", text="下幅画可以画窗台")
        assert (await restored.tick())["state"] == "finished"
        assert len([c for c in model.calls if c["task"] == "review"]) == 1
        work = reopened.list_artist_works("s", SCOPE)[0]
        assert len(reopened.list_reviews("s", SCOPE, work["work_id"])) == 1
        assert len(renderer.calls) == 1 and sender.calls == []
    finally:
        reopened.close()


async def test_revoked_recovery_can_only_collect_original_result(studio):  # noqa: F811
    runtime, store, _, model, renderer, sender = studio
    renderer.pending = True
    await runtime.tick()
    await step(studio)
    before = len(model.calls)
    store.revoke("s", SCOPE, source_ref="withdrawn", granted_by="trusted-admin")
    renderer.complete()
    with pytest.raises(StoreError, check=lambda exc: exc.code == "OFFLINE_REQUIRED"):
        await runtime.recover(reconcile=True)
    assert (await runtime.recover(offline=True, reconcile=True))[0]["state"] == "review_pending"
    assert store.budget_usage("s", SCOPE)["render"]["held"] == 0
    assert len(model.calls) == before and len(renderer.calls) == 1 and sender.calls == []
    with pytest.raises(StoreError, check=lambda exc: exc.code == "AUTH_REQUIRED"):
        await runtime.tick()
    assert len(model.calls) == before


async def test_persisted_review_cannot_act_after_reauthorization(studio):  # noqa: F811
    runtime, store, _, model, renderer, sender = studio
    renderer.unknown_artifact = True
    model.revise = 1
    await runtime.tick()
    await step(studio)
    assert (await step(studio))["reason"] == "RENDER_UNRESOLVED"
    work = store.list_artist_works("s", SCOPE)[0]
    assert len(store.list_reviews("s", SCOPE, work["work_id"])) == 1
    before = len(model.calls)
    store.revoke("s", SCOPE, source_ref="withdrawn", granted_by="admin")
    store.grant("s", SCOPE, Delegation("new-consent", "admin", continuous=True))
    renderer.complete()
    assert (await step(studio)) == {"state": "blocked", "reason": "AUTH_CHANGED"}
    assert store.get_artist_work("s", SCOPE, work["work_id"])["status"] == "review_pending"
    assert len(model.calls) == before and len(renderer.calls) == 1 and sender.calls == []


async def test_repeated_cancel_does_not_release_cycle_before_unknown_write(studio, monkeypatch):  # noqa: F811
    runtime, store, _, _, renderer, _ = studio
    entered, release, finished = Event(), Event(), Event()
    original = store.mark_unknown

    def delayed(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(5)
            return original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(store, "mark_unknown", delayed)
    renderer.lose_receipt = True
    await runtime.tick()
    task = asyncio.create_task(step(studio))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and not finished.is_set()
        assert store.db.execute("SELECT count(*) FROM artist_claims WHERE state='active'").fetchone()[0] == 1
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert finished.is_set()
    assert store.db.execute("SELECT count(*) FROM artist_claims WHERE state='active'").fetchone()[0] == 0
    assert store.budget_usage("s", SCOPE)["render"]["held"] == 1
    assert (await step(studio))["state"] == "review_pending"
    assert len(renderer.calls) == 1
