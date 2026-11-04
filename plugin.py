"""MaiBot commands for an external MCP service. Identity comes only from Host."""

from pathlib import Path
import asyncio
import hashlib
import logging
import re

from maibot_sdk import API, Command, HookHandler, Field, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode

from .bridge import Bridge
from .delivery import Deliveries
from .host_runtime import Autonomy, HostRuntime, joined
from .group_thought_runtime import (
    GroupThoughtRuntime, group_thought_after_response, group_thought_before_request, group_thought_receipt,
)
from .policy import Scope
from .rendering import MCPRenderer
from .studio import Studio
from .studio_chat import (
    StudioChat, StudioChatBinding, StudioChatPolicy, StudioChatSnapshot, is_studio_status_message,
    is_studio_suggestion_message,
    snapshot_from_summary, studio_chat_before_replyer_request, studio_chat_before_request,
)

logger = logging.getLogger(__name__)


class PluginSection(PluginConfigBase):
    config_version: str = "0.11.0"
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
    poll_seconds: float = Field(default=2.0, ge=0.1, le=30)
    watch_seconds: float = Field(default=360.0, ge=1, le=3600)
    send_timeout_seconds: float = Field(default=240.0, gt=0, le=600, strict=True,
                                       description="图片发送的 Host RPC 等待秒数；独立于模型超时")


class Config(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    connection: Connection = Field(default_factory=Connection)
    autonomy: Autonomy = Field(default_factory=Autonomy)


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
    studio_chat_before_request = studio_chat_before_request
    studio_chat_before_replyer_request = studio_chat_before_replyer_request
    group_thought_before_request = group_thought_before_request
    group_thought_after_response = group_thought_after_response
    group_thought_receipt = group_thought_receipt

    @classmethod
    def build_config_schema(cls, **metadata):
        schema = super().build_config_schema(**metadata)
        # Keep legacy catalog entries readable for migration, out of the settings form.
        autonomy_fields = schema["sections"]["autonomy"]["fields"]
        # An existing persona still guides the artist; it is not a catalog editor.
        autonomy_fields["persona"]["hidden"] = True
        fields = autonomy_fields["spaces"]["item_fields"]
        for legacy in ("profiles", "series"):
            fields.pop(legacy, None)
        return schema

    def __init__(self):
        super().__init__()
        self.bridge = None
        self.journal = None
        self.startup_error = ""
        self.lifecycle = asyncio.Lock()
        self.watchers: dict[str, asyncio.Task] = {}
        self.studio_chat = None
        self.artist_runtime = None
        self.group_thought_runtime = None

    async def on_load(self):
        async with self.lifecycle:
            await self._load()

    async def _load(self):
        if self.journal is not None:
            return
        cfg = self.config.connection
        self.startup_error = ""
        connection_error = ""
        try:
            if self.config.plugin.enabled and cfg.allowed_users:
                try:
                    self.bridge = Bridge(cfg.url, cfg.secret)
                except ValueError as exc:
                    # Still reconcile revoked delegations even when a connection
                    # edit is invalid. No worker can start without a Bridge.
                    connection_error = str(exc)
            await joined(self._open_journal())
            self.artist_runtime = HostRuntime(self)
            await self.artist_runtime.start()
            self.group_thought_runtime = GroupThoughtRuntime(self)
            self.studio_chat = StudioChat(self.ctx, policy=self._studio_chat_policy, read=self._studio_chat_read,
                                         read_receipt=self._studio_suggestion_receipt)
            if connection_error:
                raise ValueError(connection_error)
        except ValueError as exc:
            # Keep configuration and the diagnostic command available for first-time setup.
            self.startup_error = connection_error or str(exc)
            await self._unload()
        except BaseException:
            await self._unload()
            raise

    async def on_unload(self):
        self._stop_artist_workers()
        async with self.lifecycle:
            await self._unload()

    async def _open_journal(self):
        self.journal = await asyncio.to_thread(
            Deliveries, Path(self.ctx.paths.data_dir) / "delivery.sqlite3"
        )

    def _stop_artist_workers(self):
        if self.group_thought_runtime is not None:
            self.group_thought_runtime.stop()
        if self.studio_chat is not None:
            self.studio_chat.clear_queries()
            self.studio_chat = None
        # A command may hold lifecycle while awaiting MCP. Config is already
        # replaced by SDK: stop autonomous dispatch before waiting for that lock.
        if self.artist_runtime is not None:
            self.artist_runtime.stopping = True
            for task in self.artist_runtime.tasks.values():
                task.cancel()

    async def _unload(self):
        self._stop_artist_workers()
        await joined(self._cleanup())

    async def _cleanup(self):
        if self.group_thought_runtime is not None:
            await self.group_thought_runtime.close()
            self.group_thought_runtime = None
        if self.artist_runtime is not None:
            await self.artist_runtime.close()
            self.artist_runtime = None
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
        self._stop_artist_workers()
        async with self.lifecycle:
            await self._unload()
            await self._load()

    def _studio_chat_policy(self):
        cfg = self.config
        return StudioChatPolicy(
            enabled=bool(self.studio_chat and cfg.plugin.enabled and cfg.autonomy.enabled),
            allowed_users=tuple(cfg.connection.allowed_users),
            bindings=tuple(StudioChatBinding(
                item.space_id, Scope(item.platform, item.stream_id, item.user_id), item.group_id,
                item.continuous, item.consent_ref,
            ) for item in cfg.autonomy.spaces if item.space_id),
        )

    async def _studio_chat_read(self, space_id, scope):
        # The planner only sees its own verified binding. It cannot select an
        # administrative projection or supply scope fields in tool arguments.
        async with self.lifecycle:
            view = Studio(Path(self.ctx.paths.data_dir), scopes={space_id: scope})
            summary = await asyncio.to_thread(view.summary, space_id)
            return snapshot_from_summary(space_id, scope, summary)

    async def _studio_suggestion_receipt(self, space_id, scope, message_id):
        async with self.lifecycle:
            view = Studio(Path(self.ctx.paths.data_dir), scopes={space_id: scope})
            runtime = self.artist_runtime
            if runtime is None or runtime.stopping or space_id not in runtime.grant_versions:
                raise ValueError("Inactive studio delegation")
            receipt = await asyncio.to_thread(view.inspiration_receipt, space_id, message_id,
                                              expected_auth_version=runtime.grant_versions[space_id])
            auth = receipt["authorization"]
            return StudioChatSnapshot(receipt["space_id"], scope, auth["version"], auth["authorized"],
                                      {"inspiration_received": receipt["inspiration_received"]})

    @API("studio_spaces", version="1", public=False)
    async def studio_spaces(self, offset: int = 0, limit: int = 20):
        return await asyncio.to_thread(Studio.for_plugin(self).spaces, offset, limit)

    @API("studio_summary", version="1", public=False)
    async def studio_summary(self, space_id: str):
        return await asyncio.to_thread(Studio.for_plugin(self).summary, space_id)

    @API("studio_records", version="1", public=False)
    async def studio_records(self, space_id: str, section: str, offset: int = 0, limit: int = 20, work_id: str = ""):
        return await asyncio.to_thread(Studio.for_plugin(self).records, space_id, section, offset, limit, work_id)

    @API("studio_image", version="1", public=False)
    async def studio_image(self, space_id: str, version_id: str, thumbnail: bool = True, offset: int = 0):
        return await asyncio.to_thread(Studio.for_plugin(self).image, space_id, version_id, thumbnail, offset)

    def _artist_renderer(self, store, space_id, scope, binding_id):
        return MCPRenderer(store, space_id, scope, self.bridge, binding_id=binding_id)

    @HookHandler(
        "chat.receive.after_process",
        name="codex_image_artist_chat",
        description="从受委托聊天积累绘画灵感，不拦截消息",
        mode=HookMode.OBSERVE,
    )
    async def artist_chat(self, **kwargs):
        try:
            if not self.config.plugin.enabled or not self.config.autonomy.enabled:
                return None
            scope, message_id = trusted_identity(kwargs)
            message = kwargs.get("message")
            is_query = is_studio_status_message(message)
            # Invalidate older reads on receipt, before an in-flight read can
            # hold lifecycle and delay this event. No I/O or await in this step.
            chat, runtime = self.studio_chat, self.artist_runtime
            owner = Scope(**scope)
            suggestion = not is_query and is_studio_suggestion_message(message)
            noted = chat is not None and chat.note_ingress(owner, message_id, is_query=is_query,
                                                            is_suggestion=suggestion)
            if is_query:
                return None
            async with self.lifecycle:
                if self.config.plugin.enabled and self.config.autonomy.enabled and self.artist_runtime is not None:
                    accepted = await self.artist_runtime.on_chat(scope, message_id, message)
                    # on_chat returns True only for a newly committed wake. A replay,
                    # skipped message or runtime replacement cannot issue a receipt.
                    if (accepted is True and noted and suggestion and self.studio_chat is chat
                            and self.artist_runtime is runtime):
                        chat.note_suggestion_accepted(owner, message_id)
        except ValueError as exc:
            # Rejected context must not interrupt MaiBot's normal chat flow.
            logger.warning("Artist ignored Host message: %s", getattr(exc, "code", "IDENTITY_INVALID"))
        return None

    @Command(
        "codex_image_command",
        description="订阅绘图、状态查询和取图",
        pattern=r"^/(?P<action>画图|画图状态|重发画图|画图诊断)(?:\s+(?P<arg>[\s\S]+))?$",
        permission="operator",
        # Covers the maximum 600s send plus MCP lookup and final text receipt.
        timeout_ms=720000,
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
                        break
                    if result["status"] in {"failed", "outcome_unknown"}:
                        await self.ctx.send.text(
                            f"绘图任务 {job}：{result['status']}，不会自动重画。",
                            scope["stream_id"],
                        )
                        return
                    await asyncio.sleep(self.config.connection.poll_seconds)
            # The drawing watch deadline must not cancel an upload that starts
            # near its end. Sending owns its separate bounded RPC wait.
            await self._deliver(scope, job)
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
        from .host_adapters import send_image_receipt

        image = await self.bridge.call("result", scope, {"job_id": job})
        if not await joined(asyncio.to_thread(self.journal.begin, job, manual)):
            raise ValueError("图片已处理或发送结果未知，请先检查聊天记录。")
        try:
            result = await send_image_receipt(
                self.ctx, image, scope["stream_id"],
                timeout_seconds=self.config.connection.send_timeout_seconds,
            )
            state = "sent" if result else "send_failed"
            await joined(asyncio.to_thread(self.journal.finish, job, state))
            if state != "sent":
                raise ValueError("图片已生成，但未确认发送成功；不会重新生成。")
        except BaseException as exc:
            # Host authorization rejection occurs before invoking the send handler.
            # Other errors can have an unknown external outcome and remain locked.
            if getattr(exc, "code", None) == "E_CAPABILITY_DENIED":
                await joined(
                    asyncio.to_thread(self.journal.finish, job, "send_failed")
                )
            # Preserve a known failure; convert a still-pending intent to unknown.
            await joined(asyncio.to_thread(self._mark_uncertain, job))
            raise

    def _mark_uncertain(self, job):
        with self.journal.lock, self.journal.db:
            self.journal.db.execute(
                "UPDATE delivery SET state='send_unknown' WHERE job=? AND state='sending'",
                (job,),
            )


def create_plugin():
    return CodexImagePlugin()
