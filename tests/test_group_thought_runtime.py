"""Planner note tests against real plugin lifecycle and isolated SQLite only."""
from copy import deepcopy
from threading import Event
from types import SimpleNamespace
import asyncio

import pytest

from image_plugin_under_test.group_thought_runtime import GroupThoughtRuntime, PARAMETERS, TOOL_NAME
from image_plugin_under_test.policy import Scope
from test_group_collection import groups as groups


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


async def page(plugin, group="a"):
    return await plugin.studio_records(group, "group_thoughts")


async def test_model_may_note_wait_or_repeat_without_drawing_or_attributing_members(notes, tmp_path):
    plugin, runtime, clock = notes
    original = request()
    copy = deepcopy(original)
    update = await plugin.group_thought_before_request(**original)
    assert original == copy
    token = update["modified_kwargs"]["tool_definitions"][1]["function"]["parameters"][
        "properties"]["notebook_context"]["enum"][0]
    before = await plugin.group_thought_receipt(tags=["rain", "store"], notebook_context=token,
                                              stream_id="stream-a", chat_id="stream-a")
    assert before["success"] is False  # Invoking a tool does not create a thought.
    assert not (tmp_path / "group_thoughts.sqlite3").exists()
    await plugin.group_thought_after_response(**response(token))
    await plugin.group_thought_after_response(**response(token))  # response replay
    result = await plugin.group_thought_receipt(tags=["rain", "store"], notebook_context=token,
                                              stream_id="stream-a", chat_id="stream-a")
    assert result["structured_content"] == {"recorded": True}
    assert not any(key in str(result) for key in ("member_count", "source_count", "owner"))
    assert len((await page(plugin))["items"]) == 1
    next_token = await offer(plugin)
    await plugin.group_thought_after_response(session_id="stream-a", item_schema_version=1, output_items=[])
    assert next_token != token and len((await page(plugin))["items"]) == 1
    for table in ("artist_wakes", "artist_works", "operations"):
        assert plugin.artist_runtime.store.db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0


async def test_two_groups_share_neither_offer_nor_note(notes):
    plugin, runtime, _ = notes
    a, b = await offer(plugin, "stream-a"), await offer(plugin, "stream-b")
    await plugin.group_thought_after_response(**response(a, session="stream-b"))
    assert not (await page(plugin, "a"))["items"]
    assert not (await page(plugin, "b"))["items"]
    await plugin.group_thought_after_response(**response(a))
    await plugin.group_thought_after_response(**response(b, ["forest", "mist"], "stream-b"))
    assert len((await page(plugin, "a"))["items"]) == len((await page(plugin, "b"))["items"]) == 1
    wrong = await plugin.group_thought_receipt(tags=["rain", "store"], notebook_context=a, stream_id="stream-b")
    assert wrong["success"] is False


@pytest.mark.parametrize("session", ["stream-c", "private", "foreign", "", None])
async def test_disabled_or_unbound_chat_cannot_obtain_tool(notes, session):
    plugin, _, _ = notes
    result = await plugin.group_thought_before_request(**request(session))
    names = [d["function"]["name"] for d in result["modified_kwargs"]["tool_definitions"]]
    assert names == ["ordinary_reply"]


async def test_host_disabled_tool_is_never_readded(notes):
    plugin, runtime, _ = notes
    args = request()
    args["tool_definitions"].pop()
    assert await plugin.group_thought_before_request(**args) is None
    assert not runtime.offers


@pytest.mark.parametrize("change", ["stale_round", "expired", "revoke", "off", "allowlist", "reload", "stopping"])
async def test_authority_changes_between_model_request_and_response_refuse_storage(notes, tmp_path, change):
    plugin, runtime, clock = notes
    token = await offer(plugin)
    if change == "stale_round":
        await offer(plugin)
    elif change == "expired":
        clock.monotonic += 121
    elif change == "revoke":
        plugin.artist_runtime.store.revoke("a", Scope("qq", "stream-a", "owner"), source_ref="test", granted_by="test")
    elif change == "off":
        plugin.config.autonomy.enabled = False
    elif change == "allowlist":
        plugin.config.connection.allowed_users.clear()
    elif change == "reload":
        plugin.set_plugin_config(plugin.config.model_dump())
    else:
        plugin._stop_artist_workers()
    await plugin.group_thought_after_response(**response(token))
    assert not (tmp_path / "group_thoughts.sqlite3").exists()


@pytest.mark.parametrize("tamper", ["extra_identity", "raw_text", "bad_tag", "wrong_token", "text_item",
                                   "multiple_calls", "bool_schema", "bad_args", "foreign_name"])
async def test_malformed_or_forged_model_output_does_not_write(notes, tmp_path, tamper):
    plugin, _, _ = notes
    token = await offer(plugin)
    payload = response(token)
    item = payload["output_items"][0]
    args = item["tool_call"]["args"]
    if tamper == "extra_identity":
        args["stream_id"] = "stream-b"
    elif tamper == "raw_text":
        args["text"] = "raw-group-message-must-not-be-stored"
    elif tamper == "bad_tag":
        args["tags"] = ["private-user-name"]
    elif tamper == "wrong_token":
        args["notebook_context"] = "a" * 48
    elif tamper == "text_item":
        item["item_type"] = "AssistantMessageItem"
    elif tamper == "multiple_calls":
        payload["output_items"].append(deepcopy(item))
    elif tamper == "bool_schema":
        payload["item_schema_version"] = True
    elif tamper == "bad_args":
        item["tool_call"]["args"] = '{"tags":["rain"]}'
    else:
        item["tool_call"]["func_name"] = "ordinary_reply"
    await plugin.group_thought_after_response(**payload)
    assert not (tmp_path / "group_thoughts.sqlite3").exists()


async def test_repeated_plan_is_deduplicated_and_revoke_never_rewrites_existing_note(notes, tmp_path):
    plugin, runtime, _ = notes
    first = await offer(plugin)
    await plugin.group_thought_after_response(**response(first))
    snapshot = (tmp_path / "group_thoughts.sqlite3").read_bytes()
    # Direct receipt reads never write, even if requested through forged context.
    await plugin.group_thought_receipt(tags=["coast"], notebook_context=first, stream_id="stream-a")
    assert (tmp_path / "group_thoughts.sqlite3").read_bytes() == snapshot
    again = await offer(plugin)
    await plugin.group_thought_after_response(**response(again))
    assert len((await page(plugin))["items"]) == 1
    plugin.artist_runtime.store.revoke("a", Scope("qq", "stream-a", "owner"), source_ref="test", granted_by="test")
    snapshot = (tmp_path / "group_thoughts.sqlite3").read_bytes()
    await plugin.group_thought_after_response(**response(again, ["coast"]))
    assert (tmp_path / "group_thoughts.sqlite3").read_bytes() == snapshot


async def test_close_and_cancellation_wait_for_started_storage(notes, monkeypatch):
    plugin, runtime, _ = notes
    token = await offer(plugin)
    entered, release = Event(), Event()
    original = runtime._record
    def delayed(value):
        entered.set()
        release.wait(5)
        return original(value)
    monkeypatch.setattr(runtime, "_record", delayed)
    task = asyncio.create_task(plugin.group_thought_after_response(**response(token)))
    await asyncio.wait_for(asyncio.to_thread(entered.wait), 3)
    task.cancel()
    closing = asyncio.create_task(plugin.on_unload())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await closing
    assert plugin.group_thought_runtime is None
    assert plugin.artist_runtime is None
