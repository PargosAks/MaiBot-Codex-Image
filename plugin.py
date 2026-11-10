"""MaiBot commands for an external MCP service. Identity comes only from Host."""

from contextlib import nullcontext
from pathlib import Path
import asyncio
import hashlib
import logging
import re

from maibot_sdk import API, Command, HookHandler, Field, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import HookMode

from .mcp_client.bridge import Bridge
from .domain.delivery import Deliveries
from .host.delivery_runtime import DeliveryBlocked, dispatch_delivery
from .host.host_runtime import Autonomy, HostRuntime, RuntimeLease, joined
from .host.runtime_generation import GenerationHost, RuntimeGeneration, RuntimeUse
from .host.group_thought_runtime import (
    GroupThoughtRuntime, group_thought_after_response, group_thought_before_request, group_thought_receipt,
)
from .domain.policy import Scope, StoreError
from .creation.rendering import MCPRenderer
from .workspace.studio import Studio
from .host.studio_chat import (
    StudioChat, StudioChatBinding, StudioChatPolicy, StudioChatSnapshot, is_studio_status_message,
    is_studio_suggestion_message,
    snapshot_from_summary, studio_chat_before_replyer_request, studio_chat_before_request,
)

logger = logging.getLogger(__name__)
_SUBMIT_TIMEOUT_SECONDS = 90
_SUBMIT_NOTICE_TIMEOUT_SECONDS = 10
_SUBMIT_UNKNOWN = "绘图提交结果未知，未确认任务编号。请勿重复提交；如已取得原任务编号，请查询原任务。"
_SUBMIT_UNSENT = "本次尚未提交绘图。"
_SUBMIT_REVOKED = "配置或运行状态已变更，本次尚未提交绘图。"


class PluginSection(PluginConfigBase):
    config_version: str = "0.12.0"
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
        self._generation: RuntimeGeneration | None = None
        self._delivery_lease: RuntimeLease | None = None
        self._starting: asyncio.Task | None = None
        self._unload_task: asyncio.Task | None = None
        self._unloading = False
        self.startup_error = ""
        self.lifecycle = asyncio.Lock()
        self.watchers: dict[str, asyncio.Task] = {}
        self.studio_chat = None
        self.artist_runtime = None
        self._dispatch_epoch = 0
        self.group_thought_runtime = None

    @property
    def bridge(self) -> Bridge | None:
        return self._generation.bridge if self._generation is not None else None

    @bridge.setter
    def bridge(self, bridge: Bridge | None) -> None:
        self._bound_resources().bridge = bridge

    @property
    def journal(self) -> Deliveries | None:
        return self._generation.journal if self._generation is not None else None

    @journal.setter
    def journal(self, journal: Deliveries | None) -> None:
        self._bound_resources().journal = journal

    def _bound_resources(self) -> RuntimeGeneration:
        # 保留现有适配器读取和测试注入入口；替换资源不会重绑到更新的配置。
        if self._generation is None:
            self._generation = RuntimeGeneration(self.config, self._dispatch_epoch, ready=True)
        return self._generation

    def _matches_generation(self, generation: RuntimeGeneration) -> bool:
        return (self._generation is generation and not self._unloading
                and generation.epoch == self._dispatch_epoch and generation.config is self.config)

    def _current(self, generation: RuntimeGeneration) -> bool:
        return generation.ready and self._matches_generation(generation)

    def set_plugin_config(self, config: dict) -> None:
        super().set_plugin_config(config)
        if self._generation is not None:
            # SDK 先注入再调异步回调。这里立即停自主派发，手动命令在派发前
            # 复核代身份；生命周期回调取消旧操作，但保留已派发 submit 的原回执。
            self._stop_artist_workers(cancel_users=False)

    async def on_load(self):
        async with self.lifecycle:
            if self._unload_task is not None and not self._unload_task.done():
                return
            if self._generation is None:
                self._unloading = False
            await self._load()

    async def _load(self):
        if self._generation is not None:
            return
        generation = self._generation = RuntimeGeneration(self.config, self._dispatch_epoch)
        config, cfg = generation.config, generation.config.connection
        self.startup_error = ""
        connection_error = ""
        try:
            await joined(self._open_journal())
            if not self._matches_generation(generation):
                await joined(self._cleanup(release_lease=False))
                return
            if config.plugin.enabled and cfg.allowed_users:
                try:
                    generation.bridge = Bridge(cfg.url, cfg.secret)
                except ValueError as exc:
                    # 连接配置无效时仍要撤销已失效的委托；没有 Bridge 不启动创作。
                    connection_error = str(exc)
            self.artist_runtime = HostRuntime(GenerationHost(self, generation))
            # 配置再次替换时可以中止旧代启动；HostRuntime 会等 SQLite 写入结束。
            self._starting = asyncio.create_task(self.artist_runtime.start())
            try:
                await self._starting
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling() or self._matches_generation(generation):
                    raise
                await joined(self._cleanup(release_lease=False))
                return
            finally:
                self._starting = None
            if not self._matches_generation(generation):
                self._stop_artist_workers()
                await joined(self._cleanup(release_lease=False))
                return
            self.group_thought_runtime = GroupThoughtRuntime(self)
            self.studio_chat = StudioChat(self.ctx, policy=self._studio_chat_policy, read=self._studio_chat_read,
                                         read_receipt=self._studio_suggestion_receipt)
            if connection_error:
                raise ValueError(connection_error)
            generation.ready = True
        except ValueError as exc:
            # Keep configuration and the diagnostic command available for first-time setup.
            self.startup_error = connection_error or str(exc)
            self._stop_artist_workers()
            # 配置无效时插件仍可诊断和热重载；目录仍归此实例，直到卸载。
            await joined(self._cleanup(release_lease=False))
        except BaseException:
            await self._unload()
            raise

    async def on_unload(self):
        if self._unload_task is None or self._unload_task.done():
            self._unloading = True
            self._stop_artist_workers()
            self._unload_task = asyncio.create_task(self._finish_unload(self._generation))
        # 从排队等锁开始就拥有收尾。重复卸载共享任务，调用方取消不释放它的所有权。
        await joined(self._unload_task)

    async def _finish_unload(self, generation: RuntimeGeneration | None):
        if generation is not None:
            await generation.wait_submissions()
        async with self.lifecycle:
            await joined(self._cleanup())

    async def _open_journal(self):
        path = Path(self.ctx.paths.data_dir) / "delivery.sqlite3"
        if self._delivery_lease is None:
            # 先取发送目录锁，后打开会恢复 sending 的数据库，再取可选的 Artist 锁。
            # 两把锁都是非阻塞的；配置热重载期间不让出发送目录锁。
            try:
                self._delivery_lease = await asyncio.to_thread(RuntimeLease, path.with_suffix(".lock"))
            except StoreError as exc:
                raise StoreError("DELIVERY_IN_USE", "Another Host owns this plugin data directory") from exc
        self.journal = await asyncio.to_thread(Deliveries, path)

    def _stop_artist_workers(self, *, cancel_users: bool = True) -> None:
        self._dispatch_epoch += 1
        # 先撤销本代并停止派发，再等生命周期锁；慢清理不能让旧图继续派发。
        if cancel_users and self._generation is not None:
            self._generation.cancel_users()
        if self._starting is not None and not self._starting.cancelling():
            self._starting.cancel()
        for task in self.watchers.values():
            if not task.cancelling():
                task.cancel()
        if self.group_thought_runtime is not None:
            self.group_thought_runtime.stop()
        if self.studio_chat is not None:
            self.studio_chat.clear_queries()
            self.studio_chat = None
        # SDK 已替换配置；自主派发也必须在进入生命周期锁前停止。
        if self.artist_runtime is not None:
            self.artist_runtime.stopping = True
            for task in self.artist_runtime.tasks.values():
                if not task.cancelling():
                    task.cancel()

    async def _unload(self):
        self._stop_artist_workers()
        await joined(self._cleanup())

    async def _cleanup(self, *, release_lease: bool = True):
        generation = self._generation
        if generation is not None:
            # 已在锁前排队的命令可能在撤权后才进入，只允许它退出旧代。
            generation.cancel_users()
        tasks = list(self.watchers.values())
        await asyncio.gather(*tasks, return_exceptions=True)
        self.watchers.clear()
        if generation is not None:
            await generation.drain()
        if self.group_thought_runtime is not None:
            await self.group_thought_runtime.close()
            self.group_thought_runtime = None
        if self.artist_runtime is not None:
            await self.artist_runtime.close()
            self.artist_runtime = None
        if generation is not None and generation.journal is not None:
            await asyncio.to_thread(generation.journal.close)
        self._generation = None
        if release_lease and self._delivery_lease is not None:
            await asyncio.to_thread(self._delivery_lease.close)
            self._delivery_lease = None

    async def on_config_update(self, scope, config_data, version):
        del scope, config_data, version
        self._stop_artist_workers()
        generation = self._generation
        if generation is not None and generation.has_submissions:
            await joined(self._reload_after_submissions(generation))
        else:
            await self._reload_after_submissions(generation)

    async def _reload_after_submissions(self, generation: RuntimeGeneration | None):
        if generation is not None:
            # 旧代已失效；等在途 submit 的原回执时不占用聊天 Hook 的生命周期锁。
            await generation.wait_submissions()
        async with self.lifecycle:
            if self._unloading or (self._generation is not None and self._generation is not generation):
                return
            await joined(self._cleanup(release_lease=False))
            if not self._unloading:
                await self._load()

    def _studio_chat_policy(self):
        cfg = self.config
        return StudioChatPolicy(
            enabled=bool(self.studio_chat and cfg.plugin.enabled and cfg.autonomy.enabled),
            allowed_users=tuple(cfg.connection.allowed_users),
            bindings=tuple(StudioChatBinding(
                item.space_id, Scope(item.platform, item.stream_id, item.user_id), item.group_id,
                item.continuous and not item.paused, item.consent_ref,
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
                generation = self._generation
                if (generation is not None and self._current(generation)
                        and generation.config.plugin.enabled and generation.config.autonomy.enabled
                        and self.artist_runtime is not None):
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
        # 生命周期锁只用于借用一代；MCP 和上传不能堵住同一把锁上的 Hook。
        async with self.lifecycle:
            generation = self._generation
        ownership = generation.use() if generation is not None else nullcontext()
        with ownership as operation:
            result = await self._handle(kwargs, generation, operation)
            stream_id = kwargs.get("stream_id")
            if isinstance(stream_id, str) and stream_id:
                if operation is not None and operation.submitting:
                    # 卸载期间也允许把原提交回执送回原聊天；失败不覆盖已知的任务 ID。
                    await self._send_submit_notice(result[1], stream_id)
                elif not self._unloading:
                    await self.ctx.send.text(result[1], stream_id)
            return result

    async def _send_submit_notice(self, text: str, stream_id: str) -> None:
        try:
            async with asyncio.timeout(_SUBMIT_NOTICE_TIMEOUT_SECONDS):
                await self.ctx.send.text(text, stream_id)
        except Exception as exc:
            logger.warning("未能将绘图提交回执送回原聊天 (%s)", type(exc).__name__)

    async def _handle(
        self, kwargs, generation: RuntimeGeneration | None = None, operation: RuntimeUse | None = None,
    ):
        try:
            generation = generation if generation is not None else self._generation
            scope, message_id = trusted_identity(kwargs)
            live = self.config
            if not live.plugin.enabled:
                raise ValueError("绘图插件已禁用。")
            if f"{scope['platform']}:{scope['user_id']}" not in live.connection.allowed_users:
                raise ValueError("当前用户未获绘图授权。")
            if generation is not None and not self._current(generation):
                raise ValueError("绘图配置正在更新，本次未提交操作。")
            if generation is None or generation.bridge is None:
                raise ValueError(self.startup_error or "绘图服务未就绪，请检查插件配置并重新加载。")
            bridge = generation.bridge
            matched = kwargs.get("matched_groups")
            if not isinstance(matched, dict):
                raise ValueError("缺少宿主命令匹配信息。")
            action, text = matched.get("action"), (matched.get("arg") or "").strip()
            if action not in {"画图", "画图状态", "重发画图", "画图诊断"}:
                raise ValueError("不支持的绘图命令。")
            if action in {"画图", "重发画图"}:
                await self._guard_single_work_command(scope)
            if action == "画图诊断":
                await bridge.call("health", scope, {})
                if not self._current(generation):
                    raise ValueError("绘图配置已变更，本次诊断已停止。")
                return True, "宿主机 MCP 服务连接正常；Codex 登录状态会在绘图时检查。诊断没有生成图片。", 2
            if not text:
                raise ValueError(
                    "用法：/画图 描述；/画图状态 任务编号；/重发画图 任务编号"
                )
            if action == "画图":
                key = hashlib.sha256(
                    (scope["platform"] + ":" + message_id + ":draw").encode()
                ).hexdigest()
                user = f"{scope['platform']}:{scope['user_id']}"

                def authorized() -> bool:
                    return (self._current(generation) and generation.config.plugin.enabled
                            and user in generation.config.connection.allowed_users)

                if not authorized():
                    raise ValueError(_SUBMIT_REVOKED)

                dispatched = False

                def before_dispatch() -> bool:
                    nonlocal dispatched
                    # 握手仍可取消。实际派发前复核本次命令绑定的代与授权，再保护原回执。
                    if not authorized() or asyncio.current_task().cancelling():
                        return False
                    dispatched = True
                    if operation is not None:
                        operation.submitting = True
                    return True

                try:
                    async with asyncio.timeout(_SUBMIT_TIMEOUT_SECONDS):
                        result = await bridge.call("submit", scope, {"request_id": key, "prompt": text},
                                                   before_dispatch=before_dispatch)
                    job = result["job_id"]
                    if not isinstance(job, str) or not job:
                        return False, _SUBMIT_UNKNOWN, 2
                except asyncio.CancelledError:
                    await self._send_submit_notice(_SUBMIT_UNKNOWN if dispatched else _SUBMIT_UNSENT,
                                                   scope["stream_id"])
                    raise
                except Exception:
                    if dispatched:
                        return False, _SUBMIT_UNKNOWN, 2
                    return False, _SUBMIT_UNSENT if authorized() else _SUBMIT_REVOKED, 2
                if not self._current(generation):
                    return True, f"任务已提交：{job}；运行状态已变更，自动取图已停止，请查询原任务。", 2
                if job not in self.watchers:
                    task = asyncio.create_task(self._watch(scope, job, generation))
                    self.watchers[job] = task
                    task.add_done_callback(
                        lambda done, job=job: self._discard_watcher(job, done)
                    )
                return True, f"绘图任务已提交：{job}。完成后会发到当前聊天。", 2
            if not re.fullmatch(r"img_[0-9a-f]{32}", text):
                raise ValueError("任务编号格式不正确。")
            if action == "画图状态":
                result = await bridge.call("status", scope, {"job_id": text})
                if not self._current(generation):
                    raise ValueError("绘图配置已变更，本次查询已停止。")
                return (
                    True,
                    f"任务 {text}：{result['status']}；错误码：{result.get('error_code') or '无'}",
                    2,
                )
            if not self._current(generation):
                raise ValueError("配置或运行状态已变更，本次尚未提交取图。")
            await self._deliver(scope, text, manual=True, generation=generation)
            return True, "取图操作已处理。", 2
        except ValueError as exc:
            return False, str(exc), 2
        except Exception:
            return (
                False,
                "服务连接中断，结果可能未知。请用原任务编号查询，不要重复提交。",
                2,
            )

    def _discard_watcher(self, job: str, done: asyncio.Task) -> None:
        if self.watchers.get(job) is done:
            self.watchers.pop(job)

    async def _watch(self, scope, job, generation: RuntimeGeneration | None = None):
        generation = generation if generation is not None else self._generation
        if generation is None or generation.bridge is None:
            return
        cfg, bridge = generation.config.connection, generation.bridge
        try:
            async with asyncio.timeout(cfg.watch_seconds):
                while self._current(generation):
                    result = await bridge.call("status", scope, {"job_id": job})
                    if not self._current(generation):
                        return
                    if result["artifacts"]:
                        break
                    if result["status"] in {"failed", "outcome_unknown"}:
                        await self.ctx.send.text(
                            f"绘图任务 {job}：{result['status']}，不会自动重画。",
                            scope["stream_id"],
                        )
                        return
                    await asyncio.sleep(cfg.poll_seconds)
                else:
                    return
            # 轮询到期不能截断刚开始的上传；发送使用独立的 RPC 超时。
            await self._deliver(scope, job, generation=generation)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 结果未知时不自动重画或重发；旧代退出也不再发送通知。
            if not self._current(generation):
                return
            try:
                await self.ctx.send.text(
                    f"任务 {job} 暂停自动取图，请查询状态或使用 /重发画图 {job}。",
                    scope["stream_id"],
                )
            except Exception:
                pass

    async def _deliver(self, scope, job, manual=False, *, generation: RuntimeGeneration | None = None):
        from .host.host_adapters import send_image_receipt

        ownership = nullcontext()
        if generation is None:
            generation = self._generation
            if generation is not None:
                ownership = generation.use()
        with ownership:
            await self._guard_single_work_command(scope)
            if (generation is None or generation.bridge is None or generation.journal is None
                    or not self._current(generation) or not generation.config.plugin.enabled):
                raise ValueError("配置已变更，原手动取图已停止；请核查原任务。")
            image = await generation.bridge.call("result", scope, {"job_id": job})
            await self._guard_single_work_command(scope)
            if not self._current(generation) or not generation.config.plugin.enabled:
                raise ValueError("配置已变更，原手动取图已停止；请核查原任务。")

            async def recheck() -> None:
                if not self._current(generation) or not generation.config.plugin.enabled:
                    raise ValueError("配置已变更，尚未发送图片；请核查原任务。")

            async def dispatch() -> bool:
                return await send_image_receipt(
                    self.ctx, image, scope["stream_id"],
                    timeout_seconds=generation.config.connection.send_timeout_seconds,
                )

            try:
                await dispatch_delivery(generation.journal, job, manual=manual,
                                        before_dispatch=recheck, dispatch=dispatch)
            except DeliveryBlocked as exc:
                raise ValueError("图片已处理或发送结果未知，请先检查聊天记录。") from exc

    async def _guard_single_work_command(self, scope):
        # A manual command or old watcher must not bypass the lifetime ledger.
        # Read persisted policy even after its config entry has been removed.
        restricted = any(item.single_work and item.platform == scope["platform"]
                         and item.stream_id == scope["stream_id"]
                         for item in self.config.autonomy.spaces)
        paths = getattr(self.ctx, "paths", None)
        if not restricted and paths is not None:
            view = Studio(Path(paths.data_dir), scopes={})
            restricted = await asyncio.to_thread(view.single_work_for_stream,
                                                 scope["platform"], scope["stream_id"])
        if restricted:
            raise ValueError("当前聊天处于单作品验收限制；仅由自主流程创作和分享，不能手动绘图或重发。")


def create_plugin():
    return CodexImagePlugin()
