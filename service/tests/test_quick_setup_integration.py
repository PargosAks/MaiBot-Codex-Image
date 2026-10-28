"""Generated configuration drives real MCP HTTP and a plugin send recorder."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import asyncio
import socket
import tomllib

import pytest
import uvicorn

from codex_image_mcp.config import Settings
from codex_image_mcp.server import create_app
from image_plugin_under_test import CodexImagePlugin
from conftest import SCOPE
from test_mcp_plugin import Sender
from test_quick_setup import installation, setup, single_checkout  # noqa: F401


@pytest.mark.usefixtures("installation")
@pytest.mark.parametrize("layout", ["separate", "single"])
async def test_generated_configuration_submits_and_sends_one_image(request, driver_factory, monkeypatch, tmp_path, layout):
    existing = request.getfixturevalue("installation")
    repo, plugin_dir, _ = existing
    data = repo
    if layout == "single":
        plugin_dir, data, _ = single_checkout(existing)
        repo = plugin_dir
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    generated = setup.configure(repo, plugin_dir, ["qq:123"], port=port, data_dir=data)
    monkeypatch.delenv("CODEX_IMAGE_SECRET", raising=False)
    monkeypatch.setenv("CODEX_IMAGE_SECRET_FILE", str(data / "secret.txt"))
    cfg = Settings.load(Path(generated["service_config"]))
    assert cfg.port == port
    app = create_app(replace(cfg, timeout=2), driver_factory())
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="on"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    plugin, sender = CodexImagePlugin(), Sender()
    plugin.set_plugin_config(tomllib.loads((plugin_dir / "config.toml").read_text(encoding="utf-8")))
    plugin._set_context(SimpleNamespace(send=sender, paths=SimpleNamespace(data_dir=tmp_path / "plugin-data")))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(.02)
        await plugin.on_load()
        result = await plugin.command(**SCOPE, message_id="generated-config-message",
                                      matched_groups={"action": "画图", "arg": "circle"})
        assert result[0], result
        await asyncio.wait_for(asyncio.gather(*list(plugin.watchers.values())), 10)
        assert len(sender.images) == 1 and sender.images[0][1] == SCOPE["stream_id"]
    finally:
        await plugin.on_unload()
        server.should_exit = True
        await asyncio.wait_for(serving, 5)
        sock.close()
