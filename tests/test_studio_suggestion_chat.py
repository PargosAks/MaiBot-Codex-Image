"""Trusted new ingress -> minimal private receipt; no live model/image/send."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import asyncio
import hashlib
import json

import pytest

from maibot_sdk.context import PluginContext

from image_plugin_under_test.host import studio_chat as chat_module
from image_plugin_under_test.domain.policy import Delegation
from image_plugin_under_test.workspace.studio import Studio
from support.studio_chat import active_studio_plugin as active_studio_plugin, Harness, OTHER, SCOPE, payload, sdk as sdk


MESSAGE = "full-trusted-suggestion-message-id"
ORIGINAL = "你要不要试试按照之前的角色设定画一张湖边散步的图，色彩柔和一些？"
POSITIVE = [
    ORIGINAL, "按照之前的角色设定图画一个在湖边散步的场景", "帮我画猫", "请画鲸鱼娘",
    "给我画一张头像", "画一个在湖边散步的场景", "画只小猫", "再画一只鲸鱼娘",
    "参考之前的设定画一张图", "根据之前的角色设定绘制一张头像", "我想看你画小猫",
    "可以帮我画一张猫咪吗？", "你能不能画一只猫？", "能不能帮我画一只猫？",
    "你要不要画个猫？", "要不要试试画个猫？", "可不可以帮我画猫", "帮我生成一张图片",
    "帮我生成图片", "请生成一张头像", "帮我创作一幅插画",
]
NEGATIVE = [
    "别画了", "我画了一张图", "你觉得这幅画怎么样", "这个计划里有一个图画一个表",
    "不要画猫", "能不能不要画猫", "你要不要试试不要画猫", "你不能画猫", "不需要帮我画猫",
    "别帮我画猫", "不用画了", "先别画猫", "取消画图", "请不要画猫", "请画猫，但不要现在画",
    "今天讨论画图", "这幅画真好看", "画字怎么写", "绘画是我的爱好", "我想学画画",
    "帮我生成代码", "请生成一个计划", "帮我生成一张表", "请问这个计划怎么样", "明天见",
    "你画了吗", "现在在画什么",
    "/画图 猫", " /画图 猫", "请用 /画图 画猫", "!画图 猫", "#画图 猫", "画猫\n不要画了",
    "", None, {}, "画一个" + "猫" * 512,
]


class SuggestionHarness(Harness):
    def __init__(self, sdk_api):
        super().__init__(sdk_api, planner=False)
        self.clock = 0.0
        self.chat._query_clock = lambda: self.clock
        self.receipts, self.receipt_hook = [], None
        self.receipt = sdk_api.chat.StudioChatSnapshot("studio", SCOPE, 1, True, {"inspiration_received": True})
        self.chat.read_receipt = self.read_receipt

    async def read_receipt(self, space_id, scope, message_id):
        assert (space_id, scope, message_id) == ("studio", SCOPE, MESSAGE)
        self.receipts.append((space_id, scope, message_id))
        if self.receipt_hook:
            await self.receipt_hook()
        return self.receipt

    def arm(self):
        assert self.chat.note_ingress(SCOPE, MESSAGE, is_query=False, is_suggestion=True)
        assert self.chat.note_suggestion_accepted(SCOPE, MESSAGE)

    async def invoke(self, stage):
        if stage == "planner":
            return await self.chat.before_request(**payload(ORIGINAL, message_id=MESSAGE))
        return await self.chat.before_replyer_request(
            session_id=SCOPE.stream_id, reply_message_id=MESSAGE, item_schema_version=1,
            items=[{"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "正常回复当前用户"}]}],
            reply_tool_args={"inspiration_received": True, "scope": "forged", "facts": "插件故障"},
        )


def integrated_plugin(plugin, h, on_chat):
    plugin.studio_chat = h.chat
    plugin.artist_runtime.on_chat = on_chat
    return plugin


async def ingress(plugin, text=ORIGINAL, **extra):
    await plugin.artist_chat(
        platform=SCOPE.platform, stream_id=SCOPE.stream_id, user_id=SCOPE.user_id,
        message_id=MESSAGE, message={"processed_plain_text": text, **extra},
    )


@pytest.mark.parametrize("text", POSITIVE)
def test_conservative_known_drawing_requests_including_complete_original(sdk, text):  # noqa: F811
    assert sdk.chat.is_studio_suggestion_message({"processed_plain_text": text})


@pytest.mark.parametrize("text", NEGATIVE)
async def test_negation_narrative_critique_commands_and_unrelated_never_inject(active_studio_plugin, sdk, text):  # noqa: F811
    h = SuggestionHarness(sdk)
    assert not sdk.chat.is_studio_suggestion_message({"processed_plain_text": text})

    async def on_chat(*args):
        return True  # Even a new ordinary wake is not a suggestion attestation.

    await ingress(integrated_plugin(active_studio_plugin, h, on_chat), text)
    result = await h.invoke("replyer")
    if sdk.chat.is_studio_status_query(text):
        assert json.loads(result["modified_kwargs"]["items"][-1]["parts"][0]["text"])["data"] == h.snapshot.data
        assert len(h.reads) == 1  # Pure status queries keep the original read-only path.
    else:
        assert result is None and h.reads == []
    assert h.receipts == []


@pytest.mark.parametrize("extra", [
    {"is_command": True}, {"is_notify": True}, {"raw_message": "malformed"},
    {"raw_message": [{"type": "image"}]}, {"raw_message": [{"type": "text"}, {"type": "reply"}]},
])
def test_nonordinary_events_keep_normal_path(sdk, extra):  # noqa: F811
    assert not sdk.chat.is_studio_suggestion_message({"processed_plain_text": ORIGINAL, **extra})


async def test_original_at_71_seconds_in_both_hooks_preserves_payload_and_only_minimal_data(sdk):  # noqa: F811
    h = SuggestionHarness(sdk)
    h.arm()
    h.clock = 71.0
    args = payload(ORIGINAL, message_id=MESSAGE, model_options={"temperature": 0.4})
    original = deepcopy(args)
    result = await h.chat.before_request(**args)
    assert args == original
    assert result["modified_kwargs"]["items"][:-2] == original["items"]
    assert result["modified_kwargs"]["model_options"] == original["model_options"]
    assert result["modified_kwargs"]["built_message_count"] == len(original["items"]) + 2
    reply = await h.invoke("replyer")
    for actual in (result, reply):
        rule, data = actual["modified_kwargs"]["items"][-2:]
        assert rule["parts"][0]["text"] == sdk.chat.SUGGESTION_DATA_RULE
        assert json.loads(data["parts"][0]["text"]) == {
            "source": "codex_image_studio_readonly", "data": {"inspiration_received": True},
        }
    assert len(h.receipts) == len(h.calls) == 2 and h.reads == []
    assert await h.invoke("planner") is None
    assert await h.invoke("replyer") is None


def test_receipt_rule_separates_evidence_and_explicitly_allows_normal_reply(sdk):  # noqa: F811
    rule = sdk.chat.SUGGESTION_DATA_RULE
    for required in ("灵感已入账", "不是故障诊断证据", "额度余额", "已出图", "作品档案",
                     "不得据此宣称插件未连接、插件故障或绘图功能不可用", "不得要求用户改用 /画图",
                     "正常聊天链路使用 reply", "不要据此调用绘图、重试或分享图片工具"):
        assert required in rule
    assert "不要发送" not in rule and "不要据此调用工具" not in rule


@pytest.mark.parametrize("accepted", [True, False, None, 1, "true", {"accepted": True}])
async def test_real_plugin_gate_requires_exact_true_new_ingress(active_studio_plugin, sdk, accepted):  # noqa: F811
    h = SuggestionHarness(sdk)

    async def on_chat(*args):
        return accepted

    await ingress(integrated_plugin(active_studio_plugin, h, on_chat))
    assert (await h.invoke("planner") is not None) is (accepted is True)
    assert (await h.invoke("replyer") is not None) is (accepted is True)
    assert len(h.receipts) == (2 if accepted is True else 0) and h.reads == []


@pytest.mark.parametrize("newer", [False, True])
async def test_delayed_true_at_71_seconds_cannot_accept_a_superseded_message(active_studio_plugin, sdk, newer):  # noqa: F811
    h = SuggestionHarness(sdk)

    async def on_chat(*args):
        h.clock = 71
        if newer:
            assert h.chat.note_ingress(SCOPE, "newer-during-ingress", is_query=False)
        return True

    await ingress(integrated_plugin(active_studio_plugin, h, on_chat))
    assert h.chat._queries[(SCOPE, MESSAGE)].deadline == 120.0
    assert (await h.invoke("planner") is not None) is (not newer)
    assert (await h.invoke("replyer") is not None) is (not newer)


async def test_missing_reader_does_not_allow_recovery_of_an_old_suggestion(sdk):  # noqa: F811
    h = SuggestionHarness(sdk)
    h.arm()
    h.chat.read_receipt = None
    assert await h.invoke("planner") is None
    h.chat.read_receipt = h.read_receipt
    assert await h.invoke("replyer") is None
    assert h.receipts == h.calls == []


@pytest.mark.parametrize("stage", ["planner", "replyer"])
@pytest.mark.parametrize("missing", ["ingress", "accepted", "reader", "reload", "late-binding"])
async def test_database_and_new_bindings_cannot_reconstruct_missing_attestation(sdk, stage, missing):  # noqa: F811
    h = SuggestionHarness(sdk)
    if missing == "late-binding":
        policy = h.policy
        h.policy = replace(h.policy, bindings=())
        assert not h.chat.note_ingress(SCOPE, MESSAGE, is_query=False, is_suggestion=True)
        h.policy = policy
        assert not h.chat.note_suggestion_accepted(SCOPE, MESSAGE)
    elif missing == "ingress":
        assert not h.chat.note_suggestion_accepted(SCOPE, MESSAGE)
    else:
        assert h.chat.note_ingress(SCOPE, MESSAGE, is_query=False, is_suggestion=True)
        if missing != "accepted":
            assert h.chat.note_suggestion_accepted(SCOPE, MESSAGE)
        if missing == "reader":
            h.chat.read_receipt = None
        elif missing == "reload":
            h.chat.clear_queries()
    assert await h.invoke(stage) is None
    assert h.receipts == h.reads == h.calls == []


async def test_duplicate_ingress_does_not_rearm_reorder_or_extend_ttl(active_studio_plugin, sdk):  # noqa: F811
    h = SuggestionHarness(sdk)
    calls = []

    async def on_chat(*args):
        calls.append(args)
        return len(calls) == 1

    plugin = integrated_plugin(active_studio_plugin, h, on_chat)
    await ingress(plugin)
    h.clock = 71.0
    await ingress(plugin)
    assert h.chat._queries[(SCOPE, MESSAGE)].deadline == 120.0
    assert await h.invoke("planner")
    assert await h.invoke("replyer")
    await ingress(plugin)
    assert await h.invoke("planner") is None
    assert await h.invoke("replyer") is None
    assert len(h.receipts) == 2


@pytest.mark.parametrize("stage", ["planner", "replyer"])
@pytest.mark.parametrize("age,valid", [(71, True), (119.999, True), (120, False), (121, False)])
async def test_suggestion_has_exclusive_120_second_deadline(sdk, stage, age, valid):  # noqa: F811
    h = SuggestionHarness(sdk)
    h.arm()
    h.clock = age
    assert (await h.invoke(stage) is not None) is valid


async def test_query_and_unrelated_event_ttls_are_unchanged(sdk):  # noqa: F811
    h = SuggestionHarness(sdk)
    assert h.chat.note_query(SCOPE, "old-query")
    assert h.chat.note_ingress(SCOPE, "ordinary", is_query=False)
    assert h.chat._queries[(SCOPE, "old-query")].deadline == 60.0
    assert h.chat._queries[(SCOPE, "ordinary")].deadline == 60.0
    h.chat.query_ttl_seconds = 30
    assert h.chat.note_query(SCOPE, "configured-query")
    assert h.chat._queries[(SCOPE, "configured-query")].deadline == 30.0
    h.clock = 30
    assert await h.chat.before_request(**payload(message_id="configured-query")) is None


@pytest.mark.parametrize("stage", ["planner", "replyer"])
async def test_new_shorter_lived_message_permanently_supersedes_suggestion(sdk, stage):  # noqa: F811
    h = SuggestionHarness(sdk)
    h.arm()
    h.clock = 1
    assert h.chat.note_ingress(SCOPE, "new-unrelated", is_query=False)
    assert not h.chat.note_ingress(SCOPE, MESSAGE, is_query=False, is_suggestion=True)
    h.clock = 71  # New event expired, original suggestion has not: never resurface it.
    assert await h.invoke(stage) is None
    assert not h.chat.note_suggestion_accepted(SCOPE, MESSAGE)
    assert h.receipts == h.calls == []


@pytest.mark.parametrize("stage", ["planner", "replyer"])
@pytest.mark.parametrize("data", [None, {}, {"inspiration_received": False}, {"inspiration_received": 1},
                                 {"inspiration_received": "true"},
                                 {"inspiration_received": True, "fault": "private error"}])
async def test_receipt_data_is_exact_strict_and_failure_cannot_retry_other_stage(sdk, stage, data):  # noqa: F811
    h = SuggestionHarness(sdk)
    h.arm()
    h.receipt = replace(h.receipt, data=data)
    assert await h.invoke(stage) is None
    h.receipt = replace(h.receipt, data={"inspiration_received": True})
    assert await h.invoke("planner") is None
    assert await h.invoke("replyer") is None
    assert len(h.receipts) == 1 and h.reads == []


@pytest.mark.parametrize("change", [{"space_id": "foreign"}, {"scope": OTHER}, {"authorized": False},
                                    {"authorized": 1}, {"auth_version": 0}, {"auth_version": True}])
async def test_fresh_receipt_must_match_current_owner_and_delegation(sdk, change):  # noqa: F811
    h = SuggestionHarness(sdk)
    h.arm()
    h.receipt = replace(h.receipt, **change)
    assert await h.invoke("planner") is None
    assert await h.invoke("replyer") is None


@pytest.mark.parametrize("change", ["whitelist", "binding", "group", "disabled"])
async def test_observed_policy_revocation_or_rebinding_cannot_be_restored_into_old_receipt(sdk, change):  # noqa: F811
    h = SuggestionHarness(sdk)
    h.arm()
    policy = h.policy
    if change == "whitelist":
        h.policy = replace(h.policy, allowed_users=())
    elif change == "disabled":
        h.policy = replace(h.policy, enabled=False)
    else:
        field = {"consent_ref": "new-delegation"} if change == "binding" else {"group_id": "group"}
        h.policy = replace(h.policy, bindings=(replace(h.policy.bindings[0], **field),))
    assert await h.invoke("planner") is None
    h.policy = policy
    assert await h.invoke("replyer") is None
    assert h.receipts == h.calls == []


async def test_replyer_does_not_reuse_planner_delegation_version(sdk):  # noqa: F811
    h = SuggestionHarness(sdk)
    h.arm()
    assert await h.invoke("planner")
    h.receipt = replace(h.receipt, auth_version=2)
    assert await h.invoke("replyer") is None
    assert len(h.receipts) == 2


@pytest.mark.parametrize("stage", ["planner", "replyer"])
@pytest.mark.parametrize("action", ["new-message", "clear", "expire", "revoke", "error", "timeout"])
async def test_read_races_and_failures_invalidate_both_stages(sdk, stage, action, caplog):  # noqa: F811
    h = SuggestionHarness(sdk)
    h.arm()

    async def interleave():
        if action == "new-message":
            h.chat.note_ingress(SCOPE, "new-im-during-read", is_query=False)
        elif action == "clear":
            h.chat.clear_queries()
        elif action == "expire":
            h.clock = 120
        elif action == "revoke":
            h.policy = replace(h.policy, allowed_users=())
        elif action == "error":
            raise RuntimeError("private-state-must-not-be-logged")
        else:
            await asyncio.Event().wait()

    h.receipt_hook = interleave
    h.chat.timeout_seconds = .01
    assert await h.invoke(stage) is None
    h.receipt_hook = None
    assert await h.invoke("planner") is None
    assert await h.invoke("replyer") is None
    assert "private-state-must-not-be-logged" not in caplog.text
    assert h.reads == []


@pytest.mark.parametrize("first,second", [("planner", "planner"), ("replyer", "replyer"),
                                          ("planner", "replyer"), ("replyer", "planner")])
async def test_concurrent_suggestion_claims_conservatively_invalidate_both(sdk, first, second):  # noqa: F811
    h = SuggestionHarness(sdk)
    h.arm()
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()

    h.receipt_hook = hold
    task = asyncio.create_task(h.invoke(first))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert await h.invoke(second) is None
    finally:
        release.set()
    assert await task is None
    assert await h.invoke("replyer") is None
    assert len(h.receipts) == 1


@pytest.mark.parametrize("stage", ["planner", "replyer"])
async def test_cancellation_does_not_rearm_either_hook(sdk, stage):  # noqa: F811
    h = SuggestionHarness(sdk)
    h.arm()
    entered = asyncio.Event()

    async def wait():
        entered.set()
        await asyncio.Event().wait()

    h.receipt_hook = wait
    task = asyncio.create_task(h.invoke(stage))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await h.invoke("planner") is None
    assert await h.invoke("replyer") is None


@pytest.mark.parametrize("stage", ["planner", "replyer"])
@pytest.mark.parametrize("denial", ["host-group", "host-error", "foreign-session", "foreign-message", "schema"])
async def test_host_identity_and_target_are_required_independently_of_model_arguments(sdk, stage, denial):  # noqa: F811
    h = SuggestionHarness(sdk)
    h.arm()
    if denial == "host-group":
        h.stream.update(group_id="group", is_group_session=True, chat_type="group")
    elif denial == "host-error":
        h.response = {"success": 1, "stream": h.stream}
    args = payload(ORIGINAL, message_id="foreign" if denial == "foreign-message" else MESSAGE)
    if stage == "replyer":
        args["reply_message_id"] = "foreign" if denial == "foreign-message" else MESSAGE
    if denial == "foreign-session":
        args["session_id"] = OTHER.stream_id
    elif denial == "schema":
        args["item_schema_version"] = True
    args["reply_tool_args"] = {"reply_message_id": MESSAGE, "session_id": SCOPE.stream_id, "accepted": True}
    hook = h.chat.before_request if stage == "planner" else h.chat.before_replyer_request
    assert await hook(**args) is None
    assert h.receipts == h.reads == []


@pytest.mark.parametrize("ttl", [True, 0, -1, 121, float("inf"), float("nan"), "120"])
def test_suggestion_ttl_is_bounded(sdk, ttl):  # noqa: F811
    with pytest.raises(ValueError):
        sdk.chat.StudioChat(None, policy=lambda: None, read=None, suggestion_ttl_seconds=ttl)


async def test_real_plugin_receipt_adapter_reads_exact_sqlite_row_without_side_effects(active_studio_plugin):
    # The real plugin and adapter share one module identity. The optional SDK
    # fixture reloads only studio_chat, so test that fixture separately above.
    h = SuggestionHarness(SimpleNamespace(chat=chat_module, context=PluginContext))
    plugin = active_studio_plugin
    runtime = plugin.artist_runtime
    store = runtime.store
    store.create_space("studio", SCOPE, binding_id="isolated-studio")
    store.grant("studio", SCOPE, Delegation("explicit-consent", "synthetic-admin", True))

    async def on_chat(scope, message_id, message):
        return store.enqueue_wake("studio", SCOPE, event_id="chat:" + message_id, kind="chat",
                                  source_ref=message_id, text=message["processed_plain_text"])

    integrated_plugin(plugin, h, on_chat)
    runtime.grant_versions = {"studio": 1}
    h.chat.read_receipt = plugin._studio_suggestion_receipt
    await ingress(plugin)
    # 生命周期启动时已创建 SQLite 和租约文件；只读调用不能再新增文件或修改账本。
    ledger = store.path
    root = ledger.parent
    before = hashlib.sha256(ledger.read_bytes()).digest()
    files = {path.name for path in root.iterdir()}
    h.clock = 71
    assert await h.invoke("planner")
    assert await h.invoke("replyer")
    assert hashlib.sha256(ledger.read_bytes()).digest() == before
    assert {path.name for path in root.iterdir()} == files
    receipt = Studio(root, scopes={"studio": SCOPE}).inspiration_receipt(
        "studio", MESSAGE, expected_auth_version=1)
    assert receipt["inspiration_received"] is True
    assert h.reads == []
