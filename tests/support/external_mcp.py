"""仅在显式选择公开源码后启动独立 MCP，禁止实际生成。"""

from pathlib import Path
import asyncio
import importlib
import os
import socket

import pytest
import uvicorn

from image_plugin_under_test.mcp_client.bridge import Bridge
from .settings import SECRET


SOURCE = os.environ.get("CODEX_IMAGE_TEST_SERVICE_SOURCE")


@pytest.fixture
async def actual_mcp(tmp_path, monkeypatch):
    source = Path(SOURCE).resolve(strict=True)
    monkeypatch.syspath_prepend(str(source))
    module = importlib.import_module("codex_image_mcp.server")
    config = importlib.import_module("codex_image_mcp.config")
    assert Path(module.__file__).resolve().is_relative_to(source)

    async def no_worker(self):
        # Server lookup itself must not start or advance any queued render.
        return None

    monkeypatch.setattr(module.Service, "start", no_worker)

    class NoDrawing:
        async def generate(self, *args):
            pytest.fail("Recovery must never invoke a drawing driver")

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    settings = config.Settings(
        binary="not-a-real-codex",
        home=tmp_path / "unused-home",
        state=tmp_path / "service-state",
        secret=SECRET,
        port=sock.getsockname()[1],
    ).validated()
    app = module.create_app(settings, NoDrawing())
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="on", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Isolated MCP startup failed")
                await asyncio.sleep(0.01)
        yield Bridge(f"http://127.0.0.1:{settings.port}/mcp", SECRET), app.service
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        finally:
            sock.close()
