"""Real isolated SQLite plus a no-network Bridge; Artist remains the only writer."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import asyncio
import copy
import hashlib
import json

import pytest

from image_plugin_under_test.artist import RenderResult, render_digest
from image_plugin_under_test.bridge import Bridge, BridgeError
from image_plugin_under_test.policy import Delegation, Limits, Scope, StoreError
from image_plugin_under_test.rendering import MCPRenderer
from image_plugin_under_test.store import ArtistStore
from protocol_fixture import PNG


SCOPE = Scope("synthetic", "studio", "owner")
OTHER = Scope("synthetic", "elsewhere", "another-owner")
BINDING = "isolated-service"
PROMPT = "保留有意的空格  "
HASH = hashlib.sha256(PNG).hexdigest()
BRIEF = dict(intent="practice", subject="cat", mood="quiet", prompt=PROMPT, completion_criteria="clear")


@pytest.fixture
def ledger(tmp_path):
    db = ArtistStore(tmp_path / "renderer.sqlite3")
    db.create_space("s", SCOPE, binding_id=BINDING)
    db.grant("s", SCOPE, Delegation("explicit-consent", "trusted-adapter", continuous=True))
    limits = Limits(render=10, think=10, share=10, max_inflight=10, max_revisions=5)
    db.set_global_limits(limits)
    db.set_limits("s", SCOPE, limits)
    db.plan_work("s", SCOPE, work_id="work", brief=BRIEF, inspiration_keys=[])
    yield db
    db.close()


def attempt(db, *, action="submit", request_id="r", state="submitting"):
    params = {"prompt": PROMPT}
    if action == "edit":
        params.update(source_job_id="original", source_sha256=HASH)
    db.reserve("s", SCOPE, request_id=request_id, action=action, params=params, work_id="work")
    db.update_artist_work("s", SCOPE, "work", changes={"pending_request": request_id, "status": "creating"})
    if state != "reserved":
        db.mark_submitting("s", SCOPE, request_id)
    if state == "unknown":
        db.mark_unknown("s", SCOPE, request_id, reason="synthetic lost receipt")
    elif state in {"accepted", "succeeded", "failed"}:
        db.mark_accepted("s", SCOPE, request_id, job_id="job")
        if state != "accepted":
            db.settle("s", SCOPE, request_id, outcome=state, evidence="synthetic terminal")
    return db.get_operation("s", SCOPE, request_id)


def parent(db):
    attempt(db, request_id="original-request")
    db.settle("s", SCOPE, "original-request", outcome="succeeded", evidence="synthetic", job_id="original")
    version = db.record_version(
        "s",
        SCOPE,
        work_id="work",
        version_id="parent",
        request_id="original-request",
        job_id="original",
        data=PNG,
        mime="image/png",
    )
    db.update_artist_work("s", SCOPE, "work", changes={"parent_version": "parent", "edit_prompt": PROMPT})
    return {key: version[key] for key in ("job_id", "sha256", "data", "mime")}


class FakeBridge:
    def __init__(self):
        self.calls = []
        self.capability_calls = []
        self.supported = frozenset({"lookup_v1", "edit_v1"})
        self.job = dict(job_id="job", status="queued", turn_status=None, error_code=None, artifacts=[])
        self.found, self.failure, self.image_failure = True, None, None
        self.receipt_changes = {}
        self.image_data = PNG
        self.legacy = None

    def receipt(self, request_id, operation, digest):
        return (
            dict(
                operation=operation, request_id=request_id, request_digest=digest, job=copy.deepcopy(self.job)
            )
            | self.receipt_changes
        )

    def available(self, status="artifact_ready", turn=None):
        self.job.update(
            status=status,
            turn_status=turn,
            artifacts=[dict(asset_id="image", sha256=HASH, mime_type="image/png", size_bytes=len(PNG))],
        )

    async def capabilities(self, scope):
        self.capability_calls.append(copy.deepcopy(scope))
        return self.supported

    async def call(self, operation, scope, args):
        assert operation == "submit", "No direct status/result, text fallback or other call"
        assert self.capability_calls, "Recovery capability must be checked before generation"
        self.calls.append((operation, copy.deepcopy(scope), copy.deepcopy(args)))
        if self.failure:
            raise self.failure
        return self.legacy if self.legacy is not None else {"job_id": self.job["job_id"], "status": "queued"}

    async def edit(self, scope, request_id, *, before_dispatch=None, **params):
        assert self.capability_calls, "Edits also require recovery capability before generation"
        if before_dispatch is not None:
            await before_dispatch()
        self.calls.append(("edit", copy.deepcopy(scope), dict(request_id=request_id, **params)))
        if self.failure:
            raise self.failure
        return self.receipt(request_id, "edit", render_digest("edit", params))

    async def lookup(self, scope, request_id, **expected):
        self.calls.append(("lookup", copy.deepcopy(scope), dict(request_id=request_id, **expected)))
        if not self.found:
            return {"found": False}
        return {
            "found": True,
            **self.receipt(request_id, expected["expected_operation"], expected["expected_digest"]),
        }

    async def image(self, scope, job_id, **expected):
        self.calls.append(("image", copy.deepcopy(scope), dict(job_id=job_id, **expected)))
        if self.image_failure:
            raise self.image_failure
        return dict(data=self.image_data, mime="image/png", sha256=HASH)


def renderer(db, bridge, **changes):
    return MCPRenderer(db, "s", SCOPE, bridge, binding_id=changes.get("binding_id", BINDING))


async def submit(adapter, **changes):
    return await adapter.submit(**(dict(scope=SCOPE, request_id="r", prompt=PROMPT, source=None) | changes))


async def test_original_submit_lookup_verification_and_zero_ledger_writes(ledger):
    op = attempt(ledger)
    bridge = FakeBridge()
    bridge.available("completed", "completed")
    adapter = renderer(ledger, bridge)
    changes = ledger.db.total_changes
    result = await submit(adapter)
    assert result == RenderResult(
        "r", render_digest("submit", op["params"]), "job", "completed", PNG, "image/png", HASH
    )
    assert [call[0] for call in bridge.calls] == ["submit", "lookup", "image"]
    assert all(call[1] == json.loads(SCOPE.snapshot()) for call in bridge.calls)
    assert bridge.calls[0][2] == {"request_id": "r", "prompt": PROMPT}
    assert bridge.calls[1][2] == dict(
        request_id="r",
        expected_operation="submit",
        expected_digest=result.request_digest,
        expected_job_id="job",
    )
    assert ledger.db.total_changes == changes
    assert ledger.get_operation("s", SCOPE, "r") == op
    assert ledger.list_versions("s", SCOPE, "work") == []
    assert adapter.simulation is False and adapter.supports_edit is True


async def test_edit_uses_only_frozen_original_image_receipt(ledger):
    image = parent(ledger)
    op = attempt(ledger, action="edit")
    bridge = FakeBridge()
    result = await submit(renderer(ledger, bridge), source=image)
    assert result.request_digest == render_digest("edit", op["params"])
    assert bridge.calls == [("edit", json.loads(SCOPE.snapshot()), dict(request_id="r", **op["params"]))]
    assert ledger.get_operation("s", SCOPE, "r") == op


@pytest.mark.parametrize("method", ["submit", "lookup", "status"])
@pytest.mark.parametrize("boundary", ["scope", "binding"])
async def test_scope_and_binding_rejected_before_any_external_call(ledger, method, boundary):
    attempt(ledger, state="accepted" if method == "status" else "submitting")
    bridge = FakeBridge()
    adapter = renderer(ledger, bridge, binding_id="wrong" if boundary == "binding" else BINDING)
    kwargs = {"scope": OTHER if boundary == "scope" else SCOPE}
    if method == "status":
        kwargs["job_id"] = "job"
    else:
        kwargs["request_id"] = "r"
    if method == "submit":
        kwargs.update(prompt=PROMPT, source=None)
    with pytest.raises(StoreError):
        await getattr(adapter, method)(**kwargs)
    assert not bridge.calls


@pytest.mark.parametrize("field,value", [("prompt", "changed"), ("source", {"path": "untrusted"})])
async def test_original_cannot_override_frozen_parameters(ledger, field, value):
    attempt(ledger)
    bridge = FakeBridge()
    with pytest.raises(StoreError, check=lambda exc: exc.code == "REQUEST_CONFLICT"):
        await submit(renderer(ledger, bridge), **{field: value})
    assert not bridge.calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_id", "foreign"),
        ("sha256", "0" * 64),
        ("data", b"changed"),
        ("mime", "image/jpeg"),
        ("path", "arbitrary"),
    ],
)
async def test_edit_source_must_match_persisted_parent(ledger, field, value):
    image = parent(ledger)
    attempt(ledger, action="edit")
    image[field] = value
    bridge = FakeBridge()
    with pytest.raises(StoreError, check=lambda exc: exc.code == "REQUEST_CONFLICT"):
        await submit(renderer(ledger, bridge), source=image)
    assert not bridge.calls


async def test_parent_from_another_work_or_damaged_bytes_rejected(ledger):
    image = parent(ledger)
    attempt(ledger, action="edit")
    bridge = FakeBridge()
    # Synthetic corruption cannot be created through the public Store API.
    ledger.db.execute("UPDATE versions SET data=? WHERE version_id='parent'", (b"corrupt",))
    image["data"] = b"corrupt"
    with pytest.raises(StoreError):
        await submit(renderer(ledger, bridge), source=image)
    ledger.create_work("s", SCOPE, "other")
    ledger.db.execute("UPDATE versions SET work_id='other',data=? WHERE version_id='parent'", (PNG,))
    image["data"] = PNG
    with pytest.raises(StoreError):
        await submit(renderer(ledger, bridge), source=image)
    assert not bridge.calls


@pytest.mark.parametrize("state", ["reserved", "accepted", "unknown", "succeeded", "failed"])
async def test_only_submitting_can_dispatch(ledger, state):
    attempt(ledger, state=state)
    bridge = FakeBridge()
    with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_STATE"):
        await submit(renderer(ledger, bridge))
    assert not bridge.calls


@pytest.mark.parametrize(
    "status,turn,expected",
    [
        ("queued", None, "queued"),
        ("starting", None, "queued"),
        ("running", "inProgress", "running"),
        ("artifact_ready", None, "outcome_unknown"),
        ("artifact_ready", "unknown", "outcome_unknown"),
        ("artifact_ready", "completed", "completed"),
        ("artifact_ready", "failed", "failed"),
        ("artifact_ready", "interrupted", "failed"),
        ("artifact_ready", "cancelled", "failed"),
        ("artifact_ready", "running", "running"),
        ("completed", None, "completed"),
        ("failed", "completed", "failed"),
        ("outcome_unknown", None, "outcome_unknown"),
    ],
)
async def test_turn_outcome_is_independent_of_available_image(ledger, status, turn, expected):
    op = attempt(ledger, state="unknown")
    bridge = FakeBridge()
    bridge.available(status, turn)
    result = await renderer(ledger, bridge).lookup(scope=SCOPE, request_id="r")
    assert result.state == expected and result.image == PNG and result.sha256 == HASH
    assert ledger.get_operation("s", SCOPE, "r") == op
    assert [call[0] for call in bridge.calls] == ["lookup", "image"]


@pytest.mark.parametrize("mismatch", ["operation", "request_id", "request_digest", "job_id", "contradiction"])
async def test_lookup_receipt_fails_closed(ledger, mismatch):
    attempt(ledger, state="accepted")
    bridge = FakeBridge()
    if mismatch == "job_id":
        bridge.job["job_id"] = "foreign"
    elif mismatch == "contradiction":
        bridge.job.update(status="completed", turn_status="running")
    else:
        bridge.receipt_changes[mismatch] = "different"
    with pytest.raises(BridgeError):
        await renderer(ledger, bridge).lookup(scope=SCOPE, request_id="r")
    assert [call[0] for call in bridge.calls] == ["lookup"]


async def test_lost_reply_reopen_and_status_use_original_request_only(ledger, tmp_path):
    attempt(ledger)
    bridge = FakeBridge()
    failure = BridgeError("TRANSPORT_UNKNOWN", kind="transport")
    bridge.failure = failure
    with pytest.raises(BridgeError) as caught:
        await submit(renderer(ledger, bridge))
    assert caught.value is failure
    # Simulate the sole coordinator's durable uncertainty handling.
    ledger.mark_unknown("s", SCOPE, "r", reason="response lost")
    bridge.failure = None
    with closing(ArtistStore(tmp_path / "renderer.sqlite3")) as reopened:
        adapter = renderer(reopened, bridge)
        result = await adapter.lookup(scope=SCOPE, request_id="r")
        reopened.reconcile(
            "s", SCOPE, "r", outcome="accepted", evidence="verified lookup", job_id=result.job_id
        )
        result2 = await renderer(reopened, bridge).status(scope=SCOPE, job_id="job")
        assert result2 == result
    assert [call[0] for call in bridge.calls] == ["submit", "lookup", "lookup"]


async def test_missing_lookup_does_not_retry_or_write(ledger):
    op = attempt(ledger, state="accepted")
    bridge = FakeBridge()
    bridge.found = False
    adapter = renderer(ledger, bridge)
    assert await adapter.lookup(scope=SCOPE, request_id="r") is None
    with pytest.raises(BridgeError, check=lambda exc: exc.code == "LOOKUP_NOT_FOUND"):
        await adapter.status(scope=SCOPE, job_id="job")
    assert ledger.get_operation("s", SCOPE, "r") == op
    assert all(call[0] == "lookup" for call in bridge.calls)


async def test_image_fetch_failure_next_poll_only_reads_original_image(ledger):
    op = attempt(ledger, state="accepted")
    bridge = FakeBridge()
    bridge.available("completed", "completed")
    bridge.image_failure = BridgeError("ARTIFACT_INVALID", kind="protocol")
    adapter = renderer(ledger, bridge)
    with pytest.raises(BridgeError) as caught:
        await adapter.status(scope=SCOPE, job_id="job")
    assert caught.value is bridge.image_failure
    bridge.image_failure = None
    assert (await adapter.status(scope=SCOPE, job_id="job")).image == PNG
    assert [call[0] for call in bridge.calls] == ["lookup", "image", "lookup", "image"]
    assert ledger.get_operation("s", SCOPE, "r") == op


@pytest.mark.parametrize("oversized", [False, True])
async def test_untrusted_image_bytes_and_size_rejected(ledger, oversized):
    attempt(ledger, state="unknown")
    bridge = FakeBridge()
    bridge.available()
    if oversized:
        bridge.job["artifacts"][0]["size_bytes"] = 8 * 1024 * 1024 + 1
    else:
        bridge.image_data = b"corrupt"
    with pytest.raises(BridgeError, check=lambda exc: exc.code == "ARTIFACT_INVALID"):
        await renderer(ledger, bridge).lookup(scope=SCOPE, request_id="r")
    assert [call[0] for call in bridge.calls] == (["lookup"] if oversized else ["lookup", "image"])


async def test_edit_unsupported_preserves_error_and_never_falls_back(ledger):
    image = parent(ledger)
    attempt(ledger, action="edit")
    bridge = FakeBridge()
    failure = BridgeError("EDIT_UNSUPPORTED", kind="capability")
    bridge.failure = failure
    with pytest.raises(BridgeError) as caught:
        await submit(renderer(ledger, bridge), source=image)
    assert caught.value is failure
    assert [call[0] for call in bridge.calls] == ["edit"]


async def test_job_query_is_scope_bound_and_refuses_ambiguous_binding(ledger):
    attempt(ledger, state="succeeded")
    assert ledger.get_render_operation_by_job("s", SCOPE, "job")["request_id"] == "r"
    for space, scope, job in [("missing", SCOPE, "job"), ("s", OTHER, "job"), ("s", SCOPE, "missing")]:
        with pytest.raises(StoreError, check=lambda exc: exc.code == "NOT_FOUND"):
            ledger.get_render_operation_by_job(space, scope, job)
    attempt(ledger, request_id="second", state="accepted")
    bridge = FakeBridge()
    with pytest.raises(StoreError, check=lambda exc: exc.code == "REQUEST_CONFLICT"):
        await renderer(ledger, bridge).status(scope=SCOPE, job_id="job")
    assert not bridge.calls


async def test_revoked_submission_blocked_but_existing_lookup_allowed(ledger):
    attempt(ledger)
    ledger.revoke("s", SCOPE, source_ref="stop", granted_by="adapter")
    bridge = FakeBridge()
    adapter = renderer(ledger, bridge)
    with pytest.raises(StoreError, check=lambda exc: exc.code == "AUTH_REQUIRED"):
        await submit(adapter)
    assert (await adapter.lookup(scope=SCOPE, request_id="r")).state == "queued"
    assert [call[0] for call in bridge.calls] == ["lookup"]


async def test_cancelled_external_call_is_not_retried_or_written(ledger):
    op = attempt(ledger)
    entered = asyncio.Event()

    class Blocking(FakeBridge):
        async def call(self, *args):
            self.calls.append("submit")
            entered.set()
            await asyncio.Event().wait()

    bridge = Blocking()
    task = asyncio.create_task(submit(renderer(ledger, bridge)))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bridge.calls == ["submit"]
    assert ledger.get_operation("s", SCOPE, "r") == op


@pytest.mark.parametrize("code", [None, "NO_IMAGE", "SOURCE_HASH_MISMATCH"])
async def test_terminal_error_code_is_preserved_without_ledger_write(ledger, code):
    op = attempt(ledger, state="unknown")
    bridge = FakeBridge()
    bridge.job.update(status="failed", turn_status="completed", error_code=code)
    result = await renderer(ledger, bridge).lookup(scope=SCOPE, request_id="r")
    assert result.state == "failed" and result.error_code == code and result.image is None
    assert ledger.get_operation("s", SCOPE, "r") == op


@pytest.mark.parametrize("code", ["raw server message", "lowercase", "A" * 65, "FAIL\n", 3])
async def test_invalid_error_code_fails_closed_before_fetch(ledger, code):
    attempt(ledger, state="unknown")
    bridge = FakeBridge()
    bridge.available("failed", "failed")
    bridge.job["error_code"] = code
    with pytest.raises(BridgeError, check=lambda exc: exc.code == "INVALID_RESPONSE"):
        await renderer(ledger, bridge).lookup(scope=SCOPE, request_id="r")
    assert [call[0] for call in bridge.calls] == ["lookup"]


@pytest.mark.parametrize("failure", ["malformed", "missing", "foreign", "digest"])
async def test_original_submit_requires_valid_legacy_and_matching_lookup(ledger, failure):
    op = attempt(ledger)
    bridge = FakeBridge()
    if failure == "malformed":
        bridge.legacy = {"job_id": "job"}
    elif failure == "missing":
        bridge.found = False
    elif failure == "foreign":
        bridge.legacy = {"job_id": "different", "status": "queued"}
    else:
        bridge.receipt_changes["request_digest"] = "0" * 64
    with pytest.raises(BridgeError):
        await submit(renderer(ledger, bridge))
    assert [call[0] for call in bridge.calls] == (
        ["submit"] if failure == "malformed" else ["submit", "lookup"]
    )
    assert ledger.get_operation("s", SCOPE, "r") == op


async def test_edit_receipt_identity_is_checked_without_extra_lookup(ledger):
    image = parent(ledger)
    attempt(ledger, action="edit")
    bridge = FakeBridge()
    bridge.receipt_changes["operation"] = "submit"
    with pytest.raises(BridgeError, check=lambda exc: exc.code == "RECEIPT_MISMATCH"):
        await submit(renderer(ledger, bridge), source=image)
    assert [call[0] for call in bridge.calls] == ["edit"]


@pytest.mark.parametrize("action", ["submit", "edit"])
async def test_missing_lookup_capability_blocks_generation_without_writes(ledger, action):
    image = parent(ledger) if action == "edit" else None
    op = attempt(ledger, action=action)
    bridge = FakeBridge()
    bridge.supported = frozenset({"edit_v1"})
    changes = ledger.db.total_changes
    with pytest.raises(BridgeError, check=lambda exc: exc.code == "LOOKUP_UNSUPPORTED"):
        await submit(renderer(ledger, bridge), source=image)
    assert bridge.capability_calls == [json.loads(SCOPE.snapshot())]
    assert not bridge.calls
    assert ledger.db.total_changes == changes
    assert ledger.get_operation("s", SCOPE, "r") == op


async def test_health_failure_propagates_before_generation(ledger):
    attempt(ledger)
    failure = BridgeError("TRANSPORT_UNKNOWN", kind="transport")

    class Unavailable(FakeBridge):
        async def capabilities(self, scope):
            raise failure

    bridge = Unavailable()
    with pytest.raises(BridgeError) as caught:
        await submit(renderer(ledger, bridge))
    assert caught.value is failure
    assert not bridge.calls


async def test_authority_changed_during_health_cannot_dispatch(ledger):
    attempt(ledger)

    class RevokeDuringHealth(FakeBridge):
        async def capabilities(self, scope):
            ledger.revoke("s", SCOPE, source_ref="stop", granted_by="trusted-adapter")
            ledger.grant("s", SCOPE, Delegation("renewed", "trusted-adapter", continuous=True))
            return await super().capabilities(scope)

    bridge = RevokeDuringHealth()
    with pytest.raises(StoreError, check=lambda exc: exc.code == "AUTH_CHANGED"):
        await submit(renderer(ledger, bridge))
    assert not bridge.calls


@pytest.mark.parametrize(
    "action,change,code",
    [
        (action, change, code)
        for action in ("submit", "edit")
        for change, code in (
            ("revoke", "AUTH_REQUIRED"),
            ("regrant", "AUTH_CHANGED"),
            ("global", "BUDGET_EXCEEDED"),
            ("space", "BUDGET_EXCEEDED"),
            ("inflight", "BUDGET_EXCEEDED"),
            ("day", "BUDGET_DAY_CHANGED"),
            ("pending", "REQUEST_CONFLICT"),
        )
    ]
    + [("edit", "revision", "REVISION_LIMIT")],
)
async def test_real_bridge_final_health_rechecks_latest_dispatch_policy(
    ledger, tmp_path, monkeypatch, action, change, code
):
    # Exactly the Hong Kong midnight boundary, independent of machine timezone.
    now = [datetime(2026, 11, 1, 15, 59, tzinfo=timezone.utc)]
    ledger.clock = lambda: now[0]
    image = parent(ledger) if action == "edit" else None
    op = attempt(ledger, action=action)
    changes = ledger.db.total_changes
    statements, calls = [], []
    ledger.db.set_trace_callback(statements.append)
    bridge = Bridge("http://example.invalid/mcp", "synthetic-only-" + "x" * 32)
    with closing(ArtistStore(tmp_path / "renderer.sqlite3", clock=lambda: now[0])) as writer:

        async def call(operation, scope, args):
            calls.append(operation)
            assert operation == "health", "No generation or fallback after the policy changes"
            assert scope == json.loads(SCOPE.snapshot())
            if len(calls) == (2 if action == "edit" else 1):
                if change in {"revoke", "regrant"}:
                    writer.revoke("s", SCOPE, source_ref="stop", granted_by="trusted-adapter")
                    if change == "regrant":
                        writer.grant("s", SCOPE, Delegation("renewed", "trusted-adapter", continuous=True))
                elif change in {"global", "space"}:
                    limits = Limits(render=0, think=10, share=10, max_inflight=10, max_revisions=5)
                    if change == "global":
                        writer.set_global_limits(limits)
                    else:
                        writer.set_limits("s", SCOPE, limits)
                elif change == "revision":
                    writer.set_limits("s", SCOPE, Limits(render=10, max_inflight=10, max_revisions=0))
                elif change == "inflight":
                    writer.set_limits("s", SCOPE, Limits(render=10, max_inflight=0, max_revisions=5))
                elif change == "pending":
                    writer.update_artist_work("s", SCOPE, "work", changes={"pending_request": None})
                else:
                    now[0] += timedelta(minutes=1)
            return {"capabilities": ["lookup_v1", "edit_v1"]}

        monkeypatch.setattr(bridge, "call", call)
        try:
            with pytest.raises(StoreError, check=lambda exc: exc.code == code):
                await submit(renderer(ledger, bridge), source=image)
            assert calls == (["health", "health"] if action == "edit" else ["health"])
            assert ledger.get_operation("s", SCOPE, "r") == op
            assert ledger.budget_usage("s", SCOPE)["render"]["held"] == 1
            assert ledger.db.total_changes == changes
            assert not any(
                sql.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}
                for sql in statements
            )
        finally:
            ledger.db.set_trace_callback(None)


async def test_real_bridge_edit_hook_accepts_unchanged_claim_without_writing(ledger, monkeypatch):
    image = parent(ledger)
    op = attempt(ledger, action="edit")
    bridge = Bridge("http://example.invalid/mcp", "synthetic-only-" + "x" * 32)
    calls = []
    changes = ledger.db.total_changes

    async def call(operation, scope, args):
        calls.append(operation)
        if operation == "health":
            return {"capabilities": ["lookup_v1", "edit_v1"]}
        assert operation == "edit" and args == {"request_id": "r", **op["params"]}
        return FakeBridge().receipt("r", "edit", render_digest("edit", op["params"]))

    monkeypatch.setattr(bridge, "call", call)
    result = await submit(renderer(ledger, bridge), source=image)
    assert result.state == "queued" and calls == ["health", "health", "edit"]
    assert ledger.get_operation("s", SCOPE, "r") == op
    assert ledger.db.total_changes == changes


@pytest.mark.parametrize("state", ["reserved", "accepted", "unknown", "succeeded", "failed", "bound"])
def test_validate_submitting_refuses_non_dispatchable_claim_without_mutation(ledger, state):
    attempt(ledger, state="submitting" if state == "bound" else state)
    if state == "bound":
        ledger.db.execute("UPDATE operations SET job_id='job' WHERE request_id='r'")
    before = ledger.get_operation("s", SCOPE, "r")
    changes = ledger.db.total_changes
    with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_STATE"):
        ledger.validate_submitting("s", SCOPE, "r")
    assert ledger.get_operation("s", SCOPE, "r") == before
    assert ledger.db.total_changes == changes
