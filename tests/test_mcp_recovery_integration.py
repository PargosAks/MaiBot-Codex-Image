"""Opt-in cross-repository test: actual MCP transport, isolated SQLite, no Codex.

Set CODEX_IMAGE_TEST_SERVICE_SOURCE to the independent service's src directory.
The default plugin suite stays independent of a production-service checkout.
"""


import pytest

from image_plugin_under_test.mcp_client.bridge import BridgeError
from image_plugin_under_test.domain.policy import Delegation, Limits, Scope
from image_plugin_under_test.domain.store import ArtistStore
from support.settings import SCOPE
from support.external_mcp import SOURCE, actual_mcp as actual_mcp

pytestmark = pytest.mark.skipif(not SOURCE, reason="Independent MCP source is explicitly opt-in")


@pytest.fixture
def ledger(tmp_path):
    scope = Scope(**SCOPE)
    store = ArtistStore(tmp_path / "plugin.sqlite3")
    store.create_space("studio", scope, binding_id="isolated-service")
    store.grant("studio", scope, Delegation("explicit-test-delegation", "admin", True))
    limits = Limits(think=10, render=10, share=0, max_inflight=10)
    store.set_global_limits(limits)
    store.set_limits("studio", scope, limits)
    store.create_work("studio", scope, "work")
    store.reserve(
        "studio",
        scope,
        request_id="lost-receipt",
        action="submit",
        work_id="work",
        params={"prompt": "synthetic circle"},
    )
    store.mark_submitting("studio", scope, "lost-receipt")
    yield store, scope
    store.close()


async def test_lost_submit_reply_is_recovered_without_second_submit(actual_mcp, ledger, monkeypatch):
    from image_plugin_under_test.creation.recovery import ReadOnlyRecovery

    bridge, service = actual_mcp
    store, scope = ledger
    exchange = bridge._exchange

    async def lost_reply(operation, owner, args):
        await exchange(operation, owner, args)
        raise ConnectionError("Simulated lost reply after actual MCP acceptance")

    monkeypatch.setattr(bridge, "_exchange", lost_reply)
    with pytest.raises(BridgeError, check=lambda e: e.code == "TRANSPORT_UNKNOWN"):
        await bridge.call("submit", SCOPE, {"request_id": "lost-receipt", "prompt": "synthetic circle"})
    store.mark_unknown("studio", scope, "lost-receipt", reason="LOST_REPLY")
    monkeypatch.setattr(bridge, "_exchange", exchange)
    before = service.store.db.total_changes
    await ReadOnlyRecovery(store, bridge).reconcile("studio", scope, "lost-receipt")
    operation = store.get_operation("studio", scope, "lost-receipt")
    assert operation["state"] == "accepted" and operation["job_id"]
    assert service.store.db.total_changes == before
    assert service.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert service.store.db.execute("SELECT status FROM jobs").fetchone()[0] == "queued"
    assert store.budget_usage("studio", scope)["render"]["held"] == 1


async def test_not_found_and_changed_digest_never_release_unknown_hold(actual_mcp, ledger):
    from image_plugin_under_test.creation.recovery import ReadOnlyRecovery

    bridge, service = actual_mcp
    store, scope = ledger
    store.mark_unknown("studio", scope, "lost-receipt", reason="LOST_REPLY")
    recovery = ReadOnlyRecovery(store, bridge)
    await recovery.reconcile("studio", scope, "lost-receipt")
    assert store.get_operation("studio", scope, "lost-receipt")["state"] == "unknown"
    assert service.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    await bridge.call("submit", SCOPE, {"request_id": "lost-receipt", "prompt": "different frozen prompt"})
    # A conflict is a real structured MCP error; it does not prove the earlier
    # request was never accepted and cannot authorize releasing its reservation.
    with pytest.raises(BridgeError, check=lambda e: e.code == "REQUEST_CONFLICT"):
        await bridge.call("submit", SCOPE, {"request_id": "lost-receipt", "prompt": "synthetic circle"})
    with pytest.raises(BridgeError, check=lambda e: e.code == "RECEIPT_MISMATCH"):
        await recovery.reconcile("studio", scope, "lost-receipt")
    assert store.get_operation("studio", scope, "lost-receipt")["state"] == "unknown"
    assert store.budget_usage("studio", scope)["render"]["held"] == 1
    assert service.store.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
