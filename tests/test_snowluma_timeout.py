"""Opt-in tests of public VPS source snapshots, using only synthetic transport.

QQTransportClient methods and Host timeout resolver are extracted with AST;
gateway RPC, websocket and QQ upload are synthetic. No service imports/startup.
"""

import ast
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from test_send_timeout import UploadRPC
from test_host_runtime import runner_bridge
from test_host_adapters import ctx as sdk_context_fixture
from image_plugin_under_test.host.host_adapters import send_image_receipt
from image_plugin_under_test.domain.policy import StoreError

ctx = sdk_context_fixture


@pytest.fixture
def public_transport():
    source = os.environ.get('CODEX_IMAGE_TEST_SNOWLUMA_SOURCE')
    if not source:
        pytest.skip('Set CODEX_IMAGE_TEST_SNOWLUMA_SOURCE to public source snapshots only')
    root = Path(source)
    tree = ast.parse((root / 'adapter_transport.py').read_text('utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'QQTransportClient')
    methods = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name in {'call_action', '_resolve_pending_action'}]
    assert len(methods) == 2
    # Scale only the adapter's wait_for; leave its future ownership/cleanup intact.
    async def scaled_wait(awaitable, timeout):
        return await asyncio.wait_for(awaitable, timeout / 100)
    namespace = {'asyncio': SimpleNamespace(get_running_loop=asyncio.get_running_loop, wait_for=scaled_wait),
                 'uuid4': uuid4, 'json': json}
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *methods], type_ignores=[])),
                 str(root / 'adapter_transport.py'), 'exec'), namespace)
    resolver = {}
    exec(compile((root / 'host_component_timeout.py').read_text('utf-8'),
                 str(root / 'host_component_timeout.py'), 'exec'), resolver)
    return namespace, resolver['resolve_component_rpc_timeout_ms']


@pytest.mark.parametrize('action_seconds,gateway_ms,expected', [
    (30, 0, 'adapter-timeout'),
    (210, 0, 'gateway-timeout'),
    (210, 230000, 'success'),
])
async def test_all_layers_must_outlast_upload(public_transport, ctx, action_seconds, gateway_ms, expected):
    methods, resolve_timeout = public_transport
    uploads, responses = [], []
    client = SimpleNamespace(_pending_actions={}, _send_lock=asyncio.Lock(),
                             _server_config=SimpleNamespace(action_timeout_sec=action_seconds))
    done = asyncio.Event()

    async def send_str(payload):
        request = json.loads(payload)
        uploads.append(request)

        async def finish_upload():
            await asyncio.sleep(1.57)
            methods['_resolve_pending_action'](client, request['echo'], {'status': 'ok', 'message_id': 'fake'})
            done.set()

        responses.append(asyncio.create_task(finish_upload()))

    client._ws = SimpleNamespace(closed=False, send_str=send_str)
    rpc = UploadRPC()
    ctx.sdk._rpc_call = runner_bridge(rpc)

    async def gateway():
        await methods['call_action'](client, 'send_msg', {'image': 'synthetic'})
        return {'sent': True}

    async def host():
        await rpc.entered.wait()
        try:
            answer = await asyncio.wait_for(gateway(), resolve_timeout(gateway_ms) / 100000)
        except TimeoutError:
            # Host normalizes a failed gateway call into an unconfirmed send.
            answer = {'sent': False}
        rpc.reply.set_result(SimpleNamespace(error=None, payload={'result': answer}))

    host_task = asyncio.create_task(host())
    try:
        if expected == 'success':
            assert await send_image_receipt(ctx, 'synthetic', 'synthetic') is True
            assert done.is_set()
        else:
            with pytest.raises(StoreError, check=lambda exc: exc.code == 'SEND_UNKNOWN'):
                await send_image_receipt(ctx, 'synthetic', 'synthetic')
            assert not done.is_set()
        await asyncio.gather(*responses)
        await host_task
        assert len(uploads) == 1 and not client._pending_actions
        assert len(rpc.calls) == 1 and rpc.calls[0]['timeout_ms'] == 240000
        assert resolve_timeout(0) == 60000 and resolve_timeout(230000) == 230000
    finally:
        for task in [host_task, *responses]:
            if not task.done():
                task.cancel()
        await asyncio.gather(host_task, *responses, return_exceptions=True)
