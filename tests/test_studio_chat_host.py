"""Real public Host text projection/schema roundtrip, without Host bootstrap.

Set CODEX_IMAGE_TEST_STUDIO_HOST_SOURCE to public MaiBot src (e.g. the installed
package's src). Only pure modules/functions are loaded; no config, IPC, database
or application startup is imported. RPC and user/session records are synthetic.
"""

from datetime import datetime
from types import SimpleNamespace
import json

import pytest

from image_plugin_under_test.host import studio_chat
from support.studio_chat import Harness, SCOPE, sdk as sdk
from support.studio_chat_host import host as host


pytestmark = pytest.mark.external_contract


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
