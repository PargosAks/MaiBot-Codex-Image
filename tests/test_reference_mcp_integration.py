"""Opt-in real MCP HTTP/SQLite integration; synthetic driver, model and sender."""

import hashlib
from copy import deepcopy

import pytest

from image_plugin_under_test.references import ReferenceInputs
from image_plugin_under_test.thinking import Thinking
from test_artist_mcp_integration import BINDING, SPACE, studio  # noqa: F401
from test_mcp_recovery_integration import SOURCE, actual_mcp  # noqa: F401

pytestmark = pytest.mark.skipif(not SOURCE, reason="Independent MCP source is explicitly opt-in")


def enable_references(value):
    value.store.configure_reference_policy(SPACE, value.scope, enabled=True,
                                          consent_ref="synthetic-image-consent", expected_auth_version=1)
    value.artist.reference_inputs = True
    value.driver.supports_references = True
    assets = []
    for index, data in enumerate(value.driver.images):
        assets.append(value.store.import_asset(
            SPACE, value.scope, event_id="trusted-current-event", message_index=index,
            data=data, mime="image/png", roles=["palette", "composition"], tags=[],
            description="合成来源元数据，尚未看图评审", consent_ref="synthetic-image-consent", expected_auth_version=1,
        ))
    model = value.model

    async def decisions(request):
        result = await model(request)
        if request["task"] == "plan" and result["decision"]["action"] == "create":
            result["decision"]["references"] = [
                {"asset_id": asset["asset_id"], "role": role, "reason": "合成参考输入验证"}
                for asset, role in zip(assets, ["palette", "composition"])
            ]
        return result

    value.artist.thinking = Thinking(decisions)

    async def generate(job, ids, image):
        driver = value.driver
        # The private I4 controller owns an event loop; only record job data.
        driver.calls.append(deepcopy({key: val for key, val in job.items() if key != "_execution"}))
        assert job["operation"] == "render"
        assert [item["data"] for item in job["reference_images"]] == driver.images
        assert [item["role"] for item in job["reference_images"]] == ["palette", "composition"]
        if job["mode"] == "edit":
            source = job["source_image"]
            assert source["data"] == driver.outputs[source["job_id"]]
            assert hashlib.sha256(source["data"]).hexdigest() == source["sha256"]
        else:
            assert job["mode"] == "create" and "source_image" not in job
        await ids(status="running", thread_id="synthetic-ref-thread", turn_id="synthetic-ref-turn")
        await driver.gate.wait()
        data = driver.images[1 if job["mode"] == "edit" else 0]
        driver.outputs[job["id"]] = data
        driver.output_root.mkdir(parents=True, exist_ok=True)
        path = driver.output_root / (job["id"] + ".png")
        path.write_bytes(data)
        await image(str(path))
        return "completed"

    value.driver.generate = generate
    return assets, decisions


@pytest.mark.parametrize("chat", [False, True])
async def test_reference_create_edit_review_share_over_real_mcp(studio, chat):  # noqa: F811
    enable_references(studio)
    # An ordinary chat sentence need not match unreviewed source metadata.
    if chat:
        await studio.artist.on_chat(message_id="chat-wakeup", text="想画雨夜灯光")
        assert (await studio.artist.run_once())["state"] == "planned"
    else:
        await studio.plan()
    assert (await studio.step())["state"] == "creating"
    await studio.finish_render()
    assert (await studio.step())["state"] == "review_pending"
    assert (await studio.step())["reason"] == "revision"
    assert (await studio.step())["state"] == "creating"
    await studio.finish_render()
    assert (await studio.step())["state"] == "review_pending"
    assert (await studio.step())["state"] == "finished"
    assert len(studio.driver.calls) == 2 and not studio.sender.calls
    assert [op["action"] for op in studio.renders()] == ["submit", "edit"]
    assert all(op["params"]["transport_operation"] == "render" for op in studio.renders())
    assert [op for op, _ in studio.trace].count("asset_register") == 2
    assert not any(op in {"submit", "edit"} for op, _ in studio.trace)
    studio.model.share = True
    assert (await studio.step())["state"] == "shared"
    assert len(studio.sender.calls) == 1


async def test_lost_asset_receipt_recovers_read_only_before_drawing(studio):  # noqa: F811
    assets, _ = enable_references(studio)
    studio.dropped.add("asset_register")
    await studio.plan()
    assert (await studio.step())["reason"] == "TRANSPORT_UNKNOWN"
    assert not studio.driver.calls and not studio.renders()
    assert (await studio.step())["state"] == "creating"
    await studio.finish_render()
    assert [op for op, _ in studio.trace].count("asset_register") == 2
    assert [op for op, _ in studio.trace].count("asset_lookup") == 1
    assert len(studio.driver.calls) == 1
    assert studio.store.get_asset_registration(SPACE, studio.scope, assets[0]["asset_id"])["state"] == "succeeded"


async def test_lost_render_receipt_restart_never_issues_second_render(studio):  # noqa: F811
    _, decisions = enable_references(studio)
    studio.model.revisions = 0
    studio.dropped.add("render")
    await studio.plan()
    assert (await studio.step())["reason"] == "TRANSPORT_UNKNOWN"
    await studio.finish_render()
    studio.reopen()
    studio.artist.reference_inputs = True
    studio.artist.thinking = Thinking(decisions)
    await studio.artist.recover(offline=True, reconcile=True)
    assert (await studio.step())["state"] in {"review_pending", "finished"}
    assert [op for op, _ in studio.trace].count("render") == 1
    assert len(studio.driver.calls) == 1


async def test_asset_lookup_after_revocation_cannot_trigger_render(studio):  # noqa: F811
    assets, _ = enable_references(studio)
    studio.dropped.add("asset_register")
    await studio.plan()
    await studio.step()
    studio.store.revoke(SPACE, studio.scope, source_ref="synthetic-stop", granted_by="admin")
    studio.driver.supports_references = False
    helper = ReferenceInputs(studio.store, SPACE, studio.scope, studio.bridge, binding_id=BINDING)
    before = studio.service.store.db.total_changes
    assert (await helper.reconcile(assets[0]["asset_id"]))["state"] == "succeeded"
    assert studio.service.store.db.total_changes == before
    assert not studio.driver.calls and not studio.renders()
