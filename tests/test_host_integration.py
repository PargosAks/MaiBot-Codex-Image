"""Isolated SDK + fixed public Host methods, never a running MaiBot instance.

The genuine receive_message/HookDispatcher/Runner/capability implementations are
AST-loaded without module bootstrap (which imports real configuration). Peripheral
message processing, session lookup, logging, RPC transport and capability effects
are synthetic. This proves the fixed ingress method's hookup, not a live adapter,
deployed Host, final Maisaka admission, model quality or platform delivery.
"""

from copy import deepcopy
import asyncio
import base64
import hashlib
import json
import os

import pytest

from image_plugin_under_test.domain.policy import Scope
from protocol_fixture import PNG
from support.host_chain import (
    OWNER,
    STREAM,
    PLATFORM,
    public_host as public_host,
    host_chain as host_chain,
)

pytestmark = pytest.mark.skipif(
    not (os.environ.get("CODEX_IMAGE_TEST_HOST_SOURCE") or os.environ.get("CODEX_IMAGE_TEST_HOST_REPO")),
    reason="Explicit public Host source is required",
)


async def test_default_off_registered_hook_is_inert_through_real_ingress(host_chain):
    h = host_chain
    await h.load(enabled=False)
    await h.message()
    assert h.runtime.store is None and not h.runtime.tasks
    assert not h.calls and not h.renderer.calls and not h.sends
    assert not (h.data_paths.data_dir / "artist.sqlite3").exists()
    assert len(h.received) == 1
    assert len(h.hook_calls) == 1 and h.hook_calls[0]["message"]["processed_plain_text"] == "雨夜暖光"


async def test_real_sdk_normalizes_stream_but_raw_capability_preserves_receipt(host_chain):
    h = host_chain
    stream = await h.plugin.ctx.chat.get_stream_by_user_id(OWNER, platform=PLATFORM)
    assert stream["stream_id"] == STREAM and "stream" not in stream and "success" not in stream
    raw = await h.plugin.ctx.call_host_method("cap.call", payload={
        "capability": "chat.get_stream_by_user_id", "args": {"user_id": OWNER, "platform": PLATFORM}})
    assert raw == {"success": True, "stream": stream}
    assert not h.renderer.calls and not h.sends


@pytest.mark.parametrize("valid_stream", [False, True])
async def test_failed_host_lookup_receipt_never_grants_even_if_stream_present(host_chain, valid_stream):
    h = host_chain
    h.stream_override = {"success": False, "stream": ({"platform": PLATFORM, "stream_id": STREAM,
        "session_id": STREAM, "user_id": OWNER, "group_id": "", "is_group_session": False} if valid_stream else None)}
    h.plugin.set_plugin_config(h.config())
    await h.plugin.on_load()
    assert h.plugin.startup_error == "Configured Host stream is unavailable"
    assert h.runtime is None and not h.renderer.calls and not h.sends


@pytest.mark.parametrize("group", [False, True])
async def test_actual_ingress_sdk_review_edit_and_send(host_chain, group):
    h = host_chain
    await h.load(group=group)
    await h.message()
    h.gate.set()
    await h.until(lambda: h.state() == "planned")
    await h.advance("review_pending")
    await h.advance("planned")
    await h.advance("review_pending")
    await h.advance("finished")
    assert len(h.renderer.calls) == len(h.reviews) == 2 and not h.sends
    assert h.renderer.calls[1]["source"]["data"] == PNG
    assert "保留小猫姿态" in h.renderer.calls[1]["prompt"]
    h.share = True
    await h.advance()
    await h.until(lambda: len(h.sends) == 1)
    await h.advance()
    await asyncio.sleep(.12)
    assert len(h.sends) == 1 and len(h.received) == 1
    assert all(name != "chat.open_session" for name, _ in h.calls)


async def test_idle_timer_uses_sdk_without_chat(host_chain):
    h = host_chain
    h.revise = False
    await h.load()
    h.gate.set()
    await h.until(lambda: h.state() == "planned")
    await h.advance("review_pending")
    await h.advance("finished")
    assert not h.received and len(h.renderer.calls) == 1 and len(h.reviews) == 1 and not h.sends


async def test_host_configuration_freezes_profile_for_drawing_and_review(host_chain):
    from support.catalog_examples import CHOICE, PROFILES, SERIES

    h = host_chain
    original_config = h.config

    def config(**kwargs):
        result = original_config(**kwargs)
        result["autonomy"]["spaces"][0].update(profiles=deepcopy(PROFILES), series=deepcopy(SERIES))
        return result

    h.config = config
    h.creative_choice, h.revise = CHOICE, False
    await h.load()
    h.gate.set()
    await h.until(lambda: h.state() == "planned")
    await h.advance("review_pending")
    await h.advance("finished")
    assert "warm amber" in h.renderer.calls[0]["prompt"]
    context = json.loads(h.reviews[0]["prompt"][1]["content"][0]["text"])
    assert context["current_work"]["brief"]["creative"]["profile"] == PROFILES[0]["data"]
    assert not h.sends


@pytest.mark.parametrize("kwargs", [{"owner": "other-owner"}, {"text": " /绘图 命令"},
                                    {"command": True}, {"notify": True}, {"text": "  "}])
async def test_ingress_rejects_nonowner_commands_notify_and_blank(host_chain, kwargs):
    h = host_chain
    await h.load(group=True)
    await h.message(**kwargs)
    scope = Scope(PLATFORM, STREAM, OWNER)
    assert not h.runtime.store.db.execute("SELECT 1 FROM artist_wakes").fetchone()
    assert not h.runtime.store.audit_operations("studio", scope) and not h.renderer.calls


@pytest.mark.parametrize("receipt", ["lost", "string", "integer", "missing"])
async def test_reload_unknown_host_send_is_never_replayed(host_chain, receipt):
    h = host_chain
    h.revise = False
    await h.load()
    h.gate.set()
    await h.until(lambda: h.state() == "planned")
    await h.advance("review_pending")
    await h.advance("finished")
    h.share = True
    h.lose_send = receipt == "lost"
    if receipt != "lost":
        h.send_override = {"success": True, "message_id": "synthetic-unconfirmed"}
        if receipt != "missing":
            h.send_override["sent"] = "false" if receipt == "string" else 1
    await h.advance()
    await h.until(lambda: len(h.sends) == 1)
    scope = Scope(PLATFORM, STREAM, OWNER)
    await h.until(lambda: any(op["action"] == "share" and op["state"] in {"unknown", "succeeded", "failed"}
                              for op in h.runtime.store.audit_operations("studio", scope)))
    operation = next(op for op in h.runtime.store.audit_operations("studio", scope) if op["action"] == "share")
    assert operation["state"] == "unknown", "An ambiguous raw receipt cannot be coerced to success or known failure"
    assert h.plugin.journal.db.execute("SELECT state FROM delivery").fetchone()[0] == "send_unknown"
    # Complete this wake before reload. Otherwise recovery may legitimately reuse
    # its cached plan, and demanding a fresh model call would test the wrong thing.
    await h.until(lambda: h.runtime.store.db.execute(
        "SELECT state FROM artist_claims ORDER BY rowid DESC LIMIT 1").fetchone()[0] != "active")
    previous, tasks = h.runtime, tuple(h.runtime.tasks.values())
    plans = sum(name == "llm.generate" for name, _ in h.calls)
    await h.plugin.on_config_update("plugin", {}, "synthetic-reload")
    assert h.runtime is not previous and previous.store is None and all(task.done() for task in tasks)
    await h.advance()
    await h.until(lambda: sum(name == "llm.generate" for name, _ in h.calls) > plans)
    await h.until(lambda: h.runtime.store.db.execute(
        "SELECT state FROM artist_claims ORDER BY rowid DESC LIMIT 1").fetchone()[0] != "active")
    assert len(h.sends) == len(h.renderer.calls) == 1
    assert h.runtime.store.budget_usage("studio", scope)["share"]["held"] == 1


async def test_revoke_configuration_stops_sdk_models_and_sends(host_chain):
    h = host_chain
    await h.load()
    h.gate.set()
    await h.until(lambda: h.state() == "planned")
    previous = h.runtime
    h.plugin.set_plugin_config(h.config(enabled=False))
    await h.plugin.on_config_update("plugin", {}, "synthetic-disable")
    count = len(h.calls)
    await h.message()
    await asyncio.sleep(.12)
    assert previous.store is None and not h.runtime.tasks
    assert len(h.calls) == count and not h.renderer.calls and not h.sends


@pytest.mark.parametrize("group", [False, True])
async def test_current_images_through_fixed_public_serializer_and_hook(host_chain, group):
    from support.media_samples import image_bytes

    h = host_chain
    await h.load(group=group, media=True)
    data = image_bytes()
    parts = [h.host.TextComponent("context"), h.host.ImageComponent(binary_hash="", binary_data=data),
             h.host.EmojiComponent(binary_hash="", binary_data=data)]
    await h.message(text="", components=parts, message_id="synthetic-media-event")
    # A replay traverses the genuine dispatcher/Runner/SDK hook again.
    await h.message(text="", components=parts, message_id="synthetic-media-event")
    scope = Scope(PLATFORM, STREAM, OWNER)
    catalog = h.runtime.store.list_assets("studio", scope)
    assert len(catalog) == 1
    asset = h.runtime.store.get_asset("studio", scope, catalog[0]["asset_id"])
    assert asset["sha256"] == hashlib.sha256(data).hexdigest() and asset["mime"] == "image/png"
    assert set(asset["roles"]) == {"character", "style", "composition", "palette"}
    from image_plugin_under_test.host.host_media import IMAGE_ONLY_TEXT, UNREVIEWED_DESCRIPTION

    assert asset["description"].endswith(UNREVIEWED_DESCRIPTION)
    wakes = h.runtime.store.db.execute("SELECT event_id,text FROM artist_wakes").fetchall()
    assert [tuple(row) for row in wakes] == [("chat:synthetic-media-event", IMAGE_ONLY_TEXT)]
    serialized = h.hook_calls[0]["message"]["raw_message"]
    assert base64.b64decode(serialized[1]["binary_data_base64"], validate=True) == data
    assert serialized[2]["type"] == "emoji"
    assert not h.runtime.media_errors and not h.renderer.calls and not h.sends
    assert all(name.startswith("chat.get_stream_by_") for name, _ in h.calls)


@pytest.mark.parametrize("media,kwargs", [
    (False, {}), (True, {"owner": "other-owner"}), (True, {"command": True}),
    (True, {"notify": True}), (True, {"text": " /command"}),
])
async def test_fixed_public_ingress_rejects_unauthorized_image_inputs(host_chain, media, kwargs):
    from support.media_samples import image_bytes

    h = host_chain
    await h.load(media=media, group=True)
    part = h.host.ImageComponent(binary_hash="", binary_data=image_bytes())
    await h.message(**{"text": "", **kwargs}, components=[part])
    assert not h.runtime.store.list_assets("studio", Scope(PLATFORM, STREAM, OWNER))
    assert not h.runtime.store.db.execute("SELECT 1 FROM artist_wakes").fetchone()
    assert not h.renderer.calls and not h.sends


async def test_media_optin_without_separate_consent_fails_at_host_startup(host_chain):
    h = host_chain
    h.plugin.set_plugin_config(h.config(media=True, media_consent=""))
    await h.plugin.on_load()
    assert "Separate media consent" in h.plugin.startup_error
    assert h.runtime is None and not h.renderer.calls and not h.sends


async def test_fixed_public_ingress_bad_image_keeps_text_and_records_code(host_chain):
    h = host_chain
    await h.load(media=True)
    part = h.host.ImageComponent(binary_hash="", binary_data=b"synthetic invalid image")
    await h.message("keep existing text", components=[part])
    assert h.runtime.media_errors == {"studio": "MEDIA_FORMAT_INVALID"}
    assert not h.runtime.store.list_assets("studio", Scope(PLATFORM, STREAM, OWNER))
    assert h.runtime.store.db.execute("SELECT text FROM artist_wakes").fetchone()[0] == "keep existing text"
    assert not h.renderer.calls and not h.sends
