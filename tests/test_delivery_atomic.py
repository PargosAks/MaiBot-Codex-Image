"""发送许可使用真实临时 SQLite；Host 回执仅用替身。"""

# ruff: noqa: I001 -- 继承上级 AGENTS.md 的 from-import 在前约定。
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace
import asyncio
import sqlite3

import pytest

import image_plugin_under_test as plugin_module
from image_plugin_under_test.domain.delivery import Deliveries
from support.runtime_generation import JOB, OWNER, command, plugin_at


@pytest.mark.parametrize("known", [False, True])
def test_one_delivery_permit_across_sqlite_connections(tmp_path, known):
    path = tmp_path / "delivery.sqlite3"
    journals = [Deliveries(path), Deliveries(path)]
    competing_reads = Barrier(2, timeout=3)
    start = Barrier(2, timeout=3)

    class ReadBarrier(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            cursor = super().execute(sql, parameters)
            if sql.startswith("SELECT state FROM delivery"):
                # 先固定各连接的读结果，再放行旧实现的写入。
                row = cursor.fetchone()
                competing_reads.wait()
                return SimpleNamespace(fetchone=lambda: row)
            return cursor

    def compete(journal):
        start.wait()
        return journal.begin(JOB, manual=known)

    try:
        if known:
            assert journals[0].begin(JOB)
            journals[0].finish(JOB, "send_failed")
        for journal in journals:
            journal.close()
            journal.db = sqlite3.connect(path, check_same_thread=False, factory=ReadBarrier)
        with ThreadPoolExecutor(2) as workers:
            outcomes = list(workers.map(compete, journals))
        assert sorted(outcomes) == [False, True]
    finally:
        for journal in journals:
            journal.close()


async def test_cancel_before_dispatch_joins_claim_and_records_known_unsent(tmp_path, monkeypatch):
    async def capability(*args, **kwargs):
        pytest.fail("已取消的发送不应调用 Host")

    plugin = plugin_at(tmp_path, monkeypatch, call_host_method=capability)
    await plugin.on_load()
    begin = plugin.journal.begin
    entered, release = Event(), Event()

    def wait_after_claim(*args):
        permitted = begin(*args)
        entered.set()
        assert release.wait(5)
        return permitted

    monkeypatch.setattr(plugin.journal, "begin", wait_after_claim)
    task = asyncio.create_task(plugin._deliver(OWNER, JOB))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert plugin.journal.db.execute("SELECT state FROM delivery").fetchone() == ("send_failed",)
        assert not begin(JOB)
        assert begin(JOB, manual=True)
        plugin.journal.finish(JOB, "sent")
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await plugin.on_unload()


async def test_cancel_during_finish_keeps_confirmed_receipt(tmp_path, monkeypatch):
    async def capability(*args, **kwargs):
        return {"sent": True}

    plugin = plugin_at(tmp_path, monkeypatch, call_host_method=capability)
    await plugin.on_load()
    finish = plugin.journal.finish
    entered, release = Event(), Event()

    def delayed_finish(*args):
        entered.set()
        assert release.wait(5)
        finish(*args)

    monkeypatch.setattr(plugin.journal, "finish", delayed_finish)
    task = asyncio.create_task(plugin._deliver(OWNER, JOB))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert plugin.journal.state(JOB) == "sent"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await plugin.on_unload()


async def test_second_manual_plugin_cannot_recover_live_intent(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def capability(method, *, payload, **kwargs):
        assert payload["capability"] == "send.image"
        entered.set()
        await release.wait()
        return {"sent": True}

    first = plugin_at(tmp_path, monkeypatch, call_host_method=capability)
    second = plugin_at(tmp_path, monkeypatch)
    await first.on_load()
    sending = asyncio.create_task(first.command(**command(OWNER, "重发画图", JOB)))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert not first.config.autonomy.enabled and first.journal.state(JOB) == "sending"
        await asyncio.wait_for(second.on_load(), 2)
        assert first.journal.state(JOB) == "sending", "第二个实例误恢复了活跃发送意图"
        assert "Another Host" in second.startup_error and second.journal is None
        release.set()
        assert (await asyncio.wait_for(sending, 2))[0]
        assert first.journal.state(JOB) == "sent"
        await first.on_unload()
        await second.on_load()
        assert second.journal.state(JOB) == "sent"
    finally:
        release.set()
        await asyncio.gather(sending, return_exceptions=True)
        await second.on_unload()
        await first.on_unload()


@pytest.mark.parametrize("invalid", [False, True])
async def test_data_directory_lease_survives_hot_reload(tmp_path, monkeypatch, invalid):
    opening, release = asyncio.Event(), asyncio.Event()
    first = plugin_at(tmp_path, monkeypatch)
    second = plugin_at(tmp_path, monkeypatch)
    await first.on_load()
    open_journal = first._open_journal

    def invalid_bridge(*args):
        raise ValueError("synthetic invalid connection")

    async def pause_reopen():
        opening.set()
        await release.wait()
        await open_journal()

    monkeypatch.setattr(first, "_open_journal", pause_reopen)
    if invalid:
        monkeypatch.setattr(plugin_module, "Bridge", invalid_bridge)
    reload = asyncio.create_task(first.on_config_update("plugin", {}, "same-data-dir"))
    try:
        await asyncio.wait_for(opening.wait(), 2)
        assert first.journal is None
        await second.on_load()
        assert "Another Host" in second.startup_error and second.journal is None
        release.set()
        await asyncio.wait_for(reload, 2)
        if invalid:
            assert first.journal is None and "synthetic invalid connection" in first.startup_error
            await second.on_load()
            assert "Another Host" in second.startup_error and second.journal is None
        else:
            assert first.journal is not None
    finally:
        release.set()
        await asyncio.gather(reload, return_exceptions=True)
        await first.on_unload()
        await second.on_unload()
    assert (tmp_path / "delivery.lock").is_file()


async def test_cancelled_unload_waiting_for_lifecycle_releases_delivery_lease(tmp_path, monkeypatch):
    opened, release = asyncio.Event(), asyncio.Event()
    first = plugin_at(tmp_path, monkeypatch)
    second = plugin_at(tmp_path, monkeypatch)
    open_journal = first._open_journal

    async def pause_open():
        await open_journal()
        opened.set()
        await release.wait()

    monkeypatch.setattr(first, "_open_journal", pause_open)
    loading = asyncio.create_task(first.on_load())
    unloading = None
    try:
        await asyncio.wait_for(opened.wait(), 2)
        unloading = asyncio.create_task(first.on_unload())
        await asyncio.sleep(0)
        assert first.lifecycle.locked() and not unloading.done()
        unloading.cancel()
        await asyncio.sleep(0)
        unloading.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(unloading, 2)
        await asyncio.wait_for(loading, 2)
        assert first.journal is None
        await asyncio.wait_for(second.on_load(), 2)
        assert second.journal is not None, second.startup_error
        await first.on_unload()
        assert second.journal is not None
    finally:
        release.set()
        await asyncio.gather(*(t for t in (loading, unloading) if t is not None), return_exceptions=True)
        await first.on_unload()
        await second.on_unload()
