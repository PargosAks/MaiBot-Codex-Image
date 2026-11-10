"""使用真实 SDK 和公开 Host 方法，外部效果由替身记录。"""

from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import ModuleType, SimpleNamespace
import ast
import asyncio
import base64
import contextlib
import hashlib
import inspect
import json
import logging
import sys
import time
import traceback
import typing

from maibot_sdk.context import PluginContext, PluginPaths
from pydantic import BaseModel, Field
import pytest

from image_plugin_under_test import CodexImagePlugin
from image_plugin_under_test.creation.artist import RenderResult, render_digest
from image_plugin_under_test.domain.policy import Scope
from image_plugin_under_test.domain.store import ArtistStore
from image_plugin_under_test.host import host_runtime
from protocol_fixture import PNG
from .artist_fakes import BRIEF, Clock
from .host_source import host_file, load_nodes
from .settings import PLUGIN as ROOT, SECRET


PLUGIN_ID = "pargosaks.maibot-codex-image"
HOOK = "chat.receive.after_process"
OWNER, STREAM, PLATFORM = "synthetic-owner", "existing-host-stream", "synthetic"


@pytest.fixture
def public_host(monkeypatch, tmp_path):
    return build_public_host(monkeypatch, tmp_path, host_file)


def build_public_host(monkeypatch, tmp_path, read_source):
    # 只执行选定的公开定义；启动、真实配置和外部 RPC 都不会被加载。
    def public_source(relative: str) -> str:
        return read_source("plugin_runtime/" + relative)

    module = ModuleType("_isolated_codex_host")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    ns = module.__dict__
    ns.update(vars(typing))
    ns.update(__name__=module.__name__, asyncio=asyncio, contextlib=contextlib, inspect=inspect,
              dataclass=dataclass, field=field, deepcopy=deepcopy, Enum=Enum, BaseModel=BaseModel,
              Field=Field, time=time, datetime=datetime, traceback=traceback, base64=base64, hashlib=hashlib,
              ABC=ABC, abstractmethod=abstractmethod, Seg=typing.Any, StandardMessageComponents=typing.Any,
              ForwardComponent=typing.Any,
              logger=logging.getLogger("isolated-host-test"), PROTOCOL_VERSION="1.0.0",
              PluginMeta=typing.Any, PluginPaths=PluginPaths, _ContextAwarePlugin=typing.Any,
              _PLUGIN_ALLOWED_RAW_HOST_METHODS=frozenset({"cap.call", "host.route_message", "host.update_message_gateway_state"}),
              _PROJECT_ROOT=tmp_path, CapabilityImpl=typing.Callable,
              SessionMessage=typing.Any, MessageInfo=typing.Any, MessageSequence=typing.Any,
              MessageDict=dict, MessageInfoDict=dict, UserInfoDict=dict, GroupInfoDict=dict,
              global_config=SimpleNamespace(voice=SimpleNamespace(enable_asr=False),
                                            plugin_runtime=SimpleNamespace(hook_blocking_timeout_sec=1)))
    load_nodes(ns, public_source("protocol/errors.py"), {"ErrorCode", "RPCError"})
    load_nodes(ns, public_source("protocol/envelope.py"),
               {"MessageType", "Envelope", "InvokePayload", "CapabilityRequestPayload", "CapabilityResponsePayload"})
    ns["_ALWAYS_ALLOWED_CAPABILITIES"] = frozenset({"api.replace_dynamic"})
    load_nodes(ns, public_source("host/authorization.py"), {"CapabilityPermissionToken", "AuthorizationManager"})
    load_nodes(ns, public_source("host/capability_service.py"), {"CapabilityService"})
    load_nodes(ns, public_source("host/hook_spec_registry.py"), {"HookSpec", "HookSpecRegistry"})
    circuit = SimpleNamespace(try_acquire=lambda *args: SimpleNamespace(allowed=True),
                              record_success=lambda *args: None, record_failure=lambda *args: None)
    ns.update(is_shutdown_requested=lambda: False, get_plugin_circuit_breaker=lambda: circuit)
    load_nodes(ns, public_source("host/hook_dispatcher.py"),
               {"HookHandlerExecutionResult", "HookDispatchResult", "_HookInvocationTarget", "HookDispatcher"})
    runner_methods = {"_inject_context", "_create_plugin_context", "_resolve_component_handler_name",
                      "_resolve_component_handler", "_invoke_plugin_callable", "_handle_hook_invoke"}
    load_nodes(ns, public_source("runner/runner_main.py"), {"PluginRunner"}, {"PluginRunner": runner_methods})
    load_nodes(ns, read_source("common/data_models/message_component_data_model.py"),
               {"BaseMessageComponentModel", "ByteComponent", "TextComponent", "ImageComponent", "EmojiComponent"})
    load_nodes(ns, public_source("host/message_utils.py"), {"PluginMessageUtils"},
               {"PluginMessageUtils": {"_session_message_to_dict", "_message_info_to_dict", "_message_sequence_to_dict",
                                       "_component_to_dict", "_binary_component_to_base64"}})
    schema_tree = ast.parse(public_source("hook_schema_utils.py"))
    functions = [node.name for node in schema_tree.body if isinstance(node, ast.FunctionDef)]
    load_nodes(ns, public_source("hook_schema_utils.py"), set(functions))
    bot = read_source("chat/message_receive/bot.py")
    load_nodes(ns, bot, {"register_chat_hook_specs", "ChatBot"},
               {"ChatBot": {"receive_message", "_invoke_message_hook"}})
    return module


class Renderer:
    simulation, supports_edit = True, True

    def __init__(self):
        self.calls, self.jobs = [], {}

    async def submit(self, *, scope, request_id, prompt, source):
        self.calls.append({"request_id": request_id, "prompt": prompt, "source": source, "scope": scope})
        params = {"prompt": prompt}
        if source:
            assert source["data"] == PNG and source["mime"] == "image/png"
            params.update(source_job_id=source["job_id"], source_sha256=source["sha256"])
        result = RenderResult(request_id, render_digest("edit" if source else "submit", params),
                              "synthetic-job-" + str(len(self.calls)), "completed", PNG,
                              sha256=hashlib.sha256(PNG).hexdigest())
        self.jobs[request_id] = result
        return result

    async def lookup(self, *, scope, request_id):
        return self.jobs.get(request_id)

    async def status(self, *, scope, job_id):
        return next(result for result in self.jobs.values() if result.job_id == job_id)


class Harness:
    def __init__(self, host, path):
        self.host, self.path, self.plugin = host, path, CodexImagePlugin()
        self.clock, self.renderer, self.gate = Clock(), Renderer(), asyncio.Event()
        self.calls, self.sends, self.reviews, self.received, self.hook_calls = [], [], [], [], []
        self.revise, self.share, self.lose_send = True, False, False
        self.creative_choice = None
        self.send_override = None
        self.stream_override = None
        self.data_paths = PluginPaths(data_dir=path / "data", runtime_dir=path / "runtime")
        self.authorization = host.AuthorizationManager()
        manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
        self.authorization.register_plugin(PLUGIN_ID, manifest["capabilities"])
        self.capabilities = host.CapabilityService(self.authorization)
        for name in ("llm.generate", "send.image", "chat.get_stream_by_user_id", "chat.get_stream_by_group_id"):
            self.capabilities.register_capability(name, self.capability)
        self.runner = host.PluginRunner()
        self.runner._rpc_client = SimpleNamespace(send_request=self.rpc)
        self.runner._warn_legacy_plugin_data_dir = lambda *args: None
        self.runner._ensure_context_llm_helpers = lambda *args: None
        self.runner._start_inflight_rpc = lambda *args: None
        self.runner._finish_inflight_rpc = lambda *args: None
        host.build_plugin_paths = lambda *args: self.data_paths
        self.runner._inject_context(PLUGIN_ID, self.plugin)
        assert isinstance(self.plugin.ctx, PluginContext)
        components = self.plugin.get_components()
        self.component = next(item for item in components if item["name"] == "codex_image_artist_chat")
        assert self.component["type"] == "HOOK_HANDLER"
        metadata = self.component["metadata"]
        assert metadata["hook"] == HOOK and metadata["mode"] == "observe"
        meta = SimpleNamespace(instance=self.plugin, config_error="",
                               component_handlers={self.component["name"]: metadata["handler_name"]})
        self.runner._loader = SimpleNamespace(get_plugin=lambda identity: meta if identity == PLUGIN_ID else None)
        entry = SimpleNamespace(name=self.component["name"], plugin_id=PLUGIN_ID,
                                full_name=PLUGIN_ID + "." + self.component["name"], is_observe=True,
                                mode=metadata["mode"], order=metadata["order"], timeout_ms=metadata["timeout_ms"],
                                error_policy=metadata["error_policy"])
        registry = SimpleNamespace(get_hook_handlers=lambda hook: [entry] if hook == HOOK else [])
        supervisor = SimpleNamespace(group_name="third_party", component_registry=registry, invoke_plugin=self.invoke)
        self.dispatcher = host.HookDispatcher(supervisors_provider=lambda: [supervisor])
        host.register_chat_hook_specs(self.dispatcher)
        self.bot = host.ChatBot()
        self.bot._get_runtime_manager = lambda: SimpleNamespace(invoke_hook=self.dispatcher.invoke_hook)
        self.bot.handle_notice_message = self.noop
        self.bot._process_adapter_lifecycle_command = self.false
        self.bot._process_clear_context_command = self.false
        self.bot._process_commands = self.commands
        self.bot._handle_command_processing_result = self.true
        self.bot._is_command_candidate = lambda message: message.processed_plain_text.lstrip().startswith("/")
        self.bot.heartflow_message_receiver = SimpleNamespace(process_message=self.received_message)
        host.RouteKeyFactory = SimpleNamespace(from_session_message=lambda message: SimpleNamespace(account_id="", scope=""))
        host.SessionUtils = SimpleNamespace(calculate_session_id=lambda *args, **kwargs: STREAM)
        host.process_received_images_in_message = lambda *args: SimpleNamespace(compressed_count=0, discarded_count=0)
        host.MessageUtils = SimpleNamespace(check_ban_words=lambda text: (False, None), check_ban_regex=lambda text: (False, None))
        host.chat_manager = SimpleNamespace(register_message=lambda message: None, get_or_create_session=self.noop)
        host.serialize_session_message = host.PluginMessageUtils._session_message_to_dict

    async def noop(self, *args, **kwargs):
        return None

    async def false(self, *args):
        return False

    async def true(self, *args):
        return True

    async def commands(self, message):
        return message.processed_plain_text.lstrip().startswith("/"), None, False

    async def received_message(self, message):
        self.received.append(message)

    async def rpc(self, *, method, plugin_id, payload, timeout_ms):
        assert method == "cap.call" and plugin_id == PLUGIN_ID
        envelope = self.host.Envelope(request_id=len(self.calls) + 1, message_type="request",
                                      method=method, plugin_id=plugin_id, payload=payload)
        return await self.capabilities.handle_capability_request(envelope)

    async def invoke(self, method, plugin_id, component_name, args, **kwargs):
        assert method == "plugin.invoke_hook"
        assert args["hook_name"] == HOOK and "message" in args
        self.hook_calls.append(deepcopy(args))
        envelope = self.host.Envelope(request_id=1, message_type="request", method=method, plugin_id=plugin_id,
                                      payload={"component_name": component_name, "args": args})
        response = await self.runner._handle_hook_invoke(envelope)
        assert response.error is None and response.payload["success"] is True
        assert response.payload["action"] == "continue"
        return response

    async def capability(self, plugin_id, name, args):
        self.calls.append((name, deepcopy(args)))
        if name.startswith("chat.get_stream_by_"):
            stream = {"platform": PLATFORM, "stream_id": STREAM, "session_id": STREAM,
                      "user_id": OWNER, "group_id": args.get("group_id", ""),
                      "is_group_session": bool(args.get("group_id"))}
            return self.stream_override or {"success": True, "stream": stream}
        if name == "send.image":
            assert args["stream_id"] == STREAM and args["return_details"] is True
            assert base64.b64decode(args["image_base64"], validate=True) == PNG
            self.sends.append(deepcopy(args))
            if self.lose_send:
                raise self.host.RPCError(self.host.ErrorCode.E_TIMEOUT, "synthetic lost send receipt")
            if self.send_override is not None:
                return deepcopy(self.send_override)
            return {"success": True, "sent": True, "message_id": "synthetic-sent"}
        assert name == "llm.generate"
        messages = args["prompt"]
        assert isinstance(messages, list) and messages[0]["role"] == "system"
        content = messages[1]["content"]
        context = json.loads(content[0]["text"])
        ideas = [{"summary": event["text"], "source_ids": [event["event_id"]]}
                 for event in context.get("events", []) if event["kind"] == "chat"][:8]
        if len(content) > 1:
            assert args["task_name"] == "vlm"
            images = [item for item in content if item["type"] == "image_url"]
            assert len(images) == len(context["review_inputs"])
            for image in images:
                url = image["image_url"]["url"]
                assert url.startswith("data:image/png;base64,")
                assert base64.b64decode(url.split(",", 1)[1], validate=True) == PNG
            self.reviews.append(deepcopy(args))
            answer = {"decision": "accept", "reason": "脚本接受合成图", "strengths": ["合成样例"], "issues": []}
            if self.revise:
                self.revise = False
                answer.update(decision="revise", issues=["脚本修订"], edit_prompt="降低背景亮度", preserve="保留小猫姿态")
            answer["comparison"] = {
                "improvements": [], "regressions": list(answer["issues"]), "preserved": ["小猫姿态"],
                "evidence_version_ids": [item["version_id"] for item in context["review_inputs"]],
            }
        elif "不得规划或覆盖" in messages[0]["content"]:
            assert args["task_name"] == "utils"
            answer = {"ideas": ideas}
        else:
            assert args["task_name"] == "utils"
            active = next((work for work in context["works"] if work["status"] in {"planned", "deferred"}), None)
            if active:
                decision = {"action": "continue", "reason": "脚本继续", "work_id": active["work_id"]}
            elif not context["works"]:
                decision = {"action": "create", "reason": "脚本计划", "brief": dict(BRIEF), "inspiration_keys": []}
                ideas = ideas or [{"summary": "自主合成主题", "source_ids": []}]
            elif self.share:
                decision = {"action": "share", "reason": "脚本分享", "work_id": context["works"][0]["work_id"]}
            else:
                decision = {"action": "wait", "reason": "先收藏"}
            answer = {"ideas": ideas, "decision": decision}
            if decision["action"] == "create" and self.creative_choice is not None:
                answer["decision"]["creative"] = deepcopy(self.creative_choice)
        return {"success": True, "response": json.dumps(answer, ensure_ascii=False), "model": "synthetic"}

    def config(self, *, enabled=True, group=False, media=False, media_consent="synthetic-media-consent"):
        limits = {"think": 100, "render": 10, "share": 10, "max_inflight": 10, "max_revisions": 2}
        return {"plugin": self.plugin.get_default_config()["plugin"],
                "connection": {"url": "http://127.0.0.1:9/mcp", "secret": SECRET,
                               "allowed_users": [PLATFORM + ":" + OWNER]},
                "autonomy": {"enabled": enabled, "tick_seconds": .1, "idle_seconds": 300,
                             "global_limits": limits, "spaces": [{"space_id": "studio", "platform": PLATFORM,
                             "user_id": OWNER, "stream_id": STREAM, "group_id": "synthetic-group" if group else "",
                             **({"group_share_mode": "original_group"} if group else {}),
                             "continuous": True, "consent_ref": "synthetic-explicit-consent", "limits": limits,
                             "reference_inputs": media, "media_consent_ref": media_consent if media else ""}]}}

    async def load(self, **kwargs):
        self.plugin.set_plugin_config(self.config(**kwargs))
        await self.plugin.on_load()
        assert not self.plugin.startup_error, self.plugin.startup_error

    async def message(self, text="雨夜暖光", *, owner=OWNER, command=False, notify=False,
                      components=None, message_id=None):
        user = SimpleNamespace(user_id=owner, user_nickname="synthetic", user_cardname=None)
        group = self.plugin.config.autonomy.spaces[0].group_id
        message = SimpleNamespace(message_id=message_id or "msg-" + str(len(self.received)) + "-" + owner, platform=PLATFORM,
            session_id=STREAM, message_info=SimpleNamespace(user_info=user, group_info=(
                SimpleNamespace(group_id=group, group_name="synthetic-group") if group else None), additional_config={}),
            raw_message=SimpleNamespace(components=components or []), processed_plain_text=text, timestamp=self.clock(),
            received_at=self.clock(), account_id="", scope="", reply_to=None,
            is_mentioned=False, is_at=False, is_emoji=False, is_picture=False, is_command=command, is_notify=notify)
        message.process = self.noop
        snapshot = self.host.serialize_session_message(message)

        def unchanged(raw):
            assert raw == snapshot  # This plugin's observation cannot mutate ingress.
            return message

        self.host.deserialize_session_message = unchanged
        await self.bot.receive_message(message)
        if self.dispatcher._background_tasks:
            await asyncio.gather(*tuple(self.dispatcher._background_tasks))
        return message

    @property
    def runtime(self):
        return self.plugin.artist_runtime

    def state(self):
        works = self.runtime.store.list_artist_works("studio", Scope(PLATFORM, STREAM, OWNER))
        return works[0]["status"] if works else None

    async def until(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(.01)

    async def advance(self, state=None):
        self.clock.advance()
        self.runtime.signals["studio"].set()
        if state:
            await self.until(lambda: self.state() == state)


@pytest.fixture
async def host_chain(public_host, tmp_path, monkeypatch):
    value = Harness(public_host, tmp_path)
    store_class, serve = ArtistStore, host_runtime.HostRuntime._serve
    monkeypatch.setattr(host_runtime, "ArtistStore", lambda path: store_class(path, clock=value.clock))

    # 只有测试显式放行时，真实调度循环才会开始执行。
    async def gated(runtime, space):
        await value.gate.wait()
        await serve(runtime, space)

    monkeypatch.setattr(host_runtime.HostRuntime, "_serve", gated)
    monkeypatch.setattr(value.plugin, "_artist_renderer", lambda *args: value.renderer)
    try:
        yield value
    finally:
        await value.plugin.on_unload()
        await value.dispatcher.stop()
