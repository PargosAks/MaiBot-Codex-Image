"""自主分享的业务许可和操作结算；Host 投递确认由 Sender 负责。"""

import asyncio

from ..domain.policy import Scope, StoreError
from .contracts import AsyncCall, Sender, stable_id


class ShareService:
    def __init__(self, *, db: AsyncCall, version: AsyncCall, sender: Sender, scope: Scope, timeout: float):
        # 依赖只暴露本用例所需能力，不把整个 Artist 传入组件。
        self.db = db
        self.version = version
        self.sender = sender
        self.scope = scope
        self.timeout = timeout

    async def share(self, work: dict, cycle: dict) -> dict:
        if work["status"] != "finished" or not work["best_version"]:
            raise StoreError("INVALID_DECISION", "Only finished reviewed work can be shared")
        if not await self.db(
            "version_accepted", work["best_version"], expected_auth_version=cycle["auth_version"],
        ):
            raise StoreError("REVIEW_REQUIRED", "Autonomous sharing requires explicit visual acceptance")
        version = await self.version(work["best_version"])
        request_id = stable_id("share", work["work_id"], version["version_id"])
        operation = await self.db(
            "reserve",
            request_id=request_id,
            action="share",
            work_id=work["work_id"],
            params={"job_id": version["job_id"], "version_id": version["version_id"]},
            expected_auth_version=cycle["auth_version"],
        )
        if operation["state"] != "reserved":
            return {"state": "share_recorded", "outcome": operation["state"]}
        await self.db("mark_submitting", request_id)
        try:
            # Let the Host RPC wait expire first, leaving time to persist its
            # uncertain receipt before the Artist's outer cancellation runs.
            result = await asyncio.wait_for(
                self.sender.send(scope=self.scope, version=version),
                self.timeout,
            )
        except BaseException as exc:
            await self.db("mark_unknown", request_id, reason="SEND_UNCONFIRMED")
            if isinstance(exc, asyncio.CancelledError):
                raise
            return {"state": "share_unknown"}
        if type(result) is not bool:
            await self.db("mark_unknown", request_id, reason="SEND_RECEIPT_INVALID")
            return {"state": "share_unknown"}
        await self.db(
            "settle",
            request_id,
            outcome="succeeded" if result else "failed",
            evidence="Sender confirmed receipt",
        )
        return {"state": "shared" if result else "share_failed"}
