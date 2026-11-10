"""共享异步收尾规则：取消等待者不能让后台 SQLite 写入脱离所有者。"""

from typing import Any, Awaitable, Callable, TypeVar
import asyncio


Result = TypeVar("Result")


async def join_task(awaitable: Awaitable[Result]) -> Result:
    """等待底层操作真正结束，再传播取消；重复取消也不会提前释放资源。"""
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        # 线程无法被 asyncio 取消。必须观察已提交写入的结果，才能关闭库或释放锁。
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            task.result()
        except Exception as exc:
            raise cancelled from exc
        raise


async def join_worker(function: Callable[..., Result], *args: Any, **kwargs: Any) -> Result:
    """把同步存储/文件工作送入线程，沿用统一的取消与异常收尾规则。"""
    return await join_task(asyncio.to_thread(function, *args, **kwargs))
