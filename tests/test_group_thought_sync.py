"""Offline lifecycle/recovery boundary for the notebook -> artist bridge."""
import asyncio
from threading import Event

import pytest

from image_plugin_under_test.host.host_runtime import HostRuntime
from image_plugin_under_test.domain.policy import Scope
from test_group_collection import groups as groups
from test_group_thought_runtime import notes as notes, offer, response


SERVE = HostRuntime._serve


def ideas(plugin, group="a"):
    return plugin.artist_runtime.store.list_inspirations(group, Scope("qq", "stream-" + group, "owner"))


async def record(plugin):
    token = await offer(plugin)
    await plugin.group_thought_after_response(**response(token))


async def test_no_source_does_not_create_database_or_wake(notes, tmp_path):
    plugin, runtime, _ = notes
    assert not await runtime.sync("a")
    assert not (tmp_path / "group_thoughts.sqlite3").exists()
    assert not ideas(plugin)
    assert plugin.artist_runtime.store.db.execute("SELECT count(*) FROM artist_wakes").fetchone()[0] == 0


async def test_source_commit_before_import_recovers_once_after_reload(notes):
    plugin, runtime, _ = notes
    await record(plugin)
    assert not ideas(plugin)  # Source saved, worker has not run.
    await plugin.on_unload()
    await plugin.on_load()
    runtime = plugin.group_thought_runtime
    assert await runtime.sync("a")
    thought, = ideas(plugin)
    assert thought["origin"] == "group_thought" and not thought["consumed_by"]
    assert not await runtime.sync("a")
    assert not await runtime.sync("b") and not ideas(plugin, "b")
    db = plugin.artist_runtime.store.db
    assert db.execute("SELECT count(*) FROM artist_wakes").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["disabled", "owner_removed", "duplicate_group", "duplicate_stream"])
async def test_current_configuration_suppresses_pending_adoption(notes, change):
    plugin, runtime, _ = notes
    await record(plugin)
    assert await runtime.sync("a") and ideas(plugin)
    cfg = plugin.config
    if change == "disabled":
        cfg.autonomy.spaces[0] = cfg.autonomy.spaces[0].model_copy(update={"group_inspiration": False})
    elif change == "owner_removed":
        cfg.connection.allowed_users.clear()
    elif change == "duplicate_group":
        cfg.autonomy.spaces[1].group_id = "a"
    else:
        cfg.autonomy.spaces[1].stream_id = "stream-a"
    assert not await runtime.sync("a")
    assert not ideas(plugin)
    assert plugin.artist_runtime.store.db.execute("SELECT count(*) FROM group_thought_links").fetchone()[0] == 1


async def test_real_worker_imports_before_regular_tick(notes, monkeypatch):
    plugin, runtime, _ = notes
    await record(plugin)
    host = plugin.artist_runtime
    reached = asyncio.Event()
    seen = []

    async def tick():
        seen.extend(ideas(plugin))
        reached.set()

    monkeypatch.setattr(host.artists["a"], "tick", tick)
    task = asyncio.create_task(SERVE(host, "a"))
    try:
        await asyncio.wait_for(reached.wait(), 3)
        assert len(seen) == 1 and seen[0]["origin"] == "group_thought"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_cancelled_import_is_joined_before_database_close(notes, monkeypatch):
    plugin, runtime, _ = notes
    await record(plugin)
    entered, release = Event(), Event()
    original = runtime._sync

    def delayed(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(runtime, "_sync", delayed)
    task = asyncio.create_task(runtime.sync("a"))
    closing = None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait), 3)
        task.cancel()
        closing = asyncio.create_task(plugin.on_unload())
        await asyncio.sleep(0)
        assert not closing.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        if closing is not None:
            await closing
    assert plugin.artist_runtime is None and plugin.group_thought_runtime is None
