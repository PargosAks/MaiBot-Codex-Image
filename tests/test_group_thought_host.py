"""Public Host hook/SDK roundtrips with synthetic sessions and temporary state.

Reuses public_host's fixed-revision AST loader and real Runner/Dispatcher. Pure
Context Item modules, hook specs, tool serialization and invocation/context
builders are also loaded from that revision. No Host bootstrap, real model,
network, QQ, or production data is used. The scheduler remains gated throughout.
"""

from contextlib import closing
from copy import deepcopy
from types import ModuleType, SimpleNamespace
import json
import os
import sqlite3
import sys

import pytest
import test_host_integration as host_integration

from image_plugin_under_test.host.group_thought_runtime import TAGS, TOOL_NAME
from image_plugin_under_test.domain.policy import Scope
from image_plugin_under_test.workspace.studio import Studio
from test_host_integration import HOST_REV, OWNER, PLATFORM, PLUGIN_ID, STREAM, host_file, load_nodes


public_host = host_integration.public_host
host_chain = host_integration.host_chain
pytestmark = pytest.mark.skipif(
    not (os.environ.get("CODEX_IMAGE_TEST_HOST_SOURCE") or os.environ.get("CODEX_IMAGE_TEST_HOST_REPO")),
    reason="Explicit public Host source is required",
)
BEFORE = "maisaka.planner.before_request"
AFTER = "maisaka.planner.after_response"
OTHER_STREAM = "other-existing-host-group-stream"


@pytest.fixture
async def thought_host(host_chain, monkeypatch):
    h, ns = host_chain, host_chain.host.__dict__
    assert HOST_REV == "062d45cf8bd4272ac57d7ec03fcf405e5a458211"
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


def ledger(h):
    # Thoughts must not append an autonomous operation, wake, or render budget.
    return tuple(h.runtime.store.db.iterdump())


def no_effects(h):
    assert not h.renderer.calls and not h.sends and not h.reviews and not h.gate.is_set()
    assert all(name.startswith("chat.get_stream_by_") for name, _ in h.calls)
    # The hook may signal the existing worker; only a subsequent budgeted cycle
    # imports/adopts a note. This fixture intentionally keeps that worker parked.


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


async def test_registered_sdk_tool_and_both_blocking_hooks_roundtrip(thought_host):
    h = thought_host
    await h.load(group=True)
    assert set(h.runtime.verified_notebook_groups) == {"studio", "other"}
    initial = ledger(h)
    token, offered = await before(h)
    schema = offered["tool_definitions"][0]["function"]["parameters"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(schema["required"]) == {"notebook_context", "tags"}
    assert schema["properties"]["notebook_context"]["enum"] == [token]
    assert schema["properties"]["tags"]["type"] == "array"
    assert set(schema["properties"]["tags"]["items"]["enum"]) == set(TAGS)
    assert "enum" not in h.thought_definition["function"]["parameters"]["properties"]["notebook_context"]
    assert counts(h) == (0, 0)
    assert (await receipt(h, token))["success"] is False
    assert counts(h) == (0, 0) and ledger(h) == initial
    items = output(h, token)
    await after(h, items)
    assert counts(h) == (1, 1)
    saved = counts(h), ledger(h)
    result = await receipt(h, token)
    assert result["success"] and result["structured_content"] == {"recorded": True}
    await after(h, items)
    assert (await receipt(h, token)) == result
    assert (counts(h), ledger(h)) == saved and ledger(h) == initial
    assert [call["hook_name"] for call in h.planner_calls] == [BEFORE, AFTER, AFTER]
    view = Studio(h.data_paths.data_dir, scopes={"studio": Scope(PLATFORM, STREAM, OWNER)},
                  group_bindings={"studio": "synthetic-group"}, enabled=False)
    rows = view.records("studio", "group_thoughts")["items"]
    assert len(rows) == 1 and rows[0]["source"] == "MaiBot 群聊构思"
    assert not ({"participant_count", "member_count", "message_count", "record_count", "members"} & rows[0].keys())
    for hidden in ("participant_count", "member_count", "message_count", OWNER, STREAM, token):
        assert hidden not in json.dumps(result) + json.dumps(rows)
    no_effects(h)


async def test_two_real_verified_groups_reject_foreign_token_and_isolate_notes(thought_host):
    h = thought_host
    await h.load(group=True)
    initial = ledger(h)
    first, _ = await before(h)
    second, _ = await before(h, OTHER_STREAM)
    assert first and second and first != second
    await after(h, output(h, first), OTHER_STREAM)
    assert counts(h) == (0, 0)
    await after(h, output(h, first))
    await after(h, output(h, second, ["forest", "mist"]), OTHER_STREAM)
    assert counts(h) == (2, 2)
    assert not (await receipt(h, first, session=OTHER_STREAM))["success"]
    assert not (await receipt(h, first, chat_id=OTHER_STREAM))["success"]
    assert not (await receipt(h, first, ["coast"]))["success"]
    assert not (await receipt(h, first, extra={"stream_id": OTHER_STREAM}))["success"]
    assert (await receipt(h, second, ["mist", "forest"], session=OTHER_STREAM))["success"]
    with closing(sqlite3.connect((h.data_paths.data_dir / "group_thoughts.sqlite3").as_uri() + "?mode=ro",
                                uri=True)) as db:
        assert db.execute("SELECT space_id,theme_key FROM thought_topics ORDER BY space_id").fetchall() == [
            ("other", "forest+mist"), ("studio", "rain+store")]
    assert ledger(h) == initial
    no_effects(h)


@pytest.mark.parametrize("group,enabled", [(False, True), (True, False)])
async def test_private_and_disabled_host_never_offer_or_record(thought_host, group, enabled):
    h = thought_host
    await h.load(group=group, enabled=enabled)
    token, offered = await before(h)
    assert token is None and not offered["tool_definitions"]
    await after(h, output(h, "0" * 48))
    assert not (await receipt(h, "0" * 48))["success"]
    assert counts(h) == (0, 0)
    no_effects(h)


async def test_host_disabled_tool_is_not_reintroduced(thought_host):
    h = thought_host
    await h.load(group=True)
    unrelated = {"type": "function", "function": {
        "name": "synthetic_other_tool", "description": "other", "parameters": {"type": "object"}}}
    token, offered = await before(h, definitions=[unrelated])
    assert token is None and offered["tool_definitions"] == [unrelated]
    assert h.plugin.group_thought_runtime.offers == {}
    await after(h, output(h, "0" * 48))
    assert counts(h) == (0, 0)
    no_effects(h)


@pytest.mark.parametrize("boundary", ["unverified", "host_lookup_mismatch", "revoked"])
async def test_binding_and_authorization_required_before_offering(thought_host, boundary):
    h = thought_host
    await h.load(group=True)
    if boundary == "unverified":
        h.runtime.verified_notebook_groups.pop("studio")
    elif boundary == "host_lookup_mismatch":
        h.stream_override = {"success": True, "stream": {
            "platform": PLATFORM, "stream_id": OTHER_STREAM, "session_id": OTHER_STREAM,
            "user_id": OWNER, "group_id": "synthetic-group", "is_group_session": True}}
    else:
        await h.runtime.db("revoke", "studio", Scope(PLATFORM, STREAM, OWNER),
                           source_ref="synthetic-revocation", granted_by="synthetic-test")
    initial = ledger(h)
    token, offered = await before(h)
    assert token is None and not offered["tool_definitions"]
    assert counts(h) == (0, 0) and ledger(h) == initial
    no_effects(h)


async def test_after_without_offer_and_ordinary_planner_text_cannot_record(thought_host):
    h = thought_host
    await h.load(group=True)
    initial = ledger(h)
    await after(h, output(h, "0" * 48))
    token, _ = await before(h)
    ci = h.host
    text = ci.AssistantMessageItem(meta=ci.ContextItemMeta.create(logical_turn_id="synthetic-text-turn"),
                                   parts=(ci.ContextTextPart("我想到雨夜中的便利店"),))
    await after(h, ci.serialize_prompt_items([text]))
    assert counts(h) == (0, 0) and not (await receipt(h, token))["success"]
    assert ledger(h) == initial
    no_effects(h)


@pytest.mark.parametrize("invalid", ["wrong_token", "unknown_tag", "string_tags", "duplicate_tags",
                                        "identity_args", "old_wire_shape", "two_calls", "wrong_function"])
async def test_after_rejects_unbound_or_malformed_model_calls(thought_host, invalid):
    h = thought_host
    await h.load(group=True)
    token, _ = await before(h)
    items = output(h, token)
    call = items[0]["tool_call"]
    if invalid == "wrong_token":
        call["args"]["notebook_context"] = "0" * 48
    elif invalid == "unknown_tag":
        call["args"]["tags"] = ["arbitrary original message"]
    elif invalid == "string_tags":
        call["args"]["tags"] = "rain"
    elif invalid == "duplicate_tags":
        call["args"]["tags"] = ["rain", "rain"]
    elif invalid == "identity_args":
        call["args"]["stream_id"] = STREAM
    elif invalid == "old_wire_shape":
        items = [{"type": "FunctionCallItem", "tool_calls": [{
            "id": call["call_id"], "function": {"name": TOOL_NAME, "arguments": call["args"]}}]}]
    elif invalid == "two_calls":
        items += output(h, token)
    else:
        call["func_name"] = "synthetic_other_tool"
    await after(h, items)
    assert counts(h) == (0, 0) and not (await receipt(h, token))["success"]
    no_effects(h)


@pytest.mark.parametrize("change", ["new_round", "revoke", "verified_removed", "expired", "host_tool_removed"])
async def test_current_round_authorization_rechecked_at_after(thought_host, change):
    h = thought_host
    await h.load(group=True)
    token, _ = await before(h)
    if change == "new_round":
        fresh, _ = await before(h)
        assert fresh != token
    elif change == "revoke":
        await h.runtime.db("revoke", "studio", Scope(PLATFORM, STREAM, OWNER),
                           source_ref="synthetic-revocation", granted_by="synthetic-test")
    elif change == "verified_removed":
        h.runtime.verified_notebook_groups.pop("studio")
    elif change == "host_tool_removed":
        absent, _ = await before(h, definitions=[])
        assert absent is None
    else:
        h.plugin.group_thought_runtime.monotonic = lambda: float("inf")
    initial = ledger(h)
    await after(h, output(h, token))
    assert counts(h) == (0, 0) and not (await receipt(h, token))["success"]
    assert ledger(h) == initial
    no_effects(h)
