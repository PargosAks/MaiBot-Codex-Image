"""手动取图和自主分享共用的发送意图及回执边界。"""

# ruff: noqa: I001 -- 继承上级 AGENTS.md 的 from-import 在前约定。
from collections.abc import Awaitable, Callable
import asyncio

from ..domain.delivery import Deliveries
from ..domain.policy import StoreError
from .host_runtime import joined


class DeliveryBlocked(StoreError):
    """未获得发送许可，不能收尾或修改原意图。"""

    def __init__(self):
        super().__init__("SEND_UNKNOWN", "Original send is in progress or its outcome is unknown")


def capability_denied(exc: BaseException) -> bool:
    # Host 在调用发送处理器之前拒绝能力；只有这个错误能证明未派发。
    return getattr(exc, "code", None) == "E_CAPABILITY_DENIED"


async def dispatch_delivery(
    journal: Deliveries, job: str, *, manual: bool = False,
    before_dispatch: Callable[[], Awaitable[None]],
    dispatch: Callable[[], Awaitable[bool]],
) -> bool:
    """在已有图片与目标上申请一次许可，由调用方提供授权复核和 Host 回执。"""
    acquired = False
    state = "send_failed"

    def claim() -> None:
        nonlocal acquired
        # 取消等待不等于 SQLite 停止。在线程内记录所有权，供取消后的收尾使用。
        acquired = journal.begin(job, manual)

    try:
        await joined(asyncio.to_thread(claim))
        if not acquired:
            raise DeliveryBlocked()
        await before_dispatch()
        # 从这一行起不能假定 Host 未发送；无效回执、异常和取消都保持未知。
        state = "send_unknown"
        try:
            sent = await dispatch()
        except Exception as exc:
            if capability_denied(exc):
                state = "send_failed"
            raise
        if sent is not True:
            raise StoreError("SEND_UNKNOWN", "Host did not confirm the send outcome")
        state = "sent"
        return True
    finally:
        if acquired:
            await joined(asyncio.to_thread(journal.finish, job, state))
