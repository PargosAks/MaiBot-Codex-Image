"""工作室聊天上下文的 SDK 夹具、输入和能力记录器。"""

from pathlib import Path
from types import SimpleNamespace
import importlib
import importlib.util
import os
import sys

from maibot_sdk.components import collect_components
from maibot_sdk.context import PluginContext
import pytest

from image_plugin_under_test import CodexImagePlugin
from image_plugin_under_test.domain.policy import Scope
from image_plugin_under_test.host import studio_chat as chat_module


SCOPE = Scope("synthetic", "actual-private-session", "delegating-owner")


OTHER = Scope("synthetic", "another-private-session", "another-owner")


@pytest.fixture(params=["2.16", pytest.param("2.17", marks=pytest.mark.external_contract)])
def sdk(request):
    if request.param == "2.16":
        yield SimpleNamespace(chat=chat_module, context=PluginContext, collect=collect_components)
        return
    source = os.environ.get("CODEX_IMAGE_TEST_SDK_SOURCE")
    if not source:
        pytest.skip("Set CODEX_IMAGE_TEST_SDK_SOURCE to the public SDK 2.17 site-packages")
    previous = {key: value for key, value in sys.modules.items()
                if key == "maibot_sdk" or key.startswith("maibot_sdk.")}
    paths = list(sys.path)
    alias = "image_plugin_under_test.host._studio_chat_sdk217"
    try:
        for key in previous:
            del sys.modules[key]
        sys.path.insert(0, source)
        context = importlib.import_module("maibot_sdk.context").PluginContext
        collect = importlib.import_module("maibot_sdk.components").collect_components
        spec = importlib.util.spec_from_file_location(alias, Path(chat_module.__file__))
        module = importlib.util.module_from_spec(spec)
        sys.modules[alias] = module
        spec.loader.exec_module(module)
        yield SimpleNamespace(chat=module, context=context, collect=collect)
    finally:
        for key in list(sys.modules):
            if key == "maibot_sdk" or key.startswith("maibot_sdk."):
                del sys.modules[key]
        sys.modules.update(previous)
        sys.modules.pop(alias, None)
        sys.path[:] = paths


class Harness:
    def __init__(self, sdk, *, planner=True):
        self.module = m = sdk.chat
        self.calls, self.reads = [], []
        self.policy = m.StudioChatPolicy(True, ("synthetic:delegating-owner",), (
            m.StudioChatBinding("studio", SCOPE, continuous=True, consent_ref="explicit-test-delegation"),))
        self.stream = {"session_id": SCOPE.stream_id, "stream_id": SCOPE.stream_id,
                       "user_id": SCOPE.user_id, "platform": SCOPE.platform, "group_id": "",
                       "is_group_session": False, "chat_type": "private"}
        self.response = {"success": True, "stream": self.stream}
        self.snapshot = m.StudioChatSnapshot("studio", SCOPE, 1, True,
                                             {"status": "waiting", "counts": {"works": 0}})
        self.rpc_hook, self.read_hook = None, None
        self.ctx = sdk.context("isolated-studio-query", rpc_call=self.rpc)
        self.chat = m.StudioChat(self.ctx, policy=lambda: self.policy, read=self.read)
        if planner:
            assert self.chat.note_query(SCOPE, "synthetic-msg")

    async def rpc(self, method, plugin_id, payload, timeout_ms=None):
        # Any accidental draw/think/send or other Host capability fails the test.
        assert method == "cap.call" and plugin_id == "isolated-studio-query"
        assert payload == {"capability": "chat.get_stream_by_user_id",
                           "args": {"platform": SCOPE.platform, "user_id": SCOPE.user_id}}
        self.calls.append((method, payload, timeout_ms))
        if self.rpc_hook:
            await self.rpc_hook()
        return self.response

    async def read(self, space_id, scope):
        assert space_id == "studio" and scope == SCOPE
        self.reads.append((space_id, scope))
        if self.read_hook:
            await self.read_hook()
        return self.snapshot


def payload(text="现在在画什么", *, message_id="synthetic-msg", **extra):
    prefix = f'<message msg_id="{chat_module._display_message_id(message_id)}" time="12:34:56" user="owner">\n'
    return {"session_id": SCOPE.stream_id, "item_schema_version": 1,
            "items": [{"item_type": "UserMessageItem", "meta": {},
                       "parts": [{"type": "text", "text": prefix + text if type(text) is str else text}]}],
            "tool_definitions": [], "built_message_count": 1, **extra}



@pytest.fixture
async def active_studio_plugin(tmp_path):
    """通过插件生命周期发布测试运行代，入口仍只会调用测试替身。"""
    plugin = CodexImagePlugin()
    config = plugin.build_default_config()
    config["plugin"]["enabled"] = config["autonomy"]["enabled"] = True
    assert not config["autonomy"]["spaces"] and not config["connection"]["allowed_users"]
    plugin.set_plugin_config(config)

    async def forbidden(*args, **kwargs):
        pytest.fail("Studio 入口测试不应调用 Host")

    plugin._set_context(SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path), call_host_method=forbidden))
    try:
        await plugin.on_load()
        assert not plugin.startup_error and plugin.artist_runtime is not None
        yield plugin
    finally:
        await plugin.on_unload()
