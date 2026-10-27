from types import SimpleNamespace
import asyncio
import base64

import httpx
import pytest

from image_plugin_under_test.bridge import Bridge
from image_plugin_under_test.delivery import Deliveries
from image_plugin_under_test import CodexImagePlugin, trusted_identity
from conftest import SECRET, SCOPE


async def test_real_mcp_transport_auth_and_image(live_service):
    cfg = live_service
    url = f"http://127.0.0.1:{cfg.port}/mcp"
    async with httpx.AsyncClient(trust_env=False) as client:
        assert (await client.post(url, json={})).status_code == 401
    bridge = Bridge(url, SECRET)
    health = await bridge.call("health", SCOPE, {})
    assert health["worker_running"] and health["generation_probe"] is False
    submitted = await bridge.call(
        "submit", SCOPE, {"request_id": "mcp-message", "prompt": "circle"}
    )
    job = submitted["job_id"]
    async with asyncio.timeout(10):
        while True:
            status = await bridge.call("status", SCOPE, {"job_id": job})
            if status["status"] == "completed":
                break
            await asyncio.sleep(0.05)
    image = await bridge.call("result", SCOPE, {"job_id": job})
    assert base64.b64decode(image).startswith(b"\x89PNG")
    with pytest.raises(ValueError):
        await bridge.call("result", dict(SCOPE, user_id="other"), {"job_id": job})


class Sender:
    def __init__(self, fail=False):
        self.images = []
        self.texts = []
        self.fail = fail

    async def text(self, text, stream):
        self.texts.append((text, stream))
        return True

    async def image(self, image, stream, **kwargs):
        self.images.append((image, stream))
        if isinstance(self.fail, BaseException):
            raise self.fail
        if self.fail:
            raise ConnectionError("host response lost")
        return {"sent": True, "message_id": "sent-message"}


async def plugin_instance(tmp_path, url, fail=False):
    plugin = CodexImagePlugin()
    plugin.set_plugin_config(
        {
            "plugin": {"config_version": "0.1.0"},
            "connection": {
                "url": url,
                "secret": SECRET,
                "allowed_users": ["qq:123"],
                "poll_seconds": 0.1,
                "watch_seconds": 10,
            },
        }
    )
    sender = Sender(fail)
    plugin._set_context(
        SimpleNamespace(send=sender, paths=SimpleNamespace(data_dir=tmp_path))
    )
    await plugin.on_load()
    return plugin, sender


async def test_plugin_command_to_mcp_to_image(live_service, tmp_path):
    url = f"http://127.0.0.1:{live_service.port}/mcp"
    plugin, sender = await plugin_instance(tmp_path, url)
    kwargs = dict(
        SCOPE,
        message_id="host-message",
        matched_groups={"action": "画图", "arg": "circle"},
    )
    try:
        result = await plugin.command(**kwargs)
        assert result[0], result
        await asyncio.wait_for(asyncio.gather(*list(plugin.watchers.values())), 10)
        assert len(sender.images) == 1 and sender.images[0][1] == SCOPE["stream_id"]
        assert base64.b64decode(sender.images[0][0]).startswith(b"\x89PNG")
        # Same source message must not generate or deliver a second image.
        await plugin.command(**kwargs)
        await asyncio.wait_for(asyncio.gather(*list(plugin.watchers.values())), 10)
        assert len(sender.images) == 1
    finally:
        await plugin.on_unload()


def test_missing_identity_is_rejected():
    with pytest.raises(ValueError):
        trusted_identity({"stream_id": "forged", "user_id": "123"})


def test_host_message_identity_and_conflict():
    message = {
        "session_id": "real-stream",
        "platform": "qq",
        "message_id": "m1",
        "message_info": {"user_info": {"user_id": "123"}},
    }
    assert trusted_identity({"message": message}) == (SCOPE, "m1")
    with pytest.raises(ValueError, match="不一致"):
        trusted_identity({"message": message, "stream_id": "other-stream"})


async def test_unknown_send_is_locked(tmp_path):
    plugin, sender = await plugin_instance(
        tmp_path, "http://127.0.0.1:1/mcp", fail=True
    )

    class FakeBridge:
        async def call(self, *args):
            return base64.b64encode(b"image").decode()

    plugin.bridge = FakeBridge()
    try:
        with pytest.raises(ConnectionError):
            await plugin._deliver(SCOPE, "job")
        with pytest.raises(ValueError, match="发送结果未知"):
            await plugin._deliver(SCOPE, "job", manual=True)
        assert len(sender.images) == 1
    finally:
        await plugin.on_unload()
    restored = Deliveries(tmp_path / "delivery.sqlite3")
    try:
        assert not restored.begin("job", manual=True)
    finally:
        restored.close()


async def test_host_denial_can_resend_without_generation(tmp_path):
    class HostDenied(Exception):
        code = "E_CAPABILITY_DENIED"

    plugin, sender = await plugin_instance(
        tmp_path, "http://127.0.0.1:1/mcp", fail=HostDenied()
    )

    class ExistingImage:
        def __init__(self):
            self.operations = []

        async def call(self, operation, *args):
            self.operations.append(operation)
            return base64.b64encode(b"existing image").decode()

    bridge = ExistingImage()
    plugin.bridge = bridge
    try:
        with pytest.raises(HostDenied):
            await plugin._deliver(SCOPE, "job")
        sender.fail = False
        await plugin._deliver(SCOPE, "job", manual=True)
        assert bridge.operations == ["result", "result"]
        assert (
            plugin.journal.db.execute(
                "SELECT state FROM delivery WHERE job='job'"
            ).fetchone()[0]
            == "sent"
        )
    finally:
        await plugin.on_unload()
