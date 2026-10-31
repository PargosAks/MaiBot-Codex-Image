"""Durable reference registration and Artist dispatch with synthetic bytes."""

import hashlib
import io
from copy import deepcopy

import pytest
from PIL import Image

from image_plugin_under_test.artist import RenderResult, render_digest
from image_plugin_under_test.bridge import BridgeError
from image_plugin_under_test.policy import StoreError
from image_plugin_under_test.references import ReferenceInputs
from image_plugin_under_test.thinking import Thinking
from test_artist_flow import SCOPE, step, studio  # noqa: F401


def png():
    target = io.BytesIO()
    Image.new("RGB", (11, 7), "purple").save(target, format="PNG")
    return target.getvalue()


class AssetsBridge:
    def __init__(self):
        self.writes, self.lookups, self.assets = [], [], {}
        self.lose_reply = False
        self.missing = False
        self.supported = True
        self.before_write = None

    async def call(self, operation, scope, args):
        assert operation == "health" and args == {}
        return {"capabilities": ["reference_inputs_v1"] if self.supported else [], "reference_limits": {
            "max_image_bytes": 8388608, "max_references": 3,
            "max_total_bytes": 33554432, "max_asset_bytes": 67108864,
        }}

    async def asset_register(self, scope, request_id, *, data, mime, expected_sha256, before_dispatch):
        if self.before_write:
            await self.before_write()
        await before_dispatch()
        assert hashlib.sha256(data).hexdigest() == expected_sha256
        self.writes.append(request_id)
        result = {"asset_id": "remote-" + expected_sha256, "sha256": expected_sha256,
                  "mime": mime, "size_bytes": len(data)}
        self.assets[request_id] = result
        if self.lose_reply:
            raise BridgeError("TRANSPORT_UNKNOWN", kind="transport")
        return result

    async def asset_lookup(self, scope, request_id, **expected):
        self.lookups.append(request_id)
        return {"found": False} if self.missing else {"found": True, "asset": self.assets[request_id]}


def prepare(studio):  # noqa: F811
    runtime, store, _, model, renderer, _ = studio
    store.configure_reference_policy("s", SCOPE, enabled=True, consent_ref="explicit-images", expected_auth_version=1)
    data = png()
    asset = store.import_asset(
        "s", SCOPE, event_id="current-host-event", message_index=0, data=data, mime="image/png",
        roles=["palette"], tags=[], description="授权聊天图片来源，未进行视觉评审",
        consent_ref="explicit-images", expected_auth_version=1,
    )
    bridge = AssetsBridge()
    inputs = ReferenceInputs(store, "s", SCOPE, bridge, binding_id="simulation-service")
    runtime.reference_inputs = True

    async def decisions(request):
        if request["task"] == "review":
            assert all(image["data"] == data for image in request["images"])
            result = {"decision": "accept", "reason": "合成流程接受", "strengths": ["测试图"], "issues": [],
                      "comparison": {"improvements": [], "regressions": [], "preserved": [],
                                     "evidence_version_ids": [image["version_id"] for image in request["images"]]}}
            if model.revise:
                model.revise -= 1
                result.update(decision="revise", issues=["调整背景"], edit_prompt="调整背景亮度", preserve="保留主体")
            return result
        result = await model(request)
        if request["task"] == "plan" and result["decision"]["action"] == "create":
            result["decision"]["references"] = [{"asset_id": asset["asset_id"], "role": "palette", "reason": "色彩练习"}]
        return result

    runtime.thinking = Thinking(decisions)

    async def prepare_inputs(*, scope, references, expected_auth_version, source_size=0):
        assert scope == SCOPE
        return await inputs.prepare(references, expected_auth_version=expected_auth_version, source_size=source_size)

    async def submit(*, scope, request_id, prompt, source):
        operation = store.get_operation("s", SCOPE, request_id)
        renderer.calls.append(deepcopy(operation))
        assert operation["params"]["reference_contract"] == 1
        assert operation["params"]["reference_inputs"][0]["sha256"] == hashlib.sha256(data).hexdigest()
        assert operation["action"] == ("edit" if source else "submit")
        return RenderResult(request_id, render_digest(operation["action"], operation["params"]),
                            "reference-job-" + str(len(renderer.calls)), "completed", data,
                            sha256=hashlib.sha256(data).hexdigest())

    renderer.prepare_references = prepare_inputs
    renderer.submit = submit
    return asset, bridge, inputs


async def test_reference_create_edit_review_keeps_original_work_and_separate_share(studio):  # noqa: F811
    runtime, store, _, model, renderer, sender = studio
    _, bridge, _ = prepare(studio)
    model.revise = 1
    assert (await runtime.tick())["state"] == "planned"
    assert (await step(studio))["state"] == "review_pending"
    assert (await step(studio))["reason"] == "revision"
    assert (await step(studio))["state"] == "review_pending"
    assert (await step(studio))["state"] == "finished"
    assert len(bridge.writes) == 1 and len(renderer.calls) == 2 and sender.calls == []
    assert renderer.calls[0]["work_id"] == renderer.calls[1]["work_id"]
    assert renderer.calls[1]["action"] == "edit"
    assert store.budget_usage("s", SCOPE)["render"]["consumed"] == 2


async def test_registration_receipt_loss_never_uploads_again_or_reserves_draw_early(studio):  # noqa: F811
    runtime, store, _, _, renderer, _ = studio
    asset, bridge, _ = prepare(studio)
    bridge.lose_reply = True
    await runtime.tick()
    assert (await step(studio))["reason"] == "TRANSPORT_UNKNOWN"
    assert not renderer.calls and store.budget_usage("s", SCOPE)["render"]["held"] == 0
    bridge.missing = True
    assert (await step(studio))["reason"] == "ASSET_REGISTRATION_UNKNOWN"
    assert len(bridge.writes) == 1 and not renderer.calls
    bridge.missing = False
    assert (await step(studio))["state"] == "review_pending"
    assert len(bridge.writes) == len(renderer.calls) == 1
    assert store.get_asset_registration("s", SCOPE, asset["asset_id"])["state"] == "succeeded"


async def test_registration_can_reconcile_after_revocation_without_rendering(studio):  # noqa: F811
    runtime, store, _, _, renderer, _ = studio
    asset, bridge, inputs = prepare(studio)
    bridge.lose_reply = True
    await runtime.tick()
    await step(studio)
    store.revoke("s", SCOPE, source_ref="stop", granted_by="admin")
    result = await inputs.reconcile(asset["asset_id"])
    assert result["state"] == "succeeded" and not renderer.calls
    assert len(bridge.writes) == len(bridge.lookups) == 1


async def test_policy_revoked_during_capability_probe_stops_upload(studio):  # noqa: F811
    runtime, store, _, _, renderer, _ = studio
    _, bridge, _ = prepare(studio)

    async def revoke():
        store.revoke("s", SCOPE, source_ref="stop", granted_by="admin")

    bridge.before_write = revoke
    await runtime.tick()
    assert (await step(studio))["state"] == "blocked"
    assert not bridge.writes and not renderer.calls


async def test_old_service_blocks_image_plan_without_text_fallback(studio):  # noqa: F811
    runtime, store, _, _, renderer, _ = studio
    _, bridge, _ = prepare(studio)
    bridge.supported = False
    await runtime.tick()
    assert (await step(studio))["reason"] == "REFERENCE_INPUTS_UNSUPPORTED"
    assert not bridge.writes and not renderer.calls
    assert store.budget_usage("s", SCOPE)["render"]["held"] == 0


def test_reference_digest_vector_and_old_submit_digest_are_distinct():
    params = {"prompt": "雨夜", "transport_operation": "render", "reference_contract": 1,
              "reference_inputs": [{"asset_id": "a", "sha256": "1" * 64, "role": "palette"}]}
    from image_plugin_under_test.policy import canonical_json
    expected = hashlib.sha256(canonical_json({"operation": "render", "mode": "create", "prompt": "雨夜",
                                             "source": None, "references": params["reference_inputs"]}).encode()).hexdigest()
    assert render_digest("submit", params) == expected
    assert render_digest("submit", {"prompt": "雨夜"}) == hashlib.sha256("雨夜".encode()).hexdigest()
    with pytest.raises(StoreError):
        render_digest("submit", {**params, "reference_contract": 2})
