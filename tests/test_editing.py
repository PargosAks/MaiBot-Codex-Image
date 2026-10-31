"""Original-image edit orchestration with isolated SQLite, no network or drawing."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from threading import Event
import asyncio
import base64
import copy
import hashlib
import json

import pytest

from image_plugin_under_test.artist import render_digest
from image_plugin_under_test.bridge import Bridge, BridgeError
from image_plugin_under_test.editing import ImageEdits
from image_plugin_under_test.policy import Delegation, Limits, Scope, StoreError
from image_plugin_under_test.store import ArtistStore
from conftest import SECRET


SCOPE = Scope("test", "verified-stream", "verified-owner")
OTHER = Scope("test", "other-stream", "other-owner")
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)
DIGEST = hashlib.sha256(PNG).hexdigest()
PROMPT = "保留构图，细化花瓣  "
LIMITS = Limits(think=20, render=20, share=20, max_inflight=20, max_revisions=5)


def error(code):
    return pytest.raises(StoreError, check=lambda exc: exc.code == code)


def source(db, *, work="work", request="source", job="original", version="parent"):
    db.create_work("s", SCOPE, work)
    db.reserve("s", SCOPE, request_id=request, action="submit", params={"prompt": "original"}, work_id=work)
    db.mark_submitting("s", SCOPE, request)
    db.settle("s", SCOPE, request, outcome="succeeded", evidence="synthetic completed", job_id=job)
    return db.record_version(
        "s",
        SCOPE,
        work_id=work,
        version_id=version,
        request_id=request,
        job_id=job,
        data=PNG,
        mime="image/png",
    )


@pytest.fixture
def ledger(tmp_path):
    db = ArtistStore(tmp_path / "isolated.sqlite3")
    db.create_space("s", SCOPE, binding_id="synthetic-service")
    db.grant("s", SCOPE, Delegation("explicit-consent", "trusted-adapter", continuous=True))
    db.set_global_limits(LIMITS)
    db.set_limits("s", SCOPE, LIMITS)
    source(db)
    yield db
    db.close()


class FakeBridge:
    def __init__(self, *, failure=None):
        self.failure = failure
        self.edits, self.lookups, self.images = [], [], []
        self.status, self.turn, self.found, self.available = "queued", None, True, False
        self.data = PNG

    def receipt(self, request_id, digest, *, lookup=False):
        result = {
            "operation": "edit",
            "request_id": request_id,
            "request_digest": digest,
            "job": {
                "job_id": "edited",
                "status": self.status,
                "turn_status": self.turn,
                "error_code": None,
                "artifacts": [
                    {"asset_id": "image", "sha256": DIGEST, "mime_type": "image/png", "size_bytes": len(PNG)}
                ]
                if self.available
                else [],
            },
        }
        if lookup:
            result["found"] = True
        return result

    async def edit(self, scope, request_id, source_job_id, source_sha256, prompt, *, before_dispatch=None):
        if before_dispatch is not None:
            await before_dispatch()
        params = {"source_job_id": source_job_id, "source_sha256": source_sha256, "prompt": prompt}
        self.edits.append((copy.deepcopy(scope), request_id, params))
        if self.failure:
            raise self.failure
        return self.receipt(request_id, render_digest("edit", params))

    async def lookup(self, scope, request_id, **expected):
        self.lookups.append((copy.deepcopy(scope), request_id, expected))
        if not self.found:
            return {"found": False}
        return self.receipt(request_id, expected["expected_digest"], lookup=True)

    async def image(self, scope, job_id, **expected):
        self.images.append((copy.deepcopy(scope), job_id, expected))
        return {"data": self.data, "mime": "image/png", "sha256": DIGEST}

    async def submit(self, *args, **kwargs):
        pytest.fail("An image edit must never fall back to text-only generation")

    async def result(self, *args, **kwargs):
        pytest.fail("Image import must use the validated image API")


async def submit(edits, **changes):
    args = dict(
        space_id="s",
        scope=SCOPE,
        work_id="work",
        source_version_id="parent",
        request_id="edit",
        prompt=PROMPT,
    )
    return await edits.submit(**(args | changes))


async def test_submit_freezes_source_and_one_attempt_then_collect_immutable(ledger):
    bridge = FakeBridge()
    edits = ImageEdits(ledger, bridge)
    record = await submit(edits)
    assert record["state"] == "accepted" and record["job_id"] == "edited"
    assert bridge.edits == [
        (
            json.loads(SCOPE.snapshot()),
            "edit",
            {
                "prompt": PROMPT,
                "source_job_id": "original",
                "source_sha256": DIGEST,
            },
        )
    ]
    assert await submit(edits) == record and len(bridge.edits) == 1
    with error("REQUEST_CONFLICT"):
        await submit(edits, prompt="different")
    assert await edits.collect("s", SCOPE, "edit", "parent") is None
    bridge.status, bridge.turn, bridge.available = "completed", "completed", True
    child = await edits.collect("s", SCOPE, "edit", "parent")
    assert child["parent_id"] == "parent" and child["job_id"] == "edited"
    assert child["data"] == PNG and child["sha256"] == DIGEST
    assert ledger.get_version("s", SCOPE, "parent")["job_id"] == "original"
    assert bridge.images == [
        (
            json.loads(SCOPE.snapshot()),
            "edited",
            {
                "expected_sha256": DIGEST,
                "expected_mime": "image/png",
                "expected_size": len(PNG),
            },
        )
    ]
    calls = len(bridge.lookups), len(bridge.images)
    bridge.data = b"changed"
    assert await edits.collect("s", SCOPE, "edit", "parent") == child
    assert (len(bridge.lookups), len(bridge.images)) == calls
    assert len(bridge.edits) == 1


@pytest.mark.parametrize(
    "change,code",
    [
        ("revoke", "AUTH_REQUIRED"),
        ("regrant", "AUTH_CHANGED"),
        ("space", "BUDGET_EXCEEDED"),
        ("global", "BUDGET_EXCEEDED"),
        ("revision", "REVISION_LIMIT"),
        ("day", "BUDGET_DAY_CHANGED"),
    ],
)
async def test_standalone_edit_rechecks_policy_after_bridge_health(ledger, change, code):
    now = datetime(2026, 10, 31, 2, tzinfo=timezone.utc)
    ledger.clock = lambda: now
    bridge, calls = Bridge("http://127.0.0.1/mcp", SECRET), []

    async def call(operation, scope, args):
        nonlocal now
        calls.append(operation)
        assert operation == "health", "Policy changes must stop the actual edit"
        with closing(ArtistStore(ledger.path, clock=ledger.clock)) as other:
            if change in {"revoke", "regrant"}:
                other.revoke("s", SCOPE, source_ref="withdrawn", granted_by="trusted-admin")
                if change == "regrant":
                    other.grant("s", SCOPE, Delegation("new-consent", "trusted-admin", continuous=True))
            elif change == "global":
                other.set_global_limits(Limits(think=20, render=0, share=20, max_inflight=20))
            elif change == "space":
                other.set_limits("s", SCOPE, Limits(think=20, render=0, share=20, max_inflight=20))
            elif change == "revision":
                other.set_limits(
                    "s", SCOPE, Limits(think=20, render=20, share=20, max_inflight=20, max_revisions=0)
                )
            elif change == "day":
                now += timedelta(days=1)
        return {"capabilities": ["edit_v1", "lookup_v1"]}

    bridge.call = call
    with error(code):
        await submit(ImageEdits(ledger, bridge))
    assert calls == ["health"]
    op = ledger.get_operation("s", SCOPE, "edit")
    assert op["state"] == "unknown" and op["job_id"] is None
    assert ledger.budget_usage("s", SCOPE)["render"]["held"] == 1


@pytest.mark.parametrize(
    "changes", [{"work_id": "other"}, {"scope": OTHER}, {"scope": {}}, {"space_id": "other"}]
)
async def test_source_cannot_cross_work_or_scope(ledger, changes):
    bridge = FakeBridge()
    with pytest.raises(StoreError):
        await submit(ImageEdits(ledger, bridge), **changes)
    assert bridge.edits == []
    assert len(ledger.audit_operations("s", SCOPE)) == 1


@pytest.mark.parametrize("change", ["hash", "job"])
async def test_source_bytes_and_original_job_checked_before_reserving(ledger, change):
    if change == "hash":
        ledger.db.execute("UPDATE versions SET data=? WHERE version_id='parent'", (b"damaged",))
    else:
        ledger.db.execute("UPDATE versions SET job_id='foreign' WHERE version_id='parent'")
    bridge = FakeBridge()
    with error("SOURCE_HASH_MISMATCH" if change == "hash" else "REQUEST_CONFLICT"):
        await submit(ImageEdits(ledger, bridge))
    assert not bridge.edits and len(ledger.audit_operations("s", SCOPE)) == 1


@pytest.mark.parametrize(
    "limit",
    [
        Limits(render=1, max_inflight=10, max_revisions=10),
        Limits(render=20, max_inflight=10, max_revisions=0),
    ],
)
async def test_budget_and_revision_limits_prevent_dispatch(ledger, limit):
    ledger.set_limits("s", SCOPE, limit)
    bridge = FakeBridge()
    with pytest.raises(StoreError):
        await submit(ImageEdits(ledger, bridge))
    assert not bridge.edits


@pytest.mark.parametrize("regrant", [False, True])
async def test_latest_authorization_rechecked_before_external_call(ledger, monkeypatch, regrant):
    original = ledger.mark_submitting

    def revoke_before_start(*args, **kwargs):
        ledger.revoke("s", SCOPE, source_ref="stop", granted_by="adapter")
        if regrant:
            ledger.grant("s", SCOPE, Delegation("new-consent", "adapter", continuous=True))
        return original(*args, **kwargs)

    monkeypatch.setattr(ledger, "mark_submitting", revoke_before_start)
    bridge = FakeBridge()
    with error("AUTH_CHANGED" if regrant else "AUTH_REQUIRED"):
        await submit(ImageEdits(ledger, bridge))
    assert not bridge.edits
    assert ledger.get_operation("s", SCOPE, "edit")["state"] == "reserved"


@pytest.mark.parametrize(
    "code,kind",
    [
        ("EDIT_UNSUPPORTED", "capability"),
        ("QUEUE_FULL", "remote"),
        ("TRANSPORT_UNKNOWN", "transport"),
        ("REQUEST_CONFLICT", "remote"),
    ],
)
async def test_errors_never_refund_or_retry_and_preserve_original_exception(ledger, code, kind):
    failure = BridgeError(code, kind=kind)
    bridge = FakeBridge(failure=failure)
    edits = ImageEdits(ledger, bridge)
    with pytest.raises(BridgeError) as caught:
        await submit(edits)
    assert caught.value is failure
    assert (await submit(edits))["state"] == "unknown"
    assert len(bridge.edits) == 1
    assert ledger.budget_usage("s", SCOPE)["render"] == {"held": 1, "consumed": 1, "total": 2}


async def test_lost_receipt_reopen_collect_unknown_image_does_not_unlock_next_edit(ledger, tmp_path):
    bridge = FakeBridge(failure=BridgeError("TRANSPORT_UNKNOWN", kind="transport"))
    with pytest.raises(BridgeError):
        await submit(ImageEdits(ledger, bridge))
    bridge.failure, bridge.status, bridge.available = None, "artifact_ready", True
    with closing(ArtistStore(tmp_path / "isolated.sqlite3")) as reopened:
        edits = ImageEdits(reopened, bridge)
        assert (await submit(edits))["state"] == "unknown"
        child = await edits.collect("s", SCOPE, "edit", "parent")
        assert child["parent_id"] == "parent"
        assert reopened.get_operation("s", SCOPE, "edit")["state"] == "unknown"
        with error("WORK_BUSY"):
            await submit(edits, request_id="next", source_version_id=child["version_id"])
        bridge.status, bridge.turn = "completed", "completed"
        assert await edits.collect("s", SCOPE, "edit", "parent") == child
        assert reopened.get_operation("s", SCOPE, "edit")["state"] == "succeeded"
    assert len(bridge.edits) == 1 and len(bridge.lookups) == 2 and len(bridge.images) == 1


async def test_lookup_missing_retains_unknown_without_image_or_resubmit(ledger):
    bridge = FakeBridge()
    edits = ImageEdits(ledger, bridge)
    await submit(edits)
    bridge.found = False
    assert await edits.collect("s", SCOPE, "edit", "parent") is None
    assert ledger.get_operation("s", SCOPE, "edit")["state"] == "unknown"
    assert not bridge.images and len(bridge.edits) == 1


async def test_collect_scope_parent_and_direct_store_parent_binding(ledger):
    source(ledger, work="other", request="other", job="other-job", version="other-version")
    source(ledger, request="second", job="second-job", version="second-version")
    bridge = FakeBridge()
    edits = ImageEdits(ledger, bridge)
    await submit(edits)
    for scope, parent, code in [
        (OTHER, "parent", "NOT_FOUND"),
        (SCOPE, "other-version", "NOT_FOUND"),
        (SCOPE, "second-version", "REQUEST_CONFLICT"),
    ]:
        with error(code):
            await edits.collect("s", scope, "edit", parent)
    for parent in [None, "second-version"]:
        with error("REQUEST_CONFLICT"):
            ledger.record_version(
                "s",
                SCOPE,
                work_id="work",
                version_id="bad",
                request_id="edit",
                job_id="edited",
                data=PNG,
                mime="image/png",
                parent_id=parent,
            )
    assert not bridge.lookups and not bridge.images


@pytest.mark.parametrize(
    "state,turn,expected", [("completed", None, "succeeded"), ("artifact_ready", "failed", "failed")]
)
async def test_terminal_image_collect_and_revoke_does_not_erase_result(ledger, state, turn, expected):
    bridge = FakeBridge()
    bridge.status, bridge.turn, bridge.available = state, turn, True
    edits = ImageEdits(ledger, bridge)
    assert (await submit(edits))["state"] == expected
    ledger.revoke("s", SCOPE, source_ref="stop", granted_by="adapter")
    assert (await edits.collect("s", SCOPE, "edit", "parent"))["parent_id"] == "parent"
    assert ledger.get_operation("s", SCOPE, "edit")["state"] == expected
    assert len(bridge.edits) == len(bridge.lookups) == 1


async def test_cancel_submission_retains_unknown_and_no_retry(ledger):
    entered = asyncio.Event()

    class Blocking(FakeBridge):
        async def edit(self, *args, **kwargs):
            self.edits.append("attempt")
            entered.set()
            await asyncio.Event().wait()

    bridge = Blocking()
    edits = ImageEdits(ledger, bridge)
    task = asyncio.create_task(submit(edits))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await submit(edits))["state"] == "unknown" and len(bridge.edits) == 1


async def test_cancel_waits_for_mark_submitting_worker(ledger, monkeypatch):
    entered, release = Event(), Event()
    original = ledger.mark_submitting

    def blocked(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(ledger, "mark_submitting", blocked)
    bridge = FakeBridge()
    task = asyncio.create_task(submit(ImageEdits(ledger, bridge)))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ledger.get_operation("s", SCOPE, "edit")["state"] == "unknown"
    assert not bridge.edits


async def test_bad_image_bytes_not_imported_and_pending_hold_retained(ledger):
    bridge = FakeBridge()
    edits = ImageEdits(ledger, bridge)
    await submit(edits)
    bridge.available, bridge.status, bridge.data = True, "artifact_ready", b"tampered"
    with error("SOURCE_HASH_MISMATCH"):
        await edits.collect("s", SCOPE, "edit", "parent")
    assert len(ledger.list_versions("s", SCOPE, "work")) == 1
    assert ledger.get_operation("s", SCOPE, "edit")["state"] == "unknown"


async def test_two_connections_same_request_dispatch_only_once(ledger, tmp_path):
    bridge = FakeBridge()
    with closing(ArtistStore(tmp_path / "isolated.sqlite3")) as second:
        results = await asyncio.gather(
            submit(ImageEdits(ledger, bridge)), submit(ImageEdits(second, bridge)), return_exceptions=True
        )
    assert len(bridge.edits) == 1
    assert any(isinstance(item, dict) and item["state"] == "accepted" for item in results)
    assert all(
        isinstance(item, dict) or (isinstance(item, StoreError) and item.code == "INVALID_STATE")
        for item in results
    )


async def test_authorization_snapshot_cannot_survive_regrant_during_source_read(ledger, monkeypatch):
    bridge = FakeBridge()
    edits = ImageEdits(ledger, bridge)
    original = edits._source

    def replace_grant(*args):
        result = original(*args)
        ledger.revoke("s", SCOPE, source_ref="stop", granted_by="adapter")
        ledger.grant("s", SCOPE, Delegation("new-consent", "adapter", continuous=True))
        return result

    monkeypatch.setattr(edits, "_source", replace_grant)
    with error("AUTH_CHANGED"):
        await submit(edits)
    assert not bridge.edits and len(ledger.audit_operations("s", SCOPE)) == 1


async def test_budget_tightening_after_reservation_prevents_dispatch(ledger, monkeypatch):
    original = ledger.mark_submitting

    def tighten(*args):
        ledger.set_limits("s", SCOPE, Limits(render=1, max_inflight=20, max_revisions=5))
        return original(*args)

    monkeypatch.setattr(ledger, "mark_submitting", tighten)
    bridge = FakeBridge()
    with pytest.raises(StoreError):
        await submit(ImageEdits(ledger, bridge))
    assert not bridge.edits and ledger.get_operation("s", SCOPE, "edit")["state"] == "reserved"


async def test_known_failed_edit_is_not_retried_and_consumes_attempt(ledger):
    bridge = FakeBridge()
    bridge.status, bridge.turn = "failed", "completed"
    edits = ImageEdits(ledger, bridge)
    assert (await submit(edits))["state"] == "failed"
    assert (await submit(edits))["state"] == "failed"
    assert len(bridge.edits) == 1
    assert ledger.budget_usage("s", SCOPE)["render"] == {"held": 0, "consumed": 2, "total": 2}


@pytest.mark.parametrize("mismatch", ["source_job_id", "source_sha256"])
def test_store_rejects_parent_that_disagrees_with_frozen_edit_source(ledger, mismatch):
    params = {"prompt": PROMPT, "source_job_id": "original", "source_sha256": DIGEST}
    params[mismatch] = "different" if mismatch == "source_job_id" else "0" * 64
    ledger.reserve("s", SCOPE, request_id="edit", action="edit", work_id="work", params=params)
    ledger.mark_submitting("s", SCOPE, "edit")
    ledger.mark_accepted("s", SCOPE, "edit", job_id="edited")
    with error("REQUEST_CONFLICT"):
        ledger.record_version(
            "s",
            SCOPE,
            work_id="work",
            version_id="child",
            request_id="edit",
            job_id="edited",
            data=PNG,
            mime="image/png",
            parent_id="parent",
        )
    assert len(ledger.list_versions("s", SCOPE, "work")) == 1


async def test_collect_cancel_keeps_unknown_and_cannot_generate(ledger):
    entered = asyncio.Event()

    class BlockingImage(FakeBridge):
        async def image(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    bridge = BlockingImage()
    edits = ImageEdits(ledger, bridge)
    await submit(edits)
    bridge.available, bridge.status, bridge.turn = True, "artifact_ready", "running"
    task = asyncio.create_task(edits.collect("s", SCOPE, "edit", "parent"))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ledger.get_operation("s", SCOPE, "edit")["state"] == "unknown"
    assert len(ledger.list_versions("s", SCOPE, "work")) == 1
    assert len(bridge.edits) == 1
