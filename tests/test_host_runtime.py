"""Synthetic timing tests through real SDK and the public Host RPC bridge.

No Host imports, IPC, network or real models. Temporary ledgers are synthetic.
"""

from copy import deepcopy
from types import SimpleNamespace
import asyncio
import json
import math

from pydantic import ValidationError
import pytest

from image_plugin_under_test import CodexImagePlugin
from image_plugin_under_test.host import host_runtime
from image_plugin_under_test.host.host_adapters import HostThinking
from image_plugin_under_test.creation.thinking import DEFAULT_CREATIVE_DIRECTION, Thinking
from support.artist_fakes import SCOPE
from support.artist_flow import step, studio as studio
from support.sdk_context import WAIT, ctx as ctx
from support.host_rpc import runner_bridge




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


@pytest.mark.external_contract
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


@pytest.mark.external_contract
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
@pytest.mark.external_contract
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


@pytest.fixture
async def wired_host(tmp_path, monkeypatch, request):
    timeout, direction = getattr(request, 'param', (None, None))
    worker_started = asyncio.Event()
    model_calls = []

    async def gated_worker(self, space_id):
        worker_started.set()
        await asyncio.Future()

    async def host_method(method, *, payload, timeout_ms=None):
        assert method == 'cap.call'
        if payload['capability'] == 'llm.generate':
            model_calls.append(payload['args'])
            return {'success': True, 'response': json.dumps(WAIT)}
        assert payload['capability'] == 'chat.get_stream_by_user_id'
        return {'success': True, 'stream': {**json.loads(SCOPE.snapshot()), 'session_id': SCOPE.stream_id,
                                          'is_group_session': False}}

    monkeypatch.setattr(host_runtime.HostRuntime, '_serve', gated_worker)
    plugin = CodexImagePlugin()
    budget = {'think': 1, 'max_inflight': 1}
    autonomy = {'enabled': True, 'global_limits': budget,
                'spaces': [{'space_id': 's', **json.loads(SCOPE.snapshot()), 'continuous': True,
                            'consent_ref': 'synthetic-consent', 'limits': budget}]}
    if timeout is not None:
        autonomy['timeout_seconds'] = timeout
    if direction is not None:
        autonomy['creative_direction'] = direction
    config = {'plugin': plugin.get_default_config()['plugin'],
              'connection': {'url': 'http://127.0.0.1:9/mcp', 'secret': 's' * 32,
                             'send_timeout_seconds': 217.5,
                             'allowed_users': [SCOPE.platform + ':' + SCOPE.user_id]},
              'autonomy': autonomy}
    plugin.set_plugin_config(config)
    plugin._set_context(SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path), call_host_method=host_method))
    try:
        await plugin.on_load()
        assert not plugin.startup_error, plugin.startup_error
        await asyncio.wait_for(worker_started.wait(), 1)
        yield plugin, config, model_calls
    finally:
        await plugin.on_unload()


@pytest.mark.parametrize('wired_host', [
    (None, None), (150.1234, '麦麦正在做什么？\n画清动作、表情和手部。'),
], indirect=True, ids=['defaults', 'custom-multiline'])
async def test_runtime_wires_timeout_and_creative_direction_to_artist_and_model(wired_host):
    plugin, config, model_calls = wired_host
    artist = plugin.artist_runtime.artists['s']
    adapter = artist.thinking.generate
    autonomy = config['autonomy']
    expected_timeout = autonomy.get('timeout_seconds', 30)
    assert artist.timeout == expected_timeout and adapter.timeout_ms == math.ceil(expected_timeout * 1000)
    assert artist.send_timeout_seconds == 217.5 and artist.sender.timeout_seconds == 217.5
    assert adapter.max_tokens is None
    result = await artist.tick()
    assert result['state'] == 'waiting', result
    (call,) = model_calls
    prompt = call['prompt']
    sent_direction = json.loads(prompt[1]['content'][0]['text'])['creative_direction']
    assert sent_direction == autonomy.get('creative_direction', DEFAULT_CREATIVE_DIRECTION)
    assert '角色和具体动作' in prompt[0]['content']
    if 'creative_direction' not in autonomy:
        assert '高品质' in sent_direction and '二次元角色插画' in sent_direction
        assert '麦麦（MaiBot）这个角色正在做什么' in sent_direction
        assert '叙事和场景的复杂度让位于人物美感与画质' in sent_direction


async def test_rejected_whitespace_direction_does_not_interrupt_running_host(wired_host):
    plugin, config, model_calls = wired_host
    original_config, generation, bridge = plugin.config, plugin._generation, plugin.bridge
    runtime = plugin.artist_runtime
    worker = runtime.tasks['s']
    assert generation is not None and bridge is not None

    for direction in (' ', '\t', '\n', '\u3000', '\x1c'):
        candidate = deepcopy(config)
        candidate['autonomy']['creative_direction'] = direction
        with pytest.raises(ValidationError, match='creative_direction'):
            plugin.set_plugin_config(candidate)
        assert plugin.config is original_config and plugin._generation is generation and plugin.bridge is bridge
        assert plugin.artist_runtime is runtime and runtime.tasks['s'] is worker
        assert not runtime.stopping and not worker.done() and not worker.cancelling()

    assert (await runtime.artists['s'].tick())['state'] == 'waiting'
    (call,) = model_calls
    context = json.loads(call['prompt'][1]['content'][0]['text'])
    assert context['creative_direction'] == DEFAULT_CREATIVE_DIRECTION
