"""Studio 验收运行夹具及只读快照，模型、图片和发送均为模拟。"""

from copy import deepcopy
from datetime import datetime
from importlib.metadata import version as distribution_version
from types import SimpleNamespace
import asyncio
import hashlib
import json
import subprocess

import pytest

from image_plugin_under_test.domain.policy import Scope
from image_plugin_under_test.domain.store import ArtistStore
from image_plugin_under_test.host import host_runtime
from image_plugin_under_test.host.studio_chat import is_studio_status_message
from image_plugin_under_test.workspace import studio as studio_module
from protocol_fixture import PNG
from . import host_chain as host_fixture
from .catalog_examples import PROFILES
from .host_source import HOST_REV
from .settings import PLUGIN as ROOT
from .studio_acceptance_host import build_planner_request


SCOPE = Scope(host_fixture.PLATFORM, host_fixture.STREAM, host_fixture.OWNER)
SPACE = "studio"


class StudioHarness(host_fixture.Harness):
    """Only override the scripted model boundary; keep Host/SDK execution real."""

    def __init__(self, host, path):
        super().__init__(host, path)
        self.lose_model = False
        self.revise_catalogue = False
        self.query_sequence = 0
        self.components = {item["name"]: item for item in self.plugin.get_components()}
        meta = self.runner._loader.get_plugin(host_fixture.PLUGIN_ID)
        meta.component_handlers = {name: item["metadata"]["handler_name"]
                                   for name, item in self.components.items()}

    async def api(self, name, **args):
        component = self.components[name]
        assert component["type"] == "API" and component["metadata"]["public"] is False
        envelope = self.host.Envelope(request_id=1, message_type="request", method="plugin.invoke_api",
                                      plugin_id=host_fixture.PLUGIN_ID,
                                      payload={"component_name": name, "args": args})
        response = await self.runner._handle_invoke(envelope)
        assert response.error is None and response.payload["success"] is True, response
        return response.payload["result"]

    async def query(self, text="现在在画什么", *, session_id=host_fixture.STREAM):
        self.query_sequence += 1
        message_id = f"stage3-full-platform-status-id-{self.query_sequence}"
        if session_id == host_fixture.STREAM and is_studio_status_message({"processed_plain_text": text}):
            await self.message(text, message_id=message_id)
        original, history_count = build_planner_request(self, self.planner_host, [(message_id, text)])
        namespace = self.planner_host.namespace
        args = {"session_id": session_id, "item_schema_version": 1,
                "items": namespace["serialize_prompt_items"](original), "tool_definitions": [],
                "selected_history_count": history_count, "built_message_count": len(original),
                "selection_reason": "synthetic history selection"}
        envelope = self.host.Envelope(request_id=1, message_type="request", method="plugin.invoke_hook",
                                      plugin_id=host_fixture.PLUGIN_ID, payload={
                                          "component_name": "codex_image_studio_chat", "args": args})
        response = await self.runner._handle_hook_invoke(envelope)
        assert response.error is None and response.payload["success"] is True, response
        modified = response.payload.get("modified_kwargs")
        if modified is None:
            return None
        actual = namespace["deserialize_prompt_items"](
            modified["items"], item_schema_version=modified["item_schema_version"], original_items=original)
        assert len(actual) == len(original) + 2
        assert all(actual[index] is item for index, item in enumerate(original))
        return json.loads(actual[-1].parts[0].text)["data"]

    async def replyer(self, host, *, message_id, session_id=host_fixture.STREAM, tool_args=None):
        """Return actual model request items, with no synthetic raw payload API."""
        context = host.context
        original = [context.ContextItemBuilder().set_role(context.RoleType.User).add_text_content(text).build()
                    for text in ("窗边的小猫画了吗？", "请保留用户问题，依据上下文生成回复正文。")]
        component = self.components["codex_image_studio_replyer"]
        assert component["type"] == "HOOK_HANDLER"
        assert component["metadata"]["hook"] == "maisaka.replyer.before_model_request"
        invocations = []

        async def invoke_hook(name, **kwargs):
            invocations.append(name)
            assert name == component["metadata"]["hook"]
            assert kwargs["reply_message_id"] == (message_id or "")
            assert kwargs["selected_model_name"] == "synthetic-replyer-model"
            assert kwargs["item_schema_version"] == 1
            before = deepcopy(kwargs)
            envelope = self.host.Envelope(request_id=1, message_type="request", method="plugin.invoke_hook",
                                          plugin_id=host_fixture.PLUGIN_ID, payload={
                                              "component_name": component["name"], "args": kwargs})
            response = await self.runner._handle_hook_invoke(envelope)
            assert response.error is None and response.payload["success"] is True, response
            modified = response.payload.get("modified_kwargs") or kwargs
            assert {key: value for key, value in modified.items() if key != "items"} == {
                key: value for key, value in before.items() if key != "items"}
            assert modified["items"][:len(before["items"])] == before["items"]
            return SimpleNamespace(kwargs=modified)

        generator = host.generator()
        generator.request_type = "maisaka.replyer"
        generator._get_runtime_manager = lambda: SimpleNamespace(invoke_hook=invoke_hook)
        actual = await generator._invoke_before_model_request_hook(
            request_messages=original, session_id=session_id, active_task_name="replyer", active_model_name=None,
            model_info=SimpleNamespace(name="synthetic-replyer-model", visual=False), attempt=1, retry_count=0,
            reply_message=SimpleNamespace(message_id=message_id) if message_id is not None else None,
            reply_reason="synthetic ordinary response", selected_expression_ids=[], reply_tool_args=tool_args or {},
        )
        assert len(invocations) == 1
        assert all(actual[index] is item for index, item in enumerate(original))
        return original, actual

    async def capability(self, plugin_id, name, args):
        if name == "llm.generate" and self.lose_model:
            self.calls.append((name, deepcopy(args)))
            return {"success": False, "error": "synthetic model response lost"}
        result = await super().capability(plugin_id, name, args)
        if name != "llm.generate":
            return result
        answer = json.loads(result["response"])
        decision = answer.get("decision")
        if isinstance(decision, dict) and self.revise_catalogue:
            context = json.loads(args["prompt"][1]["content"][0]["text"])
            profile = max(context["profiles"], key=lambda item: item["revision"])
            answer["decision"] = {
                "action": "revise_profile", "reason": "synthetic catalogue revision",
                "profile_id": profile["profile_id"], "base_revision": profile["revision"],
                "data": {**deepcopy(profile["data"]), "palette": "cold blue"},
            }
            self.revise_catalogue = False
            return {**result, "response": json.dumps(answer, ensure_ascii=False)}
        if not isinstance(decision, dict) or decision["action"] != "create":
            return result
        context = json.loads(args["prompt"][1]["content"][0]["text"])
        if not context.get("profiles"):
            answer["decision"] = {"action": "create_profile", "reason": "synthetic catalogue plan",
                                  "data": deepcopy(PROFILES[0]["data"])}
        elif not context.get("series"):
            profile = context["profiles"][0]
            answer["decision"] = {"action": "create_series", "reason": "synthetic series plan",
                                  "title": "Synthetic windows", "goal": "Synthetic window lighting study",
                                  "profile_id": profile["profile_id"], "profile_revision": profile["revision"]}
        else:
            series = context["series"][0]
            answer["decision"]["creative"] = {
                "profile_id": series["profile_id"], "profile_revision": series["profile_revision"],
                "series_id": series["series_id"], "changes": {"light": "moonlight"},
                "reason": "synthetic continuity",
            }
        return {**result, "response": json.dumps(answer, ensure_ascii=False)}

    def operations(self):
        return self.runtime.store.audit_operations(SPACE, SCOPE)

    async def settled(self):
        # State transitions may precede receipt settlement/claim completion.
        await self.until(lambda: not self.runtime.store.db.execute(
            "SELECT 1 FROM artist_claims WHERE state='active'"
        ).fetchone())

    async def advance(self, state=None):
        claims = self.runtime.store.db.execute("SELECT count(*) FROM artist_claims").fetchone()[0]
        self.clock.advance()
        self.runtime.signals[SPACE].set()
        await self.until(lambda: self.runtime.store.db.execute(
            "SELECT count(*) FROM artist_claims").fetchone()[0] > claims)
        await self.settled()
        if state is not None:
            assert self.state() == state

    async def catalogue(self):
        self.gate.set()
        await self.until(lambda: bool(self.runtime.store.list_profiles(SPACE, SCOPE)))
        await self.settled()
        assert not self.renderer.calls and not self.sends
        await self.advance()
        await self.until(lambda: bool(self.runtime.store.list_series(SPACE, SCOPE)))
        await self.settled()
        assert not self.renderer.calls and not self.sends
        sources = self.runtime.store.db.execute("SELECT * FROM creative_sources").fetchall()
        assert len(sources) == 2 and all(row["origin"] == "autonomous" for row in sources)
        assert all(row["auth_version"] == 1 and row["source_ref"] for row in sources)
        assert all(op["action"] == "think" and op["state"] == "succeeded" for op in self.operations())

    async def completed_work(self, *, chat, revise):
        self.revise = revise
        await self.load()
        if chat:
            await self.message("synthetic rain and warm window light", message_id="stage3-creative-idea")
        await self.catalogue()
        await self.advance("planned")
        await self.settled()
        work = self.runtime.store.list_artist_works(SPACE, SCOPE)[0]
        assert work["brief"]["creative"]["profile"] == PROFILES[0]["data"]
        await self.advance("review_pending")
        await self.settled()
        if revise:
            await self.advance("planned")
            await self.settled()
            await self.advance("review_pending")
            await self.settled()
        await self.advance("finished")
        await self.settled()
        count = 2 if revise else 1
        assert len(self.renderer.calls) == len(self.reviews) == count
        assert len(self.received) == int(chat) and not self.sends
        assert all("warm amber" in call["prompt"] for call in self.renderer.calls)
        if revise:
            assert self.renderer.calls[1]["source"]["data"] == PNG
        for review in self.reviews:
            context = json.loads(review["prompt"][1]["content"][0]["text"])
            assert context["current_work"]["brief"] == work["brief"]
            assert context["review_inputs"]  # Harness also validates actual image_url bytes.
        return work

    async def share_work(self, *, unknown=False):
        self.share, self.lose_send = True, unknown
        await self.advance()
        await self.until(lambda: len(self.sends) == 1)
        await self.until(lambda: any(op["action"] == "share" and op["state"] in
                                    {"succeeded", "unknown"} for op in self.operations()))
        await self.settled()


@pytest.fixture(scope="module")
def source_snapshot():
    tracked = sorted(["plugin.py"] + [path.relative_to(ROOT).as_posix()
                            for package in ("domain", "creation", "host", "inspiration", "mcp_client", "workspace")
                            for path in (ROOT / package).glob("*.py")]) + [
        "_manifest.json", "webui.json", "assets/studio.html", "pyproject.toml",
        "tests/conftest.py", "tests/protocol_fixture.py", "tests/test_artist_studio_e2e.py",
        "tests/test_artist_studio_suggestion_e2e.py",
        *[f"tests/support/{name}.py" for name in (
            "__init__", "artist_fakes", "catalog_examples", "host_chain", "host_source",
            "media_samples", "settings", "studio_acceptance", "studio_acceptance_host",
        )],
    ]
    return tracked, {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in tracked}


@pytest.fixture
async def studio_chain(public_host, planner_host, tmp_path, monkeypatch, record_property, source_snapshot):
    tracked, hashes = source_snapshot
    assert hashes == {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in tracked}, (
        "Source changed between acceptance cases; rerun the entire suite against one version"
    )
    value = StudioHarness(public_host, tmp_path)
    value.planner_host = planner_host
    store_class, serve = ArtistStore, host_runtime.HostRuntime._serve
    monkeypatch.setattr(host_runtime, "ArtistStore", lambda path: store_class(path, clock=value.clock))

    async def gated(runtime, space):
        await value.gate.wait()
        await serve(runtime, space)

    monkeypatch.setattr(host_runtime.HostRuntime, "_serve", gated)
    monkeypatch.setattr(value.plugin, "_artist_renderer", lambda *args: value.renderer)

    class TestDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return value.clock().astimezone(tz)

    monkeypatch.setattr(studio_module, "datetime", TestDatetime)
    record_property("host_revision", HOST_REV)
    base_revision = await asyncio.to_thread(subprocess.check_output,
                                           ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True)
    record_property("plugin_base_revision", base_revision.strip())
    record_property("sdk_version", distribution_version("maibot-plugin-sdk"))
    record_property("plugin_version", json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))["version"])
    record_property("source_digest", hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest())
    record_property("plugin_source_sha256", json.dumps(hashes, sort_keys=True))
    try:
        yield value
    finally:
        await value.plugin.on_unload()
        await value.dispatcher.stop()
        assert hashes == {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in tracked}, (
            "Source changed during acceptance; rerun against a stable version"
        )


def state_snapshot(h):
    """Logical SQLite contents and file bytes detect hidden writes/read recovery."""
    db = h.runtime.store
    with db.lock:
        ledger = tuple(db.db.iterdump())
    receipts = tuple(h.plugin.journal.db.iterdump())
    # Windows 的持有中系统锁不可再次读字节；锁只比较存在性/大小，账本仍比较完整内容。
    files = {
        str(path.relative_to(h.data_paths.data_dir)):
        ("owned-lock", path.stat().st_size) if path.name in {"artist.lock", "delivery.lock"}
        else hashlib.sha256(path.read_bytes()).hexdigest()
        for path in h.data_paths.data_dir.rglob("*") if path.is_file()
    }
    effects = [call for call in h.calls if not call[0].startswith("chat.get_stream_by_")]
    return ledger, receipts, files, deepcopy(h.renderer.calls), deepcopy(h.sends), deepcopy(effects)
