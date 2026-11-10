"""验证后端终态与图片归档分离：所有外部效果均为本地替身。"""

from types import SimpleNamespace
import asyncio
import hashlib

import pytest

from image_plugin_under_test.creation.artist import Artist
from image_plugin_under_test.creation.rendering import MCPRenderer
from image_plugin_under_test.creation.thinking import Thinking
from image_plugin_under_test.domain.policy import Delegation, Limits
from image_plugin_under_test.domain.store import ArtistStore
from image_plugin_under_test.mcp_client.bridge import BridgeError
from protocol_fixture import PNG
from support.render_ledger import BINDING, BRIEF, SCOPE, attempt


class ReceiptBridge:
    def __init__(self, state, *, missing=False):
        self.state, self.missing = state, missing
        self.failure = BridgeError("TRANSPORT_UNKNOWN", kind="transport")
        self.lookups, self.downloads = 0, 0

    async def lookup(self, scope, request_id, *, expected_operation, expected_digest, expected_job_id):
        self.lookups += 1
        return {
            "found": True, "operation": expected_operation, "request_id": request_id,
            "request_digest": expected_digest,
            "job": {"job_id": "job", "status": self.state,
                    "turn_status": self.state if self.state != "outcome_unknown" else None,
                    "error_code": None, "artifacts": [] if self.missing else [
                        {"asset_id": "image", "sha256": hashlib.sha256(PNG).hexdigest(),
                         "mime_type": "image/png", "size_bytes": len(PNG)}]},
        }

    async def image(self, scope, job_id, **expected):
        self.downloads += 1
        if self.failure is not None:
            raise self.failure
        return {"data": PNG, "mime": "image/png", "sha256": hashlib.sha256(PNG).hexdigest()}


@pytest.fixture
def receipt_flow(tmp_path):
    store = ArtistStore(tmp_path / "receipt.sqlite3")
    store.create_space("s", SCOPE, binding_id=BINDING)
    store.grant("s", SCOPE, Delegation("synthetic-consent", "test", continuous=True))
    limits = Limits(render=10, think=10, share=10, max_inflight=10, max_revisions=5)
    store.set_global_limits(limits)
    store.set_limits("s", SCOPE, limits)
    store.plan_work("s", SCOPE, work_id="work", brief=BRIEF, inspiration_keys=[])
    attempt(store, state="accepted")

    async def forbidden(*args, **kwargs):
        pytest.fail("回执恢复不得调用模型、重新绘图或发送")

    def make(state, **kwargs):
        bridge = ReceiptBridge(state, **kwargs)
        renderer = MCPRenderer(store, "s", SCOPE, bridge, binding_id=BINDING)
        artist = Artist(store, "s", SCOPE, Thinking(forbidden), renderer,
                        SimpleNamespace(simulation=True, send=forbidden), allow_network_renderer=True)
        return artist, bridge

    yield store, make
    store.close()


@pytest.mark.parametrize("state,expected", [("completed", "succeeded"), ("failed", "failed")])
async def test_terminal_receipt_is_committed_before_failed_image_download(receipt_flow, state, expected):
    store, make = receipt_flow
    artist, bridge = make(state)
    work = store.get_artist_work("s", SCOPE, "work")
    result = await artist._poll(work)
    assert result["state"] == "blocked"
    assert store.get_operation("s", SCOPE, "r")["state"] == expected
    assert not store.list_versions("s", SCOPE, "work")
    # 只补取原任务的图；已保存的终态与原请求标识保持不变。
    bridge.failure = None
    assert (await artist._poll(work))["state"] == "review_pending"
    version, = store.list_versions("s", SCOPE, "work")
    assert version["job_id"] == "job" and version["request_id"] == "r"
    assert store.get_operation("s", SCOPE, "r")["state"] == expected
    assert bridge.lookups == bridge.downloads == 2


async def test_cancellation_during_image_download_preserves_terminal_receipt(receipt_flow):
    store, make = receipt_flow
    artist, bridge = make("completed")
    bridge.failure = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await artist._poll(store.get_artist_work("s", SCOPE, "work"))
    assert store.get_operation("s", SCOPE, "r")["state"] == "succeeded"
    assert not store.list_versions("s", SCOPE, "work")


async def test_unknown_receipt_without_image_requests_backoff(receipt_flow):
    store, make = receipt_flow
    artist, bridge = make("outcome_unknown", missing=True)
    result = await artist._poll(store.get_artist_work("s", SCOPE, "work"))
    assert result == {"state": "blocked", "reason": "RENDER_OUTCOME_UNKNOWN"}
    assert store.get_operation("s", SCOPE, "r")["state"] == "unknown"
    assert bridge.downloads == 0
