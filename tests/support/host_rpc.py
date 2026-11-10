"""从公开源码提取 RPC 桥接，并模拟调用方超时后仍在进行的上传。"""

from pathlib import Path
from types import SimpleNamespace
import ast
import asyncio
import os

import pytest


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


class UploadRPC:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.reply = asyncio.get_running_loop().create_future()

    async def send_request(self, **kwargs):
        self.calls.append(kwargs)
        self.entered.set()
        # 调用方停止等待后，独立的 QQ 上传仍继续；shield 保留迟到回执。
        return await asyncio.wait_for(asyncio.shield(self.reply), kwargs['timeout_ms'] / 100000)

    def complete(self):
        self.reply.set_result(SimpleNamespace(error=None, payload={'result': {'sent': True}}))
