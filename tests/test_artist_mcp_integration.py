"""Opt-in Artist -> real localhost MCP -> synthetic image driver integration.

No Host, real model, native Codex executable or chat sender is used. PIL and the
independent service are imported only after explicit service-source opt-in.
"""

from copy import deepcopy
from types import SimpleNamespace
import asyncio
import hashlib
import io

import pytest

from image_plugin_under_test.artist import Artist
from image_plugin_under_test.policy import Delegation, Limits, Scope, StoreError
from image_plugin_under_test.store import ArtistStore
from image_plugin_under_test.thinking import Thinking
from conftest import SCOPE
from test_artist_flow import BRIEF, Clock, Sender
from test_mcp_recovery_integration import SOURCE, actual_mcp  # noqa: F401 - shared opt-in fixture

pytestmark = pytest.mark.skipif(not SOURCE, reason="Independent MCP source is explicitly opt-in")
SPACE, BINDING = "artist-integration", "isolated-artist-mcp"


class Script:
    """Canned decisions assert byte delivery, not artistic quality."""

    def __init__(self, driver):
        self.driver, self.calls, self.reviews = driver, [], []
        self.revisions, self.review_errors = 1, 0
        self.share, self.keep_first = False, True

    async def __call__(self, request):
        self.calls.append(deepcopy(request))
        context = request["context"]
        ideas = [{"summary": event["text"], "source_ids": [event["event_id"]]}
                 for event in context.get("events", []) if event["kind"] == "chat"][:8]
        if request["task"] == "inspire":
            return {"ideas": ideas}
        if request["task"] == "review":
            image = request["images"][0]["data"]
            assert type(image) is bytes and image in self.driver.outputs.values()
            assert request["images"][0]["mime"] == context["current_version"]["mime"] == "image/png"
            assert hashlib.sha256(image).hexdigest() == context["current_version"]["sha256"]
            for item in request["images"]:
                assert item["data"] in self.driver.outputs.values()
                assert hashlib.sha256(item["data"]).hexdigest() == item["sha256"]
            self.reviews.append(deepcopy(request))
            if self.review_errors:
                self.review_errors -= 1
                raise RuntimeError("Synthetic known review failure")
            result = {"decision": "accept", "reason": "脚本接受本合成版本", "strengths": ["合成样例"], "issues": []}
            if self.revisions:
                self.revisions -= 1
                result.update(decision="revise", issues=["脚本要求调整背景"],
                              edit_prompt="降低背景亮度", preserve="保留小猫姿态和原构图")
            elif self.keep_first:
                result["best_version_id"] = context["versions"][0]["version_id"]
            result["comparison"] = {
                "improvements": [], "regressions": list(result["issues"]), "preserved": ["原构图"],
                "evidence_version_ids": [item["version_id"] for item in request["images"]],
            }
            return result
        assert request["task"] == "plan"
        active = next((work for work in context["works"]
                       if work["status"] in {"planned", "deferred", "creating", "review_pending"}), None)
        if active:
            decision = {"action": "continue", "reason": "脚本决定现在继续", "work_id": active["work_id"]}
        elif not context["works"]:
            decision = {"action": "create", "reason": "脚本建立合成计划", "brief": dict(BRIEF), "inspiration_keys": []}
            ideas = ideas or [{"summary": "无人时段的自主合成主题", "source_ids": []}]
        elif self.share:
            finished = next(work for work in context["works"] if work["status"] == "finished")
            decision = {"action": "share", "reason": "独立的模拟分享决定", "work_id": finished["work_id"]}
        else:
            decision = {"action": "wait", "reason": "完成后先收藏，不自动分享"}
        return {"ideas": ideas, "decision": decision}


class SyntheticDriver:
    supports_edit = True

    def __init__(self, root, images):
        self.output_root, self.images = root, images
        self.calls, self.outputs = [], {}
        self.gate, self.unknown = asyncio.Event(), False

    async def generate(self, job, ids, image):
        self.calls.append(deepcopy({key: val for key, val in job.items() if key != "_execution"}))
        if job["operation"] == "edit":
            source = job["source_image"]
            assert set(source) == {"data", "mime", "sha256", "job_id"}
            assert source["data"] == self.outputs[source["job_id"]]
            assert source["mime"] == "image/png"
            assert hashlib.sha256(source["data"]).hexdigest() == source["sha256"]
        else:
            assert job["operation"] == "submit" and "source_image" not in job
        await ids(status="running", thread_id="synthetic-thread", turn_id="synthetic-turn")
        await self.gate.wait()
        data = self.images[1 if job["operation"] == "edit" else 0]
        self.outputs[job["id"]] = data
        self.output_root.mkdir(parents=True, exist_ok=True)
        path = self.output_root / (job["id"] + ".png")
        path.write_bytes(data)
        await image(str(path))
        if self.unknown:
            raise ConnectionError("Synthetic lost terminal receipt after image persistence")
        return "completed"


class Studio:
    def __init__(self, bridge, service, driver, path, renderer_class):
        self.bridge, self.service, self.driver = bridge, service, driver
        self.path, self.renderer_class, self.scope = path, renderer_class, Scope(**SCOPE)
        self.clock, self.sender, self.model = Clock(), Sender(), Script(driver)
        self.store = ArtistStore(path, clock=self.clock)
        self.store.create_space(SPACE, self.scope, binding_id=BINDING)
        self.store.grant(SPACE, self.scope, Delegation("synthetic-explicit-consent", "synthetic-admin", True))
        limits = Limits(think=100, render=20, share=10, max_inflight=10, max_revisions=3)
        self.store.set_global_limits(limits)
        self.store.set_limits(SPACE, self.scope, limits)
        self.build()

    def build(self):
        self.renderer = self.renderer_class(self.store, SPACE, self.scope, self.bridge, binding_id=BINDING)
        assert self.renderer.simulation is False
        self.artist = Artist(self.store, SPACE, self.scope, Thinking(self.model), self.renderer, self.sender,
                             allow_network_renderer=True)

    def reopen(self):
        # Every prior Artist call was awaited; no previous plugin worker survives.
        self.store.close()
        self.store = ArtistStore(self.path, clock=self.clock)
        self.build()

    def works(self):
        return self.store.list_artist_works(SPACE, self.scope)

    def renders(self):
        return [operation for operation in self.store.audit_operations(SPACE, self.scope)
                if operation["action"] in {"submit", "edit"}]

    def versions(self):
        return self.store.list_versions(SPACE, self.scope, self.works()[0]["work_id"])

    async def step(self):
        self.clock.advance()
        return await self.artist.tick()

    async def plan(self, chat=False):
        if chat:
            assert await self.artist.on_chat(message_id="synthetic-chat", text="雨夜的暖灯很好看")
            result = await self.artist.run_once()
        else:
            result = await self.artist.tick()
        assert result["state"] == "planned"
        assert not self.renders() and not self.driver.calls

    async def finish_render(self):
        request_id = self.works()[0]["pending_request"]
        self.driver.gate.set()
        async with asyncio.timeout(5):
            while True:
                with self.service.store.lock:
                    row = self.service.store.db.execute("SELECT * FROM jobs WHERE request_id=?", (request_id,)).fetchone()
                    assert row is not None
                    done = row["error_code"] == "GENERATION_UNCONFIRMED" if self.driver.unknown else row["status"] == "completed"
                if done:
                    return
                await asyncio.sleep(.01)

    async def original_ready(self):
        await self.plan()
        assert (await self.step())["state"] == "creating"
        await self.finish_render()
        assert (await self.step())["state"] == "review_pending"

    async def finish_without_edit(self):
        self.model.revisions = 0
        await self.original_ready()
        assert (await self.step())["state"] == "finished"


@pytest.fixture
async def studio(actual_mcp, tmp_path, monkeypatch):  # noqa: F811 - shared pytest fixture
    from PIL import Image
    from image_plugin_under_test.rendering import MCPRenderer

    bridge, service = actual_mcp
    images = []
    for size, color in [((23, 17), "purple"), ((29, 19), "orange")]:
        stream = io.BytesIO()
        Image.new("RGB", size, color).save(stream, format="PNG")
        images.append(stream.getvalue())
    driver = SyntheticDriver(service.cfg.state / "synthetic-output", images)
    monkeypatch.setattr(service, "driver", driver)
    assert service.worker is None
    service.worker = asyncio.create_task(service._work())
    exchange, trace, dropped = bridge._exchange, [], set()

    async def observed(operation, scope, args):
        trace.append((operation, deepcopy(args)))
        result = await exchange(operation, scope, args)
        if operation in dropped:
            dropped.remove(operation)
            raise ConnectionError("Synthetic reply loss after actual MCP acceptance")
        return result

    monkeypatch.setattr(bridge, "_exchange", observed)
    value = Studio(bridge, service, driver, tmp_path / "artist.sqlite3", MCPRenderer)
    value.trace, value.dropped = trace, dropped
    try:
        yield value
    finally:
        value.store.close()


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
