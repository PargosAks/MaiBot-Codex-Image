"""Load the plugin directly; no production MCP service implementation is imported."""

from pathlib import Path
import asyncio
import importlib.util
import socket
import sys

import pytest_asyncio
import uvicorn

from protocol_fixture import ProtocolFixture

PLUGIN = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "image_plugin_under_test",
    PLUGIN / "plugin.py",
    submodule_search_locations=[str(PLUGIN)],
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

SCOPE = {"platform": "qq", "stream_id": "real-stream", "user_id": "123"}
SECRET = "local-test-secret-" + "s" * 32


@pytest_asyncio.fixture
async def mcp_protocol_fixture():
    """Real Streamable HTTP transport around an in-memory protocol fixture.

    This is not the production service: no Codex, filesystem jobs, login, worker,
    subprocess or generation probe is used. Only a static synthetic PNG is sent.
    """
    protocol = ProtocolFixture(SECRET)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    protocol.url = f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
    server = uvicorn.Server(
        uvicorn.Config(protocol.app, log_level="critical", lifespan="on", access_log=False)
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("MCP protocol fixture exited before startup")
                await asyncio.sleep(0.01)
        yield protocol
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5)
        finally:
            sock.close()
