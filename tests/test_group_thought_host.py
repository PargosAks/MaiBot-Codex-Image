"""Public Host hook/SDK roundtrips with synthetic sessions and temporary state.

Reuses public_host's fixed-revision AST loader and real Runner/Dispatcher. Pure
Context Item modules, hook specs, tool serialization and invocation/context
builders are also loaded from that revision. No Host bootstrap, real model,
network, QQ, or production data is used. The scheduler remains gated throughout.
"""

from contextlib import closing
import json
import os
import sqlite3

import pytest

from image_plugin_under_test.host.group_thought_runtime import TAGS, TOOL_NAME
from image_plugin_under_test.domain.policy import Scope
from image_plugin_under_test.workspace.studio import Studio
from support.host_chain import OWNER, PLATFORM, STREAM, host_chain as host_chain, public_host as public_host
from support.group_thought_host import (
    BEFORE,
    AFTER,
    OTHER_STREAM,
    thought_host as thought_host,
    counts,
    before,
    output,
    after,
    receipt,
)


pytestmark = pytest.mark.skipif(
    not (os.environ.get("CODEX_IMAGE_TEST_HOST_SOURCE") or os.environ.get("CODEX_IMAGE_TEST_HOST_REPO")),
    reason="Explicit public Host source is required",
)
def ledger(h):
    # Thoughts must not append an autonomous operation, wake, or render budget.
    return tuple(h.runtime.store.db.iterdump())


def no_effects(h):
    assert not h.renderer.calls and not h.sends and not h.reviews and not h.gate.is_set()
    assert all(name.startswith("chat.get_stream_by_") for name, _ in h.calls)
    # The hook may signal the existing worker; only a subsequent budgeted cycle
    # imports/adopts a note. This fixture intentionally keeps that worker parked.


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
