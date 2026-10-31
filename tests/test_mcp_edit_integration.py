"""Opt-in real HTTP and native IPC contracts, with synthetic images only."""

from pathlib import Path
import asyncio
import hashlib
import importlib
import io
import json
import sys

import pytest

from image_plugin_under_test.bridge import BridgeError
from image_plugin_under_test.editing import ImageEdits
from image_plugin_under_test.policy import Delegation, Limits, Scope
from image_plugin_under_test.store import ArtistStore
from conftest import SCOPE
from test_mcp_recovery_integration import SOURCE, actual_mcp  # noqa: F401

pytestmark = pytest.mark.skipif(not SOURCE, reason="Independent MCP source is explicitly opt-in")


@pytest.mark.parametrize("lost_reply", [False, True])
async def test_original_bytes_to_native_input_and_immutable_child(
    actual_mcp, tmp_path, monkeypatch, lost_reply  # noqa: F811 - imported shared pytest fixture
):
    from PIL import Image

    bridge, service = actual_mcp
    native = importlib.import_module("codex_image_mcp.codex")
    rpc_type = native.Rpc
    fake = Path(SOURCE).parent / "tests" / "fake_codex.py"

    def rpc(argv, env, timeout):
        role = "worker" if 'cli_auth_credentials_store="ephemeral"' in argv else "broker"
        return rpc_type([sys.executable, str(fake), "edit-input", role], env, timeout)

    monkeypatch.setattr(native, "Rpc", rpc)
    service.driver = native.Codex(service.cfg)
    original = await bridge.call("submit", SCOPE, {"request_id": "source", "prompt": "original"})
    original_job = original["job_id"]
    buffer = io.BytesIO()
    Image.new("RGB", (23, 17), "purple").save(buffer, format="PNG")
    data = buffer.getvalue()
    path = service.cfg.state / "artifacts" / "synthetic-source.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    service.store.artifact(original_job, path, data, "image/png")
    service.store.update(original_job, status="completed", turn_status="completed")
    scope = Scope(**SCOPE)
    store = ArtistStore(tmp_path / "edit-ledger.sqlite3")
    try:
        store.create_space("s", scope, binding_id="isolated-mcp")
        store.grant("s", scope, Delegation("explicit-test-consent", "trusted-test", continuous=True))
        limits = Limits(think=10, render=10, share=0, max_inflight=10, max_revisions=3)
        store.set_global_limits(limits)
        store.set_limits("s", scope, limits)
        store.create_work("s", scope, "work")
        store.reserve(
            "s", scope, request_id="source", action="submit", work_id="work", params={"prompt": "original"}
        )
        store.mark_submitting("s", scope, "source")
        store.settle(
            "s", scope, "source", outcome="succeeded", evidence="synthetic source", job_id=original_job
        )
        parent = store.record_version(
            "s",
            scope,
            work_id="work",
            version_id="parent",
            request_id="source",
            job_id=original_job,
            data=data,
            mime="image/png",
        )
        edits = ImageEdits(store, bridge)
        exchange = bridge._exchange
        edit_calls = []

        async def observed(operation, owner, args):
            result = await exchange(operation, owner, args)
            if operation == "edit":
                edit_calls.append(args.copy())
                if lost_reply:
                    raise ConnectionError("Synthetic lost reply after acceptance")
            return result

        monkeypatch.setattr(bridge, "_exchange", observed)
        args = ("s", scope, "work", "parent", "edit-one", "Preserve the composition; refine the petals.")
        if lost_reply:
            with pytest.raises(BridgeError, check=lambda exc: exc.code == "TRANSPORT_UNKNOWN"):
                await edits.submit(*args)
            assert store.get_operation("s", scope, "edit-one")["state"] == "unknown"
        else:
            assert (await edits.submit(*args))["state"] == "accepted"
        assert store.budget_usage("s", scope)["render"]["held"] == 1
        # Reopening the ledger cannot submit an unknown/accepted operation again.
        store.close()
        store = ArtistStore(tmp_path / "edit-ledger.sqlite3")
        edits = ImageEdits(store, bridge)
        await edits.submit(*args)
        assert len(edit_calls) == 1
        service.worker = asyncio.create_task(service._work())
        async with asyncio.timeout(10):
            child = None
            while child is None or store.get_operation("s", scope, "edit-one")["state"] != "succeeded":
                child = await edits.collect("s", scope, "edit-one", "parent")
                await asyncio.sleep(0.02)
        assert child["parent_id"] == "parent" and child["job_id"] != original_job
        assert child["data"] != data and path.read_bytes() == data
        assert store.get_version("s", scope, "parent") == parent
        assert (await edits.collect("s", scope, "edit-one", "parent")) == child
        assert len(store.list_versions("s", scope, "work")) == 2
        assert store.budget_usage("s", scope)["render"]["held"] == 0
        assert service.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
        observations = list(service.cfg.state.rglob("observed-edit-input.json"))
        assert len(observations) == 1
        observed_input = json.loads(observations[0].read_text())
        assert observed_input["sha256"] == hashlib.sha256(data).hexdigest()
        assert observed_input["image_items"] == 1 and observed_input["size"] == len(data)
        assert Path(observed_input["path"]).read_bytes() == data
    finally:
        store.close()
