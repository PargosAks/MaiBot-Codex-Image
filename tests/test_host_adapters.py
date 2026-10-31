"""Isolated Host capability contracts; no real model, RPC, image generation or send."""

from copy import deepcopy
from pathlib import Path
from threading import Event
import ast
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import sys

import pytest

from maibot_sdk.context import PluginContext

from image_plugin_under_test.artist import stable_id
from image_plugin_under_test.delivery import Deliveries
from image_plugin_under_test.host_adapters import HostSender, HostThinking, send_image_receipt
from image_plugin_under_test.policy import Delegation, Limits, Scope, StoreError
from image_plugin_under_test.store import ArtistStore
from image_plugin_under_test.thinking import Thinking
from protocol_fixture import PNG


SCOPE = Scope("synthetic", "verified-stream", "delegating-owner")
OTHER = Scope("synthetic", "other-stream", "other-owner")
WAIT = {"ideas": [], "decision": {"action": "wait", "reason": "暂时收藏"}}
REVIEW = {"decision": "accept", "reason": "主体清楚", "strengths": ["柔和"], "issues": []}


class Denied(Exception):
    code = "E_CAPABILITY_DENIED"


class Context:
    def __init__(self, context_class=PluginContext):
        self.sdk = context_class("isolated-adapter-test", rpc_call=self._rpc)
        self.llm, self.send = self.sdk.llm, self.sdk.send
        self.calls = []
        self.rpc_timeouts = []
        self.result = {"success": True, "response": json.dumps(WAIT)}
        self.failure = None
        self.handler = None

    async def call_capability(self, capability, **payload):
        self.calls.append((capability, payload))
        if self.handler:
            return await self.handler(capability, payload)
        if self.failure:
            raise self.failure
        return self.result

    async def call_host_method(self, method, *, payload, timeout_ms=None):
        return await self.sdk.call_host_method(method, payload=payload, timeout_ms=timeout_ms)

    async def _rpc(self, method, plugin_id, payload, timeout_ms=None):
        assert method == "cap.call" and payload["capability"] in {"send.image", "llm.generate"}
        assert set(payload) == {"capability", "args"}
        self.rpc_timeouts.append(timeout_ms)
        return await self.call_capability(payload["capability"], **payload["args"])


@pytest.fixture(params=["2.16", "2.17"])
def ctx(request):
    if request.param == "2.16":
        return Context()
    source = os.environ.get("CODEX_IMAGE_TEST_SDK_SOURCE")
    if not source:
        pytest.skip("Set CODEX_IMAGE_TEST_SDK_SOURCE to opt in to public SDK source tests")
    # SDK 2.17 uses absolute package imports. Load its public package briefly,
    # then restore all module/path entries before any asynchronous test executes.
    # The returned class retains its own module globals and real RPC methods.
    previous = {key: value for key, value in sys.modules.items()
                if key == "maibot_sdk" or key.startswith("maibot_sdk.")}
    paths = list(sys.path)
    try:
        for key in previous:
            del sys.modules[key]
        sys.path.insert(0, str(Path(source)))
        context_class = importlib.import_module("maibot_sdk.context").PluginContext
    finally:
        for key in list(sys.modules):
            if key == "maibot_sdk" or key.startswith("maibot_sdk."):
                del sys.modules[key]
        sys.modules.update(previous)
        sys.path[:] = paths
    return Context(context_class)


def thinking(ctx):
    return Thinking(HostThinking(ctx, plan_task="synthetic-plan", review_task="synthetic-vision"))


async def test_actual_sdk_plan_and_inspiration_capability_no_tools(ctx):
    assert await thinking(ctx).plan({}) == WAIT
    ctx.result = {"success": True, "response": '{"ideas":[]}'}
    assert await thinking(ctx).inspire({}) == {"ideas": []}
    for name, payload in ctx.calls:
        assert name == "llm.generate"
        assert payload["task_name"] == "synthetic-plan" and "max_tokens" not in payload
        assert "tools" not in payload and "model_name" not in payload
        assert [message["role"] for message in payload["prompt"]] == ["system", "user"]
        assert payload["prompt"][1]["content"] == [{"type": "text", "text": "{}"}]


async def test_actual_sdk_review_carries_original_multimodal_bytes(ctx):
    ctx.result = {"success": True, "response": json.dumps(REVIEW)}
    assert await thinking(ctx).review({}, image=PNG, mime="image/png") == REVIEW
    name, payload = ctx.calls[0]
    assert name == "llm.generate" and payload["task_name"] == "synthetic-vision"
    content = payload["prompt"][1]["content"]
    assert content[1]["type"] == "image_url"
    url = content[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1], validate=True) == PNG


def review_v2_case(count=3):
    images = []
    for index, (role, mime) in enumerate((("current", "image/png"), ("parent", "image/jpeg"),
                                         ("previous_best", "image/webp"))):
        data = PNG + bytes([index])
        images.append({"version_id": f"v{index}", "roles": [role], "mime": mime,
                       "sha256": hashlib.sha256(data).hexdigest(), "data": data})
    images = images[:count]
    if count == 2:
        images[1]["roles"].append("previous_best")
    inputs = [{key: deepcopy(value) for key, value in image.items() if key != "data"} for image in images]
    context = {"review_contract": 2, "current_version": {"version_id": "v0"}, "review_inputs": inputs}
    return context, images


def review_v2_answer():
    return dict(REVIEW, best_version_id="v0", comparison={
        "improvements": [], "regressions": [], "preserved": ["synthetic subject"],
        "evidence_version_ids": ["v0"],
    })


@pytest.mark.parametrize("count", [1, 2, 3])
async def test_v2_host_sdk_preserves_labels_order_original_bytes_and_routing(count):
    ctx = Context()
    context, images = review_v2_case(count)
    before = deepcopy((context, images))
    answer = review_v2_answer()
    ctx.result = {"success": True, "response": json.dumps(answer)}
    assert await thinking(ctx).review(context, images=images) == answer
    assert len(ctx.calls) == 1
    name, payload = ctx.calls[0]
    assert name == "llm.generate" and payload["task_name"] == "synthetic-vision"
    assert set(payload) == {"prompt", "task_name"}
    content = payload["prompt"][1]["content"]
    assert len(content) == 1 + 2 * count
    assert json.loads(content[0]["text"]) == context
    for index, image in enumerate(images):
        label, part = content[1 + 2 * index:3 + 2 * index]
        assert label["type"] == "text"
        assert json.loads(label["text"]) == {"roles": image["roles"], "version_id": image["version_id"]}
        assert part["type"] == "image_url"
        header, encoded = part["image_url"]["url"].split(",", 1)
        assert header == f"data:{image['mime']};base64"
        assert base64.b64decode(encoded, validate=True) == image["data"]
    assert (context, images) == before


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(image=PNG, mime="image/png"),
    lambda r: r.pop("images"),
    lambda r: r.update(images=[]),
    lambda r: r["images"].reverse(),
    lambda r: r["images"][0].update(data=b"wrong hash"),
    lambda r: r["images"][0].update(data=b"x" * (8 * 1024 * 1024 + 1)),
    lambda r: r["images"][0].update(mime="image/gif"),
    lambda r: r["images"][0].update(owner="forged"),
    lambda r: r["context"]["review_inputs"][1].update(roles=["current"]),
    lambda r: r["context"]["review_inputs"][1].update(version_id="v0"),
    lambda r: r["context"].update(current_version={"version_id": "wrong"}),
    lambda r: r["context"].update(owner="forged"),
    lambda r: r["context"].update(request_id="forged"),
    lambda r: r["context"].update(review_contract=True),
    lambda r: r["context"].pop("review_contract"),
    lambda r: r.update(owner="forged"),
    lambda r: r.update(task="plan"),
    lambda r: r.update(task=[]),
    lambda r: r.pop("context"),
])
async def test_v2_host_direct_request_validation_precedes_dispatch(mutate):
    ctx = Context()
    context, images = review_v2_case()
    request = dict(task="review", instructions="Inspect every image", context=context, images=images)
    mutate(request)
    with pytest.raises(StoreError):
        await HostThinking(ctx, plan_task="plan", review_task="vision")(request)
    assert not ctx.calls


@pytest.mark.parametrize("receipt,code", [
    ({"success": False}, "THINKING_UNKNOWN"),
    ({"success": "true", "response": "{}"}, "THINKING_UNKNOWN"),
    ({"success": True, "response": "{}", "error": "private provider error"}, "THINKING_UNKNOWN"),
    ({"response": "{}"}, "THINKING_UNKNOWN"),
    (None, "THINKING_UNKNOWN"),
    ({"success": True, "response": "{}", "tool_calls": [{"name": "send"}]}, "INVALID_DECISION"),
    ({"success": True, "response": '```json\n{}\n```'}, "INVALID_DECISION"),
    ({"success": True, "response": json.dumps(REVIEW)}, "INVALID_DECISION"),
    ({"success": True, "response": '{"comparison":{},"comparison":{}}'}, "INVALID_DECISION"),
])
async def test_v2_host_strict_receipt_and_missing_comparison_never_retry(receipt, code):
    ctx = Context()
    ctx.result = receipt
    context, images = review_v2_case()
    with pytest.raises(StoreError, check=lambda exc: exc.code == code) as caught:
        await thinking(ctx).review(context, images=images)
    assert "private" not in str(caught.value) and len(ctx.calls) == 1


@pytest.mark.parametrize(
    "raw",
    [
        '```json\n{"ideas":[]}\n```',
        '{"ideas":[],"ideas":[]}',
        "[]",
        '{"n":NaN}',
        '{"n":1e999}',
        '{"ideas":[]} trailing',
        "{" + '"x":' + "[" * 17 + "0" + "]" * 17 + "}",
        '{"x":[' + ",".join("0" for _ in range(2049)) + "]}",
        " " * 32769,
    ],
    ids=["fence", "duplicate", "array", "nan", "infinite", "trailing", "depth", "nodes", "length"],
)
async def test_json_is_bounded_single_object_without_repair(raw):
    ctx = Context()
    ctx.result = {"success": True, "response": raw}
    with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_DECISION"):
        await thinking(ctx).inspire({})
    assert len(ctx.calls) == 1


@pytest.mark.parametrize(
    "mode,code",
    [
        ("denied", "THINKING_FAILED"),
        ("timeout", "THINKING_UNKNOWN"),
        ("receipt", "THINKING_UNKNOWN"),
        ("failed", "THINKING_UNKNOWN"),
        ("tools", "INVALID_DECISION"),
    ],
)
async def test_model_failure_certainty_and_no_retry(mode, code):
    ctx = Context()
    if mode == "denied":
        ctx.failure = Denied("private rejection text")
    elif mode == "timeout":
        ctx.failure = TimeoutError("private transport text")
    elif mode == "receipt":
        ctx.result = {"response": "unknown"}
    elif mode == "failed":
        ctx.result = {"success": False}
    else:
        ctx.result["tool_calls"] = [{"name": "send"}]
    with pytest.raises(StoreError, check=lambda exc: exc.code == code) as caught:
        await thinking(ctx).plan({})
    assert "private" not in str(caught.value) and len(ctx.calls) == 1


async def test_review_never_substitutes_text_when_image_is_invalid():
    ctx = Context()
    with pytest.raises(StoreError):
        await thinking(ctx).review({}, image=b"x" * (8 * 1024 * 1024 + 1), mime="image/png")
    assert not ctx.calls


@pytest.fixture
def sender_state(tmp_path):
    store = ArtistStore(tmp_path / "artist.sqlite3")
    store.create_space("s", SCOPE, binding_id="synthetic-binding")
    store.grant("s", SCOPE, Delegation("explicit-consent", "trusted-adapter", continuous=True))
    limits = Limits(render=10, share=10, think=10, max_inflight=10, max_revisions=3)
    store.set_global_limits(limits)
    store.set_limits("s", SCOPE, limits)
    store.create_work("s", SCOPE, "w")
    store.reserve(
        "s", SCOPE, request_id="render", action="submit", params={"prompt": "synthetic"}, work_id="w"
    )
    store.mark_submitting("s", SCOPE, "render")
    store.settle("s", SCOPE, "render", outcome="succeeded", job_id="job", evidence="synthetic")
    version = store.record_version(
        "s", SCOPE, work_id="w", version_id="v", request_id="render", job_id="job", data=PNG, mime="image/png"
    )
    request_id = stable_id("share", "w", "v")
    store.reserve(
        "s",
        SCOPE,
        request_id=request_id,
        action="share",
        work_id="w",
        params={"job_id": "job", "version_id": "v"},
    )
    store.mark_submitting("s", SCOPE, request_id)
    journal = Deliveries(tmp_path / "delivery.sqlite3")
    yield store, journal, version
    journal.close()
    store.close()


def sender(ctx, state):
    store, journal, _ = state
    return HostSender(ctx, journal, store=store, space_id="s", scope=SCOPE)


def delivery_state(journal):
    with journal.lock:
        row = journal.db.execute("SELECT state FROM delivery WHERE job='job'").fetchone()
        return row[0] if row else None


@pytest.mark.parametrize(
    "receipt,expected",
    [
        ({"sent": True, "message_id": "synthetic-id"}, True),
        ({"sent": False, "message_id": None}, False),
        (False, False),
    ],
)
async def test_actual_sdk_send_destination_bytes_details_and_persistent_outcome(
    ctx, sender_state, receipt, expected
):
    _, journal, version = sender_state
    ctx.result = receipt
    adapter = sender(ctx, sender_state)
    if expected:
        assert await adapter.send(scope=SCOPE, version=version) is True
    else:
        with pytest.raises(StoreError, check=lambda exc: exc.code == "SEND_UNKNOWN"):
            await adapter.send(scope=SCOPE, version=version)
    name, payload = ctx.calls[0]
    assert name == "send.image" and payload["stream_id"] == SCOPE.stream_id
    assert payload["return_details"] is True
    assert base64.b64decode(payload["image_base64"], validate=True) == PNG
    assert delivery_state(journal) == ("sent" if expected else "send_unknown")
    if expected:
        assert await adapter.send(scope=SCOPE, version=version) is True
    else:
        with pytest.raises(StoreError, check=lambda exc: exc.code == "SEND_UNKNOWN"):
            await adapter.send(scope=SCOPE, version=version)
    assert len(ctx.calls) == 1 and adapter.simulation is False
    assert journal.begin("job", manual=False) is False


@pytest.mark.parametrize("mode", ["exception", "invalid", "denied"])
async def test_send_failure_certainty_and_unknown_lock(mode, sender_state):
    _, journal, version = sender_state
    ctx = Context()
    ctx.result = {"sent": "yes"}
    if mode != "invalid":
        ctx.failure = Denied("no capability") if mode == "denied" else TimeoutError("lost receipt")
    adapter = sender(ctx, sender_state)
    if mode == "denied":
        assert await adapter.send(scope=SCOPE, version=version) is False
        assert delivery_state(journal) == "send_failed"
    else:
        with pytest.raises((TimeoutError, StoreError)):
            await adapter.send(scope=SCOPE, version=version)
        assert delivery_state(journal) == "send_unknown"
        assert journal.begin("job", manual=True) is False
        with pytest.raises(StoreError, check=lambda exc: exc.code == "SEND_UNKNOWN"):
            await adapter.send(scope=SCOPE, version=version)
    assert len(ctx.calls) == 1


@pytest.mark.parametrize("state", ["sending", "send_unknown", "sent", "send_failed"])
async def test_existing_manual_delivery_lock_is_respected(sender_state, state):
    _, journal, version = sender_state
    assert journal.begin("job")
    journal.finish("job", state)
    ctx = Context()
    adapter = sender(ctx, sender_state)
    if state in {"sending", "send_unknown"}:
        with pytest.raises(StoreError, check=lambda exc: exc.code == "SEND_UNKNOWN"):
            await adapter.send(scope=SCOPE, version=version)
    else:
        assert await adapter.send(scope=SCOPE, version=version) is (state == "sent")
    assert not ctx.calls and delivery_state(journal) == state


@pytest.mark.parametrize("change", ["scope", "data", "job", "owner", "revoked"])
async def test_sender_requires_bound_scope_stored_version_and_current_authority(sender_state, change):
    store, journal, version = sender_state
    ctx = Context()
    version = dict(version)
    scope = SCOPE
    if change == "scope":
        scope = OTHER
    elif change == "data":
        version["data"] = b"changed"
    elif change == "job":
        version["job_id"] = "foreign"
    elif change == "owner":
        scope = {"stream_id": "arbitrary", "user_id": "arbitrary"}
    else:
        store.revoke("s", SCOPE, source_ref="stop", granted_by="adapter")
    with pytest.raises(StoreError):
        await sender(ctx, sender_state).send(scope=scope, version=version)
    assert not ctx.calls and delivery_state(journal) is None


async def test_two_senders_and_manual_path_cannot_double_send(sender_state):
    _, journal, version = sender_state
    ctx = Context()
    entered, release = asyncio.Event(), asyncio.Event()

    async def send_handler(*args):
        assert delivery_state(journal) == "sending"
        entered.set()
        await release.wait()
        return {"sent": True}

    ctx.handler = send_handler
    first = asyncio.create_task(sender(ctx, sender_state).send(scope=SCOPE, version=version))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        assert journal.begin("job", manual=True) is False
        with pytest.raises(StoreError, check=lambda exc: exc.code == "SEND_UNKNOWN"):
            await sender(ctx, sender_state).send(scope=SCOPE, version=version)
    finally:
        release.set()
    assert await first is True
    assert len(ctx.calls) == 1 and delivery_state(journal) == "sent"


async def test_cancel_send_persists_unknown_and_reopen_does_not_retry(sender_state, tmp_path):
    store, journal, version = sender_state
    entered = asyncio.Event()
    ctx = Context()

    async def wait_forever(*args):
        entered.set()
        await asyncio.Event().wait()

    ctx.handler = wait_forever
    task = asyncio.create_task(sender(ctx, sender_state).send(scope=SCOPE, version=version))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert delivery_state(journal) == "send_unknown"
    reopened = Deliveries(tmp_path / "delivery.sqlite3")
    try:
        adapter = HostSender(ctx, reopened, store=store, space_id="s", scope=SCOPE)
        with pytest.raises(StoreError, check=lambda exc: exc.code == "SEND_UNKNOWN"):
            await adapter.send(scope=SCOPE, version=version)
    finally:
        reopened.close()
    assert len(ctx.calls) == 1


async def test_cancel_claim_waits_for_sqlite_worker_then_preserves_lock(sender_state, monkeypatch):
    _, journal, version = sender_state
    entered, release = Event(), Event()
    begin = journal.begin

    def delayed(*args):
        result = begin(*args)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(journal, "begin", delayed)
    ctx = Context()
    task = asyncio.create_task(sender(ctx, sender_state).send(scope=SCOPE, version=version))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not ctx.calls and delivery_state(journal) == "send_unknown"


@pytest.mark.parametrize("multi_image", [False, True])
def test_installed_host_public_parser_preserves_image_payload(multi_image):
    source = os.environ.get("CODEX_IMAGE_TEST_HOST_SOURCE")
    if not source:
        pytest.skip("Set CODEX_IMAGE_TEST_HOST_SOURCE to opt in to public Host source tests")
    path = Path(source) / "services/llm_service.py"
    # Execute only the three pure parser functions; importing the Host service
    # would initialize application dependencies and private configuration.
    names = {"_parse_data_url_image", "_append_image_content", "_append_content_parts"}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(selected) == 3
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *selected],
        type_ignores=[],
    )
    namespace = {}
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)

    class Builder:
        def __init__(self):
            self.images, self.texts, self.parts = [], [], []

        def add_image_content(self, **args):
            self.images.append(args)
            self.parts.append(("image", args))

        def add_text_content(self, text):
            self.texts.append(text)
            self.parts.append(("text", text))

    adapter = HostThinking(Context(), plan_task="plan", review_task="vision")
    if multi_image:
        context, images = review_v2_case()
        request = dict(task="review", instructions="inspect every original image", context=context, images=images)
    else:
        images = [{"data": PNG, "mime": "image/png"}]
        request = dict(task="review", instructions="inspect original image", context={}, image=PNG, mime="image/png")
    messages = adapter._messages(request)
    builder = Builder()
    namespace["_append_content_parts"](builder, messages[1]["content"])
    assert len(builder.images) == len(images)
    for index, (parsed, original) in enumerate(zip(builder.images, images)):
        assert parsed["image_format"] == original["mime"].removeprefix("image/")
        assert base64.b64decode(parsed["image_base64"], validate=True) == original["data"]
        if multi_image:
            kind, label = builder.parts[1 + 2 * index]
            assert kind == "text"
            assert json.loads(label) == {"roles": original["roles"], "version_id": original["version_id"]}
            assert builder.parts[2 + 2 * index] == ("image", parsed)


@pytest.mark.parametrize(
    "receipt,expected",
    [
        ({"success": True, "sent": True, "message_id": "synthetic"}, True),
        ({"success": False, "sent": False, "message_id": None}, False),
        (True, True),
        (False, False),
    ],
)
async def test_real_plugin_context_send_preserves_raw_boolean_receipt(sender_state, receipt, expected):
    _, journal, version = sender_state
    calls = []

    async def rpc(method, plugin_id, payload):
        calls.append((method, plugin_id, payload))
        assert delivery_state(journal) == "sending"
        return receipt

    ctx = PluginContext("isolated-test", rpc_call=rpc)
    if expected:
        assert await sender(ctx, sender_state).send(scope=SCOPE, version=version) is True
    else:
        with pytest.raises(StoreError, check=lambda exc: exc.code == "SEND_UNKNOWN"):
            await sender(ctx, sender_state).send(scope=SCOPE, version=version)
    assert calls == [
        (
            "cap.call",
            "isolated-test",
            {
                "capability": "send.image",
                "args": {
                    "image_base64": base64.b64encode(PNG).decode("ascii"),
                    "stream_id": SCOPE.stream_id,
                    "return_details": True,
                },
            },
        )
    ]
    assert delivery_state(journal) == ("sent" if expected else "send_unknown")


@pytest.mark.parametrize(
    "receipt",
    [
        {"success": True},
        {"success": False},
        {"success": True, "sent": "false"},
        {"success": False, "sent": "true"},
        {"success": False, "sent": True},
        {"success": True, "sent": False},
        {"success": "true", "sent": True},
        {"success": True, "sent": 1},
        {"success": False, "sent": False, "message_id": None, "error": "private transport exception"},
    ],
)
async def test_real_plugin_context_malformed_or_swallowed_error_stays_unknown(sender_state, receipt):
    _, journal, version = sender_state
    calls = []

    async def rpc(method, plugin_id, payload):
        calls.append(payload)
        return receipt

    ctx = PluginContext("isolated-test", rpc_call=rpc)
    adapter = sender(ctx, sender_state)
    with pytest.raises(StoreError, check=lambda exc: exc.code == "SEND_UNKNOWN") as caught:
        await adapter.send(scope=SCOPE, version=version)
    assert "private" not in str(caught.value)
    assert delivery_state(journal) == "send_unknown"
    with pytest.raises(StoreError, check=lambda exc: exc.code == "SEND_UNKNOWN"):
        await adapter.send(scope=SCOPE, version=version)
    assert not journal.begin("job", manual=True)
    assert len(calls) == 1


@pytest.mark.parametrize("denied", [False, True])
async def test_real_plugin_context_send_exception_certainty(sender_state, denied):
    _, journal, version = sender_state
    failure = Denied("private denial") if denied else TimeoutError("private lost reply")

    async def rpc(*args):
        raise failure

    ctx = PluginContext("isolated-test", rpc_call=rpc)
    if denied:
        assert await sender(ctx, sender_state).send(scope=SCOPE, version=version) is False
        assert delivery_state(journal) == "send_failed"
    else:
        with pytest.raises(TimeoutError) as caught:
            await sender(ctx, sender_state).send(scope=SCOPE, version=version)
        assert caught.value is failure and delivery_state(journal) == "send_unknown"


@pytest.mark.parametrize(
    "receipt,code",
    [
        ({"success": False, "error": "private provider exception"}, "THINKING_UNKNOWN"),
        ({"success": False}, "THINKING_UNKNOWN"),
        ({"success": "false", "response": json.dumps(WAIT)}, "THINKING_UNKNOWN"),
        ({"success": True, "response": json.dumps(WAIT)}, None),
        (None, "THINKING_UNKNOWN"),
        (False, "THINKING_UNKNOWN"),
        ([], "THINKING_UNKNOWN"),
        ("lost response", "THINKING_UNKNOWN"),
    ],
)
async def test_real_plugin_context_llm_keeps_original_success_type_and_error(receipt, code):
    calls = []

    async def rpc(method, plugin_id, payload, timeout_ms=None):
        calls.append(payload)
        assert method == "cap.call" and payload["capability"] == "llm.generate"
        assert timeout_ms == 30000
        return receipt

    ctx = PluginContext("isolated-test", rpc_call=rpc)
    if code:
        with pytest.raises(StoreError, check=lambda exc: exc.code == code) as caught:
            await thinking(ctx).plan({})
        assert "private" not in str(caught.value)
    else:
        assert await thinking(ctx).plan({}) == WAIT
    assert len(calls) == 1


async def test_shared_send_helper_preserves_capability_denial_for_caller():
    failure = Denied("synthetic rejection")

    async def rpc(*args):
        raise failure

    ctx = PluginContext("isolated-test", rpc_call=rpc)
    with pytest.raises(Denied) as caught:
        await send_image_receipt(ctx, base64.b64encode(PNG).decode("ascii"), SCOPE.stream_id)
    assert caught.value is failure


@pytest.mark.parametrize("timeout,expected_ms", [(30, 30000), (150.1234, 150124), (0.0001, 1)])
async def test_sdk_receives_explicit_thinking_rpc_timeout(ctx, timeout, expected_ms):
    adapter = HostThinking(ctx, plan_task="plan", review_task="vision", timeout_seconds=timeout)
    assert await Thinking(adapter).plan({}) == WAIT
    assert ctx.rpc_timeouts == [expected_ms]
    assert "max_tokens" not in ctx.calls[0][1]
    assert "timeout_ms" not in ctx.calls[0][1]


async def test_explicit_adapter_token_override_is_still_supported(ctx):
    adapter = HostThinking(ctx, plan_task="plan", review_task="vision", max_tokens=2048)
    assert await Thinking(adapter).plan({}) == WAIT
    assert ctx.calls[0][1]["max_tokens"] == 2048 and ctx.rpc_timeouts == [30000]


@pytest.mark.parametrize("value", [True, 0, -1, 4097, 1.5, "2048"])
def test_invalid_explicit_token_override_fails_before_rpc(value):
    ctx = Context()
    with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_ARGUMENT"):
        HostThinking(ctx, plan_task="plan", review_task="vision", max_tokens=value)
    assert not ctx.calls and not ctx.rpc_timeouts


@pytest.mark.parametrize("value", [True, 0, -1, 301, float("nan"), float("inf"), "150"])
def test_invalid_thinking_timeout_fails_before_rpc(value):
    ctx = Context()
    with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_ARGUMENT"):
        HostThinking(ctx, plan_task="plan", review_task="vision", timeout_seconds=value)
    assert not ctx.calls and not ctx.rpc_timeouts
