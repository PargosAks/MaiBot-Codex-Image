"""Plugin lifecycle owns its client and send journal, never the host server or Codex."""

from types import SimpleNamespace
import asyncio
import json
import zipfile

from image_plugin_under_test import CodexImagePlugin, Config
from image_plugin_under_test.bridge import Bridge
from conftest import SECRET, SCOPE
from test_deploy import bundle
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


async def test_reload_preserves_host_job_and_delivery_receipt(live_service, tmp_path):
    url = f"http://127.0.0.1:{live_service.port}/mcp"
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
    finally:
        await plugin.on_unload()
    # Unloading/reloading a container-side plugin cannot stop the independently owned service.
    health = await Bridge(url, SECRET).call("health", SCOPE, {})
    assert health["worker_running"] and health["login_verified"] is False


def test_thin_plugin_config_and_bundle(tmp_path):
    assert set(Config.model_fields) == {"plugin", "connection"}
    outputs = bundle.build(tmp_path / "dist")
    with zipfile.ZipFile(outputs[1][0]) as archive:
        names = set(archive.namelist())
        assert {"plugin.py", "bridge.py", "delivery.py", "config.example.toml"} <= names
        assert not any(name.startswith("service/") for name in names)
        assert not names & {"embedded.py", "client.py", "login.py", "deploy.py"}
        manifest = json.loads(archive.read("_manifest.json"))
        assert {x["name"] for x in manifest["dependencies"]} == {"httpx", "mcp"}
