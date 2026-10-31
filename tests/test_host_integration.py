"""Isolated SDK + fixed public Host methods, never a running MaiBot instance.

The genuine receive_message/HookDispatcher/Runner/capability implementations are
AST-loaded without module bootstrap (which imports real configuration). Peripheral
message processing, session lookup, logging, RPC transport and capability effects
are synthetic. This proves the fixed ingress method's hookup, not a live adapter,
deployed Host, final Maisaka admission, model quality or platform delivery.
"""

from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace
import ast
import asyncio
import base64
import contextlib
import hashlib
import inspect
import json
import logging
import os
import subprocess
import sys
import time
import traceback
import typing

from maibot_sdk.context import PluginContext, PluginPaths
from pydantic import BaseModel, Field
import pytest

from image_plugin_under_test import CodexImagePlugin
from image_plugin_under_test.artist import RenderResult, render_digest
from image_plugin_under_test import host_runtime
from image_plugin_under_test.policy import Scope
from image_plugin_under_test.store import ArtistStore
from conftest import SECRET
from protocol_fixture import PNG
from test_artist_flow import BRIEF, Clock

ROOT = Path(__file__).resolve().parents[1]
HOST_REV = "062d45cf8bd4272ac57d7ec03fcf405e5a458211"
PLUGIN_ID = "pargosaks.maibot-codex-image"
HOOK = "chat.receive.after_process"
OWNER, STREAM, PLATFORM = "synthetic-owner", "existing-host-stream", "synthetic"
pytestmark = pytest.mark.skipif(
    not (os.environ.get("CODEX_IMAGE_TEST_HOST_SOURCE") or os.environ.get("CODEX_IMAGE_TEST_HOST_REPO")),
    reason="Explicit public Host source is required",
)


def host_file(relative):
    explicit = os.environ.get("CODEX_IMAGE_TEST_HOST_SOURCE")
    if explicit:
        return (Path(explicit) / relative).read_text(encoding="utf-8")
    checkout = os.environ.get("CODEX_IMAGE_TEST_HOST_REPO")
    if not checkout:
        pytest.skip("Explicit public Host src or Git object repository is required")
    return subprocess.check_output(["git", "-C", checkout, "show", f"{HOST_REV}:src/{relative}"],
                                   stderr=subprocess.PIPE).decode("utf-8")


def public_source(relative):
    return host_file("plugin_runtime/" + relative)


def load_nodes(namespace, text, names, methods=None):
    nodes = []
    for node in ast.parse(text).body:
        name = getattr(node, "name", None)
        if name in names:
            if methods and name in methods:
                node.body = [item for item in node.body if getattr(item, "name", None) in methods[name]]
            nodes.append(node)
    assert {node.name for node in nodes} == set(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<fixed-public-host>", "exec"), namespace)


@pytest.fixture
def public_host(monkeypatch, tmp_path):
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
    load_nodes(ns, host_file("common/data_models/message_component_data_model.py"),
               {"BaseMessageComponentModel", "ByteComponent", "TextComponent", "ImageComponent", "EmojiComponent"})
    load_nodes(ns, public_source("host/message_utils.py"), {"PluginMessageUtils"},
               {"PluginMessageUtils": {"_session_message_to_dict", "_message_info_to_dict", "_message_sequence_to_dict",
                                       "_component_to_dict", "_binary_component_to_base64"}})
    schema_tree = ast.parse(public_source("hook_schema_utils.py"))
    functions = [node.name for node in schema_tree.body if isinstance(node, ast.FunctionDef)]
    load_nodes(ns, public_source("hook_schema_utils.py"), set(functions))
    bot = host_file("chat/message_receive/bot.py")
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


async def test_default_off_registered_hook_is_inert_through_real_ingress(host_chain):
    h = host_chain
    await h.load(enabled=False)
    await h.message()
    assert h.runtime.store is None and not h.runtime.tasks
    assert not h.calls and not h.renderer.calls and not h.sends
    assert not (h.data_paths.data_dir / "artist.sqlite3").exists()
    assert len(h.received) == 1
    assert len(h.hook_calls) == 1 and h.hook_calls[0]["message"]["processed_plain_text"] == "雨夜暖光"


async def test_real_sdk_normalizes_stream_but_raw_capability_preserves_receipt(host_chain):
    h = host_chain
    stream = await h.plugin.ctx.chat.get_stream_by_user_id(OWNER, platform=PLATFORM)
    assert stream["stream_id"] == STREAM and "stream" not in stream and "success" not in stream
    raw = await h.plugin.ctx.call_host_method("cap.call", payload={
        "capability": "chat.get_stream_by_user_id", "args": {"user_id": OWNER, "platform": PLATFORM}})
    assert raw == {"success": True, "stream": stream}
    assert not h.renderer.calls and not h.sends


@pytest.mark.parametrize("valid_stream", [False, True])
async def test_failed_host_lookup_receipt_never_grants_even_if_stream_present(host_chain, valid_stream):
    h = host_chain
    h.stream_override = {"success": False, "stream": ({"platform": PLATFORM, "stream_id": STREAM,
        "session_id": STREAM, "user_id": OWNER, "group_id": "", "is_group_session": False} if valid_stream else None)}
    h.plugin.set_plugin_config(h.config())
    await h.plugin.on_load()
    assert h.plugin.startup_error == "Configured Host stream is unavailable"
    assert h.runtime is None and not h.renderer.calls and not h.sends


@pytest.mark.parametrize("group", [False, True])
async def test_actual_ingress_sdk_review_edit_and_send(host_chain, group):
    h = host_chain
    await h.load(group=group)
    await h.message()
    h.gate.set()
    await h.until(lambda: h.state() == "planned")
    await h.advance("review_pending")
    await h.advance("planned")
    await h.advance("review_pending")
    await h.advance("finished")
    assert len(h.renderer.calls) == len(h.reviews) == 2 and not h.sends
    assert h.renderer.calls[1]["source"]["data"] == PNG
    assert "保留小猫姿态" in h.renderer.calls[1]["prompt"]
    h.share = True
    await h.advance()
    await h.until(lambda: len(h.sends) == 1)
    await h.advance()
    await asyncio.sleep(.12)
    assert len(h.sends) == 1 and len(h.received) == 1
    assert all(name != "chat.open_session" for name, _ in h.calls)


async def test_idle_timer_uses_sdk_without_chat(host_chain):
    h = host_chain
    h.revise = False
    await h.load()
    h.gate.set()
    await h.until(lambda: h.state() == "planned")
    await h.advance("review_pending")
    await h.advance("finished")
    assert not h.received and len(h.renderer.calls) == 1 and len(h.reviews) == 1 and not h.sends


async def test_host_configuration_freezes_profile_for_drawing_and_review(host_chain):
    from test_profiles_flow import CHOICE, PROFILES, SERIES

    h = host_chain
    original_config = h.config

    def config(**kwargs):
        result = original_config(**kwargs)
        result["autonomy"]["spaces"][0].update(profiles=deepcopy(PROFILES), series=deepcopy(SERIES))
        return result

    h.config = config
    h.creative_choice, h.revise = CHOICE, False
    await h.load()
    h.gate.set()
    await h.until(lambda: h.state() == "planned")
    await h.advance("review_pending")
    await h.advance("finished")
    assert "warm amber" in h.renderer.calls[0]["prompt"]
    context = json.loads(h.reviews[0]["prompt"][1]["content"][0]["text"])
    assert context["current_work"]["brief"]["creative"]["profile"] == PROFILES[0]["data"]
    assert not h.sends


@pytest.mark.parametrize("kwargs", [{"owner": "other-owner"}, {"text": " /绘图 命令"},
                                    {"command": True}, {"notify": True}, {"text": "  "}])
async def test_ingress_rejects_nonowner_commands_notify_and_blank(host_chain, kwargs):
    h = host_chain
    await h.load(group=True)
    await h.message(**kwargs)
    scope = Scope(PLATFORM, STREAM, OWNER)
    assert not h.runtime.store.db.execute("SELECT 1 FROM artist_wakes").fetchone()
    assert not h.runtime.store.audit_operations("studio", scope) and not h.renderer.calls


@pytest.mark.parametrize("receipt", ["lost", "string", "integer", "missing"])
async def test_reload_unknown_host_send_is_never_replayed(host_chain, receipt):
    h = host_chain
    h.revise = False
    await h.load()
    h.gate.set()
    await h.until(lambda: h.state() == "planned")
    await h.advance("review_pending")
    await h.advance("finished")
    h.share = True
    h.lose_send = receipt == "lost"
    if receipt != "lost":
        h.send_override = {"success": True, "message_id": "synthetic-unconfirmed"}
        if receipt != "missing":
            h.send_override["sent"] = "false" if receipt == "string" else 1
    await h.advance()
    await h.until(lambda: len(h.sends) == 1)
    scope = Scope(PLATFORM, STREAM, OWNER)
    await h.until(lambda: any(op["action"] == "share" and op["state"] in {"unknown", "succeeded", "failed"}
                              for op in h.runtime.store.audit_operations("studio", scope)))
    operation = next(op for op in h.runtime.store.audit_operations("studio", scope) if op["action"] == "share")
    assert operation["state"] == "unknown", "An ambiguous raw receipt cannot be coerced to success or known failure"
    assert h.plugin.journal.db.execute("SELECT state FROM delivery").fetchone()[0] == "send_unknown"
    # Complete this wake before reload. Otherwise recovery may legitimately reuse
    # its cached plan, and demanding a fresh model call would test the wrong thing.
    await h.until(lambda: h.runtime.store.db.execute(
        "SELECT state FROM artist_claims ORDER BY rowid DESC LIMIT 1").fetchone()[0] != "active")
    previous, tasks = h.runtime, tuple(h.runtime.tasks.values())
    plans = sum(name == "llm.generate" for name, _ in h.calls)
    await h.plugin.on_config_update("plugin", {}, "synthetic-reload")
    assert h.runtime is not previous and previous.store is None and all(task.done() for task in tasks)
    await h.advance()
    await h.until(lambda: sum(name == "llm.generate" for name, _ in h.calls) > plans)
    await h.until(lambda: h.runtime.store.db.execute(
        "SELECT state FROM artist_claims ORDER BY rowid DESC LIMIT 1").fetchone()[0] != "active")
    assert len(h.sends) == len(h.renderer.calls) == 1
    assert h.runtime.store.budget_usage("studio", scope)["share"]["held"] == 1


async def test_revoke_configuration_stops_sdk_models_and_sends(host_chain):
    h = host_chain
    await h.load()
    h.gate.set()
    await h.until(lambda: h.state() == "planned")
    previous = h.runtime
    h.plugin.set_plugin_config(h.config(enabled=False))
    await h.plugin.on_config_update("plugin", {}, "synthetic-disable")
    count = len(h.calls)
    await h.message()
    await asyncio.sleep(.12)
    assert previous.store is None and not h.runtime.tasks
    assert len(h.calls) == count and not h.renderer.calls and not h.sends


@pytest.mark.parametrize("group", [False, True])
async def test_current_images_through_fixed_public_serializer_and_hook(host_chain, group):
    from test_host_media import image_bytes

    h = host_chain
    await h.load(group=group, media=True)
    data = image_bytes()
    parts = [h.host.TextComponent("context"), h.host.ImageComponent(binary_hash="", binary_data=data),
             h.host.EmojiComponent(binary_hash="", binary_data=data)]
    await h.message(text="", components=parts, message_id="synthetic-media-event")
    # A replay traverses the genuine dispatcher/Runner/SDK hook again.
    await h.message(text="", components=parts, message_id="synthetic-media-event")
    scope = Scope(PLATFORM, STREAM, OWNER)
    catalog = h.runtime.store.list_assets("studio", scope)
    assert len(catalog) == 1
    asset = h.runtime.store.get_asset("studio", scope, catalog[0]["asset_id"])
    assert asset["sha256"] == hashlib.sha256(data).hexdigest() and asset["mime"] == "image/png"
    assert set(asset["roles"]) == {"character", "style", "composition", "palette"}
    from image_plugin_under_test.host_media import IMAGE_ONLY_TEXT, UNREVIEWED_DESCRIPTION

    assert asset["description"].endswith(UNREVIEWED_DESCRIPTION)
    wakes = h.runtime.store.db.execute("SELECT event_id,text FROM artist_wakes").fetchall()
    assert [tuple(row) for row in wakes] == [("chat:synthetic-media-event", IMAGE_ONLY_TEXT)]
    serialized = h.hook_calls[0]["message"]["raw_message"]
    assert base64.b64decode(serialized[1]["binary_data_base64"], validate=True) == data
    assert serialized[2]["type"] == "emoji"
    assert not h.runtime.media_errors and not h.renderer.calls and not h.sends
    assert all(name.startswith("chat.get_stream_by_") for name, _ in h.calls)


@pytest.mark.parametrize("media,kwargs", [
    (False, {}), (True, {"owner": "other-owner"}), (True, {"command": True}),
    (True, {"notify": True}), (True, {"text": " /command"}),
])
async def test_fixed_public_ingress_rejects_unauthorized_image_inputs(host_chain, media, kwargs):
    from test_host_media import image_bytes

    h = host_chain
    await h.load(media=media, group=True)
    part = h.host.ImageComponent(binary_hash="", binary_data=image_bytes())
    await h.message(**{"text": "", **kwargs}, components=[part])
    assert not h.runtime.store.list_assets("studio", Scope(PLATFORM, STREAM, OWNER))
    assert not h.runtime.store.db.execute("SELECT 1 FROM artist_wakes").fetchone()
    assert not h.renderer.calls and not h.sends


async def test_media_optin_without_separate_consent_fails_at_host_startup(host_chain):
    h = host_chain
    h.plugin.set_plugin_config(h.config(media=True, media_consent=""))
    await h.plugin.on_load()
    assert "Separate media consent" in h.plugin.startup_error
    assert h.runtime is None and not h.renderer.calls and not h.sends


async def test_fixed_public_ingress_bad_image_keeps_text_and_records_code(host_chain):
    h = host_chain
    await h.load(media=True)
    part = h.host.ImageComponent(binary_hash="", binary_data=b"synthetic invalid image")
    await h.message("keep existing text", components=[part])
    assert h.runtime.media_errors == {"studio": "MEDIA_FORMAT_INVALID"}
    assert not h.runtime.store.list_assets("studio", Scope(PLATFORM, STREAM, OWNER))
    assert h.runtime.store.db.execute("SELECT text FROM artist_wakes").fetchone()[0] == "keep existing text"
    assert not h.renderer.calls and not h.sends
