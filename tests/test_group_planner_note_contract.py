"""Probe the existing Host Action boundary, without registering or invoking a plugin action."""
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from support.host_source import host_file, load_nodes


@pytest.mark.external_contract
def test_planner_note_can_bind_to_the_runtime_stream_but_not_attribute_members():
    def execution_context(**values):
        return SimpleNamespace(metadata={}, **values)

    ns = {"ToolExecutionContext": execution_context, "ToolInvocation": object,
          "Optional": Optional, "Dict": dict, "Any": Any}
    engine = "MaisakaReasoningEngine"
    service = "ComponentQueryService"
    load_nodes(ns, host_file("maisaka/reasoning_engine.py"), {engine},
               {engine: {"_build_tool_execution_context"}})
    load_nodes(ns, host_file("plugin_runtime/component_query.py"), {service},
               {service: {"_build_tool_context_payload", "_build_tool_invocation_payload"}})
    runtime = SimpleNamespace(session_id="group-a-stream", chat_stream=SimpleNamespace(
        platform="qq", is_group_session=True, group_id="a", user_id=""))
    context = ns[engine]._build_tool_execution_context(SimpleNamespace(_runtime=runtime), "synthetic thought")
    assert context.group_id == "a" and context.is_group_chat
    assert not context.user_id  # Author of the note would be MaiBot, not a group member.
    entry = SimpleNamespace(invoke_method="plugin.invoke_action")
    args = {"tags": ["rain", "store"], "stream_id": "wrong-stream", "group_id": "wrong-group"}
    call = SimpleNamespace(arguments=args, stream_id="wrong-stream", reasoning="")
    payload = ns[service]._build_tool_invocation_payload(entry, call, context)
    assert payload["stream_id"] == payload["chat_id"] == "group-a-stream"
    assert payload["group_id"] == "wrong-group"  # Not authoritative. Reject reserved args.
    assert payload["action_data"] == args
