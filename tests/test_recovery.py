"""Read-only reconciliation with real isolated SQLite and an injected lookup only."""

from threading import Event
import asyncio
import copy
import hashlib
import json

import pytest

from image_plugin_under_test.bridge import BridgeError
from image_plugin_under_test.policy import Delegation, Limits, Scope, StoreError
from image_plugin_under_test.recovery import ReadOnlyRecovery
from image_plugin_under_test.store import ArtistStore


SCOPE = Scope("test", "verified-stream", "verified-owner")
OTHER = Scope("test", "another-stream", "another-owner")
PROMPT = "frozen 描述 with intentional spaces  "


@pytest.fixture
def ledger(tmp_path):
    store = ArtistStore(tmp_path / "isolated.sqlite3")
    store.create_space("s", SCOPE, binding_id="synthetic-service-installation")
    store.grant("s", SCOPE, Delegation("explicit-consent", "trusted-adapter", continuous=True))
    limits = Limits(think=20, render=20, share=20, max_inflight=20, max_revisions=5)
    store.set_global_limits(limits)
    store.set_limits("s", SCOPE, limits)
    store.create_work("s", SCOPE, "work")
    yield store
    store.close()


def attempt(store, *, action="submit", state="submitting", params=None):
    params = {"prompt": PROMPT} if params is None else params
    record = store.reserve("s", SCOPE, request_id="request", action=action, params=params, work_id="work")
    if state == "reserved":
        return record
    store.mark_submitting("s", SCOPE, "request")
    if state == "accepted":
        store.mark_accepted("s", SCOPE, "request", job_id="job")
    elif state == "unknown":
        store.mark_unknown("s", SCOPE, "request", reason="submit response lost")
    elif state in {"succeeded", "failed"}:
        store.settle("s", SCOPE, "request", outcome=state, evidence="known terminal", job_id="job")
    return store.get_operation("s", SCOPE, "request")


class LookupOnly:
    def __init__(self, *, status="queued", turn_status=None, failure=None, found=True):
        self.status, self.turn_status = status, turn_status
        self.failure, self.found, self.calls = failure, found, []

    async def lookup(self, scope, request_id, *, expected_operation, expected_digest, expected_job_id=None):
        self.calls.append(
            (copy.deepcopy(scope), request_id, expected_operation, expected_digest, expected_job_id)
        )
        if self.failure:
            raise self.failure
        if not self.found:
            return {"found": False}
        return {
            "found": True,
            "operation": expected_operation,
            "request_id": request_id,
            "request_digest": expected_digest,
            "job": {
                "job_id": "job",
                "status": self.status,
                "turn_status": self.turn_status,
                "error_code": None,
                "artifacts": [{"asset_id": "synthetic-artifact"}] if self.status == "artifact_ready" else [],
            },
        }

    async def submit(self, *args, **kwargs):
        pytest.fail("Recovery must not submit or generate")

    async def call(self, *args, **kwargs):
        pytest.fail("Recovery must not bypass validated lookup")

    async def result(self, *args, **kwargs):
        pytest.fail("Recovery must not fetch an image")

    async def send(self, *args, **kwargs):
        pytest.fail("Recovery must not send")


@pytest.mark.parametrize(
    "local,status,turn_status,expected",
    [
        ("submitting", "queued", None, "accepted"),
        ("unknown", "starting", None, "accepted"),
        ("accepted", "running", "inProgress", "accepted"),
        ("unknown", "completed", "completed", "succeeded"),
        ("accepted", "failed", "failed", "failed"),
        ("submitting", "outcome_unknown", None, "unknown"),
        ("accepted", "artifact_ready", None, "unknown"),
        ("unknown", "artifact_ready", "inProgress", "accepted"),
        ("submitting", "artifact_ready", "completed", "succeeded"),
        ("unknown", "artifact_ready", "failed", "failed"),
        ("unknown", "artifact_ready", "unrecognized", "unknown"),
    ],
)
async def test_lookup_binds_original_job_without_generation(ledger, local, status, turn_status, expected):
    before = attempt(ledger, state=local)
    bridge = LookupOnly(status=status, turn_status=turn_status)
    result = await ReadOnlyRecovery(ledger, bridge).reconcile("s", SCOPE, "request")
    assert result["state"] == expected and result["job_id"] == "job"
    assert len(bridge.calls) == 1
    assert bridge.calls[0] == (
        json.loads(SCOPE.snapshot()),
        "request",
        "submit",
        hashlib.sha256(PROMPT.encode()).hexdigest(),
        before["job_id"],
    )
    assert ledger.budget_usage("s", SCOPE)["render"] == (
        {"held": 0, "consumed": 1, "total": 1}
        if expected in {"succeeded", "failed"}
        else {"held": 1, "consumed": 0, "total": 1}
    )
    assert len(ledger.audit_operations("s", SCOPE)) == 1
    assert ledger.list_versions("s", SCOPE, "work") == []


@pytest.mark.parametrize("turn_status", ["running", "failed", "inProgress"])
async def test_contradictory_completed_receipt_keeps_unknown_hold(ledger, turn_status):
    attempt(ledger, state="accepted")
    bridge = LookupOnly(status="completed", turn_status=turn_status)
    with pytest.raises(StoreError, check=lambda exc: exc.code == "PROTOCOL_ERROR"):
        await ReadOnlyRecovery(ledger, bridge).reconcile("s", SCOPE, "request")
    record = ledger.get_operation("s", SCOPE, "request")
    assert record["state"] == "unknown" and record["job_id"] == "job"
    assert ledger.budget_usage("s", SCOPE)["render"] == {"held": 1, "consumed": 0, "total": 1}
    assert len(bridge.calls) == 1


@pytest.mark.parametrize(
    "status,turn_status,expected",
    [
        ("completed", None, "succeeded"),
        ("failed", "completed", "failed"),
    ],
)
async def test_nullable_completed_and_failed_completed_receipts_remain_legal(
    ledger, status, turn_status, expected
):
    attempt(ledger, state="accepted")
    bridge = LookupOnly(status=status, turn_status=turn_status)
    result = await ReadOnlyRecovery(ledger, bridge).reconcile("s", SCOPE, "request")
    assert result["state"] == expected and result["job_id"] == "job"
    assert ledger.budget_usage("s", SCOPE)["render"] == {"held": 0, "consumed": 1, "total": 1}
    assert len(bridge.calls) == 1


@pytest.mark.parametrize("state", ["submitting", "accepted", "unknown"])
async def test_not_found_keeps_unknown_reservation(ledger, state):
    before = attempt(ledger, state=state)
    bridge = LookupOnly(found=False)
    result = await ReadOnlyRecovery(ledger, bridge).reconcile("s", SCOPE, "request")
    assert result["state"] == "unknown" and result["job_id"] == before["job_id"]
    assert ledger.budget_usage("s", SCOPE)["render"]["held"] == 1
    assert len(bridge.calls) == 1


@pytest.mark.parametrize(
    "code,kind",
    [
        ("LOOKUP_UNSUPPORTED", "capability"),
        ("INVALID_RESPONSE", "protocol"),
        ("TRANSPORT_UNKNOWN", "transport"),
        ("REQUEST_CONFLICT", "remote"),
        ("QUEUE_FULL", "remote"),
        ("RECEIPT_MISMATCH", "binding"),
    ],
)
async def test_bridge_errors_preserved_without_refund_or_retry(ledger, code, kind):
    attempt(ledger, state="accepted")
    failure = BridgeError(code, kind=kind)
    bridge = LookupOnly(failure=failure)
    with pytest.raises(BridgeError) as raised:
        await ReadOnlyRecovery(ledger, bridge).reconcile("s", SCOPE, "request")
    assert raised.value is failure and raised.value.kind == kind
    result = ledger.get_operation("s", SCOPE, "request")
    assert result["state"] == "unknown" and result["job_id"] == "job"
    assert ledger.budget_usage("s", SCOPE)["render"]["held"] == 1
    assert len(bridge.calls) == 1


async def test_wrong_scope_never_reaches_bridge(ledger):
    attempt(ledger)
    bridge = LookupOnly()
    recovery = ReadOnlyRecovery(ledger, bridge)
    with pytest.raises(StoreError, check=lambda exc: exc.code == "NOT_FOUND"):
        await recovery.reconcile("s", OTHER, "request")
    ledger.create_space("other", OTHER, binding_id="synthetic-service-installation")
    with pytest.raises(StoreError, check=lambda exc: exc.code == "NOT_FOUND"):
        await recovery.reconcile("other", OTHER, "request")
    with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_SCOPE"):
        await recovery.reconcile("s", json.loads(SCOPE.snapshot()), "request")
    assert bridge.calls == []
    assert ledger.get_operation("s", SCOPE, "request")["state"] == "submitting"


@pytest.mark.parametrize("state", ["succeeded", "failed"])
async def test_terminal_is_idempotent_and_does_not_lookup(ledger, state):
    attempt(ledger, state=state)
    bridge = LookupOnly()
    recovery = ReadOnlyRecovery(ledger, bridge)
    first = await recovery.reconcile("s", SCOPE, "request")
    assert first == await recovery.reconcile("s", SCOPE, "request")
    assert first["state"] == state and bridge.calls == []


@pytest.mark.parametrize("action,state", [("submit", "reserved"), ("think", "submitting")])
async def test_unattempted_or_nonrender_is_not_a_lookup_target(ledger, action, state):
    attempt(ledger, action=action, state=state)
    bridge = LookupOnly()
    with pytest.raises(StoreError):
        await ReadOnlyRecovery(ledger, bridge).reconcile("s", SCOPE, "request")
    assert bridge.calls == []


async def test_edit_digest_uses_frozen_params_and_recovery_survives_restart(ledger):
    params = {"prompt": PROMPT, "source_job_id": "original", "source_sha256": "a" * 64}
    record = attempt(ledger, action="edit", state="unknown", params=params)
    params["prompt"] = "mutated caller input"
    expected = hashlib.sha256(
        json.dumps(
            {"operation": "edit", **record["params"]},
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    # Revocation forbids a new render, but must not prevent inspection of old work.
    ledger.revoke("s", SCOPE, source_ref="withdrawn", granted_by="trusted-adapter")
    reopened = ArtistStore(ledger.path)
    try:
        bridge = LookupOnly()
        await ReadOnlyRecovery(reopened, bridge).reconcile("s", SCOPE, "request")
        assert bridge.calls[0][2:4] == ("edit", expected)
        assert reopened.get_operation("s", SCOPE, "request")["state"] == "accepted"
        assert reopened.get_operation("s", SCOPE, "request")["params"]["prompt"] == PROMPT
        assert reopened.budget_usage("s", SCOPE)["render"]["held"] == 1
    finally:
        reopened.close()


async def test_frozen_request_and_preexisting_job_cannot_be_rebound(ledger):
    attempt(ledger, state="accepted")

    class Mismatch(LookupOnly):
        async def lookup(self, *args, **kwargs):
            result = await super().lookup(*args, **kwargs)
            result["job"]["job_id"] = "different-job"
            return result

    with pytest.raises(StoreError, check=lambda exc: exc.code == "REQUEST_CONFLICT"):
        await ReadOnlyRecovery(ledger, Mismatch()).reconcile("s", SCOPE, "request")
    record = ledger.get_operation("s", SCOPE, "request")
    assert record["state"] == "unknown" and record["job_id"] == "job"


async def test_cancelled_lookup_marks_unknown_and_does_not_retry(ledger):
    attempt(ledger, state="accepted")
    entered = asyncio.Event()

    class Blocked(LookupOnly):
        async def lookup(self, *args, **kwargs):
            self.calls.append("lookup")
            entered.set()
            await asyncio.Event().wait()

    bridge = Blocked()
    task = asyncio.create_task(ReadOnlyRecovery(ledger, bridge).reconcile("s", SCOPE, "request"))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ledger.get_operation("s", SCOPE, "request")["state"] == "unknown"
    assert ledger.budget_usage("s", SCOPE)["render"]["held"] == 1
    assert bridge.calls == ["lookup"]


async def test_repeated_cancellation_joins_sqlite_worker_before_return(ledger, monkeypatch):
    attempt(ledger)
    started, release, done = Event(), Event(), Event()
    original = ledger.mark_unknown

    def paused(*args, **kwargs):
        started.set()
        if not release.wait(timeout=5):
            raise TimeoutError("Synthetic test did not release the SQLite worker")
        original(*args, **kwargs)
        done.set()

    monkeypatch.setattr(ledger, "mark_unknown", paused)
    task = asyncio.create_task(
        ReadOnlyRecovery(ledger, LookupOnly(found=False)).reconcile("s", SCOPE, "request")
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not done.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert done.is_set()
    assert ledger.get_operation("s", SCOPE, "request")["state"] == "unknown"
