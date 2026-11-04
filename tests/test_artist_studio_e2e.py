"""Stage 3 independent acceptance using fixed public Host code and real SQLite.

External Host contract: set CODEX_IMAGE_TEST_HOST_REPO to a public MaiBot Git
repository containing HOST_REV, then run pytest tests/test_artist_studio_e2e.py.
Without that variable only this external contract is skipped. An explicitly
configured repository lacking the fixed object fails; no moving checkout is used.
Only public Host methods are AST-loaded; model answers, PNG bytes, renderer and platform
receipts are synthetic. This is not a live adapter/model/deployment acceptance.
Private WebUI APIs and planner/replyer hooks run through actual Runner methods.
WebUI authentication and browser rendering are outside this isolated acceptance.
"""

from copy import deepcopy
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from html import escape
from importlib.metadata import version as distribution_version
from pathlib import Path
from threading import Event as ThreadEvent
from types import ModuleType, SimpleNamespace
from typing import ParamSpec, TypeVar
import ast
import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys

import pytest

from image_plugin_under_test import host_runtime
from image_plugin_under_test import studio as studio_module
from image_plugin_under_test.policy import Limits, Scope
from image_plugin_under_test.store import ArtistStore
from image_plugin_under_test.studio import Studio
from image_plugin_under_test.studio_chat import is_studio_status_message
from protocol_fixture import PNG
from test_profiles_flow import PROFILES
import test_host_integration as host_fixture


ROOT = Path(__file__).resolve().parents[1]
SCOPE = Scope(host_fixture.PLATFORM, host_fixture.STREAM, host_fixture.OWNER)
SPACE = "studio"


@pytest.fixture
def public_host(monkeypatch, tmp_path):
    """Explicit opt-in selects a repository; its immutable object selects code."""
    repository = os.environ.get("CODEX_IMAGE_TEST_HOST_REPO")
    if repository is None:
        pytest.skip("External fixed-Host contract: set CODEX_IMAGE_TEST_HOST_REPO to a public MaiBot Git "
                    f"repository containing {host_fixture.HOST_REV}, then run pytest tests/test_artist_studio_e2e.py")
    assert repository.strip(), "Explicit CODEX_IMAGE_TEST_HOST_REPO must name a public MaiBot Git repository"
    host_repo = Path(repository).resolve()
    assert host_repo.is_dir(), f"Explicit CODEX_IMAGE_TEST_HOST_REPO is not a directory: {host_repo}"

    def source(relative):
        result = subprocess.run(
            ["git", "-C", str(host_repo), "show", f"{host_fixture.HOST_REV}:src/{relative}"],
            capture_output=True, check=False,
        )
        assert result.returncode == 0, (
            f"Required public Host {host_fixture.HOST_REV} missing: {relative}; "
            "acceptance must fail, not skip or use a moving checkout"
        )
        return result.stdout.decode("utf-8-sig")

    monkeypatch.setattr(host_fixture, "host_file", source)
    host = host_fixture.public_host.__wrapped__(monkeypatch, tmp_path)
    host_fixture.load_nodes(host.__dict__, host_fixture.public_source("protocol/envelope.py"),
                            {"InvokeResultPayload"})
    methods = {"_inject_context", "_create_plugin_context", "_resolve_component_handler_name",
               "_resolve_component_handler", "_invoke_plugin_callable", "_handle_hook_invoke", "_handle_invoke"}
    host_fixture.load_nodes(host.__dict__, host_fixture.public_source("runner/runner_main.py"),
                            {"PluginRunner"}, {"PluginRunner": methods})
    return host


@pytest.fixture
def replyer_host(public_host, monkeypatch):
    """Fixed Host serialization, target selection and request deserialization."""
    package = ModuleType("_stage3_replyer_context")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, package.__name__, package)
    modules = []
    for name in ("context_item", "context_snapshot", "context_protocol"):
        module = ModuleType(package.__name__ + "." + name)
        monkeypatch.setitem(sys.modules, module.__name__, module)
        source = host_fixture.host_file("llm_models/payload_content/" + name + ".py")
        exec(compile(source, "<fixed-host-" + name + ">", "exec"), module.__dict__)  # noqa: S102 - Fixed public Git object.
        modules.append(module)
    namespace = dict(public_host.__dict__)
    for module in modules:
        namespace.update(vars(module))
    host_fixture.load_nodes(namespace, host_fixture.public_source("hook_payloads.py"),
                            {"serialize_prompt_items", "deserialize_prompt_items"})
    tree = ast.parse(host_fixture.host_file("chat/replyer/maisaka_generator_base.py"))
    generator = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                     and node.name == "BaseMaisakaReplyGenerator")
    generator.bases = []
    generator.body = [node for node in generator.body
                      if getattr(node, "name", None) == "_invoke_before_model_request_hook"]
    assert len(generator.body) == 1
    constant = next(node for node in tree.body if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "REPLYER_MAX_HOOK_RETRIES" for target in node.targets))
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    exec(  # noqa: S102 - Only named methods/constants from the fixed public Git object.
        compile(ast.fix_missing_locations(ast.Module(body=[future, constant, generator], type_ignores=[])),
                "<fixed-host-replyer>", "exec"), namespace)
    return SimpleNamespace(context=modules[0], generator=namespace[generator.name], namespace=namespace)


@pytest.fixture
def planner_host(replyer_host):
    """Real request constructor, prefix, time/attention and final user reminders."""
    namespace = dict(replyer_host.namespace)
    aliases = {}
    exec(  # noqa: S102 - Pure alias helpers from the fixed public Git object.
        compile(host_fixture.host_file("maisaka/context/message_id_alias.py"), "<fixed-host-alias>", "exec"), aliases)
    namespace.update(escape=escape, to_display_message_id=aliases["to_display_message_id"],
                     ContentContextMessage=type(None), IMAGE_QUALITY_MAX_SIDE={"normal": 512},
                     global_config=SimpleNamespace(visual=SimpleNamespace(image_quality="normal")),
                     P=ParamSpec("P"), R=TypeVar("R"), wraps=wraps,
                     _pending_images=ContextVar("stage3-pending-images", default=None))
    host_fixture.load_nodes(namespace, host_fixture.host_file("maisaka/visual/deferred_images.py"),
                            {"defer_context_image_encoding"})
    host_fixture.load_nodes(namespace, host_fixture.host_file("maisaka/context/planner_messages.py"),
                            {"build_planner_prefix", "_format_quote_ids"})
    host_fixture.load_nodes(namespace, host_fixture.host_file("maisaka/context/messages.py"),
                            {"build_context_items_from_history_entry"})
    tree = ast.parse(host_fixture.host_file("maisaka/chat_loop_service.py"))
    constructor = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                       and node.name == "MaisakaChatLoopService")
    constructor.bases = []
    methods = {"_build_request_messages", "_build_current_time_user_message", "_build_time_user_message",
               "_build_current_chat_attention_tail_message", "_build_planner_final_user_reminder",
               "_validate_function_call_context_anchors"}
    constructor.body = [node for node in constructor.body if getattr(node, "name", None) in methods]
    assert {node.name for node in constructor.body} == methods
    constant = next(node for node in tree.body if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "PLANNER_FINAL_USER_REMINDER" for target in node.targets))
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    exec(  # noqa: S102 - Fixed Host constructor and reminder, without application bootstrap.
        compile(ast.fix_missing_locations(ast.Module(body=[future, constant, constructor], type_ignores=[])),
                "<fixed-host-planner>", "exec"), namespace)
    return SimpleNamespace(context=replyer_host.context, namespace=namespace, constructor=namespace[constructor.name])


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
    tracked = sorted(path.name for path in ROOT.glob("*.py")) + [
        "_manifest.json", "webui.json", "studio.html", "pyproject.toml",
        "tests/conftest.py", "tests/protocol_fixture.py", "tests/test_artist_flow.py",
        "tests/test_profiles_flow.py", "tests/test_host_integration.py", "tests/test_artist_studio_e2e.py",
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
    record_property("host_revision", host_fixture.HOST_REV)
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
    files = {str(path.relative_to(h.data_paths.data_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in h.data_paths.data_dir.rglob("*") if path.is_file() and path.name != "artist.lock"}
    effects = [call for call in h.calls if not call[0].startswith("chat.get_stream_by_")]
    return ledger, receipts, files, deepcopy(h.renderer.calls), deepcopy(h.sends), deepcopy(effects)


def assert_projection(h):
    view = Studio(h.data_paths.data_dir, scopes={SPACE: SCOPE}, clock=h.clock, enabled=True)
    summary = view.summary(SPACE)
    ledger = h.operations()
    assert summary["read_only"] and summary["authorization"]["authorized"]
    for category in ("think", "render", "share"):
        usage = h.runtime.store.budget_usage(SPACE, SCOPE)[category]
        assert summary["budget"][category]["held"] == usage["held"]
        assert summary["budget"][category]["consumed"] == usage["consumed"]
    tasks = view.records(SPACE, "tasks", limit=50)["items"]
    assert {row["request_id"]: row["state"] for row in tasks} == {
        row["request_id"]: row["state"] for row in ledger
    }
    assert view.records(SPACE, "profiles")["items"][0]["data"] == PROFILES[0]["data"]
    versions = view.records(SPACE, "versions")["items"]
    assert summary["counts"]["versions"] == len(versions)
    for version in versions:
        assert version["sha256"] == hashlib.sha256(PNG).hexdigest()
        assert view.image(SPACE, version["version_id"], thumbnail=False)["sha256"] == version["sha256"]
    return summary


async def assert_public_reads(h):
    """Read through the four WebUI API registrations and the planner hook."""
    expected = assert_projection(h)
    spaces = await h.api("studio_spaces", offset=0, limit=20)
    assert spaces["items"] == [{"space_id": SPACE, "authorized": True, "state_exists": True}]
    # Sharing permission is an administrative projection, not private context
    # automatically disclosed to the chat planner/replyer.
    assert "sharing" not in expected
    assert await h.api("studio_summary", space_id=SPACE) == {
        **expected, "sharing": {"mode": "private", "allowed": True},
    }
    for section in ("works", "tasks", "versions", "reviews", "profiles", "series", "assets"):
        result = await h.api("studio_records", space_id=SPACE, section=section, offset=0, limit=50, work_id="")
        view = Studio(h.data_paths.data_dir, scopes={SPACE: SCOPE}, clock=h.clock)
        assert result == view.records(SPACE, section, limit=50)
    versions = await h.api("studio_records", space_id=SPACE, section="versions")
    for version in versions["items"]:
        original = await h.api("studio_image", space_id=SPACE, version_id=version["version_id"],
                               thumbnail=False, offset=0)
        assert base64.b64decode(original["chunk_base64"], validate=True) == PNG
        assert original["next_offset"] is None and original["total_bytes"] == len(PNG)
        thumbnail = await h.api("studio_image", space_id=SPACE, version_id=version["version_id"],
                                thumbnail=True, offset=0)
        assert thumbnail["data_url"].startswith("data:image/jpeg;base64,")
    # Reading an ordinary status question through real receive_message must not
    # queue a new inspiration/wake; planner reading must not consume any quota.
    text = "现在在画什么"
    assert is_studio_status_message({"processed_plain_text": text})
    await h.message(text, message_id="stage3-read-only-question")
    assert_chat_matches(await h.query(text), expected)
    return expected


def assert_chat_matches(data, summary):
    assert data is not None
    for key in ("space_id", "day", "read_only", "state_exists", "budget", "counts", "waiting",
                "current_work", "recent_works"):
        assert data[key] == summary[key]
    for key in ("authorized", "version"):
        assert data["authorization"][key] == summary["authorization"][key]


@pytest.mark.parametrize("chat,revise", [(True, True), (False, False)],
                         ids=["chat-refine-share", "idle-accept-share"])
async def test_host_catalogue_review_share_and_readonly_projection(studio_chain, chat, revise):
    h = studio_chain
    await h.completed_work(chat=chat, revise=revise)
    await h.share_work()
    assert h.plugin.journal.db.execute("SELECT state FROM delivery").fetchone()[0] == "sent"
    before = state_snapshot(h)
    for _ in range(3):
        await assert_public_reads(h)
    assert state_snapshot(h) == before
    await h.advance()
    await h.settled()
    assert len(h.sends) == 1 and len(h.renderer.calls) == (2 if revise else 1)


async def test_host_unknown_send_is_visible_and_reload_does_not_replay(studio_chain):
    h = studio_chain
    await h.completed_work(chat=False, revise=False)
    await h.share_work(unknown=True)
    before = state_snapshot(h)
    for _ in range(3):
        summary = await assert_public_reads(h)
        assert "SEND_UNKNOWN" in {row["code"] for row in summary["waiting"]}
        assert summary["budget"]["share"]["held"] == 1
    assert state_snapshot(h) == before
    await h.plugin.on_config_update("plugin", {}, "stage3-synthetic-reload")
    await h.advance()
    await h.settled()
    assert len(h.sends) == len(h.renderer.calls) == 1
    assert h.runtime.store.budget_usage(SPACE, SCOPE)["share"]["held"] == 1
    assert h.plugin.journal.db.execute("SELECT state FROM delivery").fetchone()[0] == "send_unknown"


async def test_host_zero_budget_prevents_catalogue_and_effects(studio_chain):
    h = studio_chain
    await h.load()
    await h.runtime.db("set_limits", SPACE, SCOPE, Limits(think=0, render=0, share=0, max_inflight=0))
    h.gate.set()
    await asyncio.sleep(.15)
    await h.settled()
    before = state_snapshot(h)
    view = Studio(h.data_paths.data_dir, scopes={SPACE: SCOPE}, clock=h.clock)
    for _ in range(3):
        summary = await h.api("studio_summary", space_id=SPACE)
        assert_chat_matches(await h.query(), summary)
        assert "SPACE_BUDGET_EXHAUSTED" in {row["code"] for row in summary["waiting"]}
        assert not view.records(SPACE, "profiles")["items"]
        assert not view.records(SPACE, "works")["items"]
    assert state_snapshot(h) == before
    await h.advance()
    await h.settled()
    assert not h.operations() and not h.renderer.calls and not h.sends
    assert all(name != "llm.generate" for name, _ in h.calls)


@pytest.mark.parametrize("category", ["think", "render"])
async def test_unknown_model_or_render_query_and_reload_never_replays(studio_chain, monkeypatch, category):
    h = studio_chain
    await h.load()
    if category == "think":
        h.lose_model = True
        h.gate.set()
    else:
        await h.catalogue()
        await h.advance("planned")

        async def lost_render(**kwargs):
            h.renderer.calls.append(deepcopy(kwargs))
            raise OSError("synthetic renderer response lost")

        monkeypatch.setattr(h.renderer, "submit", lost_render)
        await h.advance("creating")
    await h.until(lambda: any(op["category"] == category and op["state"] == "unknown" for op in h.operations()))
    await h.settled()
    operation = next(op for op in h.operations() if op["state"] == "unknown")
    calls = sum(name == "llm.generate" for name, _ in h.calls)
    before = state_snapshot(h)
    for _ in range(3):
        summary = await h.api("studio_summary", space_id=SPACE)
        assert category.upper() + "_UNKNOWN" in {row["code"] for row in summary["waiting"]}
        assert summary["budget"][category]["held"] == 1
        assert_chat_matches(await h.query(), summary)
        await h.message("现在在画什么", message_id="stage3-unknown-status")
    assert state_snapshot(h) == before
    await h.plugin.on_config_update("plugin", {}, "stage3-unknown-reload")
    await h.message("synthetic new idea cannot unlock an unknown call", message_id="stage3-new-wake")
    await h.settled()
    await h.advance()
    later_models = [args for name, args in h.calls if name == "llm.generate"][calls:]
    if category == "think":
        assert not later_models
    else:
        # Fresh chat can legitimately inspire future work while the old render
        # is unknown; it cannot replan, redraw or unlock that operation.
        assert all("不得规划或覆盖" in args["prompt"][0]["content"] for args in later_models)
    assert len(h.renderer.calls) == int(category == "render") and not h.sends
    assert next(op for op in h.operations() if op["request_id"] == operation["request_id"])["state"] == "unknown"
    assert h.runtime.store.budget_usage(SPACE, SCOPE)[category]["held"] == 1


async def test_foreign_private_chat_and_creative_request_do_not_get_studio_context(studio_chain):
    h = studio_chain
    await h.load()
    # Keep the genuine worker gated: denial must not open a new stateful path.
    before = state_snapshot(h)
    assert await h.query(session_id="unconfigured-foreign-stream") is None
    assert await h.query("现在在画什么？再帮我画一只猫") is None
    assert not is_studio_status_message({"processed_plain_text": "现在在画什么？再帮我画一只猫"})
    assert state_snapshot(h) == before


async def test_autonomous_profile_revision_is_visible_without_changing_finished_work(studio_chain):
    h = studio_chain
    work = await h.completed_work(chat=False, revise=False)
    before = deepcopy(h.renderer.calls), deepcopy(h.sends)
    thoughts = sum(op["category"] == "think" for op in h.operations())
    h.revise_catalogue = True
    await h.advance()
    profiles = h.runtime.store.list_profiles(SPACE, SCOPE)
    assert {profile["revision"] for profile in profiles} == {1, 2}
    assert h.runtime.store.get_artist_work(SPACE, SCOPE, work["work_id"])["brief"] == work["brief"]
    assert (h.renderer.calls, h.sends) == before
    assert sum(op["category"] == "think" for op in h.operations()) == thoughts + 1
    snapshot = state_snapshot(h)
    result = await h.api("studio_records", space_id=SPACE, section="profiles", offset=0, limit=50)
    assert {row["revision"]: row["data"]["palette"] for row in result["items"]} == {
        1: "warm amber", 2: "cold blue",
    }
    summary = await h.api("studio_summary", space_id=SPACE)
    assert_chat_matches(await h.query(), summary)
    assert state_snapshot(h) == snapshot


def assert_replyer_work_matches_ledger(h, data, work_id, *, share_state):
    """Assert user-facing facts against independent raw ledger/receipt evidence."""
    store = h.runtime.store
    work = store.get_artist_work(SPACE, SCOPE, work_id)
    versions = store.db.execute("SELECT job_id FROM versions WHERE space_id=? AND work_id=?",
                                (SPACE, work_id)).fetchall()
    latest_review = store.db.execute("SELECT review FROM reviews WHERE space_id=? AND work_id=? ORDER BY id DESC LIMIT 1",
                                     (SPACE, work_id)).fetchone()
    expected = {"work_id": work_id, "subject": work["brief"]["subject"], "status": work["status"],
                "generated": bool(versions), "version_count": len(versions), "share_state": share_state,
                "review_state": json.loads(latest_review[0])["decision"] if latest_review else "not_generated"}
    recent = next(item for item in data["recent_works"] if item["work_id"] == work_id)
    assert {key: recent[key] for key in expected} == expected
    if work["status"] in {"planned", "deferred", "creating", "review_pending"}:
        assert data["current_work"] == recent
    else:
        assert data["current_work"] is None
    shares = [operation for operation in h.operations() if operation["category"] == "share"]
    receipts = [h.plugin.journal.db.execute("SELECT state FROM delivery WHERE job=?", (version["job_id"],)).fetchone()
                for version in versions]
    if share_state == "not_shared":
        assert not shares and not any(receipts)
    elif share_state == "sent":
        assert shares[-1]["state"] == "succeeded" and receipts[-1][0] == "sent"
    else:
        assert share_state == "send_unknown"
        assert shares[-1]["state"] == "unknown" and receipts[-1][0] == "send_unknown"


@pytest.mark.parametrize("stage", ["planned", "rendered", "sent", "send_unknown"])
async def test_actual_reply_target_gets_work_facts_from_real_ledger(studio_chain, replyer_host, stage):
    h = studio_chain
    if stage == "planned":
        await h.load()
        await h.catalogue()
        await h.advance("planned")
        work = h.runtime.store.list_artist_works(SPACE, SCOPE)[0]
    else:
        work = await h.completed_work(chat=False, revise=False)
        if stage != "rendered":
            await h.share_work(unknown=stage == "send_unknown")
    message_id = "stage3-full-received-platform-message-" + stage
    before = state_snapshot(h)
    await h.message("窗边的小猫画了吗？", message_id=message_id)
    assert state_snapshot(h) == before
    original, actual = await h.replyer(replyer_host, message_id=message_id, tool_args={
        "reply_message_id": "model-forged-target", "facts": "模型伪造：已全部送达", "retry": True,
    })
    assert len(actual) == len(original) + 2
    assert type(actual[-2]).__name__ == "SystemMessageItem"
    assert type(actual[-1]).__name__ == "UserMessageItem"
    projection = json.loads(actual[-1].parts[0].text)
    assert projection["source"] == "codex_image_studio_readonly"
    summary = await h.api("studio_summary", space_id=SPACE)
    assert_chat_matches(projection["data"], summary)
    assert_replyer_work_matches_ledger(h, projection["data"], work["work_id"],
                                       share_state=stage if stage in {"sent", "send_unknown"} else "not_shared")
    # A duplicate receive cannot rearm the consumed target, nor retry its reply.
    await h.message("窗边的小猫画了吗？", message_id=message_id)
    original, repeated = await h.replyer(replyer_host, message_id=message_id)
    assert repeated == original
    assert state_snapshot(h) == before


async def test_replyer_unmatched_targets_never_leak_or_consume_real_target(studio_chain, replyer_host):
    h = studio_chain
    work = await h.completed_work(chat=False, revise=False)
    await h.share_work(unknown=True)
    message_id = "stage3-received-query-full-platform-id"
    await h.message("窗边的小猫画了吗？", message_id=message_id)
    await h.message("窗边的小猫画了吗？", owner="synthetic-foreign-owner", message_id="foreign-owner-message")
    before = state_snapshot(h)
    calls = len(h.calls)
    forged = {"reply_message_id": message_id, "session_id": host_fixture.STREAM,
              "user_id": host_fixture.OWNER, "query": "窗边的小猫画了吗？", "authorized": True}
    for target, session in (("unreceived-message", host_fixture.STREAM), ("mshort", host_fixture.STREAM),
                            (None, host_fixture.STREAM), ("foreign-owner-message", host_fixture.STREAM),
                            (message_id, "foreign-private-session")):
        original, actual = await h.replyer(replyer_host, message_id=target, session_id=session, tool_args=forged)
        assert actual == original  # No facts or authority text enters the actual model request.
        assert len(h.calls) == calls  # Misses do not even consult Host lookup/ledger.
    original, actual = await h.replyer(replyer_host, message_id=message_id, tool_args=forged)
    assert len(actual) == len(original) + 2  # Earlier misses did not consume the valid note.
    data = json.loads(actual[-1].parts[0].text)["data"]
    assert_replyer_work_matches_ledger(h, data, work["work_id"], share_state="send_unknown")
    assert state_snapshot(h) == before


def build_planner_request(h, host, messages):
    ci, namespace = host.context, host.namespace
    history = []
    # Selection is synthetic; Host prefix construction, Context Items, request
    # construction, serialization and roundtrip are actual fixed public code.
    for message_id, text in messages:
        prefix = namespace["build_planner_prefix"](timestamp=h.clock(), user_name="synthetic-owner",
                                                   message_id=message_id)
        item = ci.ContextItemBuilder().set_role(ci.RoleType.User).add_text_content(prefix + text).build()
        history.append(SimpleNamespace(timestamp=h.clock(), to_context_item=lambda item=item, **kwargs: item))
    constructor = host.constructor()
    constructor._custom_chat_system_prompt = None
    constructor._session_id, constructor._is_group_chat = host_fixture.STREAM, False
    constructor._get_chat_prompt_name = lambda: "maisaka_chat"
    constructor._build_bot_nickname_notice = lambda *args, **kwargs: ""
    constructor._get_chat_prompt_for_chat = lambda *args: "synthetic current-chat attention"
    original, history_count = constructor._build_request_messages(
        history, enable_visual_message=False, system_prompt="synthetic planner system",
        final_user_message=constructor._build_planner_final_user_reminder(),
    )
    assert history_count == len(history)
    assert all(type(item).__name__ == "UserMessageItem" for item in original[-3:])
    assert original[-1].parts[0].text == namespace["PLANNER_FINAL_USER_REMINDER"]
    assert "synthetic current-chat attention" in original[-2].parts[0].text
    return original, history_count


@pytest.mark.parametrize("case", ["status-with-real-user-tails", "older-query-is-not-current",
                                   "newer-nonquery-omitted-from-selection"])
async def test_real_planner_constructor_with_tail_reminders(studio_chain, planner_host, replyer_host, case):
    h = studio_chain
    work = await h.completed_work(chat=False, revise=False)
    query = case == "status-with-real-user-tails"
    messages = [("stage3-first-query-platform-id", "现在在画什么")]
    if query:
        messages.append(("stage3-latest-query-platform-id", "窗边的小猫画了吗？"))
    for message_id, text in messages:
        await h.message(text, message_id=message_id)
    if not query:
        claims = h.runtime.store.db.execute("SELECT count(*) FROM artist_claims").fetchone()[0]
        unrelated = ("stage3-latest-unrelated-platform-id", "synthetic unrelated latest message")
        await h.message(unrelated[1], message_id=unrelated[0])
        await h.until(lambda: h.runtime.store.db.execute("SELECT count(*) FROM artist_claims").fetchone()[0] > claims)
        await h.settled()
        if case == "older-query-is-not-current":
            messages.append(unrelated)
    ci, namespace = planner_host.context, planner_host.namespace
    original, history_count = build_planner_request(h, planner_host, messages)
    args = {"session_id": host_fixture.STREAM, "item_schema_version": ci.CONTEXT_ITEM_SCHEMA_VERSION,
            "items": namespace["serialize_prompt_items"](original), "tool_definitions": [],
            "selected_history_count": history_count, "built_message_count": len(original),
            "selection_reason": "synthetic history selection"}
    before = state_snapshot(h)
    calls = len(h.calls)
    envelope = h.host.Envelope(request_id=1, message_type="request", method="plugin.invoke_hook",
                               plugin_id=host_fixture.PLUGIN_ID, payload={
                                   "component_name": "codex_image_studio_chat", "args": args})
    response = await h.runner._handle_hook_invoke(envelope)
    assert response.error is None and response.payload["success"] is True
    modified = response.payload.get("modified_kwargs")
    if query:
        assert modified is not None, "STUDIO-R1-01: genuine planner user-role reminders must not hide the latest IM query"
        actual = namespace["deserialize_prompt_items"](
            modified["items"], item_schema_version=modified["item_schema_version"], original_items=original)
        assert len(actual) == len(original) + 2
        assert all(actual[index] is item for index, item in enumerate(original))
        data = json.loads(actual[-1].parts[0].text)["data"]
        assert_replyer_work_matches_ledger(h, data, work["work_id"], share_state="not_shared")
    else:
        assert modified is None
        assert len(h.calls) == calls
        # The same newer nonquery must also supersede the old formal reply
        # target, independently of which items the planner chose to include.
        initial, actual = await h.replyer(replyer_host, message_id=messages[0][0])
        assert actual == initial
        assert len(h.calls) == calls
    assert state_snapshot(h) == before


@pytest.mark.parametrize("stage", ["planner", "replyer"])
async def test_new_ingress_invalidates_old_snapshot_while_lifecycle_is_contended(
        studio_chain, planner_host, replyer_host, monkeypatch, stage):
    h = studio_chain
    await h.load()  # Keep the synthetic worker gated; ordinary ingress still uses the real store.
    old_id, new_id = "stage3-lock-old-query-" + stage, "stage3-lock-new-ordinary-" + stage
    new_text = "synthetic ordinary message received while the old read holds lifecycle"
    await h.message("现在在画什么", message_id=old_id)
    before = state_snapshot(h)
    loop, lifecycle = asyncio.get_running_loop(), h.plugin.lifecycle
    snapshot_captured, ingress_waiting, read_completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_snapshot, release_ingress = ThreadEvent(), asyncio.Event()
    summaries = []
    real_summary, real_read = Studio.summary, h.plugin.studio_chat.read

    class ContendedLifecycle:
        async def __aenter__(self):
            if lifecycle.locked():
                # Hold the contender even after the read releases the lock.
                # Otherwise the old observer might catch up before the final
                # freshness check, masking note_ingress being inside lifecycle.
                ingress_waiting.set()
                await release_ingress.wait()
            await lifecycle.acquire()
            return self

        async def __aexit__(self, *args):
            lifecycle.release()

    def captured_summary(view, space_id):
        summary = real_summary(view, space_id)
        summaries.append(summary)
        loop.call_soon_threadsafe(snapshot_captured.set)
        assert release_snapshot.wait(5), "Snapshot release event was not signalled"
        return summary

    async def completed_read(space_id, scope):
        snapshot = await real_read(space_id, scope)
        read_completed.set()  # Distinguish supersession from timeout/cancellation or read failure.
        return snapshot

    async def old_request():
        if stage == "replyer":
            return await h.replyer(replyer_host, message_id=old_id)
        original, count = build_planner_request(h, planner_host, [(old_id, "现在在画什么")])
        namespace = planner_host.namespace
        envelope = h.host.Envelope(request_id=1, message_type="request", method="plugin.invoke_hook",
                                   plugin_id=host_fixture.PLUGIN_ID, payload={
                                       "component_name": "codex_image_studio_chat", "args": {
                                           "session_id": host_fixture.STREAM, "item_schema_version": 1,
                                           "items": namespace["serialize_prompt_items"](original),
                                           "tool_definitions": [], "selected_history_count": count,
                                           "built_message_count": len(original),
                                           "selection_reason": "synthetic contended-read selection"}})
        response = await h.runner._handle_hook_invoke(envelope)
        assert response.error is None and response.payload["success"] is True
        modified = response.payload.get("modified_kwargs")
        actual = namespace["deserialize_prompt_items"](
            modified["items"], item_schema_version=modified["item_schema_version"], original_items=original,
        ) if modified is not None else original
        return original, actual

    monkeypatch.setattr(h.plugin, "lifecycle", ContendedLifecycle())
    monkeypatch.setattr(Studio, "summary", captured_summary)
    monkeypatch.setattr(h.plugin.studio_chat, "read", completed_read)
    pending = []
    try:
        old_task = asyncio.create_task(old_request())
        pending.append(old_task)
        await asyncio.wait_for(snapshot_captured.wait(), 5)
        assert lifecycle.locked() and not old_task.done()
        new_task = asyncio.create_task(h.message(new_text, message_id=new_id))
        pending.append(new_task)
        await asyncio.wait_for(ingress_waiting.wait(), 5)
        assert h.received[-1].message_id == new_id
        assert h.hook_calls[-1]["message"]["message_id"] == new_id
        assert not new_task.done() and not old_task.done()
        release_snapshot.set()
        original, actual = await asyncio.wait_for(old_task, 5)
        assert read_completed.is_set() and len(summaries) == 1 and summaries[0]["read_only"] is True
        assert not new_task.done(), "New ordinary observer must remain blocked until stale-result assertion"
        assert actual == original, "Old query facts crossed a newer Host ingress pending on lifecycle"
        assert all(actual[index] is item for index, item in enumerate(original))
        assert state_snapshot(h) == before

        release_ingress.set()
        message = await asyncio.wait_for(new_task, 5)
        assert h.received[-1] is message and message.processed_plain_text == new_text
        wakes = h.runtime.store.db.execute(
            "SELECT event_id,kind,text,source_ref FROM artist_wakes WHERE space_id=?", (SPACE,),
        ).fetchall()
        assert [tuple(row) for row in wakes] == [("chat:" + new_id, "chat", new_text, new_id)]
        assert not h.operations() and not h.renderer.calls and not h.sends
        assert all(name != "llm.generate" for name, _ in h.calls)
    finally:
        release_snapshot.set()
        release_ingress.set()
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 5)
