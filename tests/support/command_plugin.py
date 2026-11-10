"""命令链路使用的独立插件实例和 Host 发送替身。"""

from types import SimpleNamespace

from image_plugin_under_test import CodexImagePlugin
from .settings import SECRET


class Sender:
    def __init__(self, fail=False):
        self.images = []
        self.texts = []
        self.fail = fail

    async def text(self, text, stream):
        self.texts.append((text, stream))
        return True

    async def image(self, image, stream, **kwargs):
        self.images.append((image, stream))
        if isinstance(self.fail, BaseException):
            raise self.fail
        if self.fail:
            raise ConnectionError("host response lost")
        return {"sent": True, "message_id": "sent-message"}


async def plugin_instance(tmp_path, url, fail=False):
    plugin = CodexImagePlugin()
    plugin.set_plugin_config(
        {
            "plugin": {"config_version": "0.1.0"},
            "connection": {
                "url": url,
                "secret": SECRET,
                "allowed_users": ["qq:123"],
                "poll_seconds": 0.1,
                "watch_seconds": 10,
            },
        }
    )
    sender = Sender(fail)

    async def raw_capability(method, *, payload, **kwargs):
        assert method == "cap.call" and payload["capability"] == "send.image"
        args = payload["args"]
        return await sender.image(args["image_base64"], args["stream_id"], return_details=args["return_details"])

    plugin._set_context(SimpleNamespace(send=sender, call_host_method=raw_capability,
                                       paths=SimpleNamespace(data_dir=tmp_path)))
    await plugin.on_load()
    return plugin, sender
