"""A plugin-only source update keeps the independently owned MCP process alive.

Only fake_codex speaks the Codex protocol. The child uses an isolated state/home,
and test-only response headers identify its PID, service and worker instances.
"""

from pathlib import Path
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import socket
import sys
import uuid

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[2]
SECRET = "synthetic-plugin-upgrade-secret-" + "x" * 32
SCOPE = {"platform": "qq", "user_id": "123", "stream_id": "upgrade-stream"}


async def _serve_child(scratch: Path):
    """Run production HTTP/service/driver code, replacing only the Codex executable."""
    from codex_image_mcp.codex import Codex, Rpc
    from codex_image_mcp.config import Settings
    from codex_image_mcp.server import create_app
    import codex_image_mcp.codex as driver_module
    import uvicorn

    starts = scratch / "fake-starts.jsonl"

    def fake_rpc(argv, env, timeout):
        role = "worker" if 'cli_auth_credentials_store="ephemeral"' in argv else "broker"
        with starts.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"role": role}) + "\n")
        return Rpc([sys.executable, str(Path(__file__).with_name("fake_codex.py")), "ok", role],
                   env, timeout)

    driver_module.Rpc = fake_rpc
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    cfg = Settings(binary="never-execute-real-codex", home=scratch / "fake-home",
                   state=scratch / "state", secret=SECRET, port=sock.getsockname()[1],
                   timeout=15, rpc_timeout=5).validated()
    app = create_app(cfg, Codex(cfg))
    boot = uuid.uuid4().hex

    async def identified_app(scope, receive, send):
        async def identified_send(message):
            if message["type"] == "http.response.start":
                identity = f"{os.getpid()}:{boot}:{id(app.service)}:{id(app.service.worker)}"
                message = dict(message, headers=[*message.get("headers", []),
                                                (b"x-test-instance", identity.encode())])
            await send(message)

        await app(scope, receive, identified_send)

    server = uvicorn.Server(uvicorn.Config(identified_app, log_level="critical", lifespan="on"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(15):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(.02)
        (scratch / "ready.json").write_text(json.dumps({"port": cfg.port}), encoding="utf-8")
        while not (scratch / "stop").exists():
            if serving.done():
                await serving
                raise RuntimeError("MCP exited before the test requested shutdown")
            await asyncio.sleep(.02)
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        sock.close()


def _load_bridge(path: Path):
    # Each plugin load gets a new module namespace, as a replaced Runner/client would.
    # The host MCP modules are never reloaded or patched during the source update.
    spec = importlib.util.spec_from_file_location("upgrade_bridge_" + uuid.uuid4().hex, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def independent_mcp(tmp_path):
    scratch = tmp_path / "host"
    scratch.mkdir()
    env = dict(os.environ)
    for key in ("CODEX_IMAGE_SECRET", "CODEX_IMAGE_SECRET_FILE"):
        env.pop(key, None)
    env.update(CODEX_HOME=str(scratch / "fake-home"), PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=os.pathsep.join([str(ROOT / "service" / "src"), *sys.path]))
    log = scratch / "child.log"
    with log.open("wb") as output:
        proc = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve()),
                                                    str(scratch), env=env, stdout=output, stderr=output)
        try:
            async with asyncio.timeout(20):
                while not (scratch / "ready.json").exists():
                    assert proc.returncode is None, log.read_text(encoding="utf-8", errors="replace")
                    await asyncio.sleep(.02)
            ready = json.loads((scratch / "ready.json").read_text(encoding="utf-8"))
            yield proc, scratch, f"http://127.0.0.1:{ready['port']}/mcp"
        finally:
            (scratch / "stop").touch()
            try:
                await asyncio.wait_for(proc.wait(), 15)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise AssertionError("MCP did not shut down cleanly: " +
                                     log.read_text(encoding="utf-8", errors="replace")) from None
            assert proc.returncode == 0, log.read_text(encoding="utf-8", errors="replace")


async def _identity(proc, url):
    assert proc.returncode is None
    async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
        response = await client.get(url)
    assert response.status_code == 401  # Bearer enforcement also remains in force.
    identity = response.headers["x-test-instance"]
    assert int(identity.split(":")[0]) == proc.pid
    return identity


async def test_plugin_source_update_reuses_live_host_and_owned_artifact(independent_mcp, tmp_path):
    proc, scratch, url = independent_mcp
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    bridge_path = plugin / "bridge.py"
    original = (ROOT / "bridge.py").read_bytes()
    bridge_path.write_bytes(original + b'\nCLIENT_REVISION = "before"\n')
    before_hash = hashlib.sha256(bridge_path.read_bytes()).hexdigest()
    old_module = _load_bridge(bridge_path)
    old = old_module.Bridge(url, SECRET)
    identity = await _identity(proc, url)
    assert (await old.call("health", SCOPE, {}))["worker_running"]
    args = {"request_id": "original-message", "prompt": "one fake green circle"}
    job = (await old.call("submit", SCOPE, args))["job_id"]
    async with asyncio.timeout(20):
        while True:
            status = await old.call("status", SCOPE, {"job_id": job})
            if status["status"] == "completed":
                break
            assert status["status"] in {"queued", "starting", "running", "artifact_ready"}, status
            await asyncio.sleep(.05)
    image = base64.b64decode(await old.call("result", SCOPE, {"job_id": job}), validate=True)
    assert image.startswith(b"\x89PNG")
    assert status["artifacts"][0]["sha256"] == hashlib.sha256(image).hexdigest()

    # Update the copied plugin source, discard its client, then load the new source.
    bridge_path.write_bytes(original + b'\nCLIENT_REVISION = "after-upgrade"\n')
    assert hashlib.sha256(bridge_path.read_bytes()).hexdigest() != before_hash
    new_module = _load_bridge(bridge_path)
    assert old_module.CLIENT_REVISION == "before"
    assert new_module.CLIENT_REVISION == "after-upgrade"
    assert new_module.Bridge is not old_module.Bridge
    del old
    upgraded = new_module.Bridge(url, SECRET)

    assert await _identity(proc, url) == identity
    assert await upgraded.call("status", SCOPE, {"job_id": job}) == status
    assert base64.b64decode(await upgraded.call("result", SCOPE, {"job_id": job}), validate=True) == image
    assert (await upgraded.call("submit", SCOPE, args))["job_id"] == job
    for other in (dict(SCOPE, user_id="other-user"), dict(SCOPE, stream_id="other-chat"),
                  dict(SCOPE, platform="other-platform")):
        for operation in ("status", "result"):
            with pytest.raises(ValueError):
                await upgraded.call(operation, other, {"job_id": job})

    # A valid HTTP bearer does not excuse an invalid signed request envelope.
    real_sign = new_module.sign

    def invalid_sign(secret, scope, operation, args):
        signed = real_sign(secret, scope, operation, args)
        return signed[:-1] + ("0" if signed[-1] != "0" else "1")

    new_module.sign = invalid_sign
    try:
        with pytest.raises(ValueError):
            await upgraded.call("status", SCOPE, {"job_id": job})
    finally:
        new_module.sign = real_sign
    assert await upgraded.call("status", SCOPE, {"job_id": job}) == status
    assert (await upgraded.call("health", SCOPE, {}))["worker_running"]
    assert await _identity(proc, url) == identity
    starts = [json.loads(line)["role"] for line in
              (scratch / "fake-starts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sorted(starts) == ["broker", "worker"]  # No second drawing RPC pair was constructed.
    (scratch / "evidence.json").write_text(json.dumps({
        "pid": proc.pid, "unchanged_pid_boot_service_worker": identity,
        "job_id": job, "artifact_sha256": hashlib.sha256(image).hexdigest(),
        "plugin_before_sha256": before_hash,
        "plugin_after_sha256": hashlib.sha256(bridge_path.read_bytes()).hexdigest(),
        "rpc_roles": starts, "python": sys.version,
    }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(_serve_child(Path(sys.argv[1])))
