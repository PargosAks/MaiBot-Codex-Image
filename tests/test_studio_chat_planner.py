"""STUDIO-R1-01: complete real Host planner construction, no live Host/model."""

from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
import ast
import json

import pytest

from support.studio_chat import Harness, OTHER, SCOPE, sdk as sdk
from support.studio_chat_host import host as host, load_nodes


pytestmark = pytest.mark.external_contract


@pytest.fixture
def planner_host(host):  # noqa: F811
    ns = host.ns
    ns.update(dataclass=dataclass, field=field,
              defer_context_image_encoding=lambda projector: lambda method: method,
              global_config=SimpleNamespace(visual=SimpleNamespace(image_quality="synthetic")),
              IMAGE_QUALITY_MAX_SIDE={"synthetic": 1024})
    # Real history classes, real history->Item conversion, real prefix formatter.
    load_nodes(ns, host.root / "maisaka/context/messages.py", {
        "LLMContextMessage", "ContentContextMessage", "SessionBackedMessage",
        "build_context_items_from_history_entry",
    })
    path = host.root / "maisaka/chat_loop_service.py"
    tree = ast.parse(path.read_text("utf-8-sig"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == "MaisakaChatLoopService")
    names = {"_build_request_messages", "_build_time_user_message", "_build_current_time_user_message",
             "_append_time_user_message", "_validate_function_call_context_anchors",
             "_build_current_chat_attention_tail_message", "_build_planner_final_user_reminder"}
    cls.body = [node for node in cls.body if getattr(node, "name", None) in names]
    cls.bases = []
    constants = [node for node in tree.body if isinstance(node, ast.Assign) and any(
        getattr(target, "id", None) in {"PLANNER_FINAL_USER_REMINDER", "PASSIVE_PLANNER_FINAL_USER_REMINDERS",
                                         "FOCUS_PLANNER_FINAL_USER_REMINDER_TEMPLATE"} for target in node.targets)]
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *constants, cls], type_ignores=[])),
                 str(path), "exec"), ns)
    service = ns["MaisakaChatLoopService"]()
    service._session_id, service._is_group_chat = SCOPE.stream_id, False
    service._custom_chat_system_prompt = None
    service._get_chat_prompt_name = lambda: "maisaka_chat"
    service._build_bot_nickname_notice = lambda *a, **kw: ""
    service._get_chat_prompt_for_chat = lambda *a: "仅按真实事实回答；此提醒不是用户消息。"
    return SimpleNamespace(service=service, ns=ns)


def im(planner, text, *, message_id="synthetic-msg", session_id=None, self_message=False, day=2):
    ns = planner.ns
    when = datetime(2026, 11, day, 12, 34, 56)
    prefix = ns["build_planner_prefix"](
        timestamp=when, user_name='display name "quoted" <tags>', message_id=message_id,
        is_self_message=self_message,
    )
    if session_id is not None:
        prefix = prefix.replace('<message ', f'<message session_id="{session_id}" ', 1)
    sequence = SimpleNamespace(components=[ns["TextComponent"](prefix + text)])
    return ns["SessionBackedMessage"](
        raw_message=sequence, visible_text=text, timestamp=when, message_id=message_id,
        original_message=SimpleNamespace(message_id=message_id),
    )


def request(planner, history, *, final=True, **extras):
    service = planner.service
    built, count = service._build_request_messages(
        history, enable_visual_message=False, include_day_boundary_time_messages=True,
        system_prompt="Synthetic system", final_user_message=service._build_planner_final_user_reminder() if final else None,
        **extras,
    )
    return {"session_id": SCOPE.stream_id, "items": planner.ns["serialize_prompt_items"](built),
            "item_schema_version": planner.ns["CONTEXT_ITEM_SCHEMA_VERSION"],
            "selected_history_count": len(history), "built_message_count": len(built),
            "tool_definitions": [], "selection_reason": "real complete builder"}, count


@pytest.mark.parametrize("final", [False, True])
async def test_r101_complete_host_time_reminders_and_real_ingress_target(sdk, planner_host, final):  # noqa: F811
    h = Harness(sdk)
    before, count = request(planner_host, [im(planner_host, "鲸鱼娘画了吗")], final=final,
                            injected_user_messages=["受信 Host 当轮补充资料（不是 IM）。"],
                            tail_user_messages=["受信 Host 当轮等待提醒（不是 IM）。"])
    assert count == 1
    assert all(item["item_type"] == "UserMessageItem" for item in before["items"][1:])
    assert before["items"][2]["parts"][0]["text"].startswith("受信 Host")
    assert any(item["parts"][0]["text"].startswith("时间：") for item in before["items"])
    if final:
        assert before["items"][-1]["parts"][0]["text"] == planner_host.ns["PLANNER_FINAL_USER_REMINDER"]
    result = await h.chat.before_request(**before)
    assert result is not None
    assert result["modified_kwargs"]["items"][:-2] == before["items"]
    data = json.loads(result["modified_kwargs"]["items"][-1]["parts"][0]["text"])
    assert data["data"] == h.snapshot.data and len(h.reads) == len(h.calls) == 1
    assert await h.chat.before_request(**before) is None  # Same history, later/duplicate round.
    assert len(h.reads) == 1


@pytest.mark.parametrize("barrier", ["unrelated", "mixed", "self", "other-session", "image", "assistant"])
async def test_r101_never_searches_past_a_later_real_message_or_output(sdk, planner_host, barrier):  # noqa: F811
    h = Harness(sdk)
    old = im(planner_host, "你画了吗", day=1)
    later = im(planner_host, "明天见", message_id="later-real-message")
    if barrier == "mixed":
        later = im(planner_host, "鲸鱼娘画了吗，背景改星空", message_id="later-real-message")
    elif barrier == "self":
        later = im(planner_host, "你画了吗", self_message=True)
    elif barrier == "other-session":
        later = im(planner_host, "你画了吗", session_id=OTHER.stream_id)
    elif barrier in {"image", "assistant"}:
        before, _ = request(planner_host, [old])
        ci = planner_host.ns
        if barrier == "image":
            blocked = ci["ContextItemBuilder"]().set_role(ci["RoleType"].User).add_text_content("你画了吗").build()
            raw = ci["serialize_prompt_items"]([blocked])[0]
            raw["parts"].append({"type": "image", "image_format": "png", "image_base64": "synthetic"})
        else:
            blocked = ci["ContextItemBuilder"]().set_role(ci["RoleType"].Assistant).add_text_content("已答复").build()
            raw = ci["serialize_prompt_items"]([blocked])[0]
        before["items"].insert(2, raw)
        assert await h.chat.before_request(**before) is None
        assert h.calls == h.reads == []
        return
    before, _ = request(planner_host, [old, later])
    assert await h.chat.before_request(**before) is None
    assert h.calls == h.reads == []


async def test_r101_ingress_unrelated_event_invalidates_even_omitted_selected_history(sdk, planner_host):  # noqa: F811
    h = Harness(sdk)
    before, _ = request(planner_host, [im(planner_host, "你画了吗")])
    assert h.chat.note_ingress(SCOPE, "new-unrelated-im", is_query=False)
    assert await h.chat.before_request(**before) is None
    assert h.calls == h.reads == []
    assert await h.chat.before_replyer_request(
        session_id=SCOPE.stream_id, reply_message_id="synthetic-msg", item_schema_version=1, items=[]
    ) is None


@pytest.mark.parametrize("denial", ["no-note", "expired", "cleared", "different-full-id", "no-im", "bare-query"])
async def test_r101_only_pending_live_full_ingress_id_can_authorize(sdk, planner_host, denial):  # noqa: F811
    h = Harness(sdk, planner=False)
    clock = [0.0]
    h.chat._query_clock = lambda: clock[0]
    target = "synthetic-msg" if denial != "different-full-id" else "another-platform-id"
    if denial != "no-note":
        assert h.chat.note_query(SCOPE, target)
    if denial == "expired":
        clock[0] = 60.0
    elif denial == "cleared":
        h.chat.clear_queries()
    history = [] if denial in {"no-im", "bare-query"} else [im(planner_host, "你画了吗")]
    before, _ = request(planner_host, history, injected_user_messages=["你画了吗"] if denial == "bare-query" else None)
    assert await h.chat.before_request(**before) is None
    assert h.calls == h.reads == []


async def test_r101_new_valid_question_and_alias_match_real_host_mapping(sdk, planner_host):  # noqa: F811
    h = Harness(sdk)
    message_id = "ROBOT1.0_long-real-platform-message-id" + "x" * 80
    assert h.chat.note_query(SCOPE, message_id)
    assert sdk.chat._display_message_id(message_id) == planner_host.ns["to_display_message_id"](message_id)
    before, _ = request(planner_host, [im(planner_host, "你画了吗", day=1),
                                     im(planner_host, "现在在画什么", message_id=message_id)])
    assert await h.chat.before_request(**before)


async def test_r101_new_ingress_while_read_is_awaiting_prevents_stale_injection(sdk, planner_host):  # noqa: F811
    h = Harness(sdk)

    async def unrelated():
        h.chat.note_ingress(SCOPE, "new-while-reading", is_query=False)

    h.read_hook = unrelated
    before, _ = request(planner_host, [im(planner_host, "你画了吗")])
    assert await h.chat.before_request(**before) is None


async def test_r101_real_host_short_alias_collision_fails_closed(sdk, planner_host):  # noqa: F811
    h = Harness(sdk, planner=False)
    first, second = "synthetic-message-collision-4313", "synthetic-message-collision-19269"
    assert planner_host.ns["to_display_message_id"](first) == planner_host.ns["to_display_message_id"](second)
    assert h.chat.note_query(SCOPE, first)
    assert h.chat.note_query(SCOPE, second)
    before, _ = request(planner_host, [im(planner_host, "你画了吗", message_id=second)])
    assert await h.chat.before_request(**before) is None
    assert h.calls == h.reads == []
