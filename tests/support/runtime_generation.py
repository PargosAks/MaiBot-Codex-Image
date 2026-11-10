"""命令与发送回归共享的模拟配置、Bridge 和插件构造器。"""

from types import SimpleNamespace

import image_plugin_under_test as plugin_module

from .settings import SECRET


OWNER = {"platform": "qq", "stream_id": "chat", "user_id": "owner"}
NEW_OWNER = {**OWNER, "user_id": "new-owner"}
JOB = "img_" + "a" * 32


def config(user, port):
    return {
        "plugin": {"config_version": "0.12.0"},
        "connection": {"url": f"http://127.0.0.1:{port}/mcp", "secret": SECRET,
                       "allowed_users": [f"qq:{user}"]},
    }


def command(scope=OWNER, action="画图诊断", arg=""):
    return dict(scope, message_id="host-message", matched_groups={"action": action, "arg": arg})


class SimulatedBridge:
    def __init__(self, url, secret):
        self.url = url
        self.calls = []

    async def call(self, operation, scope, args):
        self.calls.append((operation, scope))
        assert operation in {"health", "result"}
        return "synthetic image" if operation == "result" else {}


async def text_reply(*args):
    return True


def plugin_at(tmp_path, monkeypatch, **context):
    monkeypatch.setattr(plugin_module, "Bridge", SimulatedBridge)
    plugin = plugin_module.CodexImagePlugin()
    plugin.set_plugin_config(config("owner", 1111))
    plugin._set_context(SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path),
                                        send=SimpleNamespace(text=text_reply), **context))
    return plugin
