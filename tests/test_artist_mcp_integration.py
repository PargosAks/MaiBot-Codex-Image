"""Opt-in Artist -> real localhost MCP -> synthetic image driver integration.

No Host, real model, native Codex executable or chat sender is used. PIL and the
independent service are imported only after explicit service-source opt-in.
"""

from types import SimpleNamespace
import asyncio

import pytest

from image_plugin_under_test.creation.artist import Artist
from image_plugin_under_test.domain.policy import StoreError
from image_plugin_under_test.creation.thinking import Thinking
from support.external_mcp import SOURCE, actual_mcp as actual_mcp
from support.artist_mcp import SPACE, studio as studio

pytestmark = pytest.mark.skipif(not SOURCE, reason="Independent MCP source is explicitly opt-in")
@pytest.mark.parametrize("chat", [False, True])
async def test_chat_and_idle_full_edit_review_and_independent_share(studio, chat):
    await studio.plan(chat=chat)
    assert (await studio.step())["state"] == "creating"
    assert [call["task"] for call in studio.model.calls] == ["plan", "plan"]
    await studio.finish_render()
    assert (await studio.step())["state"] == "review_pending"
    assert (await studio.step()) == {"state": "planned", "reason": "revision"}
    parent = studio.versions()[0]
    assert not studio.sender.calls and len(studio.driver.calls) == 1
    studio.driver.gate = asyncio.Event()
    assert (await studio.step())["state"] == "creating"
    await studio.finish_render()
    assert (await studio.step())["state"] == "review_pending"
    assert (await studio.step())["state"] == "finished"
    versions = studio.versions()
    assert len(versions) == 2 and versions[1]["parent_id"] == parent["version_id"]
    assert studio.works()[0]["best_version"] == parent["version_id"]
    assert studio.driver.calls[1]["source_image"]["data"] == studio.driver.images[0]
    assert "保留小猫姿态和原构图" in studio.driver.calls[1]["prompt"]
    assert [review["images"][0]["data"] for review in studio.model.reviews] == studio.driver.images
    assert (await studio.step())["state"] == "waiting" and not studio.sender.calls
    studio.model.share = True
    assert (await studio.step())["state"] == "shared"
    assert (await studio.step())["state"] == "share_recorded"
    assert len(studio.sender.calls) == 1 and studio.sender.calls[0][1]["data"] == studio.driver.images[0]
    assert [operation for operation, _ in studio.trace if operation in {"submit", "edit"}] == ["submit", "edit"]
    ideas = studio.store.list_inspirations(SPACE, studio.scope)
    assert {idea["origin"] for idea in ideas} == {"chat" if chat else "idle"}


@pytest.mark.parametrize("operation", ["submit", "edit"])
async def test_lost_render_reply_reopen_recovery_only_looks_up_original(studio, operation):
    if operation == "edit":
        await studio.original_ready()
        assert (await studio.step())["reason"] == "revision"
        studio.driver.gate = asyncio.Event()
    else:
        studio.model.revisions = 0
        await studio.plan()
    studio.dropped.add(operation)
    assert await studio.step() == {"state": "blocked", "reason": "TRANSPORT_UNKNOWN"}
    pending = studio.renders()[-1]
    assert pending["state"] == "unknown" and pending["job_id"] is None
    assert studio.store.budget_usage(SPACE, studio.scope)["render"]["held"] == 1
    await studio.finish_render()
    studio.reopen()
    trace, models, sends = len(studio.trace), len(studio.model.calls), len(studio.sender.calls)
    await studio.artist.recover(offline=True, reconcile=True)
    assert set(name for name, _ in studio.trace[trace:]) <= {"health", "lookup", "result"}
    assert any(name == "lookup" and args["request_id"] == pending["request_id"] for name, args in studio.trace[trace:])
    assert len(studio.model.calls) == models and len(studio.sender.calls) == sends
    assert studio.works()[0]["status"] == "review_pending"
    assert studio.store.get_operation(SPACE, studio.scope, pending["request_id"])["state"] == "succeeded"
    assert (await studio.step())["state"] == "finished"
    assert len(studio.driver.calls) == (2 if operation == "edit" else 1)
    assert sum(name == operation for name, _ in studio.trace) == 1


async def test_lost_send_receipt_reopen_never_resends(studio):
    await studio.finish_without_edit()
    studio.model.share = studio.sender.lose_receipt = True
    assert (await studio.step())["state"] == "share_unknown"
    studio.reopen()
    calls, mutations = len(studio.model.calls), len(studio.driver.calls)
    await studio.artist.recover(offline=True, reconcile=True)
    assert len(studio.model.calls) == calls and len(studio.sender.calls) == 1
    assert await studio.step() == {"state": "share_recorded", "outcome": "unknown"}
    assert len(studio.sender.calls) == 1 and len(studio.driver.calls) == mutations == 1
    assert studio.store.budget_usage(SPACE, studio.scope)["share"]["held"] == 1


async def test_known_review_failure_retries_only_same_image_review(studio):
    studio.model.revisions, studio.model.review_errors = 0, 1
    await studio.original_ready()
    assert await studio.step() == {"state": "blocked", "reason": "THINK_FAILED"}
    assert studio.works()[0]["status"] == "review_pending"
    calls = len(studio.model.calls)
    assert (await studio.artist.run_once())["state"] == "idle"
    assert len(studio.model.calls) == calls
    assert (await studio.step())["state"] == "finished"
    assert len(studio.driver.calls) == 1 and len(studio.model.reviews) == 2
    assert studio.model.reviews[0]["images"] == studio.model.reviews[1]["images"]
    assert studio.model.reviews[0]["images"][0]["data"] == studio.driver.images[0]
    assert sum(operation == "submit" for operation, _ in studio.trace) == 1


async def test_unknown_available_holds_budget_blocks_edit_and_reuses_saved_review(studio):
    studio.driver.unknown = True
    await studio.original_ready()
    assert await studio.step() == {"state": "blocked", "reason": "RENDER_UNRESOLVED"}
    assert studio.store.budget_usage(SPACE, studio.scope)["render"]["held"] == 1
    assert len(studio.model.reviews) == 1 and len(studio.versions()) == 1
    studio.reopen()
    models = len(studio.model.calls)
    await studio.artist.recover(offline=True, reconcile=True)
    assert len(studio.model.calls) == models
    assert await studio.step() == {"state": "blocked", "reason": "RENDER_UNRESOLVED"}
    assert len(studio.model.reviews) == 1 and len(studio.driver.calls) == 1
    assert not any(operation == "edit" for operation, _ in studio.trace)
    job_id = studio.versions()[0]["job_id"]
    studio.service.store.update(job_id, status="completed", turn_status="completed", error_code=None)
    await studio.artist.recover(offline=True, reconcile=True)
    assert (await studio.step()) == {"state": "planned", "reason": "revision"}
    assert len(studio.model.reviews) == 1 and len(studio.driver.calls) == 1


async def test_unsupported_edit_has_no_text_generation_fallback(studio):
    await studio.original_ready()
    assert (await studio.step())["reason"] == "revision"
    studio.driver.supports_edit = False
    assert await studio.step() == {"state": "blocked", "reason": "EDIT_UNSUPPORTED"}
    assert len(studio.driver.calls) == len(studio.versions()) == 1
    assert [operation for operation, _ in studio.trace if operation in {"submit", "edit"}] == ["submit"]


@pytest.mark.parametrize("phase", ["planned", "creating", "finished"])
async def test_revocation_stops_new_thought_generation_and_send_but_allows_recovery(studio, phase):
    studio.model.revisions = 0
    await studio.plan()
    if phase in {"creating", "finished"}:
        assert (await studio.step())["state"] == "creating"
        await studio.finish_render()
    if phase == "finished":
        assert (await studio.step())["state"] == "review_pending"
        assert (await studio.step())["state"] == "finished"
        studio.model.share = True
    studio.store.revoke(SPACE, studio.scope, source_ref="synthetic-revocation", granted_by="synthetic-admin")
    studio.reopen()
    models, drawings, sends, trace = len(studio.model.calls), len(studio.driver.calls), len(studio.sender.calls), len(studio.trace)
    await studio.artist.recover(offline=True, reconcile=True)
    assert set(operation for operation, _ in studio.trace[trace:]) <= {"health", "lookup", "result"}
    with pytest.raises(StoreError, check=lambda exc: exc.code == "AUTH_REQUIRED"):
        await studio.artist.tick()
    assert (len(studio.model.calls), len(studio.driver.calls), len(studio.sender.calls)) == (models, drawings, sends)


async def test_network_renderer_requires_optin_and_sender_remains_simulated(studio):
    with pytest.raises(StoreError, check=lambda exc: exc.code == "SIMULATION_REQUIRED"):
        Artist(studio.store, SPACE, studio.scope, Thinking(studio.model), studio.renderer, studio.sender)
    with pytest.raises(StoreError, check=lambda exc: exc.code == "SIMULATION_REQUIRED"):
        Artist(studio.store, SPACE, studio.scope, Thinking(studio.model), studio.renderer,
               SimpleNamespace(simulation=False), allow_network_renderer=True)
