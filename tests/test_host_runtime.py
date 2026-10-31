"""Synthetic timing tests through real SDK and the public Host RPC bridge.

No Host imports, IPC, network or real models. Temporary ledgers are synthetic.
"""

from pathlib import Path
from types import SimpleNamespace
import ast
import asyncio
import json
import math
import os

import pytest

from image_plugin_under_test import CodexImagePlugin
from image_plugin_under_test import host_runtime
from image_plugin_under_test.host_adapters import HostThinking
from image_plugin_under_test.thinking import Thinking
from test_artist_flow import SCOPE, step, studio as studio_fixture
from test_host_adapters import WAIT, ctx as sdk_context_fixture


studio = studio_fixture
ctx = sdk_context_fixture


class PendingRPC:
    """An independently completing Host operation can outlive its RPC waiter."""

    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.reply = asyncio.get_running_loop().create_future()

    async def send_request(self, **kwargs):
        self.calls.append(kwargs)
        self.entered.set()
        return await asyncio.wait_for(asyncio.shield(self.reply), kwargs['timeout_ms'] / 1000)

    def complete(self, answer):
        self.reply.set_result(SimpleNamespace(error=None, payload={'result': {
            'success': True, 'response': json.dumps(answer),
        }}))


def runner_bridge(rpc):
    source = os.environ.get('CODEX_IMAGE_TEST_HOST_SOURCE')
    if not source:
        pytest.skip('Set CODEX_IMAGE_TEST_HOST_SOURCE to read the public Host RPC bridge')
    path = Path(source) / 'plugin_runtime/runner/runner_main.py'
    tree = ast.parse(path.read_text('utf-8'))
    runner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'PluginRunner')
    inject = next(n for n in runner.body if isinstance(n, ast.FunctionDef) and n.name == '_inject_context')
    bridge = next(n for n in inject.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_rpc_call')
    namespace = {'rpc_client': rpc, 'bound_plugin_id': 'isolated-adapter-test',
                 '_PLUGIN_ALLOWED_RAW_HOST_METHODS': frozenset({'cap.call'}),
                 'logger': SimpleNamespace(warning=lambda *args: None)}
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    module = ast.Module(body=[future, bridge], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), namespace)
    return namespace['_rpc_call']


async def test_real_sdk_runner_receives_timeout_and_task_default_tokens(ctx):
    rpc = PendingRPC()
    ctx.sdk._rpc_call = runner_bridge(rpc)
    adapter = HostThinking(ctx, plan_task='utils', review_task='vlm', timeout_seconds=150.1234)
    task = asyncio.create_task(Thinking(adapter).plan({}))
    await asyncio.wait_for(rpc.entered.wait(), 1)
    try:
        call = rpc.calls[0]
        assert call['timeout_ms'] == 150124 and call['plugin_id'] == 'isolated-adapter-test'
        assert call['method'] == 'cap.call' and call['payload']['capability'] == 'llm.generate'
        assert set(call['payload']['args']) == {'prompt', 'task_name'}
        assert call['payload']['args']['task_name'] == 'utils'
    finally:
        rpc.complete(WAIT)
    assert await task == WAIT and len(rpc.calls) == 1


async def test_delayed_receipt_within_explicit_sdk_runner_timeout_succeeds_once(ctx):
    rpc = PendingRPC()
    ctx.sdk._rpc_call = runner_bridge(rpc)
    task = asyncio.create_task(Thinking(HostThinking(
        ctx, plan_task='utils', review_task='vlm', timeout_seconds=.15,
    )).plan({}))
    await asyncio.wait_for(rpc.entered.wait(), 1)
    await asyncio.sleep(.04)
    assert rpc.calls[0]['timeout_ms'] == 150 and not task.done()
    rpc.complete(WAIT)
    assert await task == WAIT and len(rpc.calls) == 1


@pytest.mark.parametrize('mode', ['rpc-timeout', 'cancel'])
async def test_review_late_reply_preserves_unknown_and_never_retries(studio, ctx, mode):
    runtime, store, clock, _, renderer, _ = studio
    assert (await runtime.tick())['state'] == 'planned'
    assert (await step(studio))['state'] == 'review_pending'
    rpc = PendingRPC()
    ctx.sdk._rpc_call = runner_bridge(rpc)
    runtime.thinking = Thinking(HostThinking(ctx, plan_task='utils', review_task='vlm',
                                            timeout_seconds=.01 if mode == 'rpc-timeout' else 1))
    clock.advance()
    task = asyncio.create_task(runtime.tick())
    await asyncio.wait_for(rpc.entered.wait(), 1)
    if mode == 'cancel':
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert await task == {'state': 'blocked', 'reason': 'THINK_UNKNOWN'}
    operation = store.audit_operations('s', SCOPE)[-1]
    assert operation['state'] == 'unknown' and operation['params']['task'] == 'review'
    assert rpc.calls[0]['payload']['args']['task_name'] == 'vlm'
    assert 'max_tokens' not in rpc.calls[0]['payload']['args']
    frozen = json.loads(rpc.calls[0]['payload']['args']['prompt'][1]['content'][0]['text'])
    rpc.complete({'decision': 'accept', 'reason': 'synthetic delayed receipt', 'strengths': [], 'issues': [],
                  'comparison': {'improvements': [], 'regressions': [], 'preserved': [],
                                 'evidence_version_ids': [frozen['current_version']['version_id']]}})
    await asyncio.sleep(0)
    assert store.get_operation('s', SCOPE, operation['request_id']) == operation
    assert store.list_reviews('s', SCOPE, operation['work_id']) == []
    clock.advance(300)
    await runtime.on_chat(message_id='after-late-reply', text='synthetic new inspiration')
    assert (await runtime.run_once())['reason'] == 'THINK_UNKNOWN'
    assert len(rpc.calls) == 1 and store.budget_usage('s', SCOPE)['think']['held'] == 1
    assert len(renderer.calls) == 1


@pytest.mark.parametrize('timeout', [None, 150.1234])
async def test_runtime_wires_configured_timeout_to_artist_and_host_adapter(tmp_path, monkeypatch, timeout):
    gate = asyncio.Event()

    async def gated_worker(self, space_id):
        await gate.wait()

    async def host_method(method, *, payload, timeout_ms=None):
        assert method == 'cap.call' and payload['capability'] == 'chat.get_stream_by_user_id'
        return {'success': True, 'stream': {**json.loads(SCOPE.snapshot()), 'session_id': SCOPE.stream_id,
                                          'is_group_session': False}}

    monkeypatch.setattr(host_runtime.HostRuntime, '_serve', gated_worker)
    plugin = CodexImagePlugin()
    autonomy = {'enabled': True, 'spaces': [{'space_id': 's', **json.loads(SCOPE.snapshot()),
                                          'continuous': True, 'consent_ref': 'synthetic-consent'}]}
    if timeout is not None:
        autonomy['timeout_seconds'] = timeout
    plugin.set_plugin_config({'plugin': plugin.get_default_config()['plugin'],
                              'connection': {'url': 'http://127.0.0.1:9/mcp', 'secret': 's' * 32,
                                            'allowed_users': [SCOPE.platform + ':' + SCOPE.user_id]},
                              'autonomy': autonomy})
    plugin._set_context(SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path), call_host_method=host_method))
    try:
        await plugin.on_load()
        assert not plugin.startup_error
        artist = plugin.artist_runtime.artists['s']
        adapter = artist.thinking.generate
        expected = 30 if timeout is None else timeout
        assert artist.timeout == expected and adapter.timeout_ms == math.ceil(expected * 1000)
        assert adapter.max_tokens is None
    finally:
        await plugin.on_unload()
