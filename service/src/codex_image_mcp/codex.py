"""Native auth broker reuses login; an ephemeral-auth worker owns each isolated drawing."""

from collections.abc import Awaitable, Callable
from pathlib import Path
import asyncio
import base64
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
        self.request_handler = None

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
                        if self.request_handler and msg["method"] == "account/chatgptAuthTokens/refresh":
                            try:
                                result = await asyncio.wait_for(self.request_handler(msg["params"]), 9)
                                await self.send({"id": msg["id"], "result": result})
                            except Exception:
                                await self.send({"id": msg["id"], "error": {
                                    "code": -32000, "message": "ChatGPT authentication refresh unavailable"}})
                        else:
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

    @property
    def output_root(self) -> Path:
        return self.cfg.state / "codex-runtime"

    def environment(self, home: Path | None = None) -> dict[str, str]:
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
            "CODEX_CA_CERTIFICATE",
            "DBUS_SESSION_BUS_ADDRESS",
            "XDG_RUNTIME_DIR",
            "LOCALAPPDATA",
        }
        env = {k: v for k, v in os.environ.items() if k.upper() in keep}
        env["CODEX_HOME"] = str(home or self.cfg.home)
        return env

    def broker(self) -> Rpc:
        # The broker only performs auth RPCs. It never creates a thread or invokes tools.
        # Leave credential-store configuration to Codex so file and keyring logins both work.
        return Rpc([self.cfg.binary, "-c", 'model_provider="openai"',
                    "app-server", "--listen", "stdio://"], self.environment(), self.cfg.rpc_timeout)

    async def initialize(self, rpc: Rpc):
        await rpc.start()
        await rpc.call("initialize", {"clientInfo": {"name": "maibot_image_mcp", "version": "0.5.0"},
                                      "capabilities": {"experimentalApi": True}})
        await rpc.send({"method": "initialized"})

    async def tokens(self, broker: Rpc, expected_account: str | None = None) -> dict:
        account = await broker.call("account/read", {"refreshToken": False})
        if (account.get("account") or {}).get("type") != "chatgpt":
            raise ValueError("未找到 ChatGPT 登录。请使用运行宿主机 MCP 服务的系统用户手动登录（codex login）；可选目录需与服务 home 一致。")
        # Native Codex owns refresh and persistence in the original file/keyring store.
        auth = await broker.call("getAuthStatus", {"includeToken": True, "refreshToken": True})
        token = auth.get("authToken")
        if auth.get("authMethod") not in {"chatgpt", "chatgptAuthTokens"} or not isinstance(token, str):
            raise ValueError("Codex 未能提供 ChatGPT 登录，请检查客户端版本并重新登录。")
        try:
            payload = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            identity = claims["https://api.openai.com/auth"]["chatgpt_account_id"]
            if not isinstance(identity, str) or not identity:
                raise ValueError
        except (IndexError, KeyError, ValueError, TypeError) as exc:
            raise ValueError("Codex 返回的 ChatGPT 账号信息无效。") from exc
        if expected_account is not None and identity != expected_account:
            raise ValueError("绘图期间登录账号发生变化，请查询原任务；不会自动切换账号重画。")
        return {"accessToken": token, "chatgptAccountId": identity}

    async def check_login(self):
        """Non-inference account probe; native Codex may refresh its existing login."""
        rpc = self.broker()
        try:
            await self.initialize(rpc)
            await self.tokens(rpc)
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
        work = self.cfg.state / "work" / job["id"]
        await asyncio.to_thread(work.mkdir, parents=True, exist_ok=True)
        runtime_home = self.output_root / job["id"]
        await asyncio.to_thread(runtime_home.mkdir, parents=True, exist_ok=True, mode=0o700)
        if await asyncio.to_thread((runtime_home / "config.toml").exists):
            raise ValueError("绘图运行目录中出现未知 config.toml，请检查宿主机服务数据。")
        rpc = Rpc(
            [
                self.cfg.binary,
                "-c",
                'cli_auth_credentials_store="ephemeral"',
                "-c", 'model_provider="openai"',
                "-c", "features.hooks=false",
                "-c", "features.plugins=false",
                "-c", "features.apps=false",
                "-c", "project_doc_max_bytes=0",
                "-c",
                "features.shell_tool=false",
                "-c",
                'web_search="disabled"',
                "app-server",
                "--listen",
                "stdio://",
            ],
            self.environment(runtime_home),
            self.cfg.rpc_timeout,
        )
        thread = turn = None
        broker = self.broker()
        try:
            await self.initialize(broker)
            tokens = await self.tokens(broker)

            async def refresh(params):
                previous = params.get("previousAccountId")
                if previous is not None and previous != tokens["chatgptAccountId"]:
                    raise ValueError("AUTH_ACCOUNT_CHANGED")
                return await self.tokens(broker, tokens["chatgptAccountId"])

            rpc.request_handler = refresh
            await self.initialize(rpc)
            await rpc.call("account/login/start", {"type": "chatgptAuthTokens", **tokens})
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
            try:
                await rpc.close()
            finally:
                await broker.close()
