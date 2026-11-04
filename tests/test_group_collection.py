"""Local simulation: SDK configuration → verified space → receipt → notebook → private Studio.

Host stream lookup and post-filter receipts are synthetic. Everything persisted is under tmp_path.
There is no fake render success: spending/artist effects are forbidden in this scenario.
"""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from image_plugin_under_test import CodexImagePlugin
from image_plugin_under_test.group_collection import GroupCollection
from image_plugin_under_test.group_ingress import GroupBinding, GroupIngressCandidate, HostGroupReceipt
from image_plugin_under_test.group_notebook import DATABASE_NAME
from image_plugin_under_test.host_runtime import HostRuntime
from image_plugin_under_test.policy import Scope


@pytest.fixture
async def groups(tmp_path, monkeypatch):
    calls = []
    async def host(method, *, payload, timeout_ms):
        assert method == "cap.call"
        assert payload["capability"] == "chat.get_stream_by_group_id"
        target = payload["args"]["group_id"]
        calls.append(target)
        return {"success": True, "stream": {"platform": "qq", "group_id": target,
                "stream_id": "stream-" + target, "is_group_session": True}}

    async def parked(self, space_id):
        await asyncio.Event().wait()

    monkeypatch.setattr(HostRuntime, "_serve", parked)
    plugin = CodexImagePlugin()
    plugin._set_context(SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path), call_host_method=host))
    spaces = [dict(space_id=g, platform="qq", group_id=g, stream_id="stream-" + g,
                   user_id="owner", consent_ref="test-grant", continuous=True) for g in ("a", "b", "c")]
    for item in spaces[:2]:
        item["group_inspiration"] = True
    plugin.set_plugin_config({"plugin": plugin.get_default_config()["plugin"],
                              "connection": {"secret": "synthetic-local-only-key-" + "x" * 32,
                                             "allowed_users": ["qq:owner"]},
                              "autonomy": {"enabled": True, "spaces": spaces}})
    await plugin.on_load()
    try:
        assert not plugin.startup_error
        assert calls == ["a", "b", "c"]
        yield plugin
    finally:
        await plugin.on_unload()


class Simulation:
    def __init__(self, plugin, *, supported=True):
        self.now = 605.
        self.receipts = {}
        self.calls = []
        self.collector = GroupCollection(plugin.artist_runtime, reader=self.read if supported else None,
                                         clock=lambda: self.now)

    def event(self, group, sender, msg_id, text, **overrides):
        binding = GroupBinding("qq", "stream-" + group, group)
        key = group + ":" + msg_id
        receipt = HostGroupReceipt(binding, msg_id, sender, text, self.now, key, self.now,
                                   True, True, False, False, False, "text")
        self.receipts[key] = replace(receipt, **overrides)
        return GroupIngressCandidate(binding, msg_id, key)

    async def read(self, candidate):
        self.calls.append(candidate.receipt_id)
        return self.receipts.get(candidate.receipt_id)


async def test_members_merge_only_inside_original_group_without_spending(groups, tmp_path):
    s = Simulation(groups)
    s.now += 1
    a1 = s.event("a", "member-1", "42", "今晚下雨了，我没带伞")
    a2 = s.event("a", "member-2", "43", "便利店还亮着灯，正好进去躲一会儿")
    b1 = s.event("b", "member-1", "42", "便利店灯光")
    off = s.event("c", "member-3", "42", "雨夜")
    try:
        assert not await s.collector.accept(off) and not s.calls
        for candidate in (a1, a2, b1):
            assert await s.collector.accept(candidate)
        assert not await s.collector.accept(a1)
        assert not await s.collector.accept(s.event("a", "member-3", "44", "雨夜", filter_passed=False))
        assert s.collector.tick() == ()
        s.now = 1201.
        s.collector.tick()  # quiet groups flush without another message
        for group, members, count in (("a", 2, 2), ("b", 1, 1)):
            result = await groups.studio_records(group, "group_inspirations")
            topic, = result["items"]
            assert (topic["participant_count"], topic["message_count"]) == (members, count)
            assert "便利店" in topic["summary"]
            assert ("雨" in topic["summary"]) == (group == "a")
        db = groups.artist_runtime.store.db
        for table in ("artist_wakes", "operations", "artist_works"):
            assert db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
        assert (tmp_path / DATABASE_NAME).exists()
    finally:
        s.collector.close()


async def test_actual_host_missing_receipt_reader_never_creates_notebook(groups, tmp_path):
    s = Simulation(groups, supported=False)
    s.now += 1
    assert not await s.collector.accept(s.event("a", "member", "001", "雨夜"))
    assert s.collector.tick() == ()
    s.collector.close()
    assert not (tmp_path / DATABASE_NAME).exists()


@pytest.mark.parametrize("change", ["owner_removed", "duplicate_group", "duplicate_stream", "wrong_session", "reload"])
async def test_untrusted_route_never_requests_receipt(groups, tmp_path, change):
    s = Simulation(groups)
    s.now += 1
    candidate = s.event("a", "member", "001", "雨夜")
    cfg = groups.config
    if change == "owner_removed":
        cfg.connection.allowed_users.clear()
    elif change == "duplicate_group":
        cfg.autonomy.spaces[2].group_id = "a"
    elif change == "duplicate_stream":
        cfg.autonomy.spaces[2].stream_id = "stream-a"
    elif change == "wrong_session":
        candidate = replace(candidate, binding=replace(candidate.binding, stream_id="stream-b"))
    else:
        groups.set_plugin_config(cfg.model_dump())  # old runtime cannot accept new configuration
    try:
        assert not await s.collector.accept(candidate)
        assert not s.calls and not (tmp_path / DATABASE_NAME).exists()
    finally:
        s.collector.close()


async def test_revoke_during_receipt_read_and_before_window_flush(groups):
    s = Simulation(groups)
    s.now += 1
    assert await s.collector.accept(s.event("a", "member-a", "m", "雨夜"))
    scope = Scope("qq", "stream-a", "owner")
    async def revoke(candidate):
        groups.artist_runtime.store.revoke("a", scope, source_ref="test", granted_by="test")
        return await s.read(candidate)
    late = GroupCollection(groups.artist_runtime, reader=revoke, clock=lambda: s.now)
    s.now += 1
    try:
        assert not await late.accept(s.event("a", "member-b", "n", "便利店灯光"))
        s.now = 1201.
        assert s.collector.tick() == ()
        assert not (await groups.studio_records("a", "group_inspirations"))["items"]
    finally:
        late.close()
        s.collector.close()


async def test_reenable_requires_fresh_config_and_runtime(groups, tmp_path):
    s = Simulation(groups)
    s.now += 1
    old = s.event("a", "member", "m", "雨夜")
    with pytest.raises(ValidationError, match="frozen"):
        groups.config.autonomy.spaces[0].group_inspiration = False
    config = groups.config.model_dump()
    config["autonomy"]["spaces"][0]["group_inspiration"] = False
    groups.set_plugin_config(config)
    config["autonomy"]["spaces"][0]["group_inspiration"] = True
    groups.set_plugin_config(config)
    late = Simulation(groups)  # still attached to the superseded runtime
    late.now += 2
    late.receipts.update(s.receipts)
    try:
        assert not await s.collector.accept(old)
        assert not await late.collector.accept(old)
        assert not (tmp_path / DATABASE_NAME).exists()
    finally:
        s.collector.close()
        late.collector.close()
