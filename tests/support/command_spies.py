"""命令授权测试的 Bridge 和发送调用记录器。"""



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
