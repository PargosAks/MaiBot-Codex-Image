"""Plugin/Bridge regressions against a static, test-only MCP protocol fixture."""

from types import SimpleNamespace
import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import httpx
import pytest

from image_plugin_under_test.bridge import Bridge, sign
from image_plugin_under_test.delivery import Deliveries
from image_plugin_under_test import CodexImagePlugin, trusted_identity
from conftest import SECRET, SCOPE
from protocol_fixture import PNG


async def test_real_mcp_transport_auth_and_static_image(mcp_protocol_fixture):
    url = mcp_protocol_fixture.url
    async with httpx.AsyncClient(trust_env=False) as client:
        assert (await client.post(url, json={})).status_code == 401
        assert (
            await client.post(url, json={}, headers={"Authorization": "Bearer wrong-test-key"})
        ).status_code == 401
    bridge = Bridge(url, SECRET)
    health = await bridge.call("health", SCOPE, {})
    assert health["worker_running"] and health["generation_probe"] is False
    submitted = await bridge.call("submit", SCOPE, {"request_id": "mcp-message", "prompt": "circle"})
    job = submitted["job_id"]
    async with asyncio.timeout(10):
        while True:
            status = await bridge.call("status", SCOPE, {"job_id": job})
            if status["status"] == "completed":
                break
            await asyncio.sleep(0.05)
    image = await bridge.call("result", SCOPE, {"job_id": job})
    assert base64.b64decode(image) == PNG
    with pytest.raises(ValueError):
        await bridge.call("result", dict(SCOPE, user_id="other"), {"job_id": job})
    assert len(mcp_protocol_fixture.jobs) == 1


async def call_protocol_tool(url, operation, envelope):
    """Use the SDK transport directly to inject deliberately invalid envelopes."""
    async with (
        httpx.AsyncClient(
            headers={"Authorization": "Bearer " + SECRET}, trust_env=False, follow_redirects=False
        ) as http,
        streamable_http_client(url, http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        return await session.call_tool("codex_image_" + operation, {"envelope": envelope})


def encode_context(data, secret=SECRET):
    """Independent encoder for malformed/stale protocol inputs, never production code."""
    payload = base64.urlsafe_b64encode(json.dumps(data, sort_keys=True, separators=(",", ":")).encode())
    signature = hmac.new(secret.encode(), payload.rstrip(b"="), hashlib.sha256).hexdigest()
    return payload.decode().rstrip("=") + "." + signature


def test_sign_binds_scope_operation_arguments_and_timestamp():
    args = {"request_id": "signed-message", "prompt": "静态协议图片"}
    before = int(time.time())
    payload, signature = sign(SECRET, SCOPE, "submit", args).split(".")
    data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert set(data) == {"scope", "op", "args", "iat"}
    assert data["scope"] == SCOPE and data["op"] == "submit" and data["args"] == args
    assert before <= data["iat"] <= int(time.time())
    assert hmac.compare_digest(
        signature, hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    )


@pytest.mark.parametrize("field", ["scope", "op", "args", "iat"])
async def test_protocol_fixture_rejects_tampering_with_signed_fields(mcp_protocol_fixture, field):
    original = sign(SECRET, SCOPE, "submit", {"request_id": "bound", "prompt": "circle"})
    payload, signature = original.split(".")
    data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    data[field] = {
        "scope": dict(SCOPE, user_id="other"),
        "op": "health",
        "args": {"request_id": "bound", "prompt": "changed"},
        "iat": data["iat"] + 1,
    }[field]
    tampered = encode_context(data).split(".")[0] + "." + signature
    response = await call_protocol_tool(mcp_protocol_fixture.url, "submit", tampered)
    assert response.isError
    assert not mcp_protocol_fixture.jobs and not mcp_protocol_fixture.calls


@pytest.mark.parametrize("mode", ["wrong_operation", "expired", "future", "wrong_key"])
async def test_protocol_fixture_rejects_invalid_signed_context(mcp_protocol_fixture, mode):
    data = {
        "scope": SCOPE,
        "op": "submit",
        "args": {"request_id": "bound", "prompt": "circle"},
        "iat": int(time.time()),
    }
    if mode == "wrong_operation":
        data["op"] = "health"
    elif mode == "expired":
        data["iat"] -= 121
    elif mode == "future":
        data["iat"] += 121
    envelope = encode_context(data, "another-test-key" if mode == "wrong_key" else SECRET)
    response = await call_protocol_tool(mcp_protocol_fixture.url, "submit", envelope)
    assert response.isError
    assert not mcp_protocol_fixture.jobs


@pytest.mark.parametrize("operation", ["status", "result"])
@pytest.mark.parametrize("field", ["platform", "stream_id", "user_id"])
async def test_bridge_cross_owner_rejected(mcp_protocol_fixture, operation, field):
    bridge = Bridge(mcp_protocol_fixture.url, SECRET)
    submitted = await bridge.call("submit", SCOPE, {"request_id": "owned", "prompt": "circle"})
    with pytest.raises(ValueError, match="拒绝"):
        await bridge.call(operation, dict(SCOPE, **{field: "other"}), {"job_id": submitted["job_id"]})
    assert len(mcp_protocol_fixture.jobs) == 1


async def test_protocol_fixture_deduplicates_owner_request_and_rejects_changed_prompt(mcp_protocol_fixture):
    bridge = Bridge(mcp_protocol_fixture.url, SECRET)
    args = {"request_id": "same-message", "prompt": "circle"}
    first = await bridge.call("submit", SCOPE, args)
    assert await bridge.call("submit", SCOPE, args) == first
    with pytest.raises(ValueError, match="拒绝"):
        await bridge.call("submit", SCOPE, dict(args, prompt="different"))
    other = await bridge.call("submit", dict(SCOPE, user_id="other"), args)
    assert other["job_id"] != first["job_id"] and len(mcp_protocol_fixture.jobs) == 2


@pytest.mark.parametrize(
    ("mode", "error", "message"),
    [
        ("text", ValueError, "没有返回图片"),
        ("empty", ValueError, "大小"),
        ("invalid_base64", binascii.Error, None),
        ("error", ValueError, "拒绝"),
    ],
)
async def test_bridge_image_error_paths(mcp_protocol_fixture, mode, error, message):
    bridge = Bridge(mcp_protocol_fixture.url, SECRET)
    submitted = await bridge.call("submit", SCOPE, {"request_id": "errors", "prompt": "circle"})
    mcp_protocol_fixture.result_mode = mode
    with pytest.raises(error, match=message):
        await bridge.call("result", SCOPE, {"job_id": submitted["job_id"]})
    assert len(mcp_protocol_fixture.jobs) == 1


async def test_bridge_reads_text_status_and_rejects_empty_status(mcp_protocol_fixture):
    bridge = Bridge(mcp_protocol_fixture.url, SECRET)
    mcp_protocol_fixture.health_mode = "text"
    assert (await bridge.call("health", SCOPE, {}))["generation_probe"] is False
    mcp_protocol_fixture.health_mode = "empty"
    with pytest.raises(ValueError, match="没有返回有效状态"):
        await bridge.call("health", SCOPE, {})
    assert not mcp_protocol_fixture.jobs


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/mcp",
        "http:///mcp",
        "http://user:pass@localhost/mcp",
        "http://localhost/mcp?key=value",
        "http://localhost/mcp#fragment",
    ],
)
def test_bridge_rejects_unsafe_or_ambiguous_urls(url):
    with pytest.raises(ValueError):
        Bridge(url, SECRET)


def test_bridge_rejects_short_secret():
    with pytest.raises(ValueError, match="至少 32"):
        Bridge("http://localhost/mcp", "short")


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

    async def raw_capability(method, *, payload, **kwargs):
        assert method == "cap.call" and payload["capability"] == "send.image"
        args = payload["args"]
        return await sender.image(args["image_base64"], args["stream_id"], return_details=args["return_details"])

    plugin._set_context(SimpleNamespace(send=sender, call_host_method=raw_capability,
                                       paths=SimpleNamespace(data_dir=tmp_path)))
    await plugin.on_load()
    return plugin, sender


@pytest.mark.parametrize("receipt", [
    {"success": False, "sent": False, "error": "synthetic lost acknowledgement"},
    {"success": False, "sent": False},
    {"success": True},
    {"success": True, "sent": "false"},
    {"success": False, "sent": True},
])
async def test_command_delivery_keeps_raw_ambiguous_receipts_locked(tmp_path, receipt):
    plugin, _ = await plugin_instance(tmp_path, "http://127.0.0.1:9/mcp")
    calls = []

    async def raw_capability(method, *, payload, **kwargs):
        calls.append(payload)
        return receipt

    async def result(*args):
        return base64.b64encode(PNG).decode()

    plugin.ctx.call_host_method = raw_capability
    plugin.bridge = SimpleNamespace(call=result)
    try:
        with pytest.raises(ValueError):
            await plugin._deliver(SCOPE, "synthetic-original")
        assert plugin.journal.db.execute("SELECT state FROM delivery").fetchone()[0] == "send_unknown"
        with pytest.raises(ValueError):
            await plugin._deliver(SCOPE, "synthetic-original", manual=True)
        assert len(calls) == 1
    finally:
        await plugin.on_unload()


async def test_plugin_command_to_mcp_to_image(mcp_protocol_fixture, tmp_path):
    url = mcp_protocol_fixture.url
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
        assert len(mcp_protocol_fixture.jobs) == 1
        submissions = [args for operation, _, args in mcp_protocol_fixture.calls if operation == "submit"]
        assert len(submissions) == 2 and submissions[0] == submissions[1]
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
    plugin, sender = await plugin_instance(tmp_path, "http://127.0.0.1:1/mcp", fail=True)

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


@pytest.mark.parametrize("state", ["sending", "send_unknown"])
def test_pending_or_unknown_send_stays_locked_across_journal_reload(tmp_path, state):
    path = tmp_path / "delivery.sqlite3"
    journal = Deliveries(path)
    try:
        assert journal.begin("job")
        if state == "send_unknown":
            journal.finish("job", state)
        assert not journal.begin("job") and not journal.begin("job", manual=True)
    finally:
        journal.close()
    restored = Deliveries(path)
    try:
        assert (
            restored.db.execute("SELECT state FROM delivery WHERE job='job'").fetchone()[0] == "send_unknown"
        )
        assert not restored.begin("job") and not restored.begin("job", manual=True)
    finally:
        restored.close()


async def test_cancelled_send_stays_locked_after_plugin_reload(tmp_path):
    plugin, sender = await plugin_instance(tmp_path, "http://127.0.0.1:1/mcp", fail=asyncio.CancelledError())

    class ExistingImage:
        async def call(self, *args):
            return base64.b64encode(PNG).decode()

    plugin.bridge = ExistingImage()
    try:
        with pytest.raises(asyncio.CancelledError):
            await plugin._deliver(SCOPE, "job")
        await plugin.on_config_update("plugin", {}, "reload")
        plugin.bridge = ExistingImage()
        with pytest.raises(ValueError, match="发送结果未知"):
            await plugin._deliver(SCOPE, "job", manual=True)
        assert len(sender.images) == 1
    finally:
        await plugin.on_unload()


async def test_host_denial_can_resend_without_generation(tmp_path):
    class HostDenied(Exception):
        code = "E_CAPABILITY_DENIED"

    plugin, sender = await plugin_instance(tmp_path, "http://127.0.0.1:1/mcp", fail=HostDenied())

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
        assert plugin.journal.db.execute("SELECT state FROM delivery WHERE job='job'").fetchone()[0] == "sent"
    finally:
        await plugin.on_unload()
