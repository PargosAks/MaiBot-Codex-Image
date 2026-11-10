"""外部绘图边界的结构化错误；错误本身不授予重试或退款权限。"""

class BackendError(ValueError):
    """Machine-readable failure, never permission to retry or refund a request.

    Remote text is deliberately not copied into the user-facing message. Even
    an explicit rejection describes this call, not the fate of an earlier call.
    """

    def __init__(
        self, code: str, *, kind: str, message: str = "绘图服务拒绝请求或响应无法确认，请核对原任务。"
    ):
        self.code, self.kind = code, kind
        super().__init__(f"{message} [{code}]")
