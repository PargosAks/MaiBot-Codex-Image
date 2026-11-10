"""隔离的 Host 规划 Hook、工具注册和群念头回执。"""

from contextlib import closing
from copy import deepcopy
from types import ModuleType, SimpleNamespace
import sqlite3
import sys

import pytest

from image_plugin_under_test.host.group_thought_runtime import TOOL_NAME
from .host_chain import OWNER, PLATFORM, PLUGIN_ID, STREAM
from .host_source import host_file, load_nodes


BEFORE = "maisaka.planner.before_request"


AFTER = "maisaka.planner.after_response"


OTHER_STREAM = "other-existing-host-group-stream"


@pytest.fixture
async def thought_host(host_chain, monkeypatch):
    h, ns = host_chain, host_chain.host.__dict__
    # Execute pure Host modules under an isolated package to preserve relative
    # imports/dataclass identity without importing src or starting the Host.
    package = ModuleType("_thought_public_context")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, package.__name__, package)
    for name in ("context_item", "context_snapshot", "context_protocol", "tool_option"):
        module = ModuleType(package.__name__ + "." + name)
        monkeypatch.setitem(sys.modules, module.__name__, module)
        exec(compile(host_file(f"llm_models/payload_content/{name}.py"),  # noqa: S102 - isolated pure Host module
                     f"<public-host-{name}>", "exec"), module.__dict__)
        ns.update({key: value for key, value in vars(module).items() if not key.startswith("__")})
    ns.update(PromptMessage=dict, ToolInvocation=object,
              ToolExecutionContext=lambda **values: SimpleNamespace(metadata={}, **values))
    load_nodes(ns, host_file("plugin_runtime/hook_payloads.py"),
               {"serialize_prompt_items", "deserialize_prompt_items", "serialize_tool_definitions"})
    load_nodes(ns, host_file("maisaka/chat_loop_service.py"), {"register_maisaka_hook_specs"})
    load_nodes(ns, host_file("plugin_runtime/component_query.py"), {"ComponentQueryService"},
               {"ComponentQueryService": {"_build_tool_context_payload", "_build_tool_invocation_payload"}})
    load_nodes(ns, host_file("maisaka/reasoning_engine.py"), {"MaisakaReasoningEngine"},
               {"MaisakaReasoningEngine": {"_build_tool_execution_context"}})
    load_nodes(ns, host_file("plugin_runtime/protocol/envelope.py"), {"InvokeResultPayload"})
    runner = "PluginRunner"
    load_nodes(ns, host_file("plugin_runtime/runner/runner_main.py"), {runner},
               {runner: {"_handle_invoke"}})
    # Add the genuine generic Tool invoke method to the already injected Runner.
    h.runner._handle_invoke = ns[runner]._handle_invoke.__get__(h.runner)

    components = h.plugin.get_components()
    tool = next(item for item in components if item["name"] == TOOL_NAME)
    assert tool["type"] == "TOOL"
    assert tool["metadata"]["invoke_method"] == "plugin.invoke_tool"
    assert tool["metadata"]["chat_scope"] == "group"
    selected = [item for item in components if item["type"] == "HOOK_HANDLER"
                and item["metadata"].get("hook") in (BEFORE, AFTER)
                and "thought" in item["name"]]
    assert len(selected) == 2
    entries = {}
    for component in selected:
        meta = component["metadata"]
        assert meta["mode"] == "blocking"
        entries[meta["hook"]] = SimpleNamespace(
            name=component["name"], plugin_id=PLUGIN_ID, full_name=PLUGIN_ID + "." + component["name"],
            is_observe=False, mode=meta["mode"], order=meta["order"], timeout_ms=meta["timeout_ms"],
            error_policy=meta["error_policy"],
        )
    plugin_meta = SimpleNamespace(instance=h.plugin, config_error="", component_handlers={
        item["name"]: item["metadata"]["handler_name"] for item in components})
    h.runner._loader = SimpleNamespace(get_plugin=lambda key: plugin_meta if key == PLUGIN_ID else None)
    h.planner_calls = []

    async def invoke(method, plugin_id, component_name, args, **kwargs):
        assert method == "plugin.invoke_hook" and plugin_id == PLUGIN_ID
        h.planner_calls.append(deepcopy(args))
        envelope = h.host.Envelope(request_id=1, message_type="request", method=method, plugin_id=plugin_id,
                                   payload={"component_name": component_name, "args": args})
        response = await h.runner._handle_hook_invoke(envelope)
        assert response.error is None and response.payload["success"] is True
        return response

    registry = SimpleNamespace(get_hook_handlers=lambda hook: [entries[hook]] if hook in entries else [])
    supervisor = SimpleNamespace(group_name="third_party", component_registry=registry, invoke_plugin=invoke)
    h.planner_dispatcher = h.host.HookDispatcher(supervisors_provider=lambda: [supervisor])
    h.host.register_maisaka_hook_specs(h.planner_dispatcher)
    h.thought_definition = h.host.serialize_tool_definitions([{
        "type": "function", "function": {"name": TOOL_NAME,
        "description": tool["metadata"]["description"], "parameters": tool["metadata"]["parameters_raw"]},
    }])[0]
    base_config = h.config

    def config(**kwargs):
        value = base_config(**kwargs)
        first = value["autonomy"]["spaces"][0]
        first["group_inspiration"] = bool(first["group_id"])
        if first["group_id"]:
            second = deepcopy(first)
            second.update(space_id="other", stream_id=OTHER_STREAM, group_id="other-synthetic-group",
                          consent_ref="other-explicit-consent")
            value["autonomy"]["spaces"].append(second)
        return value

    async def capability(plugin_id, name, args):
        if name.startswith("chat.get_stream_by_"):
            h.calls.append((name, deepcopy(args)))
            group = args.get("group_id", "")
            stream = OTHER_STREAM if group == "other-synthetic-group" else STREAM
            return h.stream_override or {"success": True, "stream": {
                "platform": PLATFORM, "stream_id": stream, "session_id": stream,
                "user_id": OWNER, "group_id": group, "is_group_session": bool(group)}}
        pytest.fail("Unexpected model/send capability: " + name)

    h.config = config
    for name in ("llm.generate", "send.image", "chat.get_stream_by_user_id", "chat.get_stream_by_group_id"):
        h.capabilities.register_capability(name, capability)
    # Original Renderer records effects and never generates a real image.
    try:
        yield h
    finally:
        await h.planner_dispatcher.stop()


def counts(h):
    path = h.data_paths.data_dir / "group_thoughts.sqlite3"
    if not path.exists():
        return 0, 0
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return tuple(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                     for table in ("thought_topics", "thought_requests"))


async def before(h, session=STREAM, definitions=None):
    ci = h.host
    original = [ci.UserMessageItem(meta=ci.ContextItemMeta.create(),
                                   parts=(ci.ContextTextPart("今晚雨好大，便利店还亮着灯"),))]
    payload = {"items": ci.serialize_prompt_items(original),
               "item_schema_version": ci.CONTEXT_ITEM_SCHEMA_VERSION,
               "tool_definitions": deepcopy(definitions if definitions is not None else [h.thought_definition]),
               "selected_history_count": 1, "built_message_count": 1,
               "selection_reason": "synthetic", "session_id": session}
    saved = deepcopy(payload)
    result = await h.planner_dispatcher.invoke_hook(BEFORE, **payload)
    assert not result.errors and not result.aborted and payload == saved
    restored = ci.deserialize_prompt_items(result.kwargs["items"], original_items=original,
                                           item_schema_version=result.kwargs["item_schema_version"])
    assert restored[0] is original[0]
    assert result.kwargs["items"] == saved["items"]  # No private-chat context injection.
    definitions = result.kwargs["tool_definitions"]
    found = [item for item in definitions if item["function"]["name"] == TOOL_NAME]
    token = found[0]["function"]["parameters"]["properties"]["notebook_context"]["enum"][0] if found else None
    return token, result.kwargs


def output(h, token, tags=None, *, extra=None, name=TOOL_NAME):
    ci = h.host
    args = {"notebook_context": token, "tags": ["rain", "store"] if tags is None else tags}
    args.update(extra or {})
    call = ci.FunctionCallItem(meta=ci.ContextItemMeta.create(logical_turn_id="synthetic-planner-turn"),
                               tool_call=ci.ContextToolCall.create(call_id="synthetic-call", func_name=name, args=args))
    serialized = ci.serialize_prompt_items([call])
    assert serialized[0]["item_type"] == "FunctionCallItem"
    assert "type" not in serialized[0]  # Public Host's discriminator, not a hand-built payload.
    assert serialized[0]["tool_call"]["call_id"] == "synthetic-call"
    assert serialized[0]["tool_call"]["args"] == args
    assert "function" not in serialized[0] and "arguments" not in serialized[0]
    restored = ci.deserialize_prompt_items(serialized, item_schema_version=ci.CONTEXT_ITEM_SCHEMA_VERSION,
                                           mode=ci.ContextProtocolMode.MODEL_OUTPUT, original_items=[call])
    assert restored[0] is call
    return serialized


async def after(h, items, session=STREAM, version=1):
    payload = {"output_items": deepcopy(items), "session_id": session, "item_schema_version": version,
               "selected_history_count": 1, "built_message_count": 1, "selection_reason": "synthetic",
               "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    original = deepcopy(payload)
    result = await h.planner_dispatcher.invoke_hook(AFTER, **payload)
    assert not result.errors and not result.aborted
    assert payload == original and result.kwargs == original


async def receipt(h, token, tags=None, *, session=STREAM, chat_id=None, extra=None):
    engine_runtime = SimpleNamespace(session_id=session, chat_stream=SimpleNamespace(
        platform=PLATFORM, is_group_session=True,
        group_id="other-synthetic-group" if session == OTHER_STREAM else "synthetic-group", user_id=""))
    context = h.host.MaisakaReasoningEngine._build_tool_execution_context(
        SimpleNamespace(_runtime=engine_runtime), "synthetic thought")
    assert not context.user_id  # A planner thought has no member attribution.
    args = {"tags": ["rain", "store"] if tags is None else tags, "notebook_context": token, **(extra or {})}
    payload = h.host.ComponentQueryService._build_tool_invocation_payload(
        SimpleNamespace(invoke_method="plugin.invoke_tool"), SimpleNamespace(arguments=args), context)
    if chat_id is not None:
        payload["chat_id"] = chat_id
    envelope = h.host.Envelope(request_id=2, message_type="request", method="plugin.invoke_tool",
                               plugin_id=PLUGIN_ID, payload={"component_name": TOOL_NAME, "args": payload})
    response = await h.runner._handle_invoke(envelope)
    assert response.error is None and response.payload["success"] is True
    return response.payload["result"]
