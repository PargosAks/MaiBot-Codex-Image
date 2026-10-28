"""Command-only first release. Host identity never comes from model parameters."""

from pathlib import Path
import asyncio
import hashlib
import re

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

from .bridge import Bridge
from .delivery import Deliveries


class PluginSection(PluginConfigBase):
    config_version: str = "0.6.0"
    enabled: bool = True


class Connection(PluginConfigBase):
    url: str = Field(default="http://127.0.0.1:8765/mcp", description="绘图 MCP 地址")
    secret: str = Field(
        default="",
        description="服务共享密钥，至少 32 字符",
        json_schema_extra={"secret": True},
    )
    allowed_users: list[str] = Field(
        default_factory=list, description="允许用户，如 qq:123456；空列表拒绝所有人"
    )
    poll_seconds: float = Field(default=2, ge=0.1, le=30)
    watch_seconds: float = Field(default=360, ge=1, le=3600)


class Config(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    connection: Connection = Field(default_factory=Connection)


def trusted_identity(kwargs: dict) -> tuple[dict, str]:
    # Command context fields are injected by Host, never exposed in a Tool schema.
    scope = {key: kwargs.get(key) for key in ("platform", "stream_id", "user_id")}
    message_id = kwargs.get("message_id")
    message = kwargs.get("message")
    if isinstance(message, dict):
        info = message.get("message_info", {})
        user = info.get("user_info", {}) if isinstance(info, dict) else {}
        nested = {
            "platform": message.get("platform"),
            "stream_id": message.get("session_id"),
            "user_id": user.get("user_id") if isinstance(user, dict) else None,
        }
        for key, value in nested.items():
            if value and scope[key] and value != scope[key]:
                raise ValueError("宿主身份信息不一致，已拒绝绘图。")
            if value:
                scope[key] = value
        if message.get("message_id"):
            if message_id and message_id != message["message_id"]:
                raise ValueError("宿主消息标识不一致。")
            message_id = message["message_id"]
    if not all(
        isinstance(value, str) and value.strip()
        for value in (*scope.values(), message_id)
    ):
        raise ValueError("缺少宿主提供的用户、聊天流或消息标识，已拒绝绘图。")
    return scope, message_id


class CodexImagePlugin(MaiBotPlugin):
    config_model = Config

    def __init__(self):
        super().__init__()
        self.bridge = None
        self.journal = None
        self.startup_error = ""
        self.lifecycle = asyncio.Lock()
        self.watchers: dict[str, asyncio.Task] = {}

    async def on_load(self):
        async with self.lifecycle:
            await self._load()

    async def _load(self):
        if self.journal is not None:
            return
        cfg = self.config.connection
        self.startup_error = ""
        try:
            if self.config.plugin.enabled and cfg.allowed_users:
                self.bridge = Bridge(cfg.url, cfg.secret)
            self.journal = await asyncio.to_thread(
                Deliveries, Path(self.ctx.paths.data_dir) / "delivery.sqlite3"
            )
        except ValueError as exc:
            # Keep configuration and the diagnostic command available for first-time setup.
            self.startup_error = str(exc)
            await self._unload()
        except BaseException:
            await self._unload()
            raise

    async def on_unload(self):
        async with self.lifecycle:
            await self._unload()

    async def _unload(self):
        tasks = list(self.watchers.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.watchers.clear()
        self.bridge = None
        if self.journal:
            await asyncio.to_thread(self.journal.close)
            self.journal = None

    async def on_config_update(self, scope, config_data, version):
        del scope, config_data, version
        async with self.lifecycle:
            await self._unload()
            await self._load()

    @Command(
        "codex_image_command",
        description="订阅绘图、状态查询和取图",
        pattern=r"^/(?P<action>画图|画图状态|重发画图|画图诊断)(?:\s+(?P<arg>[\s\S]+))?$",
        permission="operator",
    )
    async def command(self, **kwargs):
        async with self.lifecycle:
            result = await self._handle(kwargs)
        stream_id = kwargs.get("stream_id")
        if isinstance(stream_id, str) and stream_id:
            await self.ctx.send.text(result[1], stream_id)
        return result

    async def _handle(self, kwargs):
        try:
            scope, message_id = trusted_identity(kwargs)
            cfg = self.config.connection
            if not self.config.plugin.enabled:
                raise ValueError("绘图插件已禁用。")
            if f"{scope['platform']}:{scope['user_id']}" not in cfg.allowed_users:
                raise ValueError("当前用户未获绘图授权。")
            if not self.bridge:
                raise ValueError(self.startup_error or "绘图服务未就绪，请检查插件配置并重新加载。")
            matched = kwargs.get("matched_groups")
            if not isinstance(matched, dict):
                raise ValueError("缺少宿主命令匹配信息。")
            action, text = matched.get("action"), (matched.get("arg") or "").strip()
            if action not in {"画图", "画图状态", "重发画图", "画图诊断"}:
                raise ValueError("不支持的绘图命令。")
            if action == "画图诊断":
                await self.bridge.call("health", scope, {})
                return True, "宿主机 MCP 服务连接正常；Codex 登录状态会在绘图时检查。诊断没有生成图片。", 2
            if not text:
                raise ValueError(
                    "用法：/画图 描述；/画图状态 任务编号；/重发画图 任务编号"
                )
            if action == "画图":
                key = hashlib.sha256(
                    (scope["platform"] + ":" + message_id + ":draw").encode()
                ).hexdigest()
                result = await self.bridge.call(
                    "submit", scope, {"request_id": key, "prompt": text}
                )
                job = result["job_id"]
                if job not in self.watchers:
                    task = asyncio.create_task(self._watch(scope, job))
                    self.watchers[job] = task
                    task.add_done_callback(
                        lambda done, job=job: self.watchers.pop(job, None)
                    )
                return True, f"绘图任务已提交：{job}。完成后会发到当前聊天。", 2
            if not re.fullmatch(r"img_[0-9a-f]{32}", text):
                raise ValueError("任务编号格式不正确。")
            if action == "画图状态":
                result = await self.bridge.call("status", scope, {"job_id": text})
                return (
                    True,
                    f"任务 {text}：{result['status']}；错误码：{result.get('error_code') or '无'}",
                    2,
                )
            await self._deliver(scope, text, manual=True)
            return True, "取图操作已处理。", 2
        except ValueError as exc:
            return False, str(exc), 2
        except Exception:
            return (
                False,
                "服务连接中断，结果可能未知。请用原任务编号查询，不要重复提交。",
                2,
            )

    async def _watch(self, scope, job):
        try:
            async with asyncio.timeout(self.config.connection.watch_seconds):
                while True:
                    result = await self.bridge.call("status", scope, {"job_id": job})
                    if result["artifacts"]:
                        await self._deliver(scope, job)
                        return
                    if result["status"] in {"failed", "outcome_unknown"}:
                        await self.ctx.send.text(
                            f"绘图任务 {job}：{result['status']}，不会自动重画。",
                            scope["stream_id"],
                        )
                        return
                    await asyncio.sleep(self.config.connection.poll_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            # No automatic generation or delivery retry on an uncertain outcome.
            try:
                await self.ctx.send.text(
                    f"任务 {job} 暂停自动取图，请查询状态或使用 /重发画图 {job}。",
                    scope["stream_id"],
                )
            except Exception:
                pass

    async def _deliver(self, scope, job, manual=False):
        image = await self.bridge.call("result", scope, {"job_id": job})
        if not await asyncio.to_thread(self.journal.begin, job, manual):
            raise ValueError("图片已处理或发送结果未知，请先检查聊天记录。")
        try:
            result = await self.ctx.send.image(
                image, scope["stream_id"], return_details=True
            )
            if isinstance(result, dict) and isinstance(result.get("sent"), bool):
                state = "sent" if result["sent"] else "send_failed"
            elif isinstance(result, bool):
                state = "sent" if result else "send_failed"
            else:
                state = "send_unknown"
            await asyncio.to_thread(self.journal.finish, job, state)
            if state != "sent":
                raise ValueError("图片已生成，但未确认发送成功；不会重新生成。")
        except BaseException as exc:
            # Host authorization rejection occurs before invoking the send handler.
            # Other errors can have an unknown external outcome and remain locked.
            if getattr(exc, "code", None) == "E_CAPABILITY_DENIED":
                await asyncio.shield(
                    asyncio.to_thread(self.journal.finish, job, "send_failed")
                )
            # Preserve a known failure; convert a still-pending intent to unknown.
            await asyncio.shield(asyncio.to_thread(self._mark_uncertain, job))
            raise

    def _mark_uncertain(self, job):
        with self.journal.lock, self.journal.db:
            self.journal.db.execute(
                "UPDATE delivery SET state='send_unknown' WHERE job=? AND state='sending'",
                (job,),
            )


def create_plugin():
    return CodexImagePlugin()
