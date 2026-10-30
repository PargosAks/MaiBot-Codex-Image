"""Plugin user authorization runs before any call to the host Bridge."""

from types import SimpleNamespace

import pytest

from image_plugin_under_test import CodexImagePlugin, Config
from conftest import SECRET, SCOPE


class BridgeSpy:
    def __init__(self):
        self.calls = []

    async def call(self, operation, scope, args):
        self.calls.append((operation, scope, args))
        return {"status": "ready"}


class Sender:
    def __init__(self):
        self.texts = []

    async def text(self, text, stream):
        self.texts.append((text, stream))
        return True


@pytest.mark.parametrize("allowed_users", [[], ["qq:someone-else"]])
@pytest.mark.parametrize("action", ["画图", "画图状态", "重发画图", "画图诊断"])
async def test_unauthorized_plugin_commands_never_call_bridge(allowed_users, action):
    plugin = CodexImagePlugin()
    config = Config().model_dump()
    config["connection"].update(secret=SECRET, allowed_users=allowed_users)
    plugin.set_plugin_config(config)
    sender, bridge = Sender(), BridgeSpy()
    plugin._set_context(SimpleNamespace(send=sender))
    # An already available bridge must not bypass the command's authorization gate.
    plugin.bridge = bridge
    result = await plugin.command(
        **SCOPE,
        message_id="host-message",
        matched_groups={"action": action, "arg": "img_" + "a" * 32},
    )
    assert result[0] is False and "未获绘图授权" in result[1]
    assert bridge.calls == []
    assert plugin.watchers == {}
    assert sender.texts == [(result[1], SCOPE["stream_id"])]


async def test_authorized_plugin_diagnostic_calls_bridge_with_trusted_scope():
    plugin = CodexImagePlugin()
    config = Config().model_dump()
    config["connection"].update(secret=SECRET, allowed_users=["qq:123"])
    plugin.set_plugin_config(config)
    sender, bridge = Sender(), BridgeSpy()
    plugin._set_context(SimpleNamespace(send=sender))
    plugin.bridge = bridge
    result = await plugin.command(
        **SCOPE,
        message_id="host-message",
        matched_groups={"action": "画图诊断", "arg": ""},
    )
    assert result[0] is True
    assert bridge.calls == [("health", SCOPE, {})]
