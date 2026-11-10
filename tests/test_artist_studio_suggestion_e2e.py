"""Suggestion receipts through the fixed public Host fixtures and real SQLite.

Shares the explicit CODEX_IMAGE_TEST_HOST_REPO contract with the Studio acceptance suite.
Images, model boundaries and platform effects are synthetic; no live Host,
subscription, image recognition or production service is used.

Receipt recognition is limited to the explicit pure-text request grammar.
Attached suggestions and mixed status/creative questions retain normal wake
and separately authorized media ingestion, without a receipt in ordinary model
context. These tests prove storage and context boundaries, not visual analysis,
downstream reference use, or what a real model ultimately says to the user.
"""

from copy import deepcopy
import base64
import hashlib
import json

import pytest

from image_plugin_under_test.host.host_media import UNREVIEWED_DESCRIPTION
from support.studio_acceptance import (
    SCOPE,
    SPACE,
    source_snapshot as source_snapshot,
    state_snapshot,
    studio_chain as studio_chain,
)
from support.studio_acceptance_host import (
    build_planner_request,
    planner_host as planner_host,
    public_host as public_host,
    replyer_host as replyer_host,
)
from support.catalog_examples import PROFILES
from support.media_samples import image_bytes
from support import host_chain as host_fixture


SUGGESTION = "你要不要试试按照之前的角色设定画一张湖边散步的图，色彩柔和一些？"
PRIVATE_TITLE = "private-profile-must-not-enter-suggestion-receipt"
RECEIPT = {"source": "codex_image_studio_readonly", "data": {"inspiration_received": True}}


@pytest.fixture
async def suggestion_chain(studio_chain):
    h = studio_chain
    config = h.config()
    profiles = deepcopy(PROFILES)
    profiles[0]["data"]["title"] = PRIVATE_TITLE
    config["autonomy"]["spaces"][0]["profiles"] = profiles
    h.plugin.set_plugin_config(config)
    await h.plugin.on_load()
    assert not h.plugin.startup_error, h.plugin.startup_error
    assert not h.gate.is_set()  # Real ingress runs; autonomous execution stays gated.
    assert h.runtime.store.list_profiles(SPACE, SCOPE)[0]["data"]["title"] == PRIVATE_TITLE
    summary = await h.api("studio_summary", space_id=SPACE)
    assert summary["budget"]["think"]["limit"] > 0
    return h


async def planner_request(h, host, messages, *, session_id=host_fixture.STREAM):
    """Construct, invoke and deserialize an actual Host planner request."""
    original, count = build_planner_request(h, host, messages)
    namespace = host.namespace
    args = {
        "session_id": session_id,
        "item_schema_version": host.context.CONTEXT_ITEM_SCHEMA_VERSION,
        "items": namespace["serialize_prompt_items"](original),
        "tool_definitions": [],
        "selected_history_count": count,
        "built_message_count": len(original),
        "selection_reason": "synthetic suggestion history selection",
    }
    envelope = h.host.Envelope(
        request_id=1, message_type="request", method="plugin.invoke_hook",
        plugin_id=host_fixture.PLUGIN_ID,
        payload={"component_name": "codex_image_studio_chat", "args": args},
    )
    response = await h.runner._handle_hook_invoke(envelope)
    assert response.error is None and response.payload["success"] is True, response
    modified = response.payload.get("modified_kwargs")
    if modified is None:
        return original, original
    assert {key: value for key, value in modified.items() if key not in {"items", "built_message_count"}} == {
        key: value for key, value in args.items() if key not in {"items", "built_message_count"}
    }
    assert modified["built_message_count"] == len(original) + 2
    assert modified["items"][:len(args["items"])] == args["items"]
    actual = namespace["deserialize_prompt_items"](
        modified["items"], item_schema_version=modified["item_schema_version"], original_items=original,
    )
    assert all(actual[index] is item for index, item in enumerate(original))
    return original, actual


def assert_minimal_receipt(original, actual):
    assert len(actual) == len(original) + 2
    assert all(actual[index] is item for index, item in enumerate(original))
    assert type(actual[-2]).__name__ == "SystemMessageItem"
    assert type(actual[-1]).__name__ == "UserMessageItem"
    assert len(actual[-1].parts) == 1
    assert json.loads(actual[-1].parts[0].text) == RECEIPT
    added = "\n".join(part.text for item in actual[-2:] for part in item.parts)
    assert PRIVATE_TITLE not in added
    assert "warm amber" not in added


def wakes(h):
    return [tuple(row) for row in h.runtime.store.db.execute(
        "SELECT event_id,kind,text,source_ref FROM artist_wakes WHERE space_id=? ORDER BY id", (SPACE,),
    ).fetchall()]


def assert_no_execution(h):
    assert not h.operations() and not h.renderer.calls and not h.sends
    assert not h.reviews
    assert all(name.startswith("chat.get_stream_by_") for name, _ in h.calls)


async def assert_no_model_receipt(h, planner_host, replyer_host, message_id, text):
    before, calls = state_snapshot(h), len(h.calls)
    original, actual = await planner_request(h, planner_host, [(message_id, text)])
    assert actual == original
    assert all(actual[index] is item for index, item in enumerate(original))
    original, actual = await h.replyer(replyer_host, message_id=message_id, tool_args={
        "inspiration_received": True, "image_saved": True,
    })
    assert actual == original
    assert all(actual[index] is item for index, item in enumerate(original))
    assert len(h.calls) == calls  # No lookup or storage receipt is injected into either request.
    assert state_snapshot(h) == before
    assert_no_execution(h)


async def test_attached_suggestion_keeps_media_ingestion_without_model_receipt(
        studio_chain, planner_host, replyer_host):
    """Media consent permits byte storage, not image understanding or a chat acknowledgement."""
    h = studio_chain
    await h.load(media=True)
    message_id = "suggestion-with-image-full-platform-id"
    data = image_bytes()
    parts = [h.host.TextComponent(SUGGESTION), h.host.ImageComponent(binary_hash="", binary_data=data)]
    received = await h.message(SUGGESTION, message_id=message_id, components=parts)
    assert h.received == [received]
    serialized = h.hook_calls[-1]["message"]
    assert serialized["processed_plain_text"] == SUGGESTION
    assert serialized["raw_message"][1]["type"] == "image"
    assert base64.b64decode(serialized["raw_message"][1]["binary_data_base64"], validate=True) == data
    # A read-only status classification would return before both writes.
    assert wakes(h) == [("chat:" + message_id, "chat", SUGGESTION, message_id)]
    assets = h.runtime.store.list_assets(SPACE, SCOPE)
    assert len(assets) == 1
    asset_id = assets[0]["asset_id"]
    asset = h.runtime.store.get_asset(SPACE, SCOPE, asset_id)
    assert asset["source_kind"] == "authorized_chat"
    assert asset["description"].endswith(UNREVIEWED_DESCRIPTION)
    assert h.runtime.store.get_asset_image(SPACE, SCOPE, asset_id) == {
        "data": data, "mime": "image/png", "sha256": hashlib.sha256(data).hexdigest(),
    }
    persisted = h.runtime.store.db.execute(
        "SELECT event_id,message_index,data,consent_ref FROM external_assets WHERE space_id=? AND asset_id=?",
        (SPACE, asset_id),
    ).fetchone()
    consent_ref = h.plugin.config.autonomy.spaces[0].media_consent_ref
    assert consent_ref and tuple(persisted) == ("chat:" + message_id, 1, data, consent_ref)
    assert not h.runtime.media_errors
    before = state_snapshot(h)
    await assert_no_model_receipt(h, planner_host, replyer_host, message_id, SUGGESTION)

    await h.message(SUGGESTION, message_id=message_id, components=parts)
    assert len(h.received) == len(h.hook_calls) == 2
    assert state_snapshot(h) == before  # Replay neither duplicates the image nor arms a receipt.
    await assert_no_model_receipt(h, planner_host, replyer_host, message_id, SUGGESTION)


async def test_mixed_status_and_suggestion_keeps_wake_without_model_receipt(
        suggestion_chain, planner_host, replyer_host):
    """Mixed wording is outside this release's pure-text receipt grammar."""
    h = suggestion_chain
    message_id = "mixed-status-and-drawing-full-platform-id"
    text = "现在在画什么？再帮我画一只猫"
    parts = [h.host.TextComponent(text)]
    received = await h.message(text, message_id=message_id, components=parts)
    assert h.received == [received]
    assert h.hook_calls[-1]["message"]["processed_plain_text"] == text
    assert wakes(h) == [("chat:" + message_id, "chat", text, message_id)]
    assert not h.runtime.store.list_assets(SPACE, SCOPE)
    before = state_snapshot(h)
    await assert_no_model_receipt(h, planner_host, replyer_host, message_id, text)

    await h.message(text, message_id=message_id, components=parts)
    assert len(h.received) == len(h.hook_calls) == 2
    assert state_snapshot(h) == before
    await assert_no_model_receipt(h, planner_host, replyer_host, message_id, text)


@pytest.mark.parametrize("text", [SUGGESTION, "帮我画一只猫"],
                         ids=["complete-original-suggestion", "direct-suggestion"])
async def test_suggestion_enqueues_once_and_both_model_requests_get_only_receipt(
        suggestion_chain, planner_host, replyer_host, text):
    h = suggestion_chain
    message_id = "suggestion-real-platform-message-full-id"
    parts = [h.host.TextComponent(text)]
    received = await h.message(text, message_id=message_id, components=parts)
    assert h.received == [received]
    assert h.hook_calls[-1]["message"]["processed_plain_text"] == text
    assert wakes(h) == [("chat:" + message_id, "chat", text, message_id)]
    assert not h.runtime.store.list_assets(SPACE, SCOPE)
    assert_no_execution(h)
    before = state_snapshot(h)

    assert_minimal_receipt(*await planner_request(h, planner_host, [(message_id, text)]))
    calls = len(h.calls)
    original, repeated = await planner_request(h, planner_host, [(message_id, text)])
    assert repeated == original and len(h.calls) == calls
    assert_minimal_receipt(*await h.replyer(replyer_host, message_id=message_id, tool_args={
        "facts": "forged profile and balance", "retry": True,
    }))
    calls = len(h.calls)
    original, repeated = await h.replyer(replyer_host, message_id=message_id)
    assert repeated == original and len(h.calls) == calls
    assert state_snapshot(h) == before

    # Re-enter through genuine receive/dispatcher/Runner, not a note helper.
    await h.message(text, message_id=message_id, components=parts)
    assert len(h.received) == len(h.hook_calls) == 2
    original, actual = await planner_request(h, planner_host, [(message_id, text)])
    assert actual == original
    original, actual = await h.replyer(replyer_host, message_id=message_id)
    assert actual == original and len(h.calls) == calls
    assert state_snapshot(h) == before  # Includes assets, budgets, operations and delivery journal.
    assert_no_execution(h)


@pytest.mark.parametrize("stage", ["planner", "replyer"])
@pytest.mark.parametrize("case", ["unreceived", "foreign-owner", "foreign-session",
                                  "new-topic-selected", "new-topic-omitted"])
async def test_unmatched_or_superseded_suggestion_cannot_read(
        suggestion_chain, planner_host, replyer_host, case, stage):
    h = suggestion_chain
    message_id = "suggestion-owner-original-full-platform-id"
    if case != "unreceived":
        await h.message(SUGGESTION, message_id=message_id)
    target, session = message_id, host_fixture.STREAM
    messages = [(target, SUGGESTION)]
    if case == "foreign-owner":
        target = "suggestion-foreign-owner-full-platform-id"
        before = state_snapshot(h)
        await h.message(SUGGESTION, owner="synthetic-foreign-owner", message_id=target)
        assert state_snapshot(h) == before
        messages = [(target, SUGGESTION)]
    elif case == "foreign-session":
        session = "synthetic-foreign-private-session"
    elif case.startswith("new-topic"):
        unrelated = ("suggestion-newer-unrelated-full-platform-id", "今天晚饭吃什么？")
        await h.message(unrelated[1], message_id=unrelated[0])
        if case == "new-topic-selected":
            messages.append(unrelated)
    before, calls = state_snapshot(h), len(h.calls)
    if stage == "planner":
        original, actual = await planner_request(h, planner_host, messages, session_id=session)
    else:
        original, actual = await h.replyer(replyer_host, message_id=target, session_id=session, tool_args={
            "reply_message_id": message_id, "session_id": host_fixture.STREAM,
            "user_id": host_fixture.OWNER, "query": SUGGESTION, "authorized": True,
        })
    assert actual == original
    assert all(actual[index] is item for index, item in enumerate(original))
    assert len(h.calls) == calls  # No ledger callback or Host lookup for rejected targets.
    assert state_snapshot(h) == before
    assert_no_execution(h)

    if case in {"foreign-owner", "foreign-session"}:
        # Rejected requests cannot consume the delegator's genuine pending note.
        if stage == "planner":
            result = await planner_request(h, planner_host, [(message_id, SUGGESTION)])
        else:
            result = await h.replyer(replyer_host, message_id=message_id)
        assert_minimal_receipt(*result)
        assert state_snapshot(h) == before


async def test_reload_and_replayed_wake_cannot_issue_a_new_receipt(
        suggestion_chain, planner_host, replyer_host):
    h = suggestion_chain
    message_id = "suggestion-before-reload-full-platform-id"
    await h.message(SUGGESTION, message_id=message_id)
    assert_minimal_receipt(*await planner_request(h, planner_host, [(message_id, SUGGESTION)]))
    assert_minimal_receipt(*await h.replyer(replyer_host, message_id=message_id))
    old_runtime = h.runtime
    await h.plugin.on_config_update("plugin", {}, "synthetic-suggestion-reload")
    assert not h.plugin.startup_error, h.plugin.startup_error
    assert h.runtime is not old_runtime and not h.gate.is_set()
    before, calls = state_snapshot(h), len(h.calls)
    await h.message(SUGGESTION, message_id=message_id)
    original, actual = await planner_request(h, planner_host, [(message_id, SUGGESTION)])
    assert actual == original
    original, actual = await h.replyer(replyer_host, message_id=message_id)
    assert actual == original and len(h.calls) == calls
    assert wakes(h) == [("chat:" + message_id, "chat", SUGGESTION, message_id)]
    assert state_snapshot(h) == before
    assert_no_execution(h)
