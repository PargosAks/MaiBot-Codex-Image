"""Host-owned autonomous workers. No timer, model or network work at import time."""

import asyncio
import hashlib
import json
import logging
import os
import re
from pathlib import Path

from maibot_sdk import Field, PluginConfigBase

from .artist import Artist
from .assets import ASSET_ROLES
from .host_media import IMAGE_ONLY_TEXT, UNREVIEWED_DESCRIPTION, current_images
from .policy import Delegation, Limits, Scope, StoreError
from .store import ArtistStore
from .thinking import Thinking

logger = logging.getLogger(__name__)


class Budget(PluginConfigBase):
    think: int = Field(default=0, ge=0, strict=True)
    render: int = Field(default=0, ge=0, strict=True)
    share: int = Field(default=0, ge=0, strict=True)
    max_inflight: int = Field(default=0, ge=0, strict=True)
    max_revisions: int = Field(default=0, ge=0, strict=True)

    def limits(self):
        return Limits(**self.model_dump())


class ArtistSpace(PluginConfigBase):
    space_id: str = ""
    platform: str = "qq"
    user_id: str = Field(default="", description="持续委托人，必须在 connection.allowed_users 内")
    stream_id: str = Field(default="", description="Host 已有聊天流 ID；不得由模型指定")
    group_id: str = Field(default="", description="空值为委托人的私聊；群聊填写真实群 ID")
    continuous: bool = Field(default=False, strict=True, description="明确授权持续自主创作，白名单本身不代表委托")
    consent_ref: str = Field(default="", description="管理员录入的持续委托依据；不要填密钥")
    reference_inputs: bool = Field(default=False, strict=True, description="单独允许采集当前消息图片作为参考素材")
    media_consent_ref: str = Field(default="", description="独立媒体采集授权依据；原文字委托不授权图片采集")
    limits: Budget = Field(default_factory=Budget)
    profiles: list[dict] = Field(default_factory=list, max_length=8,
                                 description="空间私有创作档案；修改内容需增加 revision")
    series: list[dict] = Field(default_factory=list, max_length=8,
                               description="系列目标及固定档案版本；不授予额外创作权限")


class Autonomy(PluginConfigBase):
    enabled: bool = Field(default=False, strict=True, description="自主创作总开关；启用后可消耗模型和绘图额度并发送作品")
    spaces: list[ArtistSpace] = Field(default_factory=list, max_length=32)
    global_limits: Budget = Field(default_factory=Budget)
    tick_seconds: float = Field(default=5, ge=0.1, le=60)
    idle_seconds: int = Field(default=300, ge=60, le=86400)
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    persona: str = Field(default="热爱观察与绘画的小画师", min_length=1, max_length=4000)
    plan_task: str = Field(default="utils", min_length=1, max_length=100)
    review_task: str = Field(default="vlm", min_length=1, max_length=100)


async def joined(awaitable):
    """Do not leave SQLite writes or cleanup running after the owner is cancelled."""
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            task.result()
        except Exception as exc:
            raise cancelled from exc
        raise


class RuntimeLease:
    """One Host runtime may recover this artist database at a time, across processes."""

    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if path.stat().st_size == 0:
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise StoreError("ARTIST_IN_USE", "Another Host owns autonomous state") from exc

    def close(self):
        # Closing the descriptor releases the OS lock; never delete the lock file.
        self.file.close()


class HostRuntime:
    def __init__(self, plugin):
        self.plugin = plugin
        self.config = plugin.config.autonomy
        self.store = None
        self.lease = None
        self.artists = {}
        self.tasks = {}
        self.signals = {}
        self.errors = {}
        self.media_errors = {}
        self.media_policies = {}
        self.stopping = False
        self._close_task = None

    async def db(self, name, *args, **kwargs):
        return await joined(asyncio.to_thread(getattr(self.store, name), *args, **kwargs))

    async def _verify_stream(self, item):
        ctx = self.plugin.ctx
        if item.group_id:
            capability = "chat.get_stream_by_group_id"
            args = {"group_id": item.group_id, "platform": item.platform}
        else:
            capability = "chat.get_stream_by_user_id"
            args = {"user_id": item.user_id, "platform": item.platform}
        # ChatCapability unwraps `stream` even in a success=False envelope.
        # The public raw SDK method preserves the receipt needed for authorization.
        response = await ctx.call_host_method(
            "cap.call", payload={"capability": capability, "args": args},
            timeout_ms=int(self.config.timeout_seconds * 1000),
        )
        stream = response.get("stream") if isinstance(response, dict) and response.get("success") is True else None
        if not isinstance(stream, dict):
            raise StoreError("HOST_STREAM_UNAVAILABLE", "Configured Host stream is unavailable")
        target = "group_id" if item.group_id else "user_id"
        if (
            stream.get("platform") != item.platform
            or stream.get("stream_id") != item.stream_id
            or stream.get("session_id", item.stream_id) != item.stream_id
            or stream.get(target) != (item.group_id or item.user_id)
            or stream.get("is_group_session") is not bool(item.group_id)
        ):
            raise StoreError("HOST_STREAM_MISMATCH", "Configured target does not match Host identity")

    async def start(self):
        plugin, cfg = self.plugin, self.config
        enabled = plugin.config.plugin.enabled and cfg.enabled
        path = Path(plugin.ctx.paths.data_dir) / "artist.sqlite3"
        if not enabled and not path.exists():
            return
        # Acquire before opening/recovering state. Do not orphan a newly opened
        # database if cancellation arrives while its initialization thread runs.
        self.lease = RuntimeLease(path.with_suffix(".lock"))
        await joined(self._open_store(path))
        desired = {}
        if enabled:
            seen = set()
            for item in cfg.spaces:
                if not item.space_id.strip() or item.space_id == "*" or item.space_id in seen:
                    raise StoreError("INVALID_SPACE", "Space IDs must be nonempty and unique")
                seen.add(item.space_id)
                if not item.continuous:
                    continue
                scope = Scope(item.platform, item.stream_id, item.user_id)
                if f"{scope.platform}:{scope.user_id}" not in plugin.config.connection.allowed_users:
                    continue
                if not item.consent_ref.strip():
                    raise StoreError("AUTH_REQUIRED", "Continuous consent and allowlist membership are required")
                desired[item.space_id] = (item, scope)
        # Removing an entry, disabling autonomy, or removing its allowlist owner
        # revokes persisted authority before any new work is started.
        for old in await self.db("configured_spaces"):
            if old["space_id"] not in desired and old["authorized"]:
                await self.db("revoke", old["space_id"], Scope(**json.loads(old["scope"])),
                              source_ref="host-config:disabled-or-removed", granted_by="host-config")
        await self.db("set_global_limits", cfg.global_limits.limits() if enabled else Limits())
        if desired and plugin.bridge is None:
            raise StoreError("MCP_NOT_CONFIGURED", "Autonomous rendering requires a configured connection")
        for item, scope in desired.values():
            await self._verify_stream(item)
            # The endpoint defines a stable administrative binding. Changing it
            # cannot silently move an existing space or an unresolved operation.
            binding = "mcp:" + hashlib.sha256(plugin.config.connection.url.encode()).hexdigest()
            await self.db("create_space", item.space_id, scope, binding_id=binding)
            grants = await self.db("audit_authorizations", item.space_id, scope)
            if not grants or not grants[-1]["authorized"] or grants[-1]["source_ref"] != item.consent_ref:
                await self.db("grant", item.space_id, scope,
                              Delegation(item.consent_ref, "host-config", continuous=True))
            await self.db("set_limits", item.space_id, scope, item.limits.limits())
            current = await self.db("get_space", item.space_id, scope)
            if item.reference_inputs and not item.media_consent_ref.strip():
                raise StoreError("MEDIA_CONSENT_REQUIRED", "Separate media consent is required for image inputs")
            await self.db("configure_reference_policy", item.space_id, scope,
                          enabled=item.reference_inputs, consent_ref=item.media_consent_ref,
                          expected_auth_version=current["auth_version"])
            await self.db("configure_creative", item.space_id, scope, profiles=item.profiles,
                          series=item.series, expected_auth_version=current["auth_version"])
            from .host_adapters import HostThinking, HostSender
            artist = Artist(
                self.store, item.space_id, scope,
                Thinking(HostThinking(plugin.ctx, plan_task=cfg.plan_task, review_task=cfg.review_task)),
                plugin._artist_renderer(self.store, item.space_id, scope, binding),
                HostSender(plugin.ctx, plugin.journal, store=self.store, space_id=item.space_id, scope=scope),
                persona=cfg.persona, idle_seconds=cfg.idle_seconds, timeout_seconds=cfg.timeout_seconds,
                allow_network_renderer=True, allow_host_sender=True,
                reference_inputs=item.reference_inputs,
            )
            await artist.recover(offline=True)
            self.artists[item.space_id] = artist
            self.media_policies[item.space_id] = (item.reference_inputs, item.media_consent_ref)
            self.signals[item.space_id] = asyncio.Event()
        for space_id in self.artists:
            if self.stopping:
                break
            self.tasks[space_id] = asyncio.create_task(self._serve(space_id), name="codex-artist:" + space_id)

    async def _open_store(self, path):
        self.store = await asyncio.to_thread(ArtistStore, path)

    async def _serve(self, space_id):
        artist, signal = self.artists[space_id], self.signals[space_id]
        while not self.stopping:
            signal.clear()
            try:
                await artist.tick()
                self.errors.pop(space_id, None)
            except Exception as exc:
                code = exc.code if isinstance(exc, StoreError) else "HOST_CYCLE_FAILED"
                self.errors[space_id] = code
                logger.warning("Autonomous cycle stopped: %s", code)
            try:
                await asyncio.wait_for(signal.wait(), timeout=self.config.tick_seconds)
            except TimeoutError:
                pass

    async def on_chat(self, identity, message_id, message):
        if self.stopping or not isinstance(message, dict):
            return False
        if any(message.get(flag) for flag in ("is_command", "is_notify")):
            return False
        text = message.get("processed_plain_text")
        text = text if isinstance(text, str) else ""
        if text.lstrip().startswith("/"):
            return False
        if not isinstance(message_id, str) or not message_id.strip() or len("chat:" + message_id) > 200:
            return False
        if message.get("message_id") not in (None, message_id):
            return False
        accepted = False
        for space_id, artist in self.artists.items():
            # Only the configured delegator's genuine Host messages enter this
            # scope. Other group members never inherit their spending authority.
            if identity == json.loads(artist.scope.snapshot()):
                imported = await self._ingest_media(space_id, artist.scope, message_id, message)
                if self.stopping:
                    return accepted
                if text.strip() or imported:
                    accepted |= await artist.on_chat(message_id=message_id, text=text[:4000] if text.strip()
                                                    else IMAGE_ONLY_TEXT)
                    self.signals[space_id].set()
        return accepted

    async def _ingest_media(self, space_id, scope, message_id, message):
        enabled, consent_ref = self.media_policies.get(space_id, (False, ""))
        if not enabled or not consent_ref.strip() or self.stopping:
            return False
        try:
            current = await self.db("get_space", space_id, scope)
            if not current["authorized"]:
                return False
            # Capture before decoding yields. The Store rechecks this epoch and
            # the separate media policy inside each import transaction.
            auth_version = current["auth_version"]
            images = await joined(asyncio.to_thread(current_images, message))
            imported = False
            for image in images:
                if self.stopping:
                    return imported
                await self.db("import_asset", space_id, scope,
                              event_id="chat:" + message_id, message_index=image.message_index,
                              data=image.data, mime=image.mime, roles=sorted(ASSET_ROLES), tags=[],
                              description=UNREVIEWED_DESCRIPTION, consent_ref=consent_ref,
                              expected_auth_version=auth_version)
                imported = True
            self.media_errors.pop(space_id, None)
            return imported
        except Exception as exc:  # noqa: BLE001 - isolate media errors from ordinary text ingress
            code = exc.code if isinstance(exc, StoreError) else "HOST_MEDIA_FAILED"
            if not isinstance(code, str) or re.fullmatch(r"[A-Z_]{1,64}", code) is None:
                code = "HOST_MEDIA_FAILED"
            self.media_errors[space_id] = code
            logger.warning("Host media input rejected: %s", code)
            return False

    async def close(self):
        self.stopping = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await joined(self._close_task)

    async def _close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
        if self.store is not None:
            await asyncio.to_thread(self.store.close)
            self.store = None
        if self.lease is not None:
            self.lease.close()
            self.lease = None
