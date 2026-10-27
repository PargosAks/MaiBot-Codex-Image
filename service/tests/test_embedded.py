from pathlib import Path
from types import SimpleNamespace
import asyncio
import base64
import importlib
import signal
import socket
import sys
import threading
import zipfile

import httpx
import pytest

from codex_image_mcp.codex import Rpc
from image_plugin_under_test import CodexImagePlugin
from image_plugin_under_test import embedded as embedded_module
from conftest import SCOPE
from test_deploy import bundle
from test_mcp_plugin import Sender


@pytest.fixture
def fake_embedded(monkeypatch):
    module = importlib.import_module(embedded_module.Codex.__module__)
    calls, processes = [], []

    class RecordedRpc(Rpc):
        def __init__(self, argv, env, timeout):
            super().__init__([sys.executable, str(Path(__file__).with_name("fake_codex.py")), mode[0]], env, timeout)
            processes.append(self)

        async def call(self, method, params):
            calls.append(method)
            return await super().call(method, params)

    mode = ["ok"]
    monkeypatch.setattr(module, "Rpc", RecordedRpc)
    return SimpleNamespace(calls=calls, processes=processes, mode=mode)


def make_plugin(tmp_path, config=None):
    plugin = CodexImagePlugin()
    settings = config or {
        "plugin": {"config_version": "0.2.0"},
        "connection": {"allowed_users": ["qq:123"], "poll_seconds": 0.1, "watch_seconds": 15},
        "codex": {"binary": sys.executable, "home": str(tmp_path / "auth"), "rpc_timeout": 2},
    }
    settings.setdefault("plugin", {}).setdefault("config_version", "0.2.0")
    plugin.set_plugin_config(settings)
    sender = Sender()
    plugin._set_context(SimpleNamespace(send=sender, paths=SimpleNamespace(data_dir=tmp_path / "data")))
    return plugin, sender


def command(action, message="m1", arg=""):
    return dict(SCOPE, message_id=message, matched_groups={"action": action, "arg": arg})


async def test_default_configuration_waits_for_permission(tmp_path):
    plugin, _ = make_plugin(tmp_path, {"plugin": {"enabled": True}})
    await plugin.on_load()
    try:
        assert plugin.bridge is None and plugin.embedded is None
        assert not (tmp_path / "data/embedded").exists()
        assert "未获" in (await plugin.command(**command("画图诊断")))[1]
    finally:
        await plugin.on_unload()


async def test_embedded_lifecycle_diagnostic_and_real_transport(tmp_path, fake_embedded):
    handler = signal.getsignal(signal.SIGINT)
    plugin, sender = make_plugin(tmp_path)
    await asyncio.gather(plugin.on_load(), plugin.on_load())
    runtime = plugin.embedded
    port = runtime.settings.port
    secret = runtime.settings.secret
    try:
        assert signal.getsignal(signal.SIGINT) == handler
        assert runtime.settings.host == "127.0.0.1" and port > 0
        async with httpx.AsyncClient(trust_env=False) as http:
            assert (await http.post(plugin.bridge.url, json={})).status_code == 401
        diagnostic = await plugin.command(**command("画图诊断"))
        assert diagnostic[0] and "登录有效" in diagnostic[1]
        assert fake_embedded.calls == ["initialize", "account/read"]
        result = await plugin.command(**command("画图", arg="a green circle"))
        assert result[0], result
        await asyncio.wait_for(asyncio.gather(*list(plugin.watchers.values())), 10)
        assert len(sender.images) == 1 and base64.b64decode(sender.images[0][0]).startswith(b"\x89PNG")
        assert fake_embedded.calls.count("turn/start") == 1
        # Reconfiguration stops the old listener but retains jobs, send journal and secret.
        await plugin.on_config_update("plugin", {}, "next")
        assert runtime.task is None and runtime.socket is None and runtime.lease is None
        assert plugin.embedded.settings.secret == secret
        await plugin.command(**command("画图", arg="a green circle"))
        await asyncio.wait_for(asyncio.gather(*list(plugin.watchers.values())), 10)
        assert len(sender.images) == 1 and fake_embedded.calls.count("turn/start") == 1
    finally:
        await plugin.on_unload()
    assert all(not item.proc or item.proc.returncode is not None for item in fake_embedded.processes)
    assert signal.getsignal(signal.SIGINT) == handler
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0


async def test_missing_client_is_actionable_and_recovers(tmp_path, fake_embedded):
    plugin, _ = make_plugin(tmp_path)
    config = plugin.config.model_dump()
    config["codex"]["binary"] = str(tmp_path / "not-installed.exe")
    plugin.set_plugin_config(config)
    await plugin.on_load()
    assert plugin.bridge is None and plugin.embedded is None
    result = await plugin.command(**command("画图诊断"))
    assert not result[0] and "原生程序" in result[1]
    config["codex"]["binary"] = sys.executable
    plugin.set_plugin_config(config)
    try:
        await plugin.on_config_update("plugin", {}, "fixed")
        assert (await plugin.command(**command("画图诊断")))[0]
    finally:
        await plugin.on_unload()


async def test_login_required_does_not_submit_a_job(tmp_path, fake_embedded):
    fake_embedded.mode[0] = "api"
    plugin, _ = make_plugin(tmp_path)
    await plugin.on_load()
    try:
        result = await plugin.command(**command("画图", arg="a circle"))
        assert not result[0] and "手动登录" in result[1]
        assert "thread/start" not in fake_embedded.calls and "turn/start" not in fake_embedded.calls
        import sqlite3
        with sqlite3.connect(tmp_path / "data/embedded/state/jobs.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    finally:
        await plugin.on_unload()


async def test_two_instances_cannot_own_the_same_state(tmp_path, fake_embedded):
    first, _ = make_plugin(tmp_path)
    second, _ = make_plugin(tmp_path)
    await first.on_load()
    try:
        await second.on_load()
        assert second.bridge is None and "重复启动" in second.startup_error
        assert (await first.command(**command("画图诊断")))[0]
    finally:
        await second.on_unload()
        await first.on_unload()
    await second.on_load()
    try:
        assert second.bridge is not None
    finally:
        await second.on_unload()


async def test_unload_interrupts_owned_codex_without_replay(tmp_path, fake_embedded):
    fake_embedded.mode[0] = "timeout"
    plugin, _ = make_plugin(tmp_path)
    await plugin.on_load()
    await plugin.command(**command("画图", arg="a circle"))
    async with asyncio.timeout(6):
        while "turn/start" not in fake_embedded.calls:
            await asyncio.sleep(0.02)
    await plugin.on_unload()
    assert all(not item.proc or item.proc.returncode is not None for item in fake_embedded.processes)
    await plugin.on_load()
    try:
        await plugin.command(**command("画图", arg="a circle"))
        await asyncio.wait_for(asyncio.gather(*list(plugin.watchers.values())), 5)
        assert fake_embedded.calls.count("turn/start") == 1
    finally:
        await plugin.on_unload()


async def test_permission_update_replaces_service_allowlist(tmp_path, fake_embedded):
    plugin, _ = make_plugin(tmp_path)
    await plugin.on_load()
    config = plugin.config.model_dump()
    config["connection"]["allowed_users"] = ["qq:456"]
    plugin.set_plugin_config(config)
    try:
        await plugin.on_config_update("plugin", {}, "permissions")
        assert "未获" in (await plugin.command(**command("画图诊断")))[1]
        with pytest.raises(ValueError):
            await plugin.bridge.call("health", SCOPE, {})
        assert plugin.embedded.settings.allowed_users == ("qq:456",)
        config["plugin"]["enabled"] = False
        plugin.set_plugin_config(config)
        await plugin.on_config_update("plugin", {}, "disabled")
        assert plugin.embedded is None and plugin.bridge is None
    finally:
        await plugin.on_unload()


async def test_cancelling_start_waits_for_owned_thread(tmp_path, monkeypatch):
    runtime = embedded_module.EmbeddedMCP(tmp_path)
    plugin, _ = make_plugin(tmp_path)
    started, finish = threading.Event(), threading.Event()
    original = runtime._prepare

    def slow_prepare(cfg, users):
        started.set()
        finish.wait(3)
        original(cfg, users)

    monkeypatch.setattr(runtime, "_prepare", slow_prepare)
    task = asyncio.create_task(runtime.start(plugin.config.codex, ["qq:123"]))
    await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.lease is None and runtime.socket is None
    replacement = embedded_module.EmbeddedMCP(tmp_path)
    await replacement.start(plugin.config.codex, ["qq:123"])
    await replacement.close()


async def test_corrupt_secret_is_not_silently_replaced(tmp_path):
    path = tmp_path / "data/embedded/secret.txt"
    path.parent.mkdir(parents=True)
    path.write_text("corrupt", encoding="utf-8")
    plugin, _ = make_plugin(tmp_path)
    await plugin.on_load()
    try:
        assert "密钥文件损坏" in plugin.startup_error
        assert path.read_text() == "corrupt"
        assert plugin.embedded is None
    finally:
        await plugin.on_unload()


def test_plugin_center_zip_contains_the_embedded_service(tmp_path):
    outputs = bundle.build(tmp_path / "dist")
    with zipfile.ZipFile(outputs[1][0]) as archive:
        names = set(archive.namelist())
        assert {"embedded.py", "client.py", "login.py", "service/src/codex_image_mcp/server.py"} <= names
        assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
