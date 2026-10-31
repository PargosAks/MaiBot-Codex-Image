"""Reference transport over signed local MCP and isolated SQLite; no external effects."""

from contextlib import asynccontextmanager
from copy import deepcopy
from unittest.mock import AsyncMock, Mock
import asyncio
import base64
import hashlib
import json
import socket
import sys
import types

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult
from starlette.applications import Starlette
from starlette.routing import Mount
import pytest
import pytest_asyncio
import uvicorn

from image_plugin_under_test.artist import render_digest
from image_plugin_under_test.bridge import Bridge, BridgeError
from image_plugin_under_test.policy import Delegation, Limits, StoreError
from image_plugin_under_test.recovery import ReadOnlyRecovery
from image_plugin_under_test.rendering import MCPRenderer
from image_plugin_under_test.store import ArtistStore
from conftest import SECRET
from protocol_fixture import PNG, ProtocolFixture
from test_rendering import BINDING, BRIEF, HASH, OTHER, PROMPT, SCOPE, parent


REFERENCES = [{"asset_id": "ref-owned", "sha256": HASH, "role": "style"}]
ASSET = {"asset_id": "ref-owned", "sha256": HASH, "mime": "image/png", "size_bytes": len(PNG)}
EXPECTED_ASSET = {"expected_sha256": HASH, "expected_mime": "image/png", "expected_size": len(PNG)}
JOB = {"job_id": "job", "status": "queued", "turn_status": None, "error_code": None, "artifacts": []}


def wire_args(mode="create"):
    return {"mode": mode, "prompt": PROMPT, "source": None if mode == "create" else
            {"job_id": "original", "sha256": HASH}, "references": deepcopy(REFERENCES)}


def digest(args):
    # Independent wire-contract oracle, including order, Unicode and nullable source.
    return hashlib.sha256(json.dumps({"operation": "render", **args}, ensure_ascii=True,
                                    sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def receipt(args=None, **changes):
    return {"operation": "render", "request_id": "r", "request_digest": digest(args or wire_args()),
            "job": deepcopy(JOB), **changes}


def bridge_with(response, *, probe=True):
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)
    bridge.call = AsyncMock(side_effect=([{"capabilities": ["reference_inputs_v1", "lookup_v1"]}] if probe else []) + [response])
    return bridge


def reference_attempt(db, *, action="submit", state="submitting", params_change=None):
    params = {"prompt": PROMPT, "reference_inputs": deepcopy(REFERENCES), "transport_operation": "render",
              "reference_contract": 1}
    if action == "edit":
        params.update(source_job_id="original", source_sha256=HASH)
    if params_change:
        params_change(params)
    db.reserve("s", SCOPE, request_id="r", action=action, params=params, work_id="work")
    db.update_artist_work("s", SCOPE, "work", changes={"pending_request": "r", "status": "creating"})
    db.mark_submitting("s", SCOPE, "r")
    if state == "unknown":
        db.mark_unknown("s", SCOPE, "r", reason="lost receipt")
    return db.get_operation("s", SCOPE, "r")


@pytest.fixture
def ledger(tmp_path):
    db = ArtistStore(tmp_path / "reference-transport.sqlite3")
    try:
        db.create_space("s", SCOPE, binding_id=BINDING)
        db.grant("s", SCOPE, Delegation("explicit-consent", "trusted-adapter", continuous=True))
        limits = Limits(render=10, think=10, share=10, max_inflight=10, max_revisions=5)
        db.set_global_limits(limits)
        db.set_limits("s", SCOPE, limits)
        db.configure_reference_policy("s", SCOPE, enabled=True, consent_ref="image-consent", expected_auth_version=1)
        asset = db.import_asset("s", SCOPE, event_id="fixture", message_index=0, data=PNG, mime="image/png",
                                roles=["style"], tags=[], description="synthetic PNG", consent_ref="image-consent",
                                expected_auth_version=1)
        db.reserve_asset_registration("s", SCOPE, asset_id=asset["asset_id"], expected_auth_version=1)
        db.claim_asset_registration("s", SCOPE, asset["asset_id"], expected_auth_version=1)
        db.complete_asset_registration("s", SCOPE, asset["asset_id"], ASSET)
        db.plan_work("s", SCOPE, work_id="work", brief=BRIEF, inspiration_keys=[],
                     references=[{"asset_id": asset["asset_id"], "role": "style", "reason": "synthetic study"}],
                     reference_usage="image_input", expected_auth_version=1)
        yield db
    finally:
        db.close()


@pytest.fixture
def reference_policy(ledger, monkeypatch):  # noqa: F811
    gate = Mock(wraps=ledger.validate_reference_authorization)
    monkeypatch.setattr(ledger, "validate_reference_authorization", gate)
    return gate


@pytest_asyncio.fixture
async def reference_protocol():
    verifier = ProtocolFixture(SECRET)
    mcp = FastMCP("Reference wire fixture", stateless_http=True, json_response=True,
                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                      allowed_hosts=["127.0.0.1:*"], allowed_origins=[]))
    assets, renders = {}, {}

    @mcp.tool()
    async def codex_image_health(envelope: str) -> dict:
        verifier.verify(envelope, "health")
        return {"capabilities": ["reference_inputs_v1", "lookup_v1"], "reference_limits": {
            "max_image_bytes": 8 * 1024 * 1024, "max_references": 3,
            "max_total_bytes": 32 * 1024 * 1024, "max_asset_bytes": 64 * 1024 * 1024,
        }}

    @mcp.tool()
    async def codex_image_asset_register(envelope: str) -> dict:
        owner, args = verifier.verify(envelope, "asset_register")
        assert args.keys() == {"request_id", "sha256", "mime", "image_base64"}
        assert base64.b64decode(args["image_base64"], validate=True) == PNG
        assets[owner, args["request_id"]] = deepcopy(ASSET)
        return deepcopy(ASSET)

    @mcp.tool()
    async def codex_image_asset_lookup(envelope: str) -> dict:
        owner, args = verifier.verify(envelope, "asset_lookup")
        assert args.keys() == {"request_id"}
        asset = assets.get((owner, args["request_id"]))
        return {"found": False} if asset is None else {"found": True, "asset": asset}

    @mcp.tool()
    async def codex_image_render(envelope: str) -> dict:
        owner, args = verifier.verify(envelope, "render")
        assert args.keys() == {"request_id", "mode", "prompt", "source", "references"}
        rendered = {k: v for k, v in args.items() if k != "request_id"}
        result = receipt(rendered, request_id=args["request_id"])
        renders[owner, args["request_id"]] = result
        return result

    @mcp.tool()
    async def codex_image_lookup(envelope: str) -> dict:
        owner, args = verifier.verify(envelope, "lookup")
        result = renders.get((owner, args["request_id"]))
        return {"found": False} if result is None else {"found": True, **result}

    transport = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield

    app = Starlette(routes=[Mount("/", app=transport)], lifespan=lifespan)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    bridge = Bridge(f"http://127.0.0.1:{sock.getsockname()[1]}/mcp", SECRET)
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Reference fixture stopped before startup")
                await asyncio.sleep(0.01)
        yield bridge, verifier
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        finally:
            sock.close()


async def test_signed_asset_and_reference_wire_contract(reference_protocol):
    bridge, verifier = reference_protocol
    scope = json.loads(SCOPE.snapshot())
    assert await bridge.asset_lookup(scope, "asset-request", **EXPECTED_ASSET) == {"found": False}
    assert await bridge.asset_register(scope, "asset-request", expected_sha256=HASH, mime="image/png", data=PNG) == ASSET
    assert await bridge.asset_lookup(scope, "asset-request", **EXPECTED_ASSET) == {"found": True, "asset": ASSET}
    assert await bridge.asset_lookup({**scope, "user_id": "foreign"}, "asset-request", **EXPECTED_ASSET) == {"found": False}
    for mode in ("create", "edit"):
        args = wire_args(mode)
        assert (await bridge.reference_render(scope, "r", **args))["request_digest"] == digest(args)
        found = await bridge.lookup(scope, "r", expected_operation="render", expected_digest=digest(args))
        assert found["operation"] == "render"
    assert set(op for op, *_ in verifier.calls) <= {"health", "asset_register", "asset_lookup", "render", "lookup"}


@pytest.mark.parametrize("method", ["asset_register", "reference_render"])
async def test_reference_capability_is_mandatory_without_fallback(method):
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)
    bridge.call = AsyncMock(return_value={"capabilities": ["edit_v1", "lookup_v1"]})
    args = {"expected_sha256": HASH, "mime": "image/png", "data": PNG} if method == "asset_register" else (
        wire_args() if method == "reference_render" else EXPECTED_ASSET)
    with pytest.raises(BridgeError, check=lambda e: e.code == "REFERENCE_INPUTS_UNSUPPORTED"):
        await getattr(bridge, method)({}, "r", **args)
    bridge.call.assert_awaited_once_with("health", {}, {})


@pytest.mark.parametrize("method", ["asset_register", "reference_render"])
@pytest.mark.parametrize("outcome", ["allow", "revoke", "cancel"])
async def test_reference_writes_recheck_after_health_and_join_no_retry(method, outcome):
    events = []
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)

    async def exchange(op, scope, args):
        events.append(op)
        return {"capabilities": ["reference_inputs_v1"]} if op == "health" else (
            deepcopy(ASSET) if op == "asset_register" else receipt())

    async def gate():
        events.append("gate")
        if outcome == "revoke":
            raise StoreError("AUTH_CHANGED", "synthetic")
        if outcome == "cancel":
            raise asyncio.CancelledError

    bridge.call = exchange
    args = {"expected_sha256": HASH, "mime": "image/png", "data": PNG} if method == "asset_register" else wire_args()
    if outcome == "allow":
        await getattr(bridge, method)({}, "r", **args, before_dispatch=gate)
        assert events == ["health", "gate", "asset_register" if method == "asset_register" else "render"]
    else:
        with pytest.raises(asyncio.CancelledError if outcome == "cancel" else StoreError):
            await getattr(bridge, method)({}, "r", **args, before_dispatch=gate)
        assert events == ["health", "gate"]


@pytest.mark.parametrize("change", [
    {"expected_sha256": "0" * 64}, {"expected_sha256": "bad"}, {"mime": "image/jpeg"}, {"mime": "text/plain"},
    {"data": b""}, {"data": bytearray(PNG)}, {"data": b"x" * (8 * 1024 * 1024 + 1)},
])
async def test_asset_bytes_hash_mime_and_size_are_checked_before_health(change):
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)
    bridge.call = AsyncMock()
    with pytest.raises(BridgeError, check=lambda e: e.code == "INVALID_ARGUMENTS"):
        await bridge.asset_register({}, "r", **({"expected_sha256": HASH, "mime": "image/png", "data": PNG} | change))
    bridge.call.assert_not_awaited()


@pytest.mark.parametrize("change,code", [
    ({"sha256": "0" * 64}, "RECEIPT_MISMATCH"), ({"mime": "image/webp"}, "RECEIPT_MISMATCH"),
    ({"size_bytes": len(PNG) + 1}, "RECEIPT_MISMATCH"), ({"size_bytes": True}, "INVALID_RESPONSE"),
    ({"asset_id": ""}, "INVALID_RESPONSE"), ({"path": "private"}, "INVALID_RESPONSE"),
])
async def test_asset_registration_receipt_is_strict_and_content_bound(change, code):
    bridge = bridge_with({**ASSET, **change})
    with pytest.raises(BridgeError, check=lambda e: e.code == code):
        await bridge.asset_register({}, "r", expected_sha256=HASH, mime="image/png", data=PNG)
    assert bridge.call.await_count == 2


@pytest.mark.parametrize("response", [
    {}, {"found": 1}, {"found": False, "asset": ASSET}, {"found": True},
    {"found": True, "asset": {**ASSET, "sha256": "bad"}},
    {"found": True, "asset": {**ASSET, "size_bytes": 8 * 1024 * 1024 + 1}},
])
async def test_asset_lookup_rejects_malformed_response_without_registering(response):
    bridge = bridge_with(response, probe=False)
    with pytest.raises(BridgeError, check=lambda e: e.code == "INVALID_RESPONSE"):
        await bridge.asset_lookup({}, "r", **EXPECTED_ASSET)
    assert [call.args[0] for call in bridge.call.await_args_list] == ["asset_lookup"]


@pytest.mark.parametrize("change", [{"sha256": "0" * 64}, {"mime": "image/webp"}, {"size_bytes": len(PNG) + 1}])
async def test_asset_lookup_binds_expected_content(change):
    bridge = bridge_with({"found": True, "asset": {**ASSET, **change}}, probe=False)
    with pytest.raises(BridgeError, check=lambda e: e.code == "RECEIPT_MISMATCH"):
        await bridge.asset_lookup({}, "r", **EXPECTED_ASSET)
    assert [call.args[0] for call in bridge.call.await_args_list] == ["asset_lookup"]


async def test_original_asset_lookup_does_not_depend_on_drawing_capability():
    bridge = bridge_with({"found": True, "asset": ASSET}, probe=False)
    bridge.capabilities = AsyncMock(side_effect=AssertionError("Read recovery must not probe drawing capability"))
    assert await bridge.asset_lookup({}, "original", **EXPECTED_ASSET) == {"found": True, "asset": ASSET}
    bridge.call.assert_awaited_once_with("asset_lookup", {}, {"request_id": "original"})


@pytest.mark.parametrize("change", [{"expected_sha256": "bad"}, {"expected_mime": "text/plain"},
                                     {"expected_size": True}, {"expected_size": 8 * 1024 * 1024 + 1}])
async def test_asset_lookup_expected_identity_is_checked_before_health(change):
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)
    bridge.call = AsyncMock()
    with pytest.raises(BridgeError, check=lambda e: e.code == "INVALID_ARGUMENTS"):
        await bridge.asset_lookup({}, "r", **(EXPECTED_ASSET | change))
    bridge.call.assert_not_awaited()


@pytest.mark.parametrize("change", [
    {"mode": "submit"}, {"source": {}}, {"mode": "edit", "source": None},
    {"mode": "edit", "source": {"job_id": "original", "sha256": HASH, "path": "forged"}},
    {"prompt": "x" * 4001}, {"references": []}, {"references": REFERENCES * 4},
    {"references": [{**REFERENCES[0], "role": "owner"}]},
    {"references": [{**REFERENCES[0], "sha256": "bad"}]},
    {"references": [{**REFERENCES[0], "data": PNG}]},
])
async def test_reference_render_shape_mode_roles_and_bounds_before_dispatch(change):
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)
    bridge.call = AsyncMock()
    with pytest.raises(BridgeError, check=lambda e: e.code == "INVALID_ARGUMENTS"):
        await bridge.reference_render({}, "r", **(wire_args() | change))
    bridge.call.assert_not_awaited()


@pytest.mark.parametrize("change,code", [
    ({"operation": "submit"}, "RECEIPT_MISMATCH"), ({"operation": "edit"}, "RECEIPT_MISMATCH"),
    ({"request_id": "foreign"}, "RECEIPT_MISMATCH"), ({"request_digest": "0" * 64}, "RECEIPT_MISMATCH"),
    ({"job": {}}, "INVALID_RESPONSE"), ({"extra": True}, "INVALID_RESPONSE"),
])
async def test_render_receipt_cannot_cross_operation_request_or_digest(change, code):
    bridge = bridge_with(receipt(**change))
    with pytest.raises(BridgeError, check=lambda e: e.code == code):
        await bridge.reference_render({}, "r", **wire_args())
    assert [call.args[0] for call in bridge.call.await_args_list] == ["health", "render"]


@pytest.mark.parametrize("error", [TimeoutError(), ConnectionError(), asyncio.CancelledError()])
@pytest.mark.parametrize("method", ["asset_register", "reference_render"])
async def test_reference_transport_failure_has_no_retry_or_legacy_fallback(error, method):
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)
    bridge._exchange = AsyncMock(side_effect=[CallToolResult(content=[], structuredContent={
        "capabilities": ["reference_inputs_v1"]}), error])
    args = {"expected_sha256": HASH, "mime": "image/png", "data": PNG} if method == "asset_register" else wire_args()
    with pytest.raises(asyncio.CancelledError if isinstance(error, asyncio.CancelledError) else BridgeError) as caught:
        await getattr(bridge, method)({}, "r", **args)
    if not isinstance(error, asyncio.CancelledError):
        assert caught.value.code == "TRANSPORT_UNKNOWN"
    assert bridge._exchange.await_count == 2


@pytest.mark.parametrize("action", ["submit", "edit"])
async def test_renderer_reference_path_uses_frozen_render_and_does_not_write_ledger(ledger, reference_protocol, reference_policy, action):  # noqa: F811
    bridge, verifier = reference_protocol
    source = parent(ledger) if action == "edit" else None
    before = reference_attempt(ledger, action=action)
    adapter = MCPRenderer(ledger, "s", SCOPE, bridge, binding_id=BINDING)
    changes = ledger.db.total_changes
    result = await adapter.submit(scope=SCOPE, request_id="r", prompt=PROMPT, source=source)
    assert result.request_digest == digest(wire_args("create" if action == "submit" else "edit"))
    assert result.state == "queued" and ledger.db.total_changes == changes
    assert ledger.get_operation("s", SCOPE, "r") == before
    assert reference_policy.call_count == 3
    reference_policy.assert_called_with("s", SCOPE, expected_auth_version=before["auth_version"])
    assert [op for op, *_ in verifier.calls] == ["health", "health", "render"]
    assert (await adapter.lookup(scope=SCOPE, request_id="r")).request_digest == result.request_digest
    assert verifier.calls[-1][0] == "lookup"


@pytest.mark.parametrize("mutation", [
    lambda p: p.pop("transport_operation"), lambda p: p.pop("reference_inputs"),
    lambda p: p.pop("reference_contract"), lambda p: p.update(reference_contract=True),
    lambda p: p.update(reference_contract=2), lambda p: p.update(reference_contract="1"),
    lambda p: p.update(transport_operation="submit"), lambda p: p.update(reference_inputs=[]),
    lambda p: p.update(reference_inputs=[{**REFERENCES[0], "scope": "forged"}]),
])
async def test_renderer_rejects_partial_or_forged_reference_params_before_network(ledger, mutation):  # noqa: F811
    reference_attempt(ledger, params_change=mutation)
    bridge = AsyncMock()
    with pytest.raises(StoreError, check=lambda e: e.code == "REQUEST_CONFLICT"):
        await MCPRenderer(ledger, "s", SCOPE, bridge, binding_id=BINDING).submit(
            scope=SCOPE, request_id="r", prompt=PROMPT, source=None)
    assert not bridge.mock_calls


async def test_renderer_rechecks_authorization_after_reference_health(ledger, reference_policy):  # noqa: F811
    reference_attempt(ledger)
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)
    calls = []

    async def health(op, scope, args):
        calls.append(op)
        assert op == "health"
        if len(calls) == 2:
            ledger.revoke("s", SCOPE, source_ref="withdraw", granted_by="admin")
        return {"capabilities": ["lookup_v1", "reference_inputs_v1"]}

    bridge.call = health
    with pytest.raises(StoreError, check=lambda e: e.code == "AUTH_REQUIRED"):
        await MCPRenderer(ledger, "s", SCOPE, bridge, binding_id=BINDING).submit(
            scope=SCOPE, request_id="r", prompt=PROMPT, source=None)
    assert calls == ["health", "health"]


async def test_reference_policy_disabled_during_health_prevents_render(ledger, reference_policy):  # noqa: F811
    reference_attempt(ledger)
    bridge, calls = Bridge("http://127.0.0.1/mcp", SECRET), []

    async def health(op, scope, args):
        calls.append(op)
        assert op == "health"
        if len(calls) == 2:
            ledger.configure_reference_policy("s", SCOPE, enabled=False, consent_ref="image-consent",
                                              expected_auth_version=1)
        return {"capabilities": ["lookup_v1", "reference_inputs_v1"]}

    bridge.call = health
    with pytest.raises(StoreError, check=lambda e: e.code == "REFERENCE_AUTH_REQUIRED"):
        await MCPRenderer(ledger, "s", SCOPE, bridge, binding_id=BINDING).submit(
            scope=SCOPE, request_id="r", prompt=PROMPT, source=None)
    assert calls == ["health", "health"] and reference_policy.call_count == 3
    assert ledger.get_operation("s", SCOPE, "r")["state"] == "submitting"


@pytest.mark.parametrize("failure", [False, True])
async def test_prepare_references_delegates_registry_policy_and_total_size(ledger, monkeypatch, failure):  # noqa: F811
    module = types.ModuleType("image_plugin_under_test.references")
    registry = types.SimpleNamespace(prepare=AsyncMock(return_value=deepcopy(REFERENCES)))
    module.ReferenceInputs = Mock(return_value=registry)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    bridge = AsyncMock()
    adapter = MCPRenderer(ledger, "s", SCOPE, bridge, binding_id=BINDING)
    if failure:
        registry.prepare.side_effect = BridgeError("TRANSPORT_UNKNOWN", kind="transport")
        with pytest.raises(BridgeError, check=lambda e: e.code == "TRANSPORT_UNKNOWN"):
            await adapter.prepare_references(scope=SCOPE, references=REFERENCES, expected_auth_version=1, source_size=123)
    else:
        assert await adapter.prepare_references(scope=SCOPE, references=REFERENCES,
                                                expected_auth_version=1, source_size=123) == REFERENCES
    module.ReferenceInputs.assert_called_once_with(ledger, "s", SCOPE, bridge, binding_id=BINDING)
    registry.prepare.assert_awaited_once_with(REFERENCES, expected_auth_version=1, source_size=123)
    assert not bridge.mock_calls


async def test_prepare_references_rejects_cross_scope_before_registry(ledger, monkeypatch):  # noqa: F811
    module = types.ModuleType("image_plugin_under_test.references")
    module.ReferenceInputs = Mock()
    monkeypatch.setitem(sys.modules, module.__name__, module)
    adapter = MCPRenderer(ledger, "s", SCOPE, AsyncMock(), binding_id=BINDING)
    with pytest.raises(StoreError, check=lambda e: e.code == "INVALID_SCOPE"):
        await adapter.prepare_references(scope=OTHER, references=REFERENCES, expected_auth_version=1)
    module.ReferenceInputs.assert_not_called()


async def test_prepare_references_real_registry_registers_once_then_reuses_receipt(ledger, reference_protocol):
    bridge, verifier = reference_protocol
    asset = ledger.import_asset("s", SCOPE, event_id="second-image", message_index=0, data=PNG,
                                mime="image/png", roles=["style"], tags=[], description="synthetic second PNG",
                                consent_ref="image-consent", expected_auth_version=1)
    frozen = deepcopy(ledger.get_artist_work("s", SCOPE, "work")["brief"]["references"])
    frozen[0]["asset_id"] = asset["asset_id"]
    adapter = MCPRenderer(ledger, "s", SCOPE, bridge, binding_id=BINDING)
    for _ in range(2):
        assert await adapter.prepare_references(scope=SCOPE, references=frozen,
                                                expected_auth_version=1) == REFERENCES
    assert [op for op, *_ in verifier.calls] == ["health", "health", "asset_register", "health"]
    assert ledger.get_asset_registration("s", SCOPE, asset["asset_id"])["state"] == "succeeded"


@pytest.mark.parametrize("health", [
    {"capabilities": ["reference_inputs_v1"]},
    {"capabilities": ["reference_inputs_v1"], "reference_limits": {}},
])
async def test_prepare_references_registry_rejects_missing_health_limits_without_writes(ledger, health):
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)
    bridge.call = AsyncMock(return_value=health)
    adapter = MCPRenderer(ledger, "s", SCOPE, bridge, binding_id=BINDING)
    frozen = ledger.get_artist_work("s", SCOPE, "work")["brief"]["references"]
    before = ledger.db.total_changes
    with pytest.raises(BridgeError, check=lambda e: e.code == "INVALID_RESPONSE"):
        await adapter.prepare_references(scope=SCOPE, references=frozen, expected_auth_version=1)
    assert ledger.db.total_changes == before
    bridge.call.assert_awaited_once_with("health", json.loads(SCOPE.snapshot()), {})


async def test_reference_policy_revision_change_during_capability_wait_prevents_render(ledger):
    reference_attempt(ledger)
    bridge, calls = Bridge("http://127.0.0.1/mcp", SECRET), []

    async def health(op, scope, args):
        calls.append(op)
        assert op == "health"
        if len(calls) == 2:
            ledger.configure_reference_policy("s", SCOPE, enabled=True, consent_ref="new-image-consent",
                                              expected_auth_version=1)
        return {"capabilities": ["lookup_v1", "reference_inputs_v1"]}

    bridge.call = health
    with pytest.raises(StoreError, check=lambda e: e.code == "REFERENCE_AUTH_CHANGED"):
        await MCPRenderer(ledger, "s", SCOPE, bridge, binding_id=BINDING).submit(
            scope=SCOPE, request_id="r", prompt=PROMPT, source=None)
    assert calls == ["health", "health"]


@pytest.mark.parametrize("boundary", ["usage", "policy", "handle", "role", "state", "binding", "hash", "mime", "source"])
async def test_submission_rejects_reference_plan_registration_and_source_drift(ledger, monkeypatch, boundary):
    params_change = None
    if boundary in {"handle", "role"}:
        def params_change(params):
            params["reference_inputs"][0]["asset_id" if boundary == "handle" else "role"] = (
                "foreign" if boundary == "handle" else "palette")
    reference_attempt(ledger, params_change=params_change)
    if boundary in {"usage", "policy"}:
        original = ledger.get_artist_work

        def changed_work(*args, **kwargs):
            work = original(*args, **kwargs)
            work["brief"]["references"][0]["usage" if boundary == "usage" else "policy_version"] = (
                "text_context" if boundary == "usage" else True)
            return work
        monkeypatch.setattr(ledger, "get_artist_work", changed_work)
    elif boundary in {"state", "binding", "hash", "mime"}:
        original = ledger.get_asset_registration

        def changed_registration(*args, **kwargs):
            registration = original(*args, **kwargs)
            key, value = {"state": ("state", "unknown"), "binding": ("binding_id", "foreign"),
                          "hash": ("sha256", "0" * 64), "mime": ("mime", "image/jpeg")}[boundary]
            registration[key] = value
            return registration
        monkeypatch.setattr(ledger, "get_asset_registration", changed_registration)
    elif boundary == "source":
        original = ledger.get_asset_image

        def changed_source(*args, **kwargs):
            return {**original(*args, **kwargs), "sha256": "0" * 64}
        monkeypatch.setattr(ledger, "get_asset_image", changed_source)
    bridge = AsyncMock()
    with pytest.raises(StoreError):
        await MCPRenderer(ledger, "s", SCOPE, bridge, binding_id=BINDING).submit(
            scope=SCOPE, request_id="r", prompt=PROMPT, source=None)
    assert not bridge.mock_calls


@pytest.mark.parametrize("outcome", ["not_found", "wrong_operation", "wrong_digest", "malformed", "transport", "cancel", "complete"])
@pytest.mark.parametrize("action", ["submit", "edit"])
async def test_reference_unknown_recovery_binds_remote_render_and_retains_holds(ledger, outcome, action):  # noqa: F811
    reference_attempt(ledger, action=action, state="unknown")
    ledger.revoke("s", SCOPE, source_ref="withdraw", granted_by="admin")
    expected = digest(wire_args("create" if action == "submit" else "edit"))
    bridge = AsyncMock()
    result = {"found": True, **receipt(wire_args("create" if action == "submit" else "edit"))}
    if outcome == "not_found":
        result = {"found": False}
    elif outcome == "wrong_operation":
        result["operation"] = action
    elif outcome == "wrong_digest":
        result["request_digest"] = "0" * 64
    elif outcome == "malformed":
        result["job"] = {}
    elif outcome == "complete":
        result["job"].update(status="completed", turn_status="completed")
    if outcome in {"transport", "cancel"}:
        bridge.lookup.side_effect = asyncio.CancelledError() if outcome == "cancel" else BridgeError("TRANSPORT_UNKNOWN", kind="transport")
    else:
        bridge.lookup.return_value = result
    recovery = ReadOnlyRecovery(ledger, bridge)
    if outcome in {"not_found", "complete"}:
        settled = await recovery.reconcile("s", SCOPE, "r")
        assert settled["state"] == ("succeeded" if outcome == "complete" else "unknown")
    else:
        with pytest.raises(asyncio.CancelledError if outcome == "cancel" else (StoreError, BridgeError)):
            await recovery.reconcile("s", SCOPE, "r")
    state = ledger.get_operation("s", SCOPE, "r")["state"]
    assert state == ("succeeded" if outcome == "complete" else "unknown")
    assert ledger.budget_usage("s", SCOPE)["render"]["held"] == (0 if outcome == "complete" else 1)
    bridge.lookup.assert_awaited_once_with(json.loads(SCOPE.snapshot()), "r", expected_operation="render",
                                          expected_digest=expected, expected_job_id=None)
    assert len(bridge.mock_calls) == 1 and ledger.list_versions("s", SCOPE, "work") == []


@pytest.mark.parametrize("remote", ["render", "submit", "edit"])
async def test_bridge_lookup_checks_remote_render_operation_and_old_operation_binding(remote):
    bridge = bridge_with({"found": True, **receipt(operation=remote)})
    if remote == "render":
        assert (await bridge.lookup({}, "r", expected_operation="render", expected_digest=digest(wire_args())))["found"]
    else:
        with pytest.raises(BridgeError, check=lambda e: e.code == "RECEIPT_MISMATCH"):
            await bridge.lookup({}, "r", expected_operation="render", expected_digest=digest(wire_args()))


@pytest.mark.parametrize("operation", ["submit", "edit"])
async def test_legacy_lookup_cannot_accept_render_receipt_even_with_matching_digest(operation):
    bridge = bridge_with({"found": True, **receipt()})
    with pytest.raises(BridgeError, check=lambda e: e.code == "RECEIPT_MISMATCH"):
        await bridge.lookup({}, "r", expected_operation=operation, expected_digest=digest(wire_args()))


async def test_capability_is_not_cached_across_reference_writes():
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)
    bridge.call = AsyncMock(side_effect=[{"capabilities": ["reference_inputs_v1"]}, receipt(),
                                        {"capabilities": ["lookup_v1"]}])
    assert await bridge.reference_render({}, "r", **wire_args()) == receipt()
    with pytest.raises(BridgeError, check=lambda e: e.code == "REFERENCE_INPUTS_UNSUPPORTED"):
        await bridge.reference_render({}, "r", **wire_args())
    assert [call.args[0] for call in bridge.call.await_args_list] == ["health", "render", "health"]


def test_legacy_digests_are_unchanged_and_cannot_alias_reference_render():
    assert render_digest("submit", {"prompt": PROMPT}) == hashlib.sha256(PROMPT.encode()).hexdigest()
    params = {"prompt": PROMPT, "source_job_id": "original", "source_sha256": HASH}
    old_edit = hashlib.sha256(json.dumps({"operation": "edit", **params}, ensure_ascii=True,
                                        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert render_digest("edit", params) == old_edit
    assert digest(wire_args()) not in {old_edit, hashlib.sha256(PROMPT.encode()).hexdigest()}


@pytest.mark.parametrize("action", ["submit", "edit"])
def test_artist_and_transport_reference_digests_match_the_wire_contract(action):
    params = {"prompt": PROMPT, "transport_operation": "render", "reference_contract": 1,
              "reference_inputs": deepcopy(REFERENCES)}
    if action == "edit":
        params.update(source_job_id="original", source_sha256=HASH)
    assert render_digest(action, params) == digest(wire_args("create" if action == "submit" else "edit"))
