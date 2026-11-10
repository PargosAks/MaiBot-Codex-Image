"""Lifetime acceptance envelope through the actual Artist, with synthetic I/O."""

# ruff: noqa: I001 -- parent AGENTS.md requires from-imports before plain imports.

from types import SimpleNamespace
import asyncio

import pytest

from image_plugin_under_test import CodexImagePlugin, Config
from image_plugin_under_test.domain.delivery import Deliveries
from image_plugin_under_test.workspace.studio import Studio
from support.artist_fakes import SCOPE
from support.artist_flow import step, studio as studio
from support.command_spies import BridgeSpy, Sender
from support.settings import SECRET



@pytest.mark.parametrize('revise', [0, 1])
@pytest.mark.parametrize('unknown_send', [False, True])
async def test_one_work_review_share_then_stop_across_days(studio, revise, unknown_send):
    artist, store, clock, model, renderer, sender = studio
    store.configure_single_work('s', SCOPE, enabled=True)
    model.revise, model.share, sender.lose_receipt = revise, True, unknown_send
    results = []
    for _ in range(10):
        results.append(await step(studio))
        if results[-1]['state'] in {'shared', 'share_unknown'}:
            break
    assert results[-1]['state'] == ('share_unknown' if unknown_send else 'shared'), results
    assert len(renderer.calls) == 1 + revise and len(sender.calls) == 1
    before = len(model.calls)
    clock.advance(86400)
    await artist.on_chat(message_id='new-day', text='another creative idea')
    assert (await artist.run_once())['state'] == 'single_work_stopped'
    assert len(model.calls) == before and len(sender.calls) == 1
    assert len(store.list_artist_works('s', SCOPE)) == 1
    status = store.single_work_status('s', SCOPE)
    assert (status['create_used'], status['edit_used'], status['share_used']) == (1, revise, 1)
    view = Studio(store.path.parent, scopes={'s': SCOPE})
    assert view.summary('s')['single_work'] == status


@pytest.mark.parametrize('persisted', [False, True])
@pytest.mark.parametrize('action', ['画图', '重发画图'])
async def test_manual_commands_cannot_bypass_stream_limit(studio, persisted, action):
    _, store, *_ = studio
    plugin = CodexImagePlugin()
    config = Config().model_dump()
    config['connection'].update(secret=SECRET, allowed_users=[SCOPE.platform + ':other-owner'])
    if persisted:
        store.configure_single_work('s', SCOPE, enabled=True)
    else:
        config['autonomy']['spaces'] = [{'space_id': 's', 'platform': SCOPE.platform,
            'stream_id': SCOPE.stream_id, 'user_id': SCOPE.user_id, 'single_work': True}]
    plugin.set_plugin_config(config)
    bridge, sender = BridgeSpy(), Sender()
    plugin._set_context(SimpleNamespace(send=sender, paths=SimpleNamespace(data_dir=store.path.parent)))
    plugin.bridge = bridge
    scope = {'platform': SCOPE.platform, 'stream_id': SCOPE.stream_id, 'user_id': 'other-owner'}
    result = await plugin._handle({**scope, 'message_id': 'one-message',
        'matched_groups': {'action': action, 'arg': 'img_' + 'a' * 32}})
    assert result[0] is False and '单作品' in result[1] and bridge.calls == []
    with pytest.raises(ValueError, match='单作品'):
        await plugin._deliver(scope, 'img_' + 'a' * 32)
    assert bridge.calls == []
    assert not Studio(store.path.parent, scopes={}).single_work_for_stream(SCOPE.platform, 'other-stream')


async def test_single_work_abandoned_never_starts_another_plan(studio):
    artist, store, _, model, renderer, sender = studio
    store.configure_single_work('s', SCOPE, enabled=True)
    assert (await artist.tick())['state'] == 'planned'
    model.plan_action = 'abandon'
    assert (await step(studio))['state'] == 'abandoned'
    calls = len(model.calls)
    assert (await step(studio, 86400))['state'] == 'single_work_stopped'
    assert len(model.calls) == calls and not renderer.calls and not sender.calls


async def test_second_revision_suggestion_stops_without_fabricated_acceptance(studio):
    _, store, _, model, renderer, sender = studio
    store.configure_single_work('s', SCOPE, enabled=True)
    model.revise, model.share = 2, True
    states = [await step(studio) for _ in range(6)]
    assert any(s.get('reason') == 'SINGLE_WORK_REVISION_LIMIT' for s in states)
    work, = store.list_artist_works('s', SCOPE)
    assert work['status'] == 'abandoned'
    calls = len(model.calls)
    assert (await step(studio, 86400))['state'] == 'single_work_stopped'
    assert len(model.calls) == calls and len(renderer.calls) == 2 and not sender.calls


async def test_config_boundary_cancels_watcher_before_slow_runtime_close(tmp_path, monkeypatch):
    plugin = CodexImagePlugin()
    plugin.set_plugin_config(Config().model_dump())
    plugin._set_context(SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path)))
    plugin.journal = Deliveries(tmp_path / 'delivery.sqlite3')
    entered, release, closing, close_release = (asyncio.Event() for _ in range(4))
    sent = []

    async def call(operation, scope, args):
        if operation == 'status':
            return {'status': 'completed', 'artifacts': [1]}
        assert operation == 'result'
        entered.set()
        await release.wait()
        return 'synthetic'

    async def send(*args, **kwargs):
        sent.append(True)
        return True

    async def close():
        closing.set()
        await close_release.wait()

    async def load():
        pass

    monkeypatch.setattr('image_plugin_under_test.host.host_adapters.send_image_receipt', send)
    monkeypatch.setattr(plugin, '_load', load)
    plugin.bridge = SimpleNamespace(call=call)
    plugin.artist_runtime = SimpleNamespace(stopping=False, tasks={}, close=close)
    scope = {'platform': SCOPE.platform, 'stream_id': SCOPE.stream_id, 'user_id': SCOPE.user_id}
    watcher = asyncio.create_task(plugin._watch(scope, 'old-job'))
    plugin.watchers['old-job'] = watcher
    reload = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        config = Config().model_dump()
        config['autonomy']['spaces'] = [{'space_id': 's', **scope, 'single_work': True, 'paused': True}]
        plugin.set_plugin_config(config)
        reload = asyncio.create_task(plugin.on_config_update('plugin', {}, 'new'))
        await asyncio.wait_for(closing.wait(), 1)
        assert watcher.cancelled() and not plugin.watchers
        release.set()
        assert not sent
    finally:
        close_release.set()
        if reload is not None:
            await reload
        await plugin.on_unload()
    assert not sent


async def test_changed_config_after_result_never_starts_send(tmp_path):
    plugin = CodexImagePlugin()
    original = Config().model_dump()
    plugin.set_plugin_config(original)
    plugin._set_context(SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path)))
    plugin.journal = Deliveries(tmp_path / 'delivery.sqlite3')

    async def call(*args):
        plugin.set_plugin_config(original)
        return 'synthetic'

    plugin.bridge = SimpleNamespace(call=call)
    try:
        with pytest.raises(ValueError, match='配置已变更'):
            await plugin._deliver({'platform': 'qq', 'user_id': 'owner', 'stream_id': 'chat'}, 'old-job')
        assert plugin.journal.db.execute('SELECT count(*) FROM delivery').fetchone()[0] == 0
    finally:
        plugin.journal.close()


@pytest.mark.parametrize('replace_config', [False, True])
@pytest.mark.parametrize('action', ['画图', '重发画图'])
async def test_manual_submit_rechecks_stop_epoch_after_async_policy_read(monkeypatch, replace_config, action):
    plugin = CodexImagePlugin()
    config = Config().model_dump()
    config['connection'].update(secret=SECRET, allowed_users=['qq:owner'])
    plugin.set_plugin_config(config)
    bridge = BridgeSpy()
    plugin.bridge = bridge

    async def late_old_policy(scope):
        if replace_config:
            plugin.set_plugin_config(config)
        plugin._stop_artist_workers()

    monkeypatch.setattr(plugin, '_guard_single_work_command', late_old_policy)
    result = await plugin._handle({'platform': 'qq', 'user_id': 'owner', 'stream_id': 'chat',
        'message_id': 'real-id', 'matched_groups': {'action': action, 'arg': 'img_' + 'a' * 32}})
    assert result[0] is False and '尚未提交' in result[1] and not bridge.calls


async def test_stop_during_journal_intent_records_known_unsent(tmp_path):
    plugin = CodexImagePlugin()
    plugin.set_plugin_config(Config().model_dump())
    plugin._set_context(SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path)))
    plugin.journal = Deliveries(tmp_path / 'delivery.sqlite3')
    real_begin = plugin.journal.begin

    def begin(*args):
        result = real_begin(*args)
        # Deterministic interleaving while the joined SQLite call is awaited.
        plugin._dispatch_epoch += 1
        return result

    async def call(*args):
        return 'synthetic'

    plugin.journal.begin = begin
    plugin.bridge = SimpleNamespace(call=call)
    try:
        with pytest.raises(ValueError, match='尚未发送'):
            await plugin._deliver({'platform': 'qq', 'user_id': 'owner', 'stream_id': 'chat'}, 'old-job')
        assert plugin.journal.db.execute('SELECT state FROM delivery').fetchone()[0] == 'send_failed'
    finally:
        plugin.journal.close()
