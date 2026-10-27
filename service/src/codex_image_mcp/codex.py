"""One owned app-server process per job; no shell, user config, or API-key environment."""

from collections.abc import Awaitable, Callable
import asyncio
import json
import os
import signal

from .config import Settings


class Rpc:
    def __init__(self, argv: list[str], env: dict[str, str], timeout: float):
        self.argv, self.env, self.timeout = argv, env, timeout
        self.pending: dict[int, asyncio.Future] = {}
        self.events = asyncio.Queue(maxsize=128)
        self.serial = 0
        self.proc = None
        self.tasks = []

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=40 * 1024 * 1024,
            start_new_session=os.name != "nt",
        )
        self.tasks = [
            asyncio.create_task(self._read()),
            asyncio.create_task(self._stderr()),
        ]

    async def _stderr(self):
        # Drain output without retaining credentials, prompts or unbounded logs.
        while await self.proc.stderr.read(8192):
            pass

    async def _read(self):
        try:
            while line := await self.proc.stdout.readline():
                msg = json.loads(line)
                if "method" in msg:
                    if "id" in msg:
                        await self.send(
                            {
                                "id": msg["id"],
                                "error": {
                                    "code": -32601,
                                    "message": "Client actions are disabled",
                                },
                            }
                        )
                    elif msg["method"] in {"item/completed", "turn/completed", "error"}:
                        self.events.put_nowait(msg)
                elif msg.get("id") in self.pending:
                    future = self.pending.pop(msg["id"])
                    if not future.done():
                        future.set_result(msg)
            raise ConnectionError("CODEX_DISCONNECTED")
        except asyncio.CancelledError:
            raise
        except Exception:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("CODEX_PROTOCOL_LOST"))
            self.pending.clear()
            # Retain already received image events before reporting the disconnect.
            await self.events.put({"method": "connection/lost", "params": {}})

    async def send(self, msg: dict):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()

    async def call(self, method: str, params: dict):
        self.serial += 1
        key = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            await self.send({"id": key, "method": method, "params": params})
            result = await asyncio.wait_for(future, self.timeout)
            if "error" in result:
                raise RuntimeError("CODEX_RPC_REJECTED")
            return result["result"]
        finally:
            self.pending.pop(key, None)

    async def close(self):
        if self.proc and self.proc.returncode is None:
            try:
                if os.name == "nt":
                    self.proc.terminate()
                else:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                if os.name == "nt":
                    self.proc.kill()
                else:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                await self.proc.wait()
            except ProcessLookupError:
                pass
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


class Codex:
    def __init__(self, settings: Settings):
        self.cfg = settings

    def environment(self) -> dict[str, str]:
        # Keep networking and OS runtime settings, never inherit provider keys or model overrides.
        keep = {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        }
        env = {k: v for k, v in os.environ.items() if k.upper() in keep}
        env["CODEX_HOME"] = str(self.cfg.home)
        return env

    async def check_login(self):
        """Read-only account probe: never creates a thread or starts an inference turn."""
        if await asyncio.to_thread((self.cfg.home / "config.toml").exists):
            raise ValueError("codex.home 必须是专用登录目录，不能包含 config.toml。")
        rpc = Rpc(
            [self.cfg.binary, "-c", 'cli_auth_credentials_store="file"', "app-server", "--listen", "stdio://"],
            self.environment(), self.cfg.rpc_timeout,
        )
        try:
            await rpc.start()
            await rpc.call("initialize", {"clientInfo": {"name": "maibot_image_check", "version": "0.2.0"}})
            await rpc.send({"method": "initialized"})
            account = await rpc.call("account/read", {"refreshToken": False})
            if (account.get("account") or {}).get("type") != "chatgpt":
                raise ValueError("请先用 login.py 手动登录 ChatGPT 账号；登录目录需与 codex.home 一致。")
        except (OSError, ConnectionError, RuntimeError, TimeoutError) as exc:
            raise ValueError("Codex 客户端或登录检查失败，请检查客户端版本、登录状态和网络。") from exc
        finally:
            await rpc.close()

    async def generate(
        self,
        job: dict,
        on_ids: Callable[..., Awaitable],
        on_image: Callable[[str], Awaitable],
    ) -> str:
        # A dedicated auth-only home avoids loading an operator's MCP servers, hooks and providers.
        if await asyncio.to_thread((self.cfg.home / "config.toml").exists):
            raise ValueError("CODEX_HOME_MUST_BE_AUTH_ONLY")
        work = self.cfg.state / "work" / job["id"]
        await asyncio.to_thread(work.mkdir, parents=True, exist_ok=True)
        rpc = Rpc(
            [
                self.cfg.binary,
                "-c",
                'cli_auth_credentials_store="file"',
                "-c",
                "features.shell_tool=false",
                "-c",
                'web_search="disabled"',
                "app-server",
                "--listen",
                "stdio://",
            ],
            self.environment(),
            self.cfg.rpc_timeout,
        )
        thread = turn = None
        try:
            await rpc.start()
            await rpc.call(
                "initialize",
                {"clientInfo": {"name": "maibot_image_mcp", "version": "0.1.0"}},
            )
            await rpc.send({"method": "initialized"})
            account = await rpc.call("account/read", {"refreshToken": False})
            if (account.get("account") or {}).get("type") != "chatgpt":
                raise ValueError("CHATGPT_LOGIN_REQUIRED")
            params = {
                "cwd": str(work),
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
            }
            if self.cfg.model:
                params["model"] = self.cfg.model
            started = await rpc.call("thread/start", params)
            thread = started["thread"]["id"]
            await on_ids(thread_id=thread)
            prompt = (
                "Use only the built-in image generation tool. Generate exactly one image for this "
                "request. Do not execute commands or call external services. If image generation "
                "is unavailable, stop. User image description follows:\n"
                + job["prompt"]
            )
            result = await rpc.call(
                "turn/start",
                {"threadId": thread, "input": [{"type": "text", "text": prompt}]},
            )
            turn = result["turn"]["id"]
            await on_ids(turn_id=turn, status="running")
            async with asyncio.timeout(self.cfg.timeout):
                while True:
                    msg = await rpc.events.get()
                    method, event = msg["method"], msg.get("params", {})
                    if method in {"connection/lost", "error"}:
                        raise ConnectionError("CODEX_DISCONNECTED")
                    if (
                        event.get("threadId") != thread
                        or event.get("turnId", turn) != turn
                    ):
                        continue
                    if method == "item/completed":
                        item = event.get("item", {})
                        if (
                            item.get("type") == "imageGeneration"
                            and item.get("status") == "completed"
                        ):
                            if item.get("failure") or not item.get("savedPath"):
                                raise ValueError("IMAGE_OUTPUT_MISSING")
                            await on_image(item["savedPath"])
                    if method == "turn/completed":
                        return event["turn"]["status"]
        except (TimeoutError, asyncio.CancelledError):
            if thread and turn:
                try:
                    await asyncio.wait_for(
                        rpc.call(
                            "turn/interrupt", {"threadId": thread, "turnId": turn}
                        ),
                        3,
                    )
                except Exception:
                    pass
            raise
        finally:
            await rpc.close()
