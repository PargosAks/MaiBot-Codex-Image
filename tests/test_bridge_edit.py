"""Edit is capability-gated; image results retain their verified byte identity."""

from copy import deepcopy
from unittest.mock import AsyncMock
import base64
import hashlib

from mcp.types import CallToolResult, ImageContent
import pytest

from image_plugin_under_test.artist import render_digest
from image_plugin_under_test.bridge import Bridge, BridgeError
from conftest import SCOPE, SECRET
from protocol_fixture import PNG

PARAMS = {
    "source_job_id": "img_original",
    "source_sha256": hashlib.sha256(PNG).hexdigest(),
    "prompt": " 保留主体，柔化光线 ",
}
RESPONSE = {
    "operation": "edit",
    "request_id": "edit-request",
    "request_digest": render_digest("edit", PARAMS),
    "job": {
        "job_id": "img_child",
        "status": "queued",
        "turn_status": None,
        "error_code": None,
        "artifacts": [],
    },
}


async def test_edit_preserves_exact_frozen_parameters_and_digest():
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)
    bridge.call = AsyncMock(side_effect=[{"capabilities": ["lookup_v1", "edit_v1"]}, deepcopy(RESPONSE)])
    assert await bridge.edit(SCOPE, "edit-request", **PARAMS) == RESPONSE
    assert bridge.call.await_args_list[1].args == ("edit", SCOPE, {"request_id": "edit-request", **PARAMS})


async def test_unsupported_edit_has_no_text_only_fallback():
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)
    bridge.call = AsyncMock(return_value={"capabilities": ["lookup_v1"]})
    with pytest.raises(BridgeError, check=lambda e: e.code == "EDIT_UNSUPPORTED"):
        await bridge.edit(SCOPE, "edit-request", **PARAMS)
    bridge.call.assert_awaited_once_with("health", SCOPE, {})


@pytest.mark.parametrize(
    "change,code",
    [
        ({"request_id": "other"}, "RECEIPT_MISMATCH"),
        ({"operation": "submit"}, "RECEIPT_MISMATCH"),
        ({"request_digest": "0" * 64}, "RECEIPT_MISMATCH"),
        ({"job": {}}, "INVALID_RESPONSE"),
    ],
)
async def test_edit_rejects_receipts_for_other_content(change, code):
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)
    bridge.call = AsyncMock(side_effect=[{"capabilities": ["edit_v1"]}, dict(RESPONSE, **change)])
    with pytest.raises(BridgeError, check=lambda e: e.code == code):
        await bridge.edit(SCOPE, "edit-request", **PARAMS)
    assert bridge.call.await_count == 2


@pytest.mark.parametrize(
    "change", [{"source_sha256": "invalid"}, {"source_job_id": ""}, {"prompt": " "}, {"prompt": "x" * 4001}]
)
async def test_invalid_edit_arguments_never_reach_transport(change):
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)
    bridge.call = AsyncMock()
    with pytest.raises(BridgeError, check=lambda e: e.code == "INVALID_ARGUMENTS"):
        await bridge.edit(SCOPE, "edit-request", **dict(PARAMS, **change))
    bridge.call.assert_not_awaited()


@pytest.mark.parametrize("failure", [None, "hash", "mime", "size", "base64", "multiple"])
async def test_version_image_input_keeps_mime_size_and_hash(failure):
    bridge = Bridge("http://127.0.0.1/mcp", SECRET)
    image = ImageContent(type="image", data=base64.b64encode(PNG).decode(), mimeType="image/png")
    if failure == "mime":
        image.mimeType = "image/jpeg"
    if failure == "base64":
        image.data = "invalid base64"
    bridge._exchange = AsyncMock(
        return_value=CallToolResult(content=[image] * (2 if failure == "multiple" else 1))
    )
    arguments = {
        "expected_sha256": "0" * 64 if failure == "hash" else PARAMS["source_sha256"],
        "expected_mime": "image/png",
        "expected_size": len(PNG) + (1 if failure == "size" else 0),
    }
    if failure:
        with pytest.raises(BridgeError, check=lambda e: e.code == "ARTIFACT_INVALID"):
            await bridge.image(SCOPE, "img_child", **arguments)
    else:
        result = await bridge.image(SCOPE, "img_child", **arguments)
        assert result == {"data": PNG, "mime": "image/png", "sha256": PARAMS["source_sha256"]}
    bridge._exchange.assert_awaited_once_with("result", SCOPE, {"job_id": "img_child"})
