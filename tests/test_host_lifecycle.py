"""Ownership and authority edges around the Host lifecycle (no network/model)."""

import asyncio
from threading import Event
from types import SimpleNamespace

import pytest

from image_plugin_under_test import CodexImagePlugin
from image_plugin_under_test.host_runtime import RuntimeLease, joined
from image_plugin_under_test.policy import Delegation, Scope, StoreError
from image_plugin_under_test.store import ArtistStore
from image_plugin_under_test.delivery import Deliveries
from test_mcp_plugin import plugin_instance
from conftest import SCOPE


def plugin_at(tmp_path, config=None):
    plugin = CodexImagePlugin()
    plugin.set_plugin_config({"plugin": {"config_version": "0.8.0"}, **(config or {})})
    plugin._set_context(SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path)))
    return plugin


async def test_old_config_default_off_never_creates_artist_state(tmp_path):
    plugin = plugin_at(tmp_path, {"plugin": {"config_version": "0.4.0"}})
    await plugin.on_load()
    try:
        assert not plugin.config.autonomy.enabled
        assert plugin.artist_runtime.store is None
        assert not plugin.artist_runtime.tasks
        assert not (tmp_path / "artist.sqlite3").exists()
        assert not (tmp_path / "artist.lock").exists()
        await plugin.artist_chat(message={"processed_plain_text": "untrusted"})
    finally:
        await plugin.on_unload()


@pytest.mark.parametrize("config", [
    {},
    {"autonomy": {"enabled": True}},
    {"autonomy": {"enabled": True, "spaces": [dict(
        space_id="studio", platform="synthetic", user_id="owner", stream_id="chat",
        continuous=True, consent_ref="explicit-admin-consent",
    )]}},
])
async def test_disabled_removed_or_no_longer_allowlisted_revokes_persisted_grant(tmp_path, config):
    scope = Scope("synthetic", "chat", "owner")
    store = ArtistStore(tmp_path / "artist.sqlite3")
    store.create_space("studio", scope, binding_id="existing-mcp")
    store.grant("studio", scope, Delegation("explicit-admin-consent", "host-config", True))
    store.close()
    plugin = plugin_at(tmp_path, config)
    await plugin.on_load()
    try:
        assert not plugin.startup_error
        assert not plugin.artist_runtime.tasks
        grants = plugin.artist_runtime.store.audit_authorizations("studio", scope)
        assert len(grants) == 2 and not grants[-1]["authorized"]
    finally:
        await plugin.on_unload()
    # Repeated disabled startup does not produce redundant authorization epochs.
    await plugin.on_load()
    try:
        assert len(plugin.artist_runtime.store.audit_authorizations("studio", scope)) == 2
    finally:
        await plugin.on_unload()


def test_live_lease_cannot_be_recovered_by_a_second_runtime(tmp_path):
    path = tmp_path / "artist.lock"
    first = RuntimeLease(path)
    try:
        with pytest.raises(StoreError, match="Another Host"):
            RuntimeLease(path)
    finally:
        first.close()
    replacement = RuntimeLease(path)
    replacement.close()
    assert path.exists()


async def test_repeated_cancellation_waits_for_owned_cleanup():
    entered, release = asyncio.Event(), asyncio.Event()
    finished = []

    async def work():
        entered.set()
        await release.wait()
        finished.append(True)

    task = asyncio.create_task(joined(work()))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == [True]


async def test_close_does_not_steal_second_host_lease(tmp_path):
    plugin = plugin_at(tmp_path, {"autonomy": {"enabled": True}})
    await plugin.on_load()
    second = plugin_at(tmp_path, {"autonomy": {"enabled": True}})
    try:
        await second.on_load()
        assert "Another Host" in second.startup_error
        assert second.artist_runtime is None
        with pytest.raises(StoreError):
            RuntimeLease(tmp_path / "artist.lock")
    finally:
        await second.on_unload()
        await plugin.on_unload()


async def test_disabled_authority_is_revoked_even_with_invalid_mcp_config(tmp_path):
    scope = Scope("synthetic", "chat", "owner")
    store = ArtistStore(tmp_path / "artist.sqlite3")
    store.create_space("studio", scope, binding_id="existing-mcp")
    store.grant("studio", scope, Delegation("consent", "host-config", True))
    store.close()
    plugin = plugin_at(tmp_path, {"connection": {"allowed_users": ["synthetic:owner"], "secret": ""}})
    await plugin.on_load()
    try:
        assert plugin.startup_error and plugin.artist_runtime is None
        restored = ArtistStore(tmp_path / "artist.sqlite3")
        try:
            assert not restored.get_space("studio", scope)["authorized"]
        finally:
            restored.close()
    finally:
        await plugin.on_unload()


async def test_unload_joins_cancelled_legacy_send_intent_writer(tmp_path, monkeypatch):
    plugin, sender = await plugin_instance(tmp_path, "http://127.0.0.1:9/mcp")
    entered, release = Event(), Event()
    original = plugin.journal.begin

    def slow_begin(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    async def result(*args):
        return "synthetic-image-never-sent"

    monkeypatch.setattr(plugin.journal, "begin", slow_begin)
    plugin.bridge = SimpleNamespace(call=result)
    send = asyncio.create_task(plugin._deliver(SCOPE, "slow-intent"))
    plugin.watchers["slow-intent"] = send
    assert await asyncio.to_thread(entered.wait, 2)
    unload = asyncio.create_task(plugin.on_unload())
    try:
        await asyncio.sleep(.02)
        unload.cancel()
        await asyncio.sleep(0)
        unload.cancel()
        await asyncio.sleep(.02)
        assert not unload.done() and not send.done() and plugin.journal is not None
    finally:
        release.set()
        await asyncio.gather(send, unload, return_exceptions=True)
        await plugin.on_unload()
    assert not sender.images and plugin.journal is None
    journal = Deliveries(tmp_path / "delivery.sqlite3")
    try:
        assert journal.db.execute("SELECT state FROM delivery").fetchone()[0] == "send_unknown"
    finally:
        journal.close()


@pytest.mark.parametrize("update", [True, False])
async def test_stop_workers_before_waiting_for_a_command_lock(tmp_path, update):
    plugin = plugin_at(tmp_path, {"autonomy": {"enabled": True}})
    proceed = asyncio.Event()
    external = []

    async def worker():
        await proceed.wait()
        external.append("simulated dispatch")

    task = asyncio.create_task(worker())
    plugin.artist_runtime = SimpleNamespace(stopping=False, tasks={"space": task})
    await plugin.lifecycle.acquire()
    callback = None
    try:
        plugin.set_plugin_config({"plugin": {"config_version": "0.8.0"}, "autonomy": {"enabled": False}})
        callback = asyncio.create_task(plugin.on_config_update("plugin", {}, "disabled")
                                       if update else plugin.on_unload())
        await asyncio.sleep(.01)
        assert not callback.done()  # Still waiting for an existing command.
        proceed.set()
        await asyncio.gather(task, return_exceptions=True)
        assert external == [] and task.cancelled() and plugin.artist_runtime.stopping
    finally:
        if callback:
            callback.cancel()
            await asyncio.gather(callback, return_exceptions=True)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        plugin.lifecycle.release()
