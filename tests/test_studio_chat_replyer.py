"""Receive-note -> real replyer hook correlation, with no visible sending."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import ast
import asyncio
import json
import logging

import pytest

from image_plugin_under_test import CodexImagePlugin
from image_plugin_under_test.policy import Scope
from test_studio_chat import Harness, OTHER, SCOPE, payload, sdk  # noqa: F401
from test_studio_chat_host import host  # noqa: F401


MESSAGE_ID = "full-platform-message-id-not-a-model-alias"


def reply_payload(**extra):
    # Replyer receives its generated request prompt, not the planner's question.
    return {"session_id": SCOPE.stream_id, "reply_message_id": MESSAGE_ID, "item_schema_version": 1,
            "items": [{"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "真实用户问题请保留"}]}],
            "request_type": "maisaka.replyer", "attempt": 1, "retry_count": 0,
            "reply_tool_args": {}, **extra}


def note(h):
    assert h.chat.note_query(SCOPE, MESSAGE_ID)


async def test_replyer_receives_fresh_facts_and_preserves_original_question_and_options(sdk):  # noqa: F811
    h = Harness(sdk, planner=False)
    note(h)
    assert h.calls == h.reads == []  # Recording an event has no side effects.
    assert await h.chat.before_request(**payload(message_id=MESSAGE_ID))
    h.snapshot = replace(h.snapshot, data={"current_work": {"subject": "鲸鱼娘", "share_state": "send_unknown"}})
    before = reply_payload(reply_tool_args={"scope": "model-forged", "facts": "已发送", "retry": True})
    original = deepcopy(before)
    result = await h.chat.before_replyer_request(**before)
    assert before == original
    assert result["action"] == "continue" and set(result) == {"action", "modified_kwargs"}
    modified = result["modified_kwargs"]
    assert {key: value for key, value in modified.items() if key != "items"} == {
        key: value for key, value in original.items() if key != "items"}
    assert modified["items"][:len(before["items"])] == before["items"]
    assert modified["items"][-2]["parts"][0]["text"] == sdk.chat.DATA_RULE
    assert json.loads(modified["items"][-1]["parts"][0]["text"])["data"] == h.snapshot.data
    assert len(h.calls) == len(h.reads) == 2  # Both stages independently verify/read.
    assert await h.chat.before_replyer_request(**before) is None
    assert not h.chat.note_query(SCOPE, MESSAGE_ID)  # Duplicate receive cannot rearm a reply.
    assert len(h.calls) == 2


@pytest.mark.parametrize("change", [
    {}, {"reply_message_id": "another-message"}, {"reply_message_id": "mshort"},
    {"reply_message_id": ""}, {"reply_message_id": None}, {"session_id": OTHER.stream_id},
    {"item_schema_version": True}, {"item_schema_version": 2}, {"items": {}},
])
async def test_only_observed_full_host_target_and_session_unlock_replyer(sdk, change):  # noqa: F811
    h = Harness(sdk, planner=False)
    if change:
        note(h)
    args = {"user_id": SCOPE.user_id, "stream_id": SCOPE.stream_id,
            "reply_message_id": MESSAGE_ID, "query": "你画了吗", "authorized": True}
    assert await h.chat.before_replyer_request(**reply_payload(reply_tool_args=args, **change)) is None
    assert h.calls == h.reads == []


@pytest.mark.parametrize("scope,message_id", [
    (OTHER, MESSAGE_ID), (Scope("other", SCOPE.stream_id, SCOPE.user_id), MESSAGE_ID),
    (Scope(SCOPE.platform, SCOPE.stream_id, "other-owner"), MESSAGE_ID),
    ({"platform": SCOPE.platform}, MESSAGE_ID), (SCOPE, ""), (SCOPE, 123), (SCOPE, "x" * 513),
])
def test_note_requires_typed_configured_exact_owner_and_bounded_real_id(sdk, scope, message_id):  # noqa: F811
    h = Harness(sdk, planner=False)
    assert not h.chat.note_query(scope, message_id)
    assert h.chat._queries == {} and h.calls == h.reads == []


@pytest.mark.parametrize("where", ["before", "during"])
async def test_expiry_and_clear_during_await_invalidate_replyer_claim(sdk, where):  # noqa: F811
    for action in ("expire", "clear"):
        h = Harness(sdk, planner=False)
        clock = [10.0]
        h.chat._query_clock = lambda: clock[0]
        note(h)

        async def invalidate():
            if action == "expire":
                clock[0] = 70.0  # Deadline is exclusive.
            else:
                h.chat.clear_queries()

        if where == "before":
            await invalidate()
        else:
            h.read_hook = invalidate
        assert await h.chat.before_replyer_request(**reply_payload()) is None
        if where == "before":
            assert h.calls == h.reads == []


async def test_note_cache_is_bounded_no_text_and_duplicate_does_not_extend_ttl(sdk):  # noqa: F811
    h = Harness(sdk, planner=False)
    clock = [0.0]
    h.chat._query_clock = lambda: clock[0]
    note(h)
    original = h.chat._queries[(SCOPE, MESSAGE_ID)]
    clock[0] = 30.0
    assert not h.chat.note_query(SCOPE, MESSAGE_ID)
    assert h.chat._queries[(SCOPE, MESSAGE_ID)] is original and original.deadline == 60.0
    assert set(vars(original)) == {"binding", "deadline", "consumed", "planner_consumed", "is_query"}
    for index in range(sdk.chat.MAX_QUERY_NOTES - 1):
        assert h.chat.note_query(SCOPE, f"another-{index}")
    assert not h.chat.note_query(SCOPE, "overflow")
    assert len(h.chat._queries) == sdk.chat.MAX_QUERY_NOTES
    assert all(not item.is_query and item.consumed for item in h.chat._queries.values())
    assert not h.chat.note_query(SCOPE, MESSAGE_ID)
    clock[0] = 100.0
    assert h.chat.note_query(SCOPE, "new-event-after-expiry")
    assert len(h.chat._queries) == 1
    assert h.calls == h.reads == []


@pytest.mark.parametrize("change", ["allowlist", "binding", "group", "host", "grant", "foreign-snapshot"])
async def test_replyer_rechecks_current_authority_and_private_stream(sdk, change):  # noqa: F811
    h = Harness(sdk, planner=False)
    note(h)
    if change == "allowlist":
        h.policy = replace(h.policy, allowed_users=())
    elif change == "binding":
        h.policy = replace(h.policy, bindings=(replace(h.policy.bindings[0], consent_ref="new-consent"),))
    elif change == "group":
        h.stream.update(group_id="synthetic-group", is_group_session=True, chat_type="group")
    elif change == "host":
        h.response = {"success": False, "stream": h.stream}
    elif change == "grant":
        h.snapshot = replace(h.snapshot, authorized=False)
    else:
        h.snapshot = replace(h.snapshot, scope=OTHER)
    assert await h.chat.before_replyer_request(**reply_payload()) is None


async def test_policy_change_during_replyer_read_prevents_data_exposure(sdk):  # noqa: F811
    h = Harness(sdk, planner=False)
    note(h)

    async def revoke():
        h.policy = replace(h.policy, allowed_users=())

    h.read_hook = revoke
    assert await h.chat.before_replyer_request(**reply_payload()) is None


async def test_same_note_cannot_be_used_concurrently_or_retried_after_failure(sdk):  # noqa: F811
    h = Harness(sdk, planner=False)
    note(h)
    entered, release = asyncio.Event(), asyncio.Event()

    async def waiting_read():
        entered.set()
        await release.wait()

    h.read_hook = waiting_read
    first = asyncio.create_task(h.chat.before_replyer_request(**reply_payload()))
    await asyncio.wait_for(entered.wait(), 1)
    assert await h.chat.before_replyer_request(**reply_payload()) is None
    release.set()
    assert await first
    assert len(h.calls) == 1
    h = Harness(sdk, planner=False)
    note(h)

    async def fail():
        raise RuntimeError("synthetic failure")

    h.read_hook = fail
    assert await h.chat.before_replyer_request(**reply_payload()) is None
    h.read_hook = None
    assert await h.chat.before_replyer_request(**reply_payload()) is None
    assert len(h.calls) == 1


async def test_replyer_cancellation_keeps_note_consumed_and_never_sends(sdk):  # noqa: F811
    h = Harness(sdk, planner=False)
    note(h)
    entered = asyncio.Event()

    async def wait():
        entered.set()
        await asyncio.Event().wait()

    h.read_hook = wait
    task = asyncio.create_task(h.chat.before_replyer_request(**reply_payload()))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await h.chat.before_replyer_request(**reply_payload()) is None
    assert len(h.calls) == 1


async def test_reload_starts_without_old_correlations_and_both_hooks_register(sdk):  # noqa: F811
    h = Harness(sdk, planner=False)
    note(h)
    h.chat.clear_queries()
    new_chat = sdk.chat.StudioChat(h.ctx, policy=lambda: h.policy, read=h.read)

    class Plugin:
        studio_chat = new_chat
        studio_chat_before_request = sdk.chat.studio_chat_before_request
        studio_chat_before_replyer_request = sdk.chat.studio_chat_before_replyer_request

    components = sdk.collect(Plugin())
    assert {item["metadata"]["hook"] for item in components} == {
        sdk.chat.HOOK_NAME, sdk.chat.REPLYER_HOOK_NAME}
    assert all(item["type"] == "HOOK_HANDLER" for item in components)
    assert await Plugin().studio_chat_before_replyer_request(**reply_payload()) is None
    assert h.calls == h.reads == []


@pytest.mark.parametrize("ttl", [True, 0, -1, 121, float("inf"), "60"])
def test_note_ttl_is_bounded(sdk, ttl):  # noqa: F811
    with pytest.raises(ValueError):
        sdk.chat.StudioChat(None, policy=lambda: None, read=None, query_ttl_seconds=ttl)


@pytest.mark.parametrize("text,pure", [("鲸鱼娘画了吗", True), ("上次那幅分享了吗", True),
                                      ("鲸鱼娘画了吗，背景换星空", False), ("请画鲸鱼娘", False)])
async def test_integrated_receive_hook_notes_only_real_pure_query(sdk, text, pure):  # noqa: F811
    h = Harness(sdk, planner=False)
    plugin = CodexImagePlugin()
    config = plugin.build_default_config()
    config["plugin"]["enabled"] = config["autonomy"]["enabled"] = True
    plugin.set_plugin_config(config)
    plugin.studio_chat = h.chat
    ingresses = []

    async def on_chat(*args):
        ingresses.append(args)

    plugin.artist_runtime = SimpleNamespace(on_chat=on_chat)
    await plugin.artist_chat(
        platform=SCOPE.platform, stream_id=SCOPE.stream_id, user_id=SCOPE.user_id,
        message_id=MESSAGE_ID, message={"processed_plain_text": text, "message_id": MESSAGE_ID,
                                       "platform": SCOPE.platform, "session_id": SCOPE.stream_id,
                                       "message_info": {"user_info": {"user_id": SCOPE.user_id}}},
    )
    assert bool(ingresses) is not pure
    assert h.chat._queries[(SCOPE, MESSAGE_ID)].is_query is pure
    assert h.calls == h.reads == []
    assert (await h.chat.before_replyer_request(**reply_payload()) is not None) is pure


@pytest.fixture
def replyer_host(host):  # noqa: F811
    """Extract the actual replyer invoke + deserialize method, no application imports."""
    path = host.root / "chat/replyer/maisaka_generator_base.py"
    tree = ast.parse(path.read_text("utf-8-sig"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == "BaseMaisakaReplyGenerator")
    cls.bases = []
    cls.body = [node for node in cls.body if getattr(node, "name", None) == "_invoke_before_model_request_hook"]
    # Read the real constant used by the Host payload rather than replacing it.
    constant = next(node for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "REPLYER_MAX_HOOK_RETRIES"
                            for target in node.targets))
    host.ns["logger"] = logging.getLogger("isolated-studio-replyer")
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, constant, cls], type_ignores=[])),
                 str(path), "exec"), host.ns)
    return host.ns["BaseMaisakaReplyGenerator"]


@pytest.mark.parametrize("match", [True, False])
async def test_real_replyer_host_payload_roundtrip_reaches_visible_model_request(
        sdk, host, replyer_host, match):  # noqa: F811
    h = Harness(sdk, planner=False)
    note(h)
    ci = host.context_item
    original_question = ci.ContextItemBuilder().set_role(ci.RoleType.User).add_text_content("鲸鱼娘画了吗").build()
    internal_prompt = ci.ContextItemBuilder().set_role(ci.RoleType.User).add_text_content(
        "请依据上下文生成面向用户的正文；这不是纯状态问题的原始文本。").build()
    original = [original_question, internal_prompt]
    captured = []

    async def invoke_hook(name, **kwargs):
        assert name == sdk.chat.REPLYER_HOOK_NAME
        captured.append(deepcopy(kwargs))
        registry = host.registry.HookSpecRegistry()
        spec = next(item for item in host.ns["register_maisaka_hook_specs"](registry) if item.name == name)
        assert set(spec.parameters_schema["required"]) <= kwargs.keys()
        assert spec.allow_kwargs_mutation
        result = await sdk.chat.studio_chat_before_replyer_request(SimpleNamespace(studio_chat=h.chat), **kwargs)
        return SimpleNamespace(kwargs=result["modified_kwargs"] if result else kwargs)

    instance = replyer_host()
    instance.request_type = "maisaka.replyer"
    instance._get_runtime_manager = lambda: SimpleNamespace(invoke_hook=invoke_hook)
    target_id = MESSAGE_ID if match else "different-real-host-message"
    actual = await instance._invoke_before_model_request_hook(
        request_messages=original, session_id=SCOPE.stream_id, active_task_name="replyer",
        active_model_name=None, model_info=SimpleNamespace(name="synthetic-model", visual=False),
        attempt=1, retry_count=0, reply_message=SimpleNamespace(message_id=target_id),
        reply_reason="", selected_expression_ids=[],
        reply_tool_args={"reply_message_id": MESSAGE_ID, "session_id": SCOPE.stream_id,
                         "facts": "模型伪造：鲸鱼娘已发送", "retry": True},
    )
    assert captured[0]["reply_message_id"] == target_id
    assert captured[0]["selected_model_name"] == "synthetic-model"
    assert len(actual) == len(original) + (2 if match else 0)
    assert actual[0] is original_question and actual[1] is internal_prompt
    if match:
        assert type(actual[-2]).__name__ == "SystemMessageItem"
        assert type(actual[-1]).__name__ == "UserMessageItem"
        assert json.loads(actual[-1].parts[0].text)["data"] == h.snapshot.data
        assert len(h.calls) == len(h.reads) == 1
    else:
        assert h.calls == h.reads == []
