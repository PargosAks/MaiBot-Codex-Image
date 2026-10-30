"""Lookup transport and strict recovery response boundaries; no real service."""

from copy import deepcopy
from unittest.mock import AsyncMock
import asyncio
import hashlib
import json

import pytest
from mcp.types import CallToolResult, TextContent

from image_plugin_under_test.bridge import Bridge, BridgeError
from conftest import SCOPE, SECRET

DIGEST = hashlib.sha256(b"circle").hexdigest()
RECEIPT = {
    "found": True,
    "operation": "submit",
    "request_id": "lost",
    "request_digest": DIGEST,
    "job": {
        "job_id": "img_owned",
        "status": "outcome_unknown",
        "turn_status": None,
        "error_code": "SERVICE_RESTARTED",
        "artifacts": [],
    },
}


async def lookup(bridge, **kwargs):
    return await bridge.lookup(SCOPE, "lost", expected_operation="submit", expected_digest=DIGEST, **kwargs)


async def test_lookup_uses_real_signed_transport_and_never_submits(mcp_protocol_fixture):
    fixture = mcp_protocol_fixture
    fixture.capabilities = ["lookup_v1"]
    bridge = Bridge(fixture.url, SECRET)
    job = await bridge.call("submit", SCOPE, {"request_id": "lost", "prompt": "circle"})
    before = deepcopy((fixture.jobs, fixture.requests))
    assert (await lookup(bridge, expected_job_id=job["job_id"]))["found"] is True
    assert (
        await bridge.lookup(
            dict(SCOPE, user_id="another"), "lost", expected_operation="submit", expected_digest=DIGEST
        )
    ) == {"found": False}
    assert (fixture.jobs, fixture.requests) == before
    assert [op for op, *_ in fixture.calls] == ["submit", "health", "lookup", "health", "lookup"]


async def test_missing_capability_never_guesses_or_replays(mcp_protocol_fixture):
    fixture = mcp_protocol_fixture
    bridge = Bridge(fixture.url, SECRET)
    with pytest.raises(BridgeError, check=lambda e: e.code == "LOOKUP_UNSUPPORTED"):
        await lookup(bridge)
    assert [op for op, *_ in fixture.calls] == ["health"]
    fixture.capabilities = ["lookup_v1"]
    assert await lookup(bridge) == {"found": False}
    fixture.capabilities = None
    with pytest.raises(BridgeError, check=lambda e: e.code == "LOOKUP_UNSUPPORTED"):
        await lookup(bridge)
    assert fixture.jobs == {}


@pytest.mark.parametrize("code", ["REQUEST_CONFLICT", "QUEUE_FULL", "INVALID_ARGUMENTS", "FUTURE_ERROR"])
@pytest.mark.parametrize("structured", [True, False])
async def test_error_code_survives_without_remote_private_message(code, structured):
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)
    data = {"error": {"code": code, "message": "private-path-token-not-for-display"}}
    bridge._exchange = AsyncMock(
        return_value=CallToolResult(
            isError=True,
            structuredContent=data if structured else None,
            content=[TextContent(type="text", text=json.dumps(data))],
        )
    )
    with pytest.raises(BridgeError) as caught:
        await bridge.call("submit", SCOPE, {"request_id": "lost", "prompt": "circle"})
    assert caught.value.code == code and caught.value.kind == "remote"
    assert "private-path-token" not in str(caught.value)
    bridge._exchange.assert_awaited_once()


@pytest.mark.parametrize("text", ["legacy rejection", '{"error":{"code":"unsafe token data"}}'])
async def test_legacy_untyped_rejection_is_not_a_definite_preaccept_failure(text):
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)
    bridge._exchange = AsyncMock(
        return_value=CallToolResult(isError=True, content=[TextContent(type="text", text=text)])
    )
    with pytest.raises(BridgeError, check=lambda e: e.code == "REMOTE_ERROR_UNKNOWN"):
        await bridge.call("submit", SCOPE, {})


@pytest.mark.parametrize(
    "error", [TimeoutError(), ConnectionError(), ExceptionGroup("transport", [OSError()])]
)
async def test_transport_unknown_is_not_retried(error):
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)
    bridge._exchange = AsyncMock(side_effect=error)
    with pytest.raises(BridgeError, check=lambda e: e.code == "TRANSPORT_UNKNOWN"):
        await bridge.call("submit", SCOPE, {})
    bridge._exchange.assert_awaited_once()


async def test_cancel_propagates_without_retry():
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)
    bridge._exchange = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await bridge.call("lookup", SCOPE, {})
    bridge._exchange.assert_awaited_once()


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda r: r.update(found="true"), "INVALID_RESPONSE"),
        (lambda r: r.update(found=False), "INVALID_RESPONSE"),
        (lambda r: r.update(request_id="foreign"), "RECEIPT_MISMATCH"),
        (lambda r: r.update(operation="edit"), "RECEIPT_MISMATCH"),
        (lambda r: r.update(request_digest="0" * 64), "RECEIPT_MISMATCH"),
        (lambda r: r["job"].update(status="new-state"), "INVALID_RESPONSE"),
        (lambda r: r["job"].update(job_id="foreign"), "RECEIPT_MISMATCH"),
        (lambda r: r["job"].pop("turn_status"), "INVALID_RESPONSE"),
        (lambda r: r["job"].update(artifacts=[{"path": "/private"}]), "INVALID_RESPONSE"),
    ],
)
async def test_lookup_cannot_bind_malformed_or_wrong_receipt(change, code):
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)
    response = deepcopy(RECEIPT)
    change(response)
    bridge.call = AsyncMock(side_effect=[{"capabilities": ["lookup_v1"]}, response])
    with pytest.raises(BridgeError, check=lambda e: e.code == code):
        await lookup(bridge, expected_job_id="img_owned")
    assert [call.args[0] for call in bridge.call.await_args_list] == ["health", "lookup"]


@pytest.mark.parametrize("capabilities", [None, "lookup_v1", [""], [1], ["x"] * 33])
async def test_malformed_capabilities_are_not_guessed(capabilities):
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)
    bridge.call = AsyncMock(return_value={"capabilities": capabilities})
    with pytest.raises(BridgeError, check=lambda e: e.code == "INVALID_RESPONSE"):
        await lookup(bridge)
    bridge.call.assert_awaited_once()


@pytest.mark.parametrize("turn_status", ["running", "failed", "inProgress", "unknown"])
async def test_completed_cannot_contradict_explicit_turn_status(turn_status):
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)
    response = deepcopy(RECEIPT)
    response["job"].update(status="completed", turn_status=turn_status)
    bridge.call = AsyncMock(side_effect=[{"capabilities": ["lookup_v1"]}, response])
    with pytest.raises(BridgeError, check=lambda e: e.code == "INVALID_RESPONSE"):
        await lookup(bridge)


@pytest.mark.parametrize(
    "status,turn_status",
    [
        ("completed", None),
        ("completed", "completed"),
        ("failed", "completed"),
        ("artifact_ready", "failed"),
    ],
)
async def test_nullable_legacy_and_known_failed_turn_combinations_remain_valid(status, turn_status):
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)
    response = deepcopy(RECEIPT)
    response["job"].update(status=status, turn_status=turn_status)
    bridge.call = AsyncMock(side_effect=[{"capabilities": ["lookup_v1"]}, response])
    assert await lookup(bridge) == response
