"""从固定 Git 对象装载 Studio 验收需要的 Host 规划与回复边界。"""

from contextvars import ContextVar
from functools import wraps
from html import escape
from types import ModuleType, SimpleNamespace
from typing import ParamSpec, TypeVar
import ast
import sys

import pytest

from . import host_chain as host_fixture
from .host_source import fixed_host_file, load_nodes


@pytest.fixture
def public_host(monkeypatch, tmp_path):
    """Studio 验收始终执行指定的 Git 对象，不更改其他 Host 夹具的来源。"""
    host = host_fixture.build_public_host(monkeypatch, tmp_path, fixed_host_file)
    load_nodes(host.__dict__, fixed_host_file("plugin_runtime/protocol/envelope.py"), {"InvokeResultPayload"})
    methods = {"_inject_context", "_create_plugin_context", "_resolve_component_handler_name",
               "_resolve_component_handler", "_invoke_plugin_callable", "_handle_hook_invoke", "_handle_invoke"}
    load_nodes(host.__dict__, fixed_host_file("plugin_runtime/runner/runner_main.py"),
                            {"PluginRunner"}, {"PluginRunner": methods})
    return host


@pytest.fixture
def replyer_host(public_host, monkeypatch):
    """Fixed Host serialization, target selection and request deserialization."""
    package = ModuleType("_stage3_replyer_context")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, package.__name__, package)
    modules = []
    for name in ("context_item", "context_snapshot", "context_protocol"):
        module = ModuleType(package.__name__ + "." + name)
        monkeypatch.setitem(sys.modules, module.__name__, module)
        source = fixed_host_file("llm_models/payload_content/" + name + ".py")
        exec(compile(source, "<fixed-host-" + name + ">", "exec"), module.__dict__)  # noqa: S102 - Fixed public Git object.
        modules.append(module)
    namespace = dict(public_host.__dict__)
    for module in modules:
        namespace.update(vars(module))
    load_nodes(namespace, fixed_host_file("plugin_runtime/hook_payloads.py"),
                            {"serialize_prompt_items", "deserialize_prompt_items"})
    tree = ast.parse(fixed_host_file("chat/replyer/maisaka_generator_base.py"))
    generator = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                     and node.name == "BaseMaisakaReplyGenerator")
    generator.bases = []
    generator.body = [node for node in generator.body
                      if getattr(node, "name", None) == "_invoke_before_model_request_hook"]
    assert len(generator.body) == 1
    constant = next(node for node in tree.body if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "REPLYER_MAX_HOOK_RETRIES" for target in node.targets))
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    exec(  # noqa: S102 - Only named methods/constants from the fixed public Git object.
        compile(ast.fix_missing_locations(ast.Module(body=[future, constant, generator], type_ignores=[])),
                "<fixed-host-replyer>", "exec"), namespace)
    return SimpleNamespace(context=modules[0], generator=namespace[generator.name], namespace=namespace)


@pytest.fixture
def planner_host(replyer_host):
    """Real request constructor, prefix, time/attention and final user reminders."""
    namespace = dict(replyer_host.namespace)
    aliases = {}
    exec(  # noqa: S102 - Pure alias helpers from the fixed public Git object.
        compile(fixed_host_file("maisaka/context/message_id_alias.py"), "<fixed-host-alias>", "exec"), aliases)
    namespace.update(escape=escape, to_display_message_id=aliases["to_display_message_id"],
                     ContentContextMessage=type(None), IMAGE_QUALITY_MAX_SIDE={"normal": 512},
                     global_config=SimpleNamespace(visual=SimpleNamespace(image_quality="normal")),
                     P=ParamSpec("P"), R=TypeVar("R"), wraps=wraps,
                     _pending_images=ContextVar("stage3-pending-images", default=None))
    load_nodes(namespace, fixed_host_file("maisaka/visual/deferred_images.py"),
                            {"defer_context_image_encoding"})
    load_nodes(namespace, fixed_host_file("maisaka/context/planner_messages.py"),
                            {"build_planner_prefix", "_format_quote_ids"})
    load_nodes(namespace, fixed_host_file("maisaka/context/messages.py"),
                            {"build_context_items_from_history_entry"})
    tree = ast.parse(fixed_host_file("maisaka/chat_loop_service.py"))
    constructor = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                       and node.name == "MaisakaChatLoopService")
    constructor.bases = []
    methods = {"_build_request_messages", "_build_current_time_user_message", "_build_time_user_message",
               "_build_current_chat_attention_tail_message", "_build_planner_final_user_reminder",
               "_validate_function_call_context_anchors"}
    constructor.body = [node for node in constructor.body if getattr(node, "name", None) in methods]
    assert {node.name for node in constructor.body} == methods
    constant = next(node for node in tree.body if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "PLANNER_FINAL_USER_REMINDER" for target in node.targets))
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    exec(  # noqa: S102 - Fixed Host constructor and reminder, without application bootstrap.
        compile(ast.fix_missing_locations(ast.Module(body=[future, constant, constructor], type_ignores=[])),
                "<fixed-host-planner>", "exec"), namespace)
    return SimpleNamespace(context=replyer_host.context, namespace=namespace, constructor=namespace[constructor.name])


def build_planner_request(h, host, messages):
    ci, namespace = host.context, host.namespace
    history = []
    # Selection is synthetic; Host prefix construction, Context Items, request
    # construction, serialization and roundtrip are actual fixed public code.
    for message_id, text in messages:
        prefix = namespace["build_planner_prefix"](timestamp=h.clock(), user_name="synthetic-owner",
                                                   message_id=message_id)
        item = ci.ContextItemBuilder().set_role(ci.RoleType.User).add_text_content(prefix + text).build()
        history.append(SimpleNamespace(timestamp=h.clock(), to_context_item=lambda item=item, **kwargs: item))
    constructor = host.constructor()
    constructor._custom_chat_system_prompt = None
    constructor._session_id, constructor._is_group_chat = host_fixture.STREAM, False
    constructor._get_chat_prompt_name = lambda: "maisaka_chat"
    constructor._build_bot_nickname_notice = lambda *args, **kwargs: ""
    constructor._get_chat_prompt_for_chat = lambda *args: "synthetic current-chat attention"
    original, history_count = constructor._build_request_messages(
        history, enable_visual_message=False, system_prompt="synthetic planner system",
        final_user_message=constructor._build_planner_final_user_reminder(),
    )
    assert history_count == len(history)
    assert all(type(item).__name__ == "UserMessageItem" for item in original[-3:])
    assert original[-1].parts[0].text == namespace["PLANNER_FINAL_USER_REMINDER"]
    assert "synthetic current-chat attention" in original[-2].parts[0].text
    return original, history_count
