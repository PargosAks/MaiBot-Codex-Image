"""用事件固定命令、配置替换及外部调用的先后顺序。"""

# ruff: noqa: I001 -- 继承上级 AGENTS.md 的 from-import 在前约定。
from contextlib import asynccontextmanager
from types import SimpleNamespace
import asyncio

import pytest

import image_plugin_under_test as plugin_module
from image_plugin_under_test.mcp_client import bridge as bridge_module
from support.runtime_generation import JOB, NEW_OWNER, OWNER, command, config, plugin_at


async def test_queued_command_never_combines_new_allowlist_with_old_bridge(tmp_path, monkeypatch):
    plugin = plugin_at(tmp_path, monkeypatch)
    await plugin.on_load()
    old = plugin.bridge
    queued = callback = None
    try:
        # A 已持有锁，B 先于配置回调排队；SDK 在回调取锁前替换模型。
        async with plugin.lifecycle:
            queued = asyncio.create_task(plugin.command(**command(NEW_OWNER)))
            await asyncio.sleep(0)
            assert not queued.done()
            plugin.set_plugin_config(config("new-owner", 2222))
            callback = asyncio.create_task(plugin.on_config_update("plugin", {}, "new"))
            await asyncio.sleep(0)
            assert not callback.done()
        result = await asyncio.wait_for(queued, 2)
        await asyncio.wait_for(callback, 2)
        assert not old.calls, "新授权用户访问了旧 Bridge"
        assert result[0] is False
        assert not (await plugin.command(**command()))[0]
        assert (await plugin.command(**command(NEW_OWNER)))[0]
        assert plugin.bridge.url.endswith(":2222/mcp")
        assert plugin.bridge.calls == [("health", NEW_OWNER)]
    finally:
        await asyncio.gather(*(t for t in (queued, callback) if t is not None), return_exceptions=True)
        await plugin.on_unload()


async def test_command_upload_does_not_hold_lifecycle_lock(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def capability(method, *, payload, **kwargs):
        assert method == "cap.call" and payload["capability"] == "send.image"
        entered.set()
        await release.wait()
        return {"sent": True}

    plugin = plugin_at(tmp_path, monkeypatch, call_host_method=capability)
    await plugin.on_load()
    sending = asyncio.create_task(plugin.command(**command(OWNER, "重发画图", JOB)))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        # Studio Hook 的读取与此处使用同一把锁。
        async def hook_lock():
            async with plugin.lifecycle:
                assert not sending.done()

        await asyncio.wait_for(hook_lock(), 0.5)
    finally:
        release.set()
        assert (await asyncio.wait_for(sending, 2))[0]
        await plugin.on_unload()


async def test_unload_after_host_dispatch_persists_unknown_and_blocks_manual_retry(tmp_path, monkeypatch):
    dispatched = asyncio.Event()
    host_reply = asyncio.get_running_loop().create_future()
    calls = []

    async def capability(method, *, payload, **kwargs):
        assert method == "cap.call" and payload["capability"] == "send.image"
        calls.append(payload["args"]["stream_id"])
        dispatched.set()
        # Host 已接收请求，可以在本地取消等待后继续上传。
        return await asyncio.shield(host_reply)

    plugin = plugin_at(tmp_path, monkeypatch, call_host_method=capability)
    await plugin.on_load()
    kwargs = command(OWNER, "重发画图", JOB)
    sending = asyncio.create_task(plugin.command(**kwargs))
    try:
        await asyncio.wait_for(dispatched.wait(), 2)
        await asyncio.wait_for(plugin.on_unload(), 2)
        with pytest.raises(asyncio.CancelledError):
            await sending
        assert plugin.journal is None
        assert not host_reply.done()
        host_reply.set_result({"sent": True})
        await plugin.on_load()
        assert plugin.journal.state(JOB) == "send_unknown"
        result = await plugin.command(**kwargs)
        assert not result[0] and "发送结果未知" in result[1]
        assert calls == [OWNER["stream_id"]]
    finally:
        if not host_reply.done():
            host_reply.cancel()
        await plugin.on_unload()
        await asyncio.gather(sending, return_exceptions=True)


async def test_config_replacement_interrupts_unpublished_runtime_before_callback(tmp_path, monkeypatch):
    verifying, interrupted = asyncio.Event(), asyncio.Event()

    async def capability(method, *, payload, **kwargs):
        assert method == "cap.call" and payload["capability"] == "chat.get_stream_by_user_id"
        verifying.set()
        try:
            await asyncio.Event().wait()
        finally:
            interrupted.set()

    plugin = plugin_at(tmp_path, monkeypatch, call_host_method=capability)
    candidate = config("owner", 1111)
    candidate["autonomy"] = {"enabled": True, "spaces": [
        {"space_id": "s", **OWNER, "continuous": True, "consent_ref": "synthetic-consent"},
    ]}
    plugin.set_plugin_config(candidate)
    loading = asyncio.create_task(plugin.on_load())
    try:
        await asyncio.wait_for(verifying.wait(), 2)
        plugin.set_plugin_config(config("new-owner", 2222))
        await asyncio.wait_for(interrupted.wait(), 2)
        await asyncio.wait_for(loading, 2)
        assert plugin.bridge is None and plugin.journal is None
        await plugin.on_config_update("plugin", {}, "replacement")
        assert (await plugin.command(**command(NEW_OWNER)))[0]
        assert not plugin.artist_runtime.tasks
    finally:
        loading.cancel()
        await asyncio.gather(loading, return_exceptions=True)
        await plugin.on_unload()


async def test_revoked_status_lookup_does_not_publish_old_response(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def status(operation, scope, args):
        assert operation == "status" and scope == OWNER and args == {"job_id": JOB}
        entered.set()
        await release.wait()
        return {"status": "sensitive-old-status"}

    plugin = plugin_at(tmp_path, monkeypatch)
    await plugin.on_load()
    monkeypatch.setattr(plugin.bridge, "call", status)
    lookup = asyncio.create_task(plugin.command(**command(OWNER, "画图状态", JOB)))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        plugin.set_plugin_config(config("new-owner", 2222))
        release.set()
        result = await asyncio.wait_for(lookup, 2)
        assert not result[0] and "sensitive-old-status" not in result[1]
    finally:
        release.set()
        await asyncio.gather(lookup, return_exceptions=True)
        await plugin.on_unload()


@pytest.mark.parametrize("unload", [False, True])
async def test_retiring_runtime_preserves_pending_submit_receipt(tmp_path, monkeypatch, unload):
    entered, release, retiring = asyncio.Event(), asyncio.Event(), asyncio.Event()
    plugin = plugin_at(tmp_path, monkeypatch)
    replies = []

    async def text_reply(text, stream):
        replies.append((text, stream))

    async def submit(operation, scope, args, *, before_dispatch):
        assert before_dispatch()
        calls.append(operation)
        assert operation == "submit" and scope == OWNER and args["prompt"] == "synthetic"
        entered.set()
        await release.wait()
        return {"job_id": JOB}

    await plugin.on_load()
    calls = []
    monkeypatch.setattr(plugin.bridge, "call", submit)
    monkeypatch.setattr(plugin.ctx.send, "text", text_reply)
    original_stop = plugin._stop_artist_workers

    def stop(**kwargs):
        original_stop(**kwargs)
        retiring.set()

    monkeypatch.setattr(plugin, "_stop_artist_workers", stop)
    submitting = asyncio.create_task(plugin.command(**command(OWNER, "画图", "synthetic")))
    transition = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if unload:
            transition = asyncio.create_task(plugin.on_unload())
        else:
            plugin.set_plugin_config(config("new-owner", 2222))
            retiring.clear()
            transition = asyncio.create_task(plugin.on_config_update("plugin", {}, "replacement"))
        await asyncio.wait_for(retiring.wait(), 2)

        async def hook_lock():
            async with plugin.lifecycle:
                assert not submitting.done()

        await asyncio.wait_for(hook_lock(), 1)
        release.set()
        result = await asyncio.wait_for(submitting, 2)
        await asyncio.wait_for(transition, 2)
        assert result[0] and JOB in result[1] and "自动取图已停止" in result[1]
        assert replies == [(result[1], OWNER["stream_id"])]
        assert not plugin.watchers and calls == ["submit"]
    finally:
        release.set()
        await asyncio.gather(*(t for t in (submitting, transition) if t is not None), return_exceptions=True)
        await plugin.on_unload()


async def test_external_submit_cancellation_warns_without_replaying(tmp_path, monkeypatch):
    entered = asyncio.Event()
    plugin = plugin_at(tmp_path, monkeypatch)
    calls, replies = [], []

    async def text_reply(text, stream):
        replies.append((text, stream))

    async def submit(operation, scope, args, *, before_dispatch):
        assert before_dispatch()
        calls.append(operation)
        entered.set()
        await asyncio.Event().wait()

    await plugin.on_load()
    monkeypatch.setattr(plugin.bridge, "call", submit)
    monkeypatch.setattr(plugin.ctx.send, "text", text_reply)
    submitting = asyncio.create_task(plugin.command(**command(OWNER, "画图", "synthetic")))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        submitting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(submitting, 2)
        assert calls == ["submit"] and not plugin.watchers
        assert len(replies) == 1 and replies[0][1] == OWNER["stream_id"]
        assert "结果未知" in replies[0][0] and "请勿重复提交" in replies[0][0]
    finally:
        submitting.cancel()
        await asyncio.gather(submitting, return_exceptions=True)
        await plugin.on_unload()


async def test_submit_timeout_is_unknown_and_does_not_start_watcher(tmp_path, monkeypatch):
    plugin = plugin_at(tmp_path, monkeypatch)
    calls = []

    async def submit(operation, scope, args, *, before_dispatch):
        assert before_dispatch()
        calls.append(operation)
        await asyncio.Event().wait()

    await plugin.on_load()
    monkeypatch.setattr(plugin.bridge, "call", submit)
    monkeypatch.setattr(plugin_module, "_SUBMIT_TIMEOUT_SECONDS", 0.01)
    try:
        result = await asyncio.wait_for(plugin.command(**command(OWNER, "画图", "synthetic")), 2)
        assert not result[0] and "结果未知" in result[1] and "请勿重复提交" in result[1]
        assert calls == ["submit"] and not plugin.watchers
    finally:
        await plugin.on_unload()


@pytest.mark.parametrize("reload", [False, True])
async def test_revoked_during_mcp_handshake_does_not_dispatch_submit(tmp_path, monkeypatch, reload):
    negotiating, release, retiring = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    @asynccontextmanager
    async def http(**kwargs):
        yield object()

    @asynccontextmanager
    async def transport(*args, **kwargs):
        yield object(), object(), None

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self):
            negotiating.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # 即使握手层吃掉取消，实际 tool 仍必须复核本次调用的授权。
                await release.wait()

        async def call_tool(self, name, args):
            calls.append(name)
            return SimpleNamespace(isError=False, structuredContent={"job_id": JOB})

    plugin = plugin_at(tmp_path, monkeypatch)
    monkeypatch.setattr(plugin_module, "Bridge", bridge_module.Bridge)
    monkeypatch.setattr(bridge_module, "httpx", SimpleNamespace(AsyncClient=http))
    monkeypatch.setattr(bridge_module, "streamable_http_client", transport)
    monkeypatch.setattr(bridge_module, "ClientSession", Session)
    await plugin.on_load()

    async def reload_config():
        retiring.set()
        await plugin.on_config_update("self", {}, "revoke")

    submitting = asyncio.create_task(plugin.command(**command(OWNER, "画图", "synthetic")))
    transition = None
    try:
        await asyncio.wait_for(negotiating.wait(), 2)
        assert calls == []
        plugin.set_plugin_config(config("new-owner", 2222))
        if reload:
            transition = asyncio.create_task(reload_config())
            await asyncio.wait_for(retiring.wait(), 2)
        release.set()
        result = await asyncio.wait_for(submitting, 2)
        if transition is not None:
            await asyncio.wait_for(transition, 2)
        assert calls == [] and not plugin.watchers
        assert not result[0] and "尚未提交" in result[1] and "未知" not in result[1]
    finally:
        release.set()
        await asyncio.gather(*(t for t in (submitting, transition) if t is not None), return_exceptions=True)
        await plugin.on_unload()
