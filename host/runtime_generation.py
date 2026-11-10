"""配置、客户端和发送记录属于同一代；清理前等本代的使用者退出。"""

# ruff: noqa: I001 -- 继承上级 AGENTS.md 的 from-import 在前约定。
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
import asyncio

from ..domain.delivery import Deliveries
from ..mcp_client.bridge import Bridge

if TYPE_CHECKING:
    from .. import CodexImagePlugin, Config


@dataclass(eq=False)
class RuntimeUse:
    owner: asyncio.Task
    done: asyncio.Future[None]
    submitting: bool = False


class RuntimeGeneration:
    def __init__(self, config: "Config", epoch: int, *, ready: bool = False):
        self.config = config
        self.epoch = epoch
        self.ready = ready
        self.bridge: Bridge | None = None
        self.journal: Deliveries | None = None
        self._owners: set[RuntimeUse] = set()

    @contextmanager
    def use(self) -> Iterator[RuntimeUse]:
        owner = asyncio.current_task()
        assert owner is not None
        operation = RuntimeUse(owner, asyncio.get_running_loop().create_future())
        self._owners.add(operation)
        try:
            yield operation
        finally:
            self._owners.remove(operation)
            operation.done.set_result(None)

    def cancel_users(self) -> None:
        # 已派发的 submit 只等原任务回执，不允许换代把已确认的任务丢给用户猜测。
        protected = {operation.owner for operation in self._owners if operation.submitting}
        for owner in {operation.owner for operation in self._owners} - protected:
            if owner is not asyncio.current_task() and not owner.cancelling():
                owner.cancel()

    @property
    def has_submissions(self) -> bool:
        return any(operation.submitting for operation in self._owners)

    async def wait_submissions(self) -> None:
        pending = [operation.done for operation in self._owners if operation.submitting]
        if pending:
            await asyncio.gather(*(asyncio.shield(done) for done in pending))

    async def drain(self) -> None:
        # 等到具体操作退出，而不是等待调用者的整个任务结束。
        await asyncio.gather(*(operation.done for operation in self._owners))


class GenerationHost:
    """HostRuntime 启动过程中也只能读取同代配置和依赖。"""

    def __init__(self, plugin: "CodexImagePlugin", generation: RuntimeGeneration):
        self._plugin = plugin
        self.config = generation.config
        self.ctx = plugin.ctx
        self.bridge = generation.bridge
        self.journal = generation.journal
        self._artist_renderer = plugin._artist_renderer

    @property
    def artist_runtime(self):
        # 群收藏用当前实例做身份复核；旧代配置固定，但当前实例不能跟着固定。
        return self._plugin.artist_runtime

    @property
    def group_thought_runtime(self):
        return self._plugin.group_thought_runtime
