"""Optional planner-owned notes, using public hooks only.

Only an authenticated after_response hook can write. The tool returns a receipt;
its caller-supplied identity/arguments can never create notes or authority.
No chat history is parsed or member inferred. New notes notify the existing
budgeted artist; planning, drawing and sharing remain separate decisions.
"""
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import asyncio
import hashlib
import logging
import secrets
import time

from maibot_sdk import HookHandler, Tool
from maibot_sdk.types import HookMode

from .group_thoughts import DATABASE_NAME, GroupThoughts
from .host_runtime import joined
from .policy import Scope


logger = logging.getLogger(__name__)
TOOL_NAME = "codex_image_remember_thought"
# Kept in sync with the notebook's finite visual vocabulary. Arbitrary model
# prose cannot become stored group chat, member attribution or executable text.
TAGS = ("rain", "night", "store", "light", "forest", "coast", "lake", "mountain",
        "street", "mist", "snow", "clear", "dawn", "sunset", "warm", "cool")
PARAMETERS = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "tags": {"type": "array", "items": {"type": "string", "enum": list(TAGS)},
                 "minItems": 1, "maxItems": 6, "uniqueItems": True,
                 "description": "自己想到的视觉元素；不填写聊天原文或成员身份。"},
        "notebook_context": {"type": "string", "minLength": 32, "maxLength": 64,
                             "description": "使用本轮工具定义中给出的唯一值。"},
    },
    "required": ["tags", "notebook_context"],
}
DESCRIPTION = (
    "可选：把你受当前群话题启发而产生的视觉构思记入灵感簿。"
    "不必回应每条消息，不要求成员发绘图命令，不归因给任何成员。"
    "这里只记念头，由后续自主规划决定是否采用；不会直接绘图或向群发送消息。"
)


def _tool(definition):
    return (type(definition) is dict and definition.get("type") == "function"
            and type(definition.get("function")) is dict
            and definition["function"].get("name") == TOOL_NAME)


def _args(raw):
    if type(raw) is not dict or set(raw) != {"tags", "notebook_context"}:
        return None
    token, tags = raw["notebook_context"], raw["tags"]
    if (type(token) is not str or len(token) != 48 or any(c not in "0123456789abcdef" for c in token)
            or type(tags) is not list or not 1 <= len(tags) <= 6
            or any(type(tag) is not str or tag not in TAGS for tag in tags)
            or len(set(tags)) != len(tags)):
        return None
    return token, tuple(sorted(tags))


@dataclass
class _Offer:
    token: str
    space_id: str
    scope: Scope
    group_id: str
    auth_version: int
    consent_ref: str
    deadline: float
    tags: tuple | None = None
    result: dict | None = None


class GroupThoughtRuntime:
    def __init__(self, plugin, *, clock=time.time, monotonic=time.monotonic):
        self.plugin = plugin
        self.runtime = plugin.artist_runtime
        self.config = plugin.config
        self.clock, self.monotonic = clock, monotonic
        self.offers = {}
        self.book = None
        self.stopping = False

    def stop(self):
        self.stopping = True
        self.offers.clear()

    def _prune(self):
        for session, offer in list(self.offers.items()):
            if self.monotonic() >= offer.deadline:
                self.offers.pop(session, None)

    def _configured(self, session):
        cfg, runtime = self.plugin.config, self.runtime
        if (self.stopping or runtime is None or runtime.stopping
                or self.plugin.artist_runtime is not runtime or cfg is not self.config
                or cfg.autonomy is not runtime.config
                or not cfg.plugin.enabled or not cfg.autonomy.enabled):
            return None
        matches = [item for item in cfg.autonomy.spaces if item.stream_id == session]
        if len(matches) != 1:
            return None
        item = matches[0]
        if (not item.group_id or not item.group_inspiration or not item.continuous
                or not item.consent_ref.strip()
                or f"{item.platform}:{item.user_id}" not in cfg.connection.allowed_users):
            return None
        if sum((x.platform, x.group_id) == (item.platform, item.group_id)
               for x in cfg.autonomy.spaces) != 1:
            return None
        verified = runtime.verified_notebook_groups.get(item.space_id)
        scope = Scope(item.platform, item.stream_id, item.user_id)
        if verified != (item.group_id, scope, item.consent_ref):
            return None
        return item, scope

    async def _allowed(self, offer):
        current = self._configured(offer.scope.stream_id)
        if current is None:
            return False
        item, scope = current
        if (item.space_id, scope, item.group_id, item.consent_ref) != (
                offer.space_id, offer.scope, offer.group_id, offer.consent_ref):
            return False
        row = await self.runtime.db("get_space", item.space_id, scope)
        return (row["authorized"] and row["auth_version"] == offer.auth_version
                and row["auth_version"] == self.runtime.grant_versions.get(item.space_id)
                and self._configured(scope.stream_id) is not None)

    async def before(self, **kwargs):
        # Every genuine new round supersedes the old offer, including rounds
        # whose selected tool list no longer contains this tool.
        async with self.plugin.lifecycle:
            self._prune()
            session = kwargs.get("session_id")
            if type(session) is str:
                self.offers.pop(session, None)
            return await self._before_locked(**kwargs)

    async def _before_locked(self, **kwargs):
        definitions = kwargs.get("tool_definitions")
        if type(definitions) is not list or len(definitions) > 256:
            return None
        matching = [item for item in definitions if _tool(item)]
        if not matching:
            return None  # Never reintroduce a Host-disabled or unselected tool.
        removed = [item for item in definitions if not _tool(item)]
        modified = {**kwargs, "tool_definitions": removed}
        session = kwargs.get("session_id")
        if (type(session) is not str or not session or type(kwargs.get("item_schema_version")) is not int
                or kwargs["item_schema_version"] != 1):
            return {"action": "continue", "modified_kwargs": modified}
        current = self._configured(session)
        if current is None or len(matching) != 1:
            return {"action": "continue", "modified_kwargs": modified}
        item, scope = current
        if len(self.offers) >= 32:
            return {"action": "continue", "modified_kwargs": modified}
        await self.runtime._verify_stream(item)
        row = await self.runtime.db("get_space", item.space_id, scope)
        offer = _Offer(secrets.token_hex(24), item.space_id, scope, item.group_id,
                       row["auth_version"], item.consent_ref, self.monotonic() + 120)
        if not await self._allowed(offer):
            return {"action": "continue", "modified_kwargs": modified}
        self.offers[session] = offer
        definition = deepcopy(matching[0])
        schema = deepcopy(PARAMETERS)
        schema["properties"]["notebook_context"]["enum"] = [offer.token]
        definition["function"]["parameters"] = schema
        definition["function"]["description"] = DESCRIPTION
        modified["tool_definitions"] = [definition if _tool(item) else item for item in definitions]
        return {"action": "continue", "modified_kwargs": modified}

    async def after(self, **kwargs):
        session, items = kwargs.get("session_id"), kwargs.get("output_items")
        if (type(session) is not str or type(kwargs.get("item_schema_version")) is not int
                or kwargs["item_schema_version"] != 1 or type(items) is not list or len(items) > 128):
            return None
        async with self.plugin.lifecycle:
            self._prune()
            offer = self.offers.get(session)
            if offer is None or not await self._allowed(offer):
                return None
            calls = [item.get("tool_call") for item in items
                     if type(item) is dict and item.get("item_type") == "FunctionCallItem"
                     and type(item.get("tool_call")) is dict
                     and item["tool_call"].get("func_name") == TOOL_NAME]
            if len(calls) != 1:
                return None
            parsed = _args(calls[0].get("args"))
            if parsed is None or parsed[0] != offer.token:
                return None
            if offer.tags is not None:
                return None  # Same-round retransmission or changed parameters cannot write twice.
            offer.tags = parsed[1]
            result = await joined(asyncio.to_thread(self._record, offer))
            # The chosen thought may have committed before a concurrent stop.
            # Keep the ledger, but never revive a cleared authorization cache.
            if self.offers.get(session) is offer and not self.stopping:
                offer.result = result
                if result.get("accepted") is True:
                    # Only notify the existing budgeted worker. The hook never
                    # invokes planning, rendering or sending directly.
                    self.runtime.signals[offer.space_id].set()
        return None  # Model output itself is not rewritten.

    async def sync(self, space_id):
        """Recover pending notes on ordinary chat/idle cycles, including restart.

        Source writes and ledger adoption need no distributed transaction: the
        immutable note ID makes this import replayable; only plan_work adopts it.
        """
        async with self.plugin.lifecycle:
            if self.stopping or self.runtime is None or self.runtime.stopping:
                return False
            artist = self.runtime.artists.get(space_id)
            if artist is None:
                return False
            return await joined(asyncio.to_thread(self._sync, space_id, artist.scope))

    def _sync(self, space_id, scope):
        with self.runtime.store.lock:
            row = self.runtime.store.get_space(space_id, scope)
            version = self.runtime.grant_versions.get(space_id)
            if not row["authorized"] or row["auth_version"] != version:
                return False
            configured = self._configured(scope.stream_id)
            if configured is None or configured[0].space_id != space_id or configured[1] != scope:
                return self.runtime.store.sync_group_thoughts(
                    space_id, scope, group_id="", thoughts=[], enabled=False,
                    expected_auth_version=version,
                )
            item, _ = configured
            if self.book is None:
                path = Path(self.plugin.ctx.paths.data_dir) / DATABASE_NAME
                if not path.exists():
                    return False
                self.book = GroupThoughts(path.parent, clock=self.clock)
            topics = self.book.topics(space_id=space_id, platform=scope.platform,
                                      stream_id=scope.stream_id, group_id=item.group_id,
                                      owner_id=scope.user_id)
            return self.runtime.store.sync_group_thoughts(
                space_id, scope, group_id=item.group_id, thoughts=topics, enabled=True,
                expected_auth_version=version,
            )

    def _record(self, offer):
        # Serialize with grant/revoke and finish the worker before owner cleanup.
        # The write linearizes at this final check; later revocation keeps history.
        with self.runtime.store.lock:
            current = self._configured(offer.scope.stream_id)
            row = self.runtime.store.get_space(offer.space_id, offer.scope)
            if (current is None or not row["authorized"] or row["auth_version"] != offer.auth_version
                    or self.monotonic() >= offer.deadline):
                return {"accepted": False, "reason": "authorization_changed"}
            if self.book is None:
                self.book = GroupThoughts(Path(self.plugin.ctx.paths.data_dir), clock=self.clock)
            return self.book.record(
                space_id=offer.space_id, platform=offer.scope.platform, stream_id=offer.scope.stream_id,
                group_id=offer.group_id, owner_id=offer.scope.user_id, tags=list(offer.tags),
                request_id=hashlib.sha256(offer.token.encode("ascii")).hexdigest(),
            )

    async def receipt(self, tags, notebook_context, **kwargs):
        # Tool identity fields are not an authority source. They only narrow the
        # already recorded hook result; this function has no write path at all.
        parsed = _args({"tags": tags, "notebook_context": notebook_context})
        session = kwargs.get("stream_id")
        if (parsed is None or type(session) is not str
                or kwargs.get("chat_id", session) != session):
            return {"success": False, "error_message": "没有匹配的当前群规划回执。"}
        async with self.plugin.lifecycle:
            self._prune()
            offer = self.offers.get(session)
            if (offer is None or (offer.token, offer.tags) != parsed or offer.result is None
                    or not await self._allowed(offer)):
                return {"success": False, "error_message": "没有匹配的当前群规划回执。"}
            recorded = offer.result.get("accepted") is True
            return {"success": True, "content": "念头已记入灵感簿；是否采用由自主规划决定，进展以工作区为准。" if recorded
                    else "本轮未新增念头；可能已有相同主题或处于记录间隔内。",
                    "structured_content": {"recorded": recorded}}

    async def close(self):
        self.stop()
        if self.book is not None:
            await joined(asyncio.to_thread(self.book.close))
            self.book = None


@HookHandler("maisaka.planner.before_request", name="codex_image_thought_offer",
             mode=HookMode.BLOCKING, timeout_ms=5000,
             description="仅向授权群规划提供可选创作念头工具")
async def group_thought_before_request(plugin, **kwargs):
    runtime = plugin.group_thought_runtime
    if runtime is None:
        definitions = kwargs.get("tool_definitions")
        if type(definitions) is list and any(_tool(item) for item in definitions):
            return {"action": "continue", "modified_kwargs": {
                **kwargs, "tool_definitions": [item for item in definitions if not _tool(item)]}}
        return None
    try:
        return await runtime.before(**kwargs)
    except Exception:
        logger.warning("Group thought offer unavailable")
        definitions = kwargs.get("tool_definitions")
        if type(definitions) is list:
            return {"action": "continue", "modified_kwargs": {
                **kwargs, "tool_definitions": [item for item in definitions if not _tool(item)]}}
        return None


@HookHandler("maisaka.planner.after_response", name="codex_image_thought_record",
             mode=HookMode.BLOCKING, timeout_ms=5000,
             description="记录授权群规划自主选择的视觉主题，不绘图不发送")
async def group_thought_after_response(plugin, **kwargs):
    runtime = plugin.group_thought_runtime
    if runtime is not None:
        try:
            await runtime.after(**kwargs)
        except Exception:
            logger.warning("Group thought record unavailable")
    return None


@Tool(TOOL_NAME, description=DESCRIPTION, parameters=PARAMETERS, chat_scope="group")
async def group_thought_receipt(plugin, tags=None, notebook_context=None, **kwargs):
    runtime = plugin.group_thought_runtime
    if runtime is None:
        return {"success": False, "error_message": "当前未启用群创作念头。"}
    return await runtime.receipt(tags, notebook_context, **kwargs)
