"""One loopback MCP server owned by a plugin lifecycle, without process-wide signal handlers."""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import asyncio
import os
import secrets
import socket

import uvicorn

from .bridge import Bridge
from .client import auth_home, find_codex
from .service.src.codex_image_mcp.codex import Codex
from .service.src.codex_image_mcp.config import Settings
from .service.src.codex_image_mcp.server import create_app


class PluginServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # MaiBot's runner owns process signals, even if this loop runs in the main thread.
        yield


class EmbeddedMCP:
    def __init__(self, data: Path):
        self.root = data / "embedded"
        self.server = None
        self.task = None
        self.socket = None
        self.lease = None
        self.settings = None
        self.bridge = None
        self.app = None

    async def _prepare_async(self, cfg, users):
        task = asyncio.create_task(asyncio.to_thread(self._prepare, cfg, users))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # A cancelled to_thread await does not stop the thread. Finish it before releasing its lease.
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _build_app(self):
        task = asyncio.create_task(asyncio.to_thread(create_app, self.settings))
        try:
            self.app = await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            if not task.cancelled() and task.exception() is None:
                self.app = task.result()
            raise

    def _prepare(self, cfg, users):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lease = (self.root / "service.lock").open("a+b")
        self.lease.seek(0, 2)
        if not self.lease.tell():
            self.lease.write(b"0")
            self.lease.flush()
        self.lease.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lease.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("同一插件数据目录已有 MCP 服务运行；请检查是否重复启动 MaiBot。") from exc

        secret_file = self.root / "secret.txt"
        if not secret_file.exists():
            # Exclusive creation preserves the same secret across reloads and upgrades.
            fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(secrets.token_urlsafe(40))
        secret = secret_file.read_text(encoding="utf-8").strip()
        if len(secret) < 32:
            raise ValueError("插件内部密钥文件损坏，请从备份恢复；不会自动替换。")
        self.settings = Settings(
            binary=find_codex(cfg.binary), home=auth_home(cfg.home), state=self.root / "state",
            secret=secret, allowed_users=tuple(users), model=cfg.model,
            timeout=cfg.timeout, rpc_timeout=cfg.rpc_timeout,
        ).validated()

    async def start(self, cfg, users):
        try:
            await self._prepare_async(cfg, users)
            # Keep the prebound socket open until Uvicorn takes ownership; no port-selection race.
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.bind(("127.0.0.1", 0))
            self.socket.setblocking(False)
            self.settings = replace(self.settings, port=self.socket.getsockname()[1])
            await self._build_app()
            self.server = PluginServer(uvicorn.Config(
                self.app, host="127.0.0.1", port=self.settings.port, lifespan="on",
                access_log=False, log_config=None, log_level="warning", timeout_graceful_shutdown=3,
            ))
            self.task = asyncio.create_task(self._serve(), name="codex-image-mcp")
            async with asyncio.timeout(15):
                while not self.server.started:
                    if self.task.done():
                        await self.task
                        raise ValueError("内置 MCP 服务未能启动。")
                    await asyncio.sleep(0.02)
            self.bridge = Bridge(f"http://127.0.0.1:{self.settings.port}/mcp", self.settings.secret)
            return self.bridge
        except BaseException:
            await self.close()
            raise

    async def _serve(self):
        try:
            await self.server.serve(sockets=[self.socket])
        except SystemExit as exc:
            # A server startup error must not exit the MaiBot plugin runner.
            raise RuntimeError("内置 MCP 服务启动失败。") from exc

    async def check_login(self):
        if not self.task or self.task.done():
            raise ValueError("内置 MCP 服务已停止，请重新加载插件；不会自动重发绘图任务。")
        await Codex(self.settings).check_login()

    async def close(self):
        self.bridge = None
        try:
            if self.task:
                self.server.should_exit = True
                try:
                    await asyncio.wait_for(asyncio.shield(self.task), 12)
                except TimeoutError:
                    self.task.cancel()
                    await asyncio.gather(self.task, return_exceptions=True)
                finally:
                    self.task = None
        finally:
            try:
                # Also closes a database constructed before a cancelled or failed ASGI startup.
                if self.app:
                    await self.app.service.close()
                    self.app = None
            finally:
                if self.socket:
                    self.socket.close()
                    self.socket = None
                if self.lease:
                    self.lease.close()
                    self.lease = None
