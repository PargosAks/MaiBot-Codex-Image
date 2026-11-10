"""Existing artwork progresses on ticks independently of the idle planning timer."""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from image_plugin_under_test.creation.artist import Artist
from image_plugin_under_test.creation.thinking import Thinking
from image_plugin_under_test.domain.store import ArtistStore
from protocol_fixture import PNG
from support.artist_fakes import SCOPE
from support.artist_flow import step, studio as studio


IDLE_SECONDS = 3600
TICK_SECONDS = 5


def make_artist(store, model, renderer, sender):
    return Artist(store, "s", SCOPE, Thinking(model), renderer, sender, idle_seconds=IDLE_SECONDS)


@pytest.fixture
def slow_idle(studio):
    _, store, clock, model, renderer, sender = studio
    return make_artist(store, model, renderer, sender), store, clock, model, renderer, sender


@asynccontextmanager
async def restarted(flow):
    _, store, clock, model, renderer, sender = flow
    path = store.path
    store.close()
    reopened = ArtistStore(path, clock=clock)
    try:
        artist = make_artist(reopened, model, renderer, sender)
        await artist.recover(offline=True)
        yield artist, reopened, clock, model, renderer, sender
    finally:
        reopened.close()


async def soon(flow, state):
    """A stage may use up to three scheduler ticks, never the next idle wake."""
    results = []
    for _ in range(3):
        result = await step(flow, TICK_SECONDS)
        results.append(result)
        if result["state"] == state:
            return result
    pytest.fail(f"Expected {state!r} within 15 seconds, got {results!r}")


@pytest.mark.parametrize(
    ("single_work", "revisions", "share_outcome"),
    [
        (False, 0, "succeeded"),
        (False, 2, "succeeded"),
        (False, 0, "wait"),
        (False, 0, "failed"),
        (False, 0, "unknown"),
        (True, 0, "succeeded"),
        (True, 1, "succeeded"),
        (True, 1, "unknown"),
    ],
)
async def test_ticks_progress_from_plan_through_share_after_restart(
    slow_idle, monkeypatch, single_work, revisions, share_outcome,
):
    artist, store, _, model, renderer, sender = slow_idle
    if single_work:
        store.configure_single_work("s", SCOPE, enabled=True)
    model.revise, model.share = revisions, share_outcome != "wait"
    renderer.pending = True
    sender.lose_receipt = share_outcome == "unknown"
    if share_outcome == "failed":
        send = sender.send

        async def rejected(**kwargs):
            await send(**kwargs)
            return False

        monkeypatch.setattr(sender, "send", rejected)

    polled_jobs = []
    status = renderer.status

    async def poll(*, scope, job_id):
        polled_jobs.append(job_id)
        return await status(scope=scope, job_id=job_id)

    monkeypatch.setattr(renderer, "status", poll)

    plan = await artist.tick()
    assert plan["state"] == "planned"
    work_id = plan["work_id"]
    assert renderer.calls == sender.calls == []
    await soon(slow_idle, "creating")
    original, = renderer.jobs.values()
    assert len(renderer.calls) == 1 and renderer.calls[0]["source"] is None
    assert store.get_operation("s", SCOPE, original.request_id)["state"] == "accepted"
    assert not polled_jobs

    calls_before_poll = len(model.calls)
    await soon(slow_idle, "creating")
    assert polled_jobs == [original.job_id]
    assert len(model.calls) == calls_before_poll and len(renderer.calls) == 1 and not sender.calls
    assert not store.list_versions("s", SCOPE, work_id)

    # Keep the fake backend job but lose all in-memory Artist scheduling state.
    renderer.complete()
    async with restarted(slow_idle) as resumed:
        _, store, *_ = resumed
        await soon(resumed, "review_pending")
        assert polled_jobs[-1] == original.job_id and len(renderer.calls) == 1
        first, = store.list_versions("s", SCOPE, work_id)
        assert first["job_id"] == original.job_id
        assert not sender.calls

        renderer.pending = False
        for _ in range(revisions):
            assert (await soon(resumed, "planned"))["reason"] == "revision"
            assert not sender.calls
            await soon(resumed, "review_pending")
        await soon(resumed, "finished")
        assert not sender.calls
        versions = store.list_versions("s", SCOPE, work_id)
        assert len(versions) == 1 + revisions
        assert len(renderer.calls) == 1 + revisions
        for parent, child, edit in zip(versions[:-1], versions[1:], renderer.calls[1:], strict=True):
            assert child["parent_id"] == parent["version_id"]
            assert edit["source"]["job_id"] == parent["job_id"]
            assert edit["source"]["data"] == PNG
        assert sum(call["task"] == "review" for call in model.calls) == 1 + revisions

    # A new runtime must also discover a reviewed work's unmade sharing decision.
    send_count = int(share_outcome != "wait")
    async with restarted(resumed) as ready:
        result_state = {
            "succeeded": "shared", "failed": "share_failed", "unknown": "share_unknown", "wait": "waiting",
        }
        await soon(ready, result_state[share_outcome])
        assert len(sender.calls) == send_count
        if send_count:
            scope, sent_version = sender.calls[0]
            assert scope == SCOPE and sent_version["version_id"] == versions[-1]["version_id"]

    async with restarted(ready) as settled:
        _, store, *_ = settled
        if single_work:
            limits = store.single_work_status("s", SCOPE)
            assert limits["work_id"] == work_id
            assert (limits["create_used"], limits["edit_used"], limits["share_used"]) == (True, bool(revisions), True)
        model_count = len(model.calls)
        for _ in range(3):
            await step(settled, TICK_SECONDS)
        assert len(model.calls) == model_count
        await step(settled, 86400)
        if single_work:
            assert len(model.calls) == model_count
        assert len(renderer.calls) == 1 + revisions and len(sender.calls) == send_count
        assert len(store.list_artist_works("s", SCOPE)) == 1
        shares = [op["state"] for op in store.audit_operations("s", SCOPE) if op["action"] == "share"]
        assert shares == ([share_outcome] if send_count else [])


@pytest.mark.parametrize("decision", ["wait", "defer"])
async def test_progress_wake_honors_model_delay_and_review_backoff(slow_idle, decision):
    artist, store, _, model, renderer, sender = slow_idle
    assert (await artist.tick())["state"] == "planned"
    model.plan_action = "wait" if decision == "wait" else None
    model.defer = decision == "defer"
    await soon(slow_idle, "waiting" if decision == "wait" else "deferred")
    model.plan_action = None

    plan_calls = len(model.calls)
    for seconds in (5, 10, 584):  # 599 seconds after the model's requested delay started.
        await step(slow_idle, seconds)
        assert len(model.calls) == plan_calls and not renderer.calls and not sender.calls
    result = await step(slow_idle, 1)
    if result["state"] != "review_pending":
        await soon(slow_idle, "review_pending")
    assert len(renderer.calls) == 1

    model.review_errors = 1
    assert (await soon(slow_idle, "blocked"))["reason"] == "THINK_FAILED"
    work, = store.list_artist_works("s", SCOPE)
    assert work["status"] == "review_pending"
    failed_review_calls = len(model.calls)
    for seconds in (5, 10, 284):  # 299 seconds into the existing failure backoff.
        await step(slow_idle, seconds)
        assert len(model.calls) == failed_review_calls and len(renderer.calls) == 1 and not sender.calls
    result = await step(slow_idle, 1)
    if result["state"] != "finished":
        await soon(slow_idle, "finished")
    assert len(model.calls) == failed_review_calls + 1
    assert len(renderer.calls) == 1 and not sender.calls


@pytest.mark.parametrize("outcome", ["failed", "unknown_turn", "unknown_submit"])
async def test_progress_wake_does_not_replay_failed_or_unknown_render(slow_idle, monkeypatch, outcome):
    artist, store, clock, model, renderer, sender = slow_idle
    store.configure_single_work("s", SCOPE, enabled=True)
    renderer.pending = True
    renderer.lose_receipt = renderer.lookup_missing = outcome == "unknown_submit"
    model.share = True

    polled_jobs = []
    status = renderer.status

    async def poll(*, scope, job_id):
        polled_jobs.append(job_id)
        return await status(scope=scope, job_id=job_id)

    def backoff(db):
        value, = db.db.execute("SELECT backoff_until FROM artist_state WHERE space_id='s'").fetchone()
        return datetime.fromisoformat(value)

    monkeypatch.setattr(renderer, "status", poll)
    assert (await artist.tick())["state"] == "planned"
    retry_at = None
    if outcome == "unknown_submit":
        assert (await soon(slow_idle, "blocked"))["reason"] == "SUBMIT_UNCONFIRMED"
    else:
        await soon(slow_idle, "creating")
        request_id, = renderer.jobs
        original = renderer.jobs[request_id]
        renderer.jobs[request_id] = replace(
            original, state="failed" if outcome == "failed" else "outcome_unknown",
        )
        if outcome == "failed":
            await soon(slow_idle, "abandoned")
        else:
            assert (await soon(slow_idle, "blocked"))["reason"] == "RENDER_OUTCOME_UNKNOWN"
            assert polled_jobs == [original.job_id]
            retry_at = backoff(store)
            assert retry_at > clock() + timedelta(seconds=3 * TICK_SECONDS)

    request_id, = renderer.jobs
    expected = "failed" if outcome == "failed" else "unknown"

    def assert_original_render(db):
        op, = [row for row in db.audit_operations("s", SCOPE) if row["category"] == "render"]
        call, = renderer.calls
        assert op["request_id"] == call["request_id"] == request_id and op["state"] == expected
        if expected == "unknown":
            assert db.budget_usage("s", SCOPE)["render"] == {"held": 1, "consumed": 0, "total": 1}
        assert not sender.calls

    assert_original_render(store)
    async with restarted(slow_idle) as resumed:
        _, store, *_ = resumed
        if retry_at is not None:
            # 重启不清除退避；到期只读取原 job，未见明确终态仍占用原额度。
            assert backoff(store) == retry_at
            model_count = len(model.calls)
            assert (await step(resumed, TICK_SECONDS))["state"] == "idle"
            seconds_left = int((retry_at - clock()).total_seconds())
            assert (await step(resumed, seconds_left - 1))["state"] == "idle"
            assert polled_jobs == [original.job_id] and len(model.calls) == model_count
            assert_original_render(store)

            assert await step(resumed, 1) == {"state": "blocked", "reason": "RENDER_OUTCOME_UNKNOWN"}
            assert polled_jobs == [original.job_id, original.job_id] and len(model.calls) == model_count
            assert store.get_operation("s", SCOPE, request_id)["job_id"] == original.job_id
            assert_original_render(store)

        for seconds in (5, 5, 5, 300, IDLE_SECONDS, 86400):
            await step(resumed, seconds)
            assert_original_render(store)
        work, = store.list_artist_works("s", SCOPE)
        assert not store.list_versions("s", SCOPE, work["work_id"])
        limits = store.single_work_status("s", SCOPE)
        assert limits["create_used"] and not limits["edit_used"] and not limits["share_used"]


async def test_no_work_keeps_the_original_idle_interval(slow_idle):
    _, store, clock, _, renderer, sender = slow_idle
    calls = []

    async def rest(request):
        calls.append(clock())
        assert request["task"] == "plan" and not request["context"]["works"]
        return {"ideas": [], "decision": {"action": "wait", "reason": "暂时没有想画的主题"}}

    artist = make_artist(store, rest, renderer, sender)
    assert (await artist.tick())["state"] == "waiting"
    for seconds in (5, 5, 5, IDLE_SECONDS - 16):
        clock.advance(seconds)
        assert (await artist.tick())["state"] == "idle"
        assert len(calls) == 1
    clock.advance(1)
    assert (await artist.tick())["state"] == "waiting"
    assert len(calls) == 2 and (calls[1] - calls[0]).total_seconds() == IDLE_SECONDS
    assert not store.list_artist_works("s", SCOPE) and not renderer.calls and not sender.calls
