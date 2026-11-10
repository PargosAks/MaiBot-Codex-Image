"""Host 能力适配器的真实 SDK 上下文和 RPC 替身。"""

from pathlib import Path
import importlib.util
import json
import os
import sys

from maibot_sdk.context import PluginContext
import pytest


WAIT = {"ideas": [], "decision": {"action": "wait", "reason": "暂时收藏"}}


class Context:
    def __init__(self, context_class=PluginContext):
        self.sdk = context_class("isolated-adapter-test", rpc_call=self._rpc)
        self.llm, self.send = self.sdk.llm, self.sdk.send
        self.calls = []
        self.rpc_timeouts = []
        self.result = {"success": True, "response": json.dumps(WAIT)}
        self.failure = None
        self.handler = None

    async def call_capability(self, capability, **payload):
        self.calls.append((capability, payload))
        if self.handler:
            return await self.handler(capability, payload)
        if self.failure:
            raise self.failure
        return self.result

    async def call_host_method(self, method, *, payload, timeout_ms=None):
        return await self.sdk.call_host_method(method, payload=payload, timeout_ms=timeout_ms)

    async def _rpc(self, method, plugin_id, payload, timeout_ms=None):
        assert method == "cap.call" and payload["capability"] in {"send.image", "llm.generate"}
        assert set(payload) == {"capability", "args"}
        self.rpc_timeouts.append(timeout_ms)
        return await self.call_capability(payload["capability"], **payload["args"])


@pytest.fixture(params=["2.16", pytest.param("2.17", marks=pytest.mark.external_contract)])
def ctx(request):
    if request.param == "2.16":
        return Context()
    source = os.environ.get("CODEX_IMAGE_TEST_SDK_SOURCE")
    if not source:
        pytest.skip("Set CODEX_IMAGE_TEST_SDK_SOURCE to opt in to public SDK source tests")
    # SDK 2.17 使用绝对导入。只在取公开类时临时切换模块和路径，异步测试前恢复。
    # 返回的类仍保留原模块全局变量与真实 RPC 方法。
    previous = {key: value for key, value in sys.modules.items()
                if key == "maibot_sdk" or key.startswith("maibot_sdk.")}
    paths = list(sys.path)
    try:
        for key in previous:
            del sys.modules[key]
        sys.path.insert(0, str(Path(source)))
        context_class = importlib.import_module("maibot_sdk.context").PluginContext
    finally:
        for key in list(sys.modules):
            if key == "maibot_sdk" or key.startswith("maibot_sdk."):
                del sys.modules[key]
        sys.modules.update(previous)
        sys.path[:] = paths
    return Context(context_class)
