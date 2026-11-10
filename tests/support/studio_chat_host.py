"""从显式指定的公开源码快照隔离装载工作室聊天方法。"""

from abc import ABC, abstractmethod
from copy import deepcopy
from html import escape
from pathlib import Path
from types import ModuleType
import ast
import importlib.util
import os
import sys

import pytest


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
