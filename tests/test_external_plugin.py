"""Plugin lifecycle owns its client and send journal, never the host server or Codex."""

from types import SimpleNamespace
import asyncio

from image_plugin_under_test import CodexImagePlugin
from image_plugin_under_test.bridge import Bridge
from conftest import SECRET, SCOPE
from test_mcp_plugin import Sender, plugin_instance


def command(action, arg="", message="host-message"):
    return dict(SCOPE, message_id=message, matched_groups={"action": action, "arg": arg})


async def test_missing_key_does_not_start_local_process_and_recovers(tmp_path, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("The plugin must never launch a local process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    plugin = CodexImagePlugin()
    config = {"plugin": {"config_version": "0.4.0"}, "connection": {"allowed_users": ["qq:123"]}}
    plugin.set_plugin_config(config)
    plugin._set_context(SimpleNamespace(send=Sender(), paths=SimpleNamespace(data_dir=tmp_path)))
    await asyncio.gather(plugin.on_load(), plugin.on_load())
    try:
        assert plugin.bridge is None
        assert "共享密钥" in (await plugin.command(**command("画图诊断")))[1]
        assert not (tmp_path / "embedded").exists()
        config["connection"]["secret"] = SECRET
        plugin.set_plugin_config(config)
        await plugin.on_config_update("plugin", {}, "fixed")
        assert plugin.bridge is not None and plugin.journal is not None
    finally:
        await plugin.on_unload()


async def test_default_allowlist_denies_all_without_connecting(tmp_path):
    plugin, _ = await plugin_instance(tmp_path, "http://127.0.0.1:1/mcp")
    config = plugin.config.model_dump()
    config["connection"]["allowed_users"] = []
    plugin.set_plugin_config(config)
    try:
        await plugin.on_config_update("plugin", {}, "revoked")
        assert plugin.bridge is None
        assert "未获" in (await plugin.command(**command("画图", "circle")))[1]
    finally:
        await plugin.on_unload()


async def test_reload_preserves_protocol_fixture_job_and_delivery_receipt(mcp_protocol_fixture, tmp_path):
    url = mcp_protocol_fixture.url
    plugin, sender = await plugin_instance(tmp_path, url)
    try:
        result = await plugin.command(**command("画图", "circle"))
        assert result[0], result
        await asyncio.wait_for(asyncio.gather(*list(plugin.watchers.values())), 10)
        assert len(sender.images) == 1
        await plugin.on_config_update("plugin", {}, "reload")
        result = await plugin.command(**command("画图", "circle"))
        assert result[0], result
        await asyncio.wait_for(asyncio.gather(*list(plugin.watchers.values())), 10)
        assert len(sender.images) == 1
        diagnostic = await plugin.command(**command("画图诊断"))
        assert diagnostic[0] and "登录状态会在绘图时检查" in diagnostic[1]
        assert len(mcp_protocol_fixture.jobs) == 1
    finally:
        await plugin.on_unload()
    # The independently owned protocol fixture outlives the plugin's client lifecycle.
    health = await Bridge(url, SECRET).call("health", SCOPE, {})
    assert health["worker_running"] and health["login_verified"] is False
    restored, restored_sender = await plugin_instance(tmp_path, url)
    try:
        result = await restored.command(**command("画图", "circle"))
        assert result[0], result
        await asyncio.wait_for(asyncio.gather(*list(restored.watchers.values())), 10)
        assert restored_sender.images == [] and len(mcp_protocol_fixture.jobs) == 1
    finally:
        await restored.on_unload()


async def test_manual_resend_reads_existing_static_image_without_submission(mcp_protocol_fixture, tmp_path):
    plugin, sender = await plugin_instance(tmp_path, mcp_protocol_fixture.url)
    try:
        result = await plugin.command(**command("画图", "circle"))
        assert result[0], result
        await asyncio.wait_for(asyncio.gather(*list(plugin.watchers.values())), 10)
        job = next(iter(mcp_protocol_fixture.jobs))
        before = len(mcp_protocol_fixture.calls)
        status = await plugin.command(**command("画图状态", job, message="status-message"))
        resent = await plugin.command(**command("重发画图", job, message="resend-message"))
        assert status[0] and resent[0]
        assert len(sender.images) == 2 and sender.images[0] == sender.images[1]
        assert [op for op, _, _ in mcp_protocol_fixture.calls[before:]] == ["status", "result"]
        assert len(mcp_protocol_fixture.jobs) == 1
    finally:
        await plugin.on_unload()
