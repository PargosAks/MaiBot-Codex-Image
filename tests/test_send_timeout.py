"""Slow-upload simulations through real SDK/Host bridge, with temporary ledgers.

Transport time is scaled 100:1; no QQ, MCP, model or production database access.
"""

import asyncio
import base64
from types import SimpleNamespace

import pytest

from image_plugin_under_test import Config
from image_plugin_under_test.delivery import Deliveries
from image_plugin_under_test.host_adapters import HostSender, send_image_receipt
from image_plugin_under_test.policy import StoreError
from test_artist_flow import SCOPE, finished, step, studio as studio_fixture
from test_host_adapters import ctx as sdk_context_fixture
from test_host_runtime import runner_bridge

studio = studio_fixture
ctx = sdk_context_fixture


class UploadRPC:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.reply = asyncio.get_running_loop().create_future()

    async def send_request(self, **kwargs):
        self.calls.append(kwargs)
        self.entered.set()
        # The independent QQ upload is not cancelled when the caller stops waiting.
        return await asyncio.wait_for(asyncio.shield(self.reply), kwargs['timeout_ms'] / 100000)

    def complete(self):
        self.reply.set_result(SimpleNamespace(error=None, payload={'result': {'sent': True}}))


async def test_original_default_30s_expires_before_157s_upload(ctx):
    rpc = UploadRPC()
    ctx.sdk._rpc_call = runner_bridge(rpc)
    task = asyncio.create_task(ctx.call_host_method('cap.call', payload={
        'capability': 'send.image', 'args': {'image_base64': 'synthetic', 'stream_id': 'synthetic'},
    }))
    await asyncio.wait_for(rpc.entered.wait(), 2)
    with pytest.raises(TimeoutError):
        await task
    assert rpc.calls[0]['timeout_ms'] == 30000 and not rpc.reply.done()
    rpc.complete()
    assert len(rpc.calls) == 1


@pytest.mark.parametrize('mode', ['slow-success', 'timeout', 'outer-timeout', 'cancel'])
async def test_slow_host_share_and_late_receipt_preserve_ledgers(studio, ctx, tmp_path, mode):
    runtime, store, clock, model, renderer, _ = studio
    await finished(studio)
    model.share = True
    rpc = UploadRPC()
    ctx.sdk._rpc_call = runner_bridge(rpc)
    journal = Deliveries(tmp_path / 'synthetic-delivery.sqlite3')
    assert journal.begin('historical-unknown')
    journal.finish('historical-unknown', 'send_unknown')
    runtime.sender = HostSender(ctx, journal, store=store, space_id='s', scope=SCOPE,
                                timeout_seconds=30 if mode == 'timeout' else 600 if mode == 'outer-timeout' else 240)
    # Model/control timeout must not cancel a longer send operation.
    runtime.timeout = 1
    runtime.send_timeout_seconds = .01 if mode == 'outer-timeout' else 240
    task = asyncio.create_task(step(studio))
    try:
        await asyncio.wait_for(rpc.entered.wait(), 3)
        if mode == 'slow-success':
            await asyncio.sleep(1.57)  # 157 seconds of simulated QQ upload.
            assert not task.done()
            rpc.complete()
            assert (await task)['state'] == 'shared'
        elif mode in {'timeout', 'outer-timeout'}:
            assert (await task)['state'] == 'share_unknown'
            rpc.complete()
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            rpc.complete()
        await asyncio.sleep(0)
        operations = store.audit_operations('s', SCOPE)
        share = next(op for op in operations if op['action'] == 'share')
        expected = 'succeeded' if mode == 'slow-success' else 'unknown'
        assert share['state'] == expected
        usage = store.budget_usage('s', SCOPE)
        assert usage['share']['held'] == (0 if mode == 'slow-success' else 1)
        rows = dict(journal.db.execute('SELECT job,state FROM delivery').fetchall())
        assert rows.pop('historical-unknown') == 'send_unknown'
        assert list(rows.values()) == ['sent' if mode == 'slow-success' else 'send_unknown']
        if mode != 'slow-success':
            assert journal.begin(next(iter(rows)), manual=True) is False
        clock.advance(300)
        await runtime.tick()
        assert len(rpc.calls) == 1 and len(renderer.calls) == 1
        assert next(op for op in store.audit_operations('s', SCOPE) if op['action'] == 'share') == share
        assert store.budget_usage('s', SCOPE)['share'] == usage['share']
        assert rpc.calls[0]['timeout_ms'] == (30000 if mode == 'timeout' else 600000 if mode == 'outer-timeout' else 240000)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        journal.close()


@pytest.mark.parametrize('value', [0, -1, True, float('inf'), float('nan'), 601, '240'])
async def test_bad_timeout_rejected_before_host_call(ctx, value):
    with pytest.raises((StoreError, ValueError)):
        await send_image_receipt(ctx, 'synthetic', 'synthetic', timeout_seconds=value)
    assert not ctx.calls


def test_legacy_config_gets_send_default_without_changing_other_limits():
    old = Config().model_dump()
    old['connection'].pop('send_timeout_seconds')
    old['autonomy']['timeout_seconds'] = 150.0
    migrated = Config.model_validate(old)
    assert migrated.connection.send_timeout_seconds == 240.0
    assert migrated.autonomy.timeout_seconds == 150.0
    assert migrated.autonomy.enabled is False
    assert migrated.autonomy.global_limits.model_dump() == old['autonomy']['global_limits']


@pytest.mark.parametrize('value', [True, False, float('inf'), float('nan'), 0, 601])
def test_config_rejects_invalid_send_timeout(value):
    with pytest.raises(ValueError):
        Config.model_validate({'connection': {'send_timeout_seconds': value}})


@pytest.mark.parametrize('mode', ['command', 'watch'])
async def test_manual_and_watch_entry_keep_slow_send_alive(tmp_path, mode):
    from conftest import SCOPE as command_scope
    from protocol_fixture import PNG
    from test_mcp_plugin import plugin_instance
    from maibot_sdk.components import collect_components

    plugin, sender = await plugin_instance(tmp_path, 'http://127.0.0.1:9/mcp')
    job = 'img_' + 'a' * 32
    calls = []

    async def image_rpc(method, *, payload, timeout_ms):
        assert timeout_ms == 240000
        calls.append(payload)
        await asyncio.sleep(1.57)
        return {'sent': True}

    async def bridge_call(action, *args):
        assert action in {'status', 'result'}
        return {'artifacts': [{}]} if action == 'status' else base64.b64encode(PNG).decode('ascii')

    plugin.ctx.call_host_method = image_rpc
    plugin.bridge = SimpleNamespace(call=bridge_call)
    plugin.config.connection.watch_seconds = 1.0
    try:
        if mode == 'command':
            component = next(item for item in collect_components(plugin) if item['name'] == 'codex_image_command')
            assert component['metadata']['timeout_ms'] == 720000
            result = await asyncio.wait_for(plugin.command(
                **command_scope, message_id='synthetic-command',
                matched_groups={'action': '重发画图', 'arg': job},
            ), component['metadata']['timeout_ms'] / 100000)
            assert result[0] is True
        else:
            await plugin._watch(command_scope, job)
        assert len(calls) == 1
        assert plugin.journal.db.execute('SELECT state FROM delivery WHERE job=?', (job,)).fetchone()[0] == 'sent'
    finally:
        await plugin.on_unload()
