"""本地群聊配置、规划入参和回执，所有状态限于临时目录。"""

from copy import deepcopy
from types import SimpleNamespace
import asyncio

import pytest

from image_plugin_under_test import CodexImagePlugin
from image_plugin_under_test.host.group_thought_runtime import GroupThoughtRuntime, PARAMETERS, TOOL_NAME
from image_plugin_under_test.host.host_runtime import HostRuntime


@pytest.fixture
async def groups(tmp_path, monkeypatch):
    calls = []
    async def host(method, *, payload, timeout_ms):
        assert method == "cap.call"
        assert payload["capability"] == "chat.get_stream_by_group_id"
        target = payload["args"]["group_id"]
        calls.append(target)
        return {"success": True, "stream": {"platform": "qq", "group_id": target,
                "stream_id": "stream-" + target, "is_group_session": True}}

    async def parked(self, space_id):
        await asyncio.Event().wait()

    monkeypatch.setattr(HostRuntime, "_serve", parked)
    plugin = CodexImagePlugin()
    plugin._set_context(SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path), call_host_method=host))
    spaces = [dict(space_id=g, platform="qq", group_id=g, stream_id="stream-" + g,
                   user_id="owner", consent_ref="test-grant", continuous=True) for g in ("a", "b", "c")]
    for item in spaces[:2]:
        item["group_inspiration"] = True
    plugin.set_plugin_config({"plugin": plugin.get_default_config()["plugin"],
                              "connection": {"secret": "synthetic-local-only-key-" + "x" * 32,
                                             "allowed_users": ["qq:owner"]},
                              "autonomy": {"enabled": True, "spaces": spaces}})
    await plugin.on_load()
    try:
        assert not plugin.startup_error
        assert calls == ["a", "b", "c"]
        yield plugin
    finally:
        await plugin.on_unload()


def request(session="stream-a"):
    return {"session_id": session, "item_schema_version": 1, "items": [],
            "tool_definitions": [
                {"type": "function", "function": {"name": "ordinary_reply", "parameters": {"type": "object"}}},
                {"type": "function", "function": {"name": TOOL_NAME, "parameters": deepcopy(PARAMETERS)}}]}


async def offer(plugin, session="stream-a"):
    modified = await plugin.group_thought_before_request(**request(session))
    definitions = modified["modified_kwargs"]["tool_definitions"]
    selected, = [d for d in definitions if d["function"]["name"] == TOOL_NAME]
    return selected["function"]["parameters"]["properties"]["notebook_context"]["enum"][0]


def response(token, tags=None, session="stream-a"):
    return {"session_id": session, "item_schema_version": 1, "output_items": [{
        "item_type": "FunctionCallItem", "meta": {"logical_turn_id": "synthetic-turn"},
        "tool_call": {"call_id": "synthetic-call", "func_name": TOOL_NAME,
                      "args": {"notebook_context": token, "tags": tags or ["rain", "store"]}}}]}


@pytest.fixture
def notes(groups):
    clock = SimpleNamespace(wall=1800., monotonic=100.)
    runtime = GroupThoughtRuntime(groups, clock=lambda: clock.wall, monotonic=lambda: clock.monotonic)
    groups.group_thought_runtime = runtime
    return groups, runtime, clock
