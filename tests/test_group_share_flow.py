"""Stage 3 simulations through the real SDK, Host AST and persistent ledger.

The model deliberately requests sharing even in collect-only mode. Effects are
fixed PNG generation/editing and synthetic Host receipts in temporary databases;
no running Host, real model, renderer or platform is contacted.
"""

from copy import deepcopy

import pytest

from image_plugin_under_test.creation.artist import stable_id
from image_plugin_under_test.domain.policy import Limits, StoreError
from protocol_fixture import PNG
from support.group_adoption import (
    AdoptionModel,
    HOST_SCOPE,
    adoption_host as adoption_host,
    advance,
    remember,
    sync,
)
from support.host_chain import host_chain as host_chain, public_host as public_host
from support.group_thought_host import thought_host as thought_host
from support.host_chain import OWNER, PLATFORM, STREAM


GROUP = "synthetic-group"
MISSING = object()


class ShareModel(AdoptionModel):
    def __init__(self):
        super().__init__()
        self.create_without_note = False

    def plan(self, context):
        result = super().plan(context)
        if self.create_without_note and not context["works"]:
            from support.artist_fakes import BRIEF
            result["decision"] = {
                "action": "create", "brief": dict(BRIEF), "inspiration_keys": [],
                "reason": "自主练习，不采用群念头",
            }
        return result


@pytest.fixture
async def share_host(adoption_host):
    h = adoption_host
    h.adoption_model = ShareModel()
    h.base_share_config = h.config
    return h


async def configure(h, *, mode=MISSING, inspiration=True, enabled=True):
    """Reload through the actual plugin lifecycle; no scheduled worker runs."""
    assert not h.gate.is_set()
    await h.plugin.on_unload()

    def config(**kwargs):
        value = h.base_share_config(**kwargs)
        for item in value["autonomy"]["spaces"]:
            item["group_inspiration"] = inspiration
            if mode is MISSING:
                item.pop("group_share_mode", None)
            else:
                item["group_share_mode"] = mode
        return value

    h.config = config
    await h.load(group=True, enabled=enabled)
    if h.plugin.group_thought_runtime is not None:
        h.plugin.group_thought_runtime.clock = lambda: h.clock().timestamp()


def share_usage(h):
    return h.runtime.store.budget_usage("studio", HOST_SCOPE)["share"]


def share_operations(h):
    return [op for op in h.runtime.store.audit_operations("studio", HOST_SCOPE)
            if op["action"] == "share"]


def deliveries(h):
    return [tuple(row) for row in h.plugin.journal.db.execute("SELECT * FROM delivery ORDER BY job")]


async def finish(h, *, revise=False, note=True, wake="idle"):
    h.revise = revise
    if note:
        await remember(h)
        await sync(h)
    else:
        h.adoption_model.create_without_note = True
    if wake == "chat":
        await h.message("今晚有什么想画的？", message_id="share-policy-chat")
        planned = await h.runtime.artists["studio"].run_once()
    else:
        planned = await h.runtime.artists["studio"].tick()
    assert planned["state"] == "planned"
    assert (await advance(h))["state"] == "review_pending"
    if revise:
        assert (await advance(h))["reason"] == "revision"
        assert (await advance(h))["state"] == "review_pending"
    assert (await advance(h))["state"] == "finished"
    work, = h.runtime.store.list_artist_works("studio", HOST_SCOPE)
    assert not h.sends
    return work


@pytest.mark.parametrize("wake", ["chat", "idle"])
async def test_missing_group_mode_collects_edits_and_reviews_but_cannot_share(share_host, wake):
    h = share_host
    await configure(h)  # The field is absent, not explicitly set to collect_only.
    assert h.plugin.config.autonomy.spaces[0].group_share_mode == "collect_only"
    work = await finish(h, revise=True, wake=wake)
    versions = h.runtime.store.list_versions("studio", HOST_SCOPE, work["work_id"])
    assert len(versions) == len(h.renderer.calls) == len(h.reviews) == 2
    assert h.renderer.calls[1]["source"] == {
        "job_id": versions[0]["job_id"], "sha256": versions[0]["sha256"],
        "data": PNG, "mime": "image/png",
    }
    assert h.adoption_model.plans[-1]["sharing"] == {"mode": "collect_only", "allowed": False}
    h.adoption_model.share = True  # Deliberately ignore the policy in the prompt.
    result = await advance(h)
    assert result["state"] == "blocked" and result["reason"] == "SHARE_NOT_ALLOWED"
    assert not h.sends and not share_operations(h) and not deliveries(h)
    assert share_usage(h) == {"held": 0, "consumed": 0, "total": 0}
    assert h.runtime.store.get_artist_work("studio", HOST_SCOPE, work["work_id"])["status"] == "finished"


async def test_original_group_model_waits_then_chooses_to_share_once(share_host):
    h = share_host
    await configure(h, mode="original_group")
    work = await finish(h)
    before_lookups = sum(name == "chat.get_stream_by_group_id" for name, _ in h.calls)
    assert (await advance(h))["state"] == "waiting"
    assert h.adoption_model.plans[-1]["sharing"] == {"mode": "original_group", "allowed": True}
    assert not h.sends and not share_operations(h) and share_usage(h)["total"] == 0
    h.adoption_model.share = True
    assert (await advance(h))["state"] == "shared"
    assert (await advance(h))["state"] == "share_recorded"
    assert len(h.sends) == 1 and h.sends[0]["stream_id"] == STREAM
    checks = [args for name, args in h.calls if name == "chat.get_stream_by_group_id"]
    assert len(checks) == before_lookups + 1
    assert checks[-1] == {"platform": PLATFORM, "group_id": GROUP}
    op, = share_operations(h)
    assert op["work_id"] == work["work_id"] and op["state"] == "succeeded"
    assert share_usage(h) == {"held": 0, "consumed": 1, "total": 1}


@pytest.mark.parametrize("mode,allowed", [("collect_only", False), ("original_group", True)])
async def test_share_permission_covers_group_work_without_any_note(share_host, mode, allowed):
    h = share_host
    await configure(h, mode=mode, inspiration=False)
    work = await finish(h, note=False)
    assert work["inspiration_keys"] == []
    assert not (h.data_paths.data_dir / "group_thoughts.sqlite3").exists()
    h.adoption_model.share = True
    result = await advance(h)
    assert result["state"] == ("shared" if allowed else "blocked")
    assert len(h.sends) == int(allowed)
    assert share_usage(h)["consumed"] == int(allowed)


async def test_disabling_collection_preserves_separate_share_permission(share_host):
    h = share_host
    await configure(h, mode="original_group")
    work = await finish(h)
    before_versions = h.runtime.store.list_versions("studio", HOST_SCOPE, work["work_id"])
    await configure(h, mode="original_group", inspiration=False)
    h.adoption_model.share = True
    assert (await advance(h))["state"] == "shared"
    assert len(h.renderer.calls) == len(h.sends) == 1
    assert h.runtime.store.list_versions("studio", HOST_SCOPE, work["work_id"]) == before_versions


@pytest.mark.parametrize("change", ["denied", "group", "stream", "session", "platform", "private"])
async def test_final_raw_host_binding_failure_is_known_failure_without_send(share_host, change):
    h = share_host
    await configure(h, mode="original_group")
    await finish(h)
    stream = {"platform": PLATFORM, "stream_id": STREAM, "session_id": STREAM,
              "user_id": OWNER, "group_id": GROUP, "is_group_session": True}
    replacements = {"group": ("group_id", "another-group"), "stream": ("stream_id", "foreign-stream"),
                    "session": ("session_id", "foreign-session"), "platform": ("platform", "foreign-platform"),
                    "private": ("is_group_session", False)}
    if change in replacements:
        key, value = replacements[change]
        stream[key] = value
    # A success=False envelope with an otherwise valid stream must be rejected.
    h.stream_override = {"success": change != "denied", "stream": stream}
    h.adoption_model.share = True
    assert (await advance(h))["state"] == "share_failed"
    assert not h.sends and len(h.renderer.calls) == 1
    # Preserve the existing conservative accounting of an already started
    # attempt; a known failure releases its hold, without refunding the attempt.
    assert share_usage(h) == {"held": 0, "consumed": 1, "total": 1}
    op, = share_operations(h)
    assert op["state"] == "failed"
    assert h.plugin.journal.db.execute("SELECT state FROM delivery").fetchone()[0] == "send_failed"


async def test_config_revocation_after_review_does_not_send_or_spend_share_budget(share_host):
    h = share_host
    await configure(h, mode="original_group")
    work = await finish(h)
    render_count = len(h.renderer.calls)
    await configure(h, mode="collect_only")
    h.adoption_model.share = True
    assert (await advance(h)) == {"state": "blocked", "reason": "SHARE_NOT_ALLOWED"}
    assert not h.sends and not share_operations(h) and not deliveries(h)
    assert share_usage(h)["total"] == 0 and len(h.renderer.calls) == render_count
    assert h.runtime.store.get_artist_work("studio", HOST_SCOPE, work["work_id"])["status"] == "finished"


async def test_policy_change_during_final_lookup_is_rechecked_before_dispatch(share_host):
    h = share_host
    await configure(h, mode="original_group")
    await finish(h)

    async def revoke_policy(plugin_id, name, args):
        assert name == "chat.get_stream_by_group_id"
        h.calls.append((name, deepcopy(args)))
        version = h.runtime.store.get_space("studio", HOST_SCOPE)["auth_version"]
        h.runtime.store.configure_share_policy("studio", HOST_SCOPE, group_id=GROUP,
                                               mode="collect_only", expected_auth_version=version)
        return {"success": True, "stream": {"platform": PLATFORM, "stream_id": STREAM,
                "session_id": STREAM, "group_id": GROUP, "is_group_session": True}}

    h.capabilities.register_capability("chat.get_stream_by_group_id", revoke_policy)
    h.adoption_model.share = True
    assert (await advance(h))["state"] == "share_failed"
    assert not h.sends and share_usage(h) == {"held": 0, "consumed": 1, "total": 1}
    assert h.plugin.journal.db.execute("SELECT state FROM delivery").fetchone()[0] == "send_failed"


@pytest.mark.parametrize("failure", ["lookup_timeout", "revoke", "regrant", "budget", "day"])
async def test_failed_preflight_is_known_not_sent_without_releasing_history(share_host, failure):
    h = share_host
    await configure(h, mode="original_group")
    await finish(h)
    assert h.plugin.journal.begin("historical-unknown")
    h.plugin.journal.finish("historical-unknown", "send_unknown")

    async def preflight(plugin_id, name, args):
        assert name == "chat.get_stream_by_group_id"
        if failure == "lookup_timeout":
            raise TimeoutError("synthetic read-only lookup timeout")
        if failure in {"revoke", "regrant"}:
            h.runtime.store.revoke("studio", HOST_SCOPE, source_ref="preflight-test", granted_by="test")
            if failure == "regrant":
                from image_plugin_under_test.domain.policy import Delegation
                h.runtime.store.grant("studio", HOST_SCOPE, Delegation("new-consent", "test", True))
        elif failure == "budget":
            h.runtime.store.set_limits("studio", HOST_SCOPE,
                Limits(think=100, render=10, share=0, max_inflight=10, max_revisions=1))
        else:
            h.clock.advance(86400)
        return {"success": True, "stream": {"platform": PLATFORM, "stream_id": STREAM,
                "session_id": STREAM, "group_id": GROUP, "is_group_session": True}}

    h.capabilities.register_capability("chat.get_stream_by_group_id", preflight)
    h.adoption_model.share = True
    assert (await advance(h))["state"] == "share_failed"
    assert not h.sends
    op, = share_operations(h)
    assert op["state"] == "failed"
    rows = dict(h.plugin.journal.db.execute("SELECT job,state FROM delivery"))
    assert rows.pop("historical-unknown") == "send_unknown"
    assert list(rows.values()) == ["send_failed"]
    assert share_usage(h)["held"] == 0


@pytest.mark.parametrize("history", ["legacy_epoch", "revoked_epoch"])
async def test_reserved_share_cannot_revive_with_missing_or_revoked_permission(share_host, history):
    h = share_host
    await configure(h, mode="original_group")
    work = await finish(h)
    store = h.runtime.store
    version = store.get_version("studio", HOST_SCOPE, work["best_version"])
    request_id = stable_id("share", work["work_id"], version["version_id"])
    store.reserve("studio", HOST_SCOPE, action="share", request_id=request_id, work_id=work["work_id"],
                  params={"job_id": version["job_id"], "version_id": version["version_id"]})
    if history == "legacy_epoch":
        # Simulate a persisted share reserved by a version with no share epoch.
        with store.lock, store.db:
            store.db.execute("DELETE FROM share_policy_claims WHERE operation_id IN "
                             "(SELECT id FROM operations WHERE space_id=? AND request_id=?)",
                             ("studio", request_id))
    else:
        await configure(h, mode="collect_only")
    await configure(h, mode="original_group")
    with pytest.raises(StoreError, check=lambda exc: exc.code == "SHARE_POLICY_CHANGED"):
        h.runtime.store.mark_submitting("studio", HOST_SCOPE, request_id)
    h.adoption_model.share = True
    result = await advance(h)
    assert result["state"] == "blocked"
    assert not h.sends and len(h.renderer.calls) == 1 and not deliveries(h)
    assert share_usage(h) == {"held": 1, "consumed": 0, "total": 1}


async def test_zero_share_budget_still_blocks_original_group_model(share_host):
    h = share_host
    await configure(h, mode="original_group")
    await finish(h)
    h.runtime.store.set_limits("studio", HOST_SCOPE,
        Limits(think=100, render=10, share=0, max_inflight=10, max_revisions=1))
    h.adoption_model.share = True
    assert (await advance(h)) == {"state": "blocked", "reason": "BUDGET_EXCEEDED"}
    assert not h.sends and not share_operations(h) and not deliveries(h)
    assert share_usage(h)["total"] == 0


@pytest.mark.parametrize("unknown", [False, True])
async def test_existing_delivery_survives_policy_switch_and_reload_without_replay(share_host, unknown):
    h = share_host
    await configure(h, mode="original_group")
    await finish(h)
    h.lose_send = unknown
    h.adoption_model.share = True
    assert (await advance(h))["state"] == ("share_unknown" if unknown else "shared")
    records, operations, usage = deliveries(h), share_operations(h), share_usage(h)
    for mode in ("collect_only", "original_group"):
        await configure(h, mode=mode)
        for _ in range(2):
            await advance(h)
        assert deliveries(h) == records
        assert share_operations(h) == operations and share_usage(h) == usage
    assert len(h.sends) == len(h.renderer.calls) == 1
    assert h.plugin.journal.db.execute("SELECT state FROM delivery").fetchone()[0] == (
        "send_unknown" if unknown else "sent")


async def test_private_chat_remains_shareable_without_group_mode(host_chain):
    h = host_chain
    await h.load(group=False)
    assert h.plugin.config.autonomy.spaces[0].group_share_mode == "collect_only"
    h.revise = False
    assert (await h.runtime.artists["studio"].tick())["state"] == "planned"
    assert (await advance(h))["state"] == "review_pending"
    assert (await advance(h))["state"] == "finished"
    h.share = True
    assert (await advance(h))["state"] == "shared"
    assert len(h.sends) == 1 and share_usage(h)["consumed"] == 1
    assert not any(name == "chat.get_stream_by_group_id" for name, _ in h.calls)
