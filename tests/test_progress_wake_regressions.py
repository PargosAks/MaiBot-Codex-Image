"""Artist progress wake regressions with a fake clock, backend and sender."""

from dataclasses import replace

import pytest

from image_plugin_under_test.creation.artist import Artist
from image_plugin_under_test.creation.thinking import Thinking
from image_plugin_under_test.domain.policy import Delegation, Limits
from image_plugin_under_test.domain.store import ArtistStore
from protocol_fixture import PNG
from support.artist_fakes import SCOPE
from support.artist_flow import step, studio as studio


IDLE_SECONDS = 3600
TICK_SECONDS = 5


def _artist(store, model, renderer, sender):
    return Artist(store, "s", SCOPE, Thinking(model), renderer, sender, idle_seconds=IDLE_SECONDS)


@pytest.fixture
def progress_flow(studio):
    _, store, clock, model, renderer, sender = studio
    return _artist(store, model, renderer, sender), store, clock, model, renderer, sender


async def _finish(flow):
    assert (await step(flow, 0))["state"] == "planned"
    assert (await step(flow, TICK_SECONDS))["state"] == "review_pending"
    assert (await step(flow, TICK_SECONDS))["state"] == "finished"


def _watch_status(monkeypatch, renderer, clock):
    calls = []
    status = renderer.status

    async def record_status(*, scope, job_id):
        calls.append((clock(), job_id))
        return await status(scope=scope, job_id=job_id)

    monkeypatch.setattr(renderer, "status", record_status)
    return calls


def _publish_early_image(monkeypatch, renderer):
    """The original job has an image, but its known running turn still holds a slot."""
    renderer.pending = True
    submit = renderer.submit

    async def submit_with_image(**kwargs):
        receipt = await submit(**kwargs)
        assert receipt.state == "running"
        receipt = replace(receipt, image=PNG)
        renderer.jobs[receipt.request_id] = receipt
        return receipt

    monkeypatch.setattr(renderer, "submit", submit_with_image)


async def test_recovered_progress_claim_reuses_saved_plan_when_idle_becomes_due(progress_flow, monkeypatch):
    _, store, clock, model, renderer, sender = progress_flow
    await _finish(progress_flow)

    class PowerLoss(Exception):
        pass

    def lose_acknowledgement(*args, **kwargs):
        # The model's wait has already been recorded, but its backoff and the
        # claim acknowledgement would be written together by finish_wake.
        raise PowerLoss

    plan_calls = sum(call["task"] == "plan" for call in model.calls)
    with monkeypatch.context() as crash:
        crash.setattr(store, "finish_wake", lose_acknowledgement)
        with pytest.raises(PowerLoss):
            await step(progress_flow, IDLE_SECONDS - 2 * TICK_SECONDS - 1)
    assert sum(call["task"] == "plan" for call in model.calls) == plan_calls + 1
    usage = store.budget_usage("s", SCOPE)["think"]

    reopened = ArtistStore(store.path, clock=clock)
    try:
        restarted = _artist(reopened, model, renderer, sender)
        await restarted.recover(offline=True)
        resumed = restarted, reopened, clock, model, renderer, sender
        assert (await step(resumed, 1))["state"] == "waiting"  # Idle becomes due during recovery.
        assert (await step(resumed, TICK_SECONDS))["state"] == "idle"  # The saved wait took effect.
        assert reopened.budget_usage("s", SCOPE)["think"] == usage
        assert sum(call["task"] == "plan" for call in model.calls) == plan_calls + 1
        assert len(renderer.calls) == 1 and not sender.calls
    finally:
        reopened.close()


async def test_finished_unknown_render_backs_off_without_reopening_the_work(progress_flow, monkeypatch):
    _, store, clock, model, renderer, sender = progress_flow
    renderer.unknown_artifact = True
    await _finish(progress_flow)
    status_calls = _watch_status(monkeypatch, renderer, clock)
    request_id, = renderer.jobs
    work, = store.list_artist_works("s", SCOPE)
    usage = store.budget_usage("s", SCOPE)

    await step(progress_flow, TICK_SECONDS)
    assert len(status_calls) == 1
    for seconds in (15, 15, 269):
        await step(progress_flow, seconds)
    assert len(status_calls) == 1
    assert store.get_operation("s", SCOPE, request_id)["state"] == "unknown"
    assert store.budget_usage("s", SCOPE) == usage
    assert store.list_artist_works("s", SCOPE) == [work]
    assert len(renderer.calls) == 1 and not sender.calls

    # Backoff does not discard the old job or require a new render to resolve it.
    renderer.complete()
    await step(progress_flow, TICK_SECONDS)
    assert len(status_calls) == 2
    assert status_calls[0][1] == status_calls[1][1] == renderer.jobs[request_id].job_id
    assert store.get_operation("s", SCOPE, request_id)["state"] == "succeeded"


async def test_unchanged_running_render_waits_at_least_15_seconds_between_polls(progress_flow, monkeypatch):
    _, _, clock, _, renderer, sender = progress_flow
    renderer.pending = True
    status_calls = _watch_status(monkeypatch, renderer, clock)

    assert (await step(progress_flow, 0))["state"] == "planned"
    assert (await step(progress_flow, TICK_SECONDS))["state"] == "creating"
    await step(progress_flow, 24)  # First status at :29, just before a wall clock boundary.
    assert len(status_calls) == 1
    for _ in range(2):
        await step(progress_flow, TICK_SECONDS)
    assert len(status_calls) == 1
    # Allow normal Host tick delay; require a lower bound, not an exact arrival time.
    for _ in range(3):
        if len(status_calls) == 2:
            break
        await step(progress_flow, TICK_SECONDS)
    first, second = status_calls
    assert first[1] == second[1]
    assert (second[0] - first[0]).total_seconds() >= 15
    assert len(renderer.calls) == 1 and not sender.calls


async def test_finished_receipt_resolution_allows_sharing_on_the_next_tick(progress_flow, monkeypatch):
    _, store, clock, model, renderer, sender = progress_flow
    _publish_early_image(monkeypatch, renderer)
    model.share = True
    clock.advance(TICK_SECONDS)
    await _finish(progress_flow)  # :15
    request_id, = renderer.jobs
    assert store.get_operation("s", SCOPE, request_id)["state"] == "accepted"

    renderer.complete()
    await step(progress_flow, 15)  # :30, reconcile the old request.
    assert store.get_operation("s", SCOPE, request_id)["state"] == "succeeded"
    assert not sender.calls
    await step(progress_flow, TICK_SECONDS)  # :35, still in the same old time bucket.
    assert len(sender.calls) == 1
    _, sent = sender.calls[0]
    assert sent["job_id"] == renderer.jobs[request_id].job_id
    assert len(renderer.calls) == 1


async def test_review_from_revoked_grant_does_not_trigger_paid_progress_plan(progress_flow):
    _, store, _, model, renderer, sender = progress_flow
    model.share = True
    await _finish(progress_flow)
    work, = store.list_artist_works("s", SCOPE)
    assert store.version_accepted("s", SCOPE, work["best_version"])

    store.revoke("s", SCOPE, source_ref="revoke", granted_by="admin")
    store.grant("s", SCOPE, Delegation("replacement-grant", "admin", continuous=True))
    assert not store.version_accepted("s", SCOPE, work["best_version"])
    model_count = len(model.calls)
    usage = store.budget_usage("s", SCOPE)
    for _ in range(4):
        await step(progress_flow, TICK_SECONDS)
    assert len(model.calls) == model_count
    assert store.budget_usage("s", SCOPE) == usage
    assert len(renderer.calls) == 1 and not sender.calls


async def test_early_image_with_sole_slot_keeps_polling_running_job_before_review(progress_flow, monkeypatch):
    _, store, clock, model, renderer, sender = progress_flow
    limits = Limits(think=30, render=2, share=1, max_inflight=1, max_revisions=1)
    store.set_global_limits(limits)
    store.set_limits("s", SCOPE, limits)
    store.configure_single_work("s", SCOPE, enabled=True)
    _publish_early_image(monkeypatch, renderer)
    status_calls = _watch_status(monkeypatch, renderer, clock)

    assert (await step(progress_flow, 0))["state"] == "planned"
    assert (await step(progress_flow, TICK_SECONDS))["state"] == "review_pending"
    request_id, = renderer.jobs
    usage = store.budget_usage("s", SCOPE)["think"]
    await step(progress_flow, TICK_SECONDS)
    assert store.get_operation("s", SCOPE, request_id)["state"] == "accepted"
    assert store.budget_usage("s", SCOPE)["think"] == usage
    assert not any(call["task"] == "review" for call in model.calls)

    renderer.complete()
    for _ in range(5):
        await step(progress_flow, TICK_SECONDS)
        work, = store.list_artist_works("s", SCOPE)
        if work["status"] == "finished":
            break
    assert work["status"] == "finished"
    assert store.get_operation("s", SCOPE, request_id)["state"] == "succeeded"
    assert {job_id for _, job_id in status_calls} == {renderer.jobs[request_id].job_id}
    assert sum(call["task"] == "review" for call in model.calls) == 1
    assert len(renderer.calls) == 1 and not sender.calls
