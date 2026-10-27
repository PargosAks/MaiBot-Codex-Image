from pathlib import Path
from dataclasses import replace
import asyncio
import socket
import sys
import importlib.util

import pytest
import uvicorn

from codex_image_mcp.config import Settings
from codex_image_mcp.codex import Codex, Rpc
from codex_image_mcp.server import create_app
import codex_image_mcp.codex as driver_module

PLUGIN = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "image_plugin_under_test",
    PLUGIN / "plugin.py",
    submodule_search_locations=[str(PLUGIN)],
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

from image_plugin_under_test.bridge import sign  # noqa: E402

SCOPE = {"platform": "qq", "stream_id": "real-stream", "user_id": "123"}
SECRET = "local-test-secret-" + "s" * 32


@pytest.fixture
def settings(tmp_path):
    return Settings(
        binary="unused-codex",
        home=tmp_path / "home",
        state=tmp_path / "state",
        secret=SECRET,
        timeout=2,
        rpc_timeout=1,
    ).validated()


def envelope(op, args, scope=None):
    return sign(SECRET, SCOPE if scope is None else scope, op, args)


@pytest.fixture
def driver_factory(monkeypatch, settings):
    def factory(mode="ok"):
        def rpc(argv, env, timeout):
            role = "worker" if 'cli_auth_credentials_store="ephemeral"' in argv else "broker"
            return Rpc(
                [sys.executable, str(Path(__file__).with_name("fake_codex.py")), mode, role],
                env,
                timeout,
            )

        monkeypatch.setattr(driver_module, "Rpc", rpc)
        return Codex(settings)

    return factory


@pytest.fixture
async def live_service(settings, driver_factory):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    cfg = replace(settings, port=sock.getsockname()[1])
    app = create_app(cfg, driver_factory())
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.02)
        yield cfg
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()
