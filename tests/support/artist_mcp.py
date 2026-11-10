"""Artist 与独立 MCP 的联调夹具，仅使用合成图片和临时账本。"""

from copy import deepcopy
import asyncio
import hashlib
import io

import pytest

from image_plugin_under_test.creation.artist import Artist
from image_plugin_under_test.creation.thinking import Thinking
from image_plugin_under_test.domain.policy import Delegation, Limits, Scope
from image_plugin_under_test.domain.store import ArtistStore
from .artist_fakes import BRIEF, Clock, Sender
from .settings import SCOPE


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
    from image_plugin_under_test.creation.rendering import MCPRenderer

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
