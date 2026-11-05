"""Real public Host text projection/schema roundtrip, without Host bootstrap.

Set CODEX_IMAGE_TEST_STUDIO_HOST_SOURCE to public MaiBot src (e.g. the installed
package's src). Only pure modules/functions are loaded; no config, IPC, database
or application startup is imported. RPC and user/session records are synthetic.
"""

from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import datetime
from html import escape
from pathlib import Path
from types import ModuleType, SimpleNamespace
import ast
import importlib.util
import json
import os
import sys

import pytest

from image_plugin_under_test.host import studio_chat
from test_studio_chat import Harness, SCOPE, sdk  # noqa: F401


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_nodes(namespace, path, names):
    tree = ast.parse(path.read_text("utf-8-sig"))
    nodes = [node for node in tree.body if getattr(node, "name", None) in names]
    assert {node.name for node in nodes} == set(names)
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[])),
                 str(path), "exec"), namespace)


@pytest.fixture
def host(monkeypatch):
    source = os.environ.get("CODEX_IMAGE_TEST_STUDIO_HOST_SOURCE")
    if not source:
        pytest.skip("Set CODEX_IMAGE_TEST_STUDIO_HOST_SOURCE to public Host src")
    root = Path(source)
    package = ModuleType("_studio_public_host")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, package.__name__, package)
    prefix = package.__name__
    loaded = []
    try:
        for name in ("context_item", "context_snapshot", "context_protocol"):
            loaded.append(prefix + "." + name)
            module = load_file(loaded[-1], root / "llm_models/payload_content" / (name + ".py"))
            setattr(package, name, module)
        for name, relative in (("registry", "plugin_runtime/host/hook_spec_registry.py"),
                               ("schema", "plugin_runtime/hook_schema_utils.py"),
                               ("aliases", "maisaka/context/message_id_alias.py")):
            loaded.append(prefix + "." + name)
            setattr(package, name, load_file(loaded[-1], root / relative))
        ci, snap, protocol = package.context_item, package.context_snapshot, package.context_protocol
        ns = dict(vars(ci))
        ns.update(vars(snap))
        ns.update(vars(protocol))
        ns.update(HookSpec=package.registry.HookSpec, build_object_schema=package.schema.build_object_schema,
                  escape=escape, deepcopy=deepcopy, ABC=ABC, abstractmethod=abstractmethod,
                  to_display_message_id=package.aliases.to_display_message_id)
        load_nodes(ns, root / "plugin_runtime/hook_payloads.py",
                   {"serialize_prompt_items", "deserialize_prompt_items"})
        load_nodes(ns, root / "maisaka/chat_loop_service.py", {"register_maisaka_hook_specs"})
        load_nodes(ns, root / "maisaka/context/planner_messages.py", {"build_planner_prefix", "_format_quote_ids"})
        load_nodes(ns, root / "common/data_models/message_component_data_model.py",
                   {"BaseMessageComponentModel", "TextComponent"})
        load_nodes(ns, root / "maisaka/context/messages.py", {"_build_item_from_sequence"})
        query_tree = ast.parse((root / "plugin_runtime/component_query.py").read_text("utf-8-sig"))
        query_class = next(node for node in query_tree.body if isinstance(node, ast.ClassDef)
                           and node.name == "ComponentQueryService")
        methods = {"_build_tool_context_payload", "_build_tool_invocation_payload"}
        query_class.body = [node for node in query_class.body if getattr(node, "name", None) in methods]
        query_class.bases = []
        exec(compile(ast.fix_missing_locations(ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), query_class],
            type_ignores=[])), "<public-host-tool-context>", "exec"), ns)
        data_tree = ast.parse((root / "plugin_runtime/capabilities/data.py").read_text("utf-8-sig"))
        data_class = next(node for node in data_tree.body if isinstance(node, ast.ClassDef) and any(
            getattr(method, "name", None) == "_cap_chat_get_stream_by_user_id" for method in node.body))
        data_class.body = [node for node in data_class.body if getattr(node, "name", None) in
                           {"_serialize_stream", "_cap_chat_get_stream_by_user_id"}]
        data_class.bases = []
        exec(compile(ast.fix_missing_locations(ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), data_class],
            type_ignores=[])), "<public-host-existing-session-lookup>", "exec"), ns)
        package.data_class = ns[data_class.name]
        package.ns, package.root = ns, root
        yield package
    finally:
        for name in loaded:
            sys.modules.pop(name, None)


def projected(host, role, text, *, self_message=False):
    ns = host.ns
    prefix = ns["build_planner_prefix"](
        timestamp=datetime(2026, 11, 2, 12, 34, 56), user_name='name with " and <tags>',
        message_id="synthetic-msg", is_self_message=self_message,
    )
    sequence = SimpleNamespace(components=[ns["TextComponent"](prefix + text)])
    return ns["_build_item_from_sequence"](role, sequence, text)


@pytest.mark.parametrize("text,expected", [
    ("鲸鱼娘画了吗", True), ("你画了吗", True), ("现在在画什么", True), ("上次那幅分享了吗", True),
    ("鲸鱼娘画了吗？背景换星空", False), ("你画了吗？想要蓝色鲸鱼娘", False),
])
async def test_real_host_projection_and_hook_deserialization(sdk, host, text, expected):  # noqa: F811
    h = Harness(sdk)
    ns = host.ns
    # Real Host AssistantMessageItem history must not confuse the current User
    # item. Prefix quoting and Context Item serialization use public Host code.
    assistant = projected(host, ns["RoleType"].Assistant, "鲸鱼娘画了吗")
    user = projected(host, ns["RoleType"].User, text)
    original = [assistant, user]
    serialized = ns["serialize_prompt_items"](original)
    registry = host.registry.HookSpecRegistry()
    specs = ns["register_maisaka_hook_specs"](registry)
    spec = next(item for item in specs if item.name == studio_chat.HOOK_NAME)
    assert spec.allow_kwargs_mutation and spec.allow_blocking
    assert spec.parameters_schema["properties"]["session_id"]["type"] == "string"
    assert "items" in spec.parameters_schema["required"]
    before = {"session_id": SCOPE.stream_id, "items": serialized,
              "item_schema_version": ns["CONTEXT_ITEM_SCHEMA_VERSION"], "tool_definitions": [],
              "selected_history_count": 2, "built_message_count": 2, "selection_reason": "synthetic history"}
    result = await h.chat.before_request(**before)
    assert (result is not None) is expected
    if expected:
        modified = result["modified_kwargs"]
        actual = ns["deserialize_prompt_items"](
            modified["items"], item_schema_version=modified["item_schema_version"], original_items=original,
        )
        assert actual[0] is assistant and actual[1] is user  # Unmodified replay preserved.
        assert type(actual[-2]).__name__ == "SystemMessageItem"
        assert type(actual[-1]).__name__ == "UserMessageItem"
        assert json.loads(actual[-1].parts[0].text)["data"] == h.snapshot.data
    else:
        assert h.calls == h.reads == []


async def test_real_assistant_output_and_host_self_messages_are_not_user_queries(sdk, host):  # noqa: F811
    h = Harness(sdk)
    ns = host.ns
    for item in (projected(host, ns["RoleType"].Assistant, "现在在画什么"),
                 projected(host, ns["RoleType"].User, "现在在画什么", self_message=True)):
        assert await h.chat.before_request(
            session_id=SCOPE.stream_id, item_schema_version=ns["CONTEXT_ITEM_SCHEMA_VERSION"],
            items=ns["serialize_prompt_items"]([item]),
        ) is None
    assert h.calls == h.reads == []


def test_real_host_tool_merge_does_not_establish_identity(host):
    query = host.ns["ComponentQueryService"]
    context = SimpleNamespace(stream_id="genuine-other-session", session_id="genuine-other-session",
                              user_id="genuine-other-user", platform="synthetic", group_id="", metadata={})
    forged = {"stream_id": SCOPE.stream_id, "user_id": SCOPE.user_id, "platform": SCOPE.platform}
    invocation = SimpleNamespace(arguments=forged)
    result = query._build_tool_invocation_payload(SimpleNamespace(invoke_method="plugin.invoke_tool"),
                                                 invocation, context)
    assert result["stream_id"] == SCOPE.stream_id
    assert result["user_id"] == SCOPE.user_id
    # This is why the chat adapter registers no Tool identity argument surface.


async def test_actual_host_existing_private_session_capability_through_sdk(sdk, host):  # noqa: F811
    h = Harness(sdk)
    capability = host.data_class()
    session = SimpleNamespace(session_id=SCOPE.stream_id, platform=SCOPE.platform,
                              user_id=SCOPE.user_id, user_nickname="synthetic owner", user_cardname="",
                              group_id="", group_name="", account_id="", scope="", is_group_session=False)
    lookups = []

    def existing_sessions(**kwargs):
        lookups.append(kwargs)
        return [session]

    capability._list_sessions = existing_sessions

    async def actual_lookup():
        h.response = await capability._cap_chat_get_stream_by_user_id(
            "isolated-studio-query", "chat.get_stream_by_user_id",
            {"platform": SCOPE.platform, "user_id": SCOPE.user_id},
        )

    h.rpc_hook = actual_lookup
    result = await h.chat.before_request(
        session_id=SCOPE.stream_id, item_schema_version=host.ns["CONTEXT_ITEM_SCHEMA_VERSION"],
        items=host.ns["serialize_prompt_items"]([projected(host, host.ns["RoleType"].User, "你画了吗")]),
    )
    assert result is not None
    assert lookups == [{"platform": SCOPE.platform, "is_group_session": False}]
    assert h.response["stream"]["session_id"] == SCOPE.stream_id
    assert h.response["stream"]["chat_type"] == "private"
