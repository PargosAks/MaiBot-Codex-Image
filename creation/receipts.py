"""任务回执先落账，再归档图片，最后推进作品状态。"""

import asyncio
import hashlib
import re

from ..domain.policy import Scope, StoreError
from ..domain.errors import BackendError
from .contracts import AsyncCall, ReceiptRenderer, Renderer, RenderResult, render_digest, stable_id


class ReceiptService:
    def __init__(self, *, db: AsyncCall, change: AsyncCall, renderer: Renderer, scope: Scope, timeout: float):
        # 依赖只暴露本用例所需能力，不把整个 Artist 传入组件。
        self.db = db
        self.change = change
        self.renderer = renderer
        self.scope = scope
        self.timeout = timeout

    async def receive(self, work: dict, result: RenderResult) -> dict:
        request_id = work["pending_request"]
        operation = await self.db("get_operation", request_id)
        valid = (
            isinstance(result, RenderResult)
            and result.request_id == request_id
            and result.request_digest == render_digest(operation["action"], operation["params"])
            and isinstance(result.job_id, str)
            and bool(result.job_id.strip())
            and (not operation["job_id"] or operation["job_id"] == result.job_id)
            and result.state in {"queued", "running", "completed", "failed", "outcome_unknown"}
            and (
                result.error_code is None
                or (
                    isinstance(result.error_code, str)
                    and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", result.error_code)
                )
            )
        )
        if not valid:
            if operation["state"] in {"submitting", "accepted", "unknown"}:
                await self.db("mark_unknown", request_id, reason="RECEIPT_MISMATCH")
            return {"state": "blocked", "reason": "RECEIPT_MISMATCH"}
        if (operation["state"] == "succeeded" and result.state != "completed") or (
            operation["state"] == "failed" and result.state != "failed"
        ):
            return {"state": "blocked", "reason": "RECEIPT_MISMATCH"}
        if operation["state"] == "unknown" and (
            result.state != "outcome_unknown" or not operation["job_id"]
        ):
            await self.db(
                "reconcile",
                request_id,
                outcome="accepted",
                evidence="Matching read-only receipt",
                job_id=result.job_id,
            )
        elif operation["state"] == "submitting":
            await self.db("mark_accepted", request_id, job_id=result.job_id)
        if result.state in {"completed", "failed"}:
            if operation["state"] not in {"succeeded", "failed"}:
                await self.db(
                    "settle",
                    request_id,
                    outcome="succeeded" if result.state == "completed" else "failed",
                    evidence="Confirmed render terminal",
                    job_id=result.job_id,
                )
        elif result.state == "outcome_unknown":
            await self.db("mark_unknown", request_id, reason="RENDER_OUTCOME_UNKNOWN")
        if result.artifact is not None and result.image is None:
            # 终态已提交；下载失败或取消只影响归档，不能退回 unknown 或重新绘图。
            if not isinstance(self.renderer, ReceiptRenderer):
                return {"state": "blocked", "reason": "ARTIFACT_UNSUPPORTED"}
            try:
                downloaded = await asyncio.wait_for(
                    self.renderer.fetch_artifact(scope=self.scope, receipt=result), self.timeout,
                )
                # 两阶段接口也不能让下载器把另一任务或另一张图混入已确认回执。
                if not isinstance(downloaded, RenderResult) or (
                    downloaded.request_id, downloaded.request_digest, downloaded.job_id, downloaded.state,
                    downloaded.error_code, downloaded.sha256, downloaded.mime,
                ) != (
                    result.request_id, result.request_digest, result.job_id, result.state,
                    result.error_code, result.artifact.sha256, result.artifact.mime,
                ) or not isinstance(downloaded.image, bytes) or len(downloaded.image) != result.artifact.size_bytes:
                    return {"state": "blocked", "reason": "ARTIFACT_INVALID"}
                result = downloaded
            except asyncio.CancelledError:
                raise
            except (BackendError, StoreError) as exc:
                return {"state": "blocked", "reason": exc.code}
            except Exception:
                return {"state": "blocked", "reason": "ARTIFACT_UNAVAILABLE"}
        if result.image is not None:
            if (
                not isinstance(result.image, bytes)
                or not 0 < len(result.image) <= 8 * 1024 * 1024
                or result.mime not in {"image/png", "image/jpeg", "image/webp"}
                or hashlib.sha256(result.image).hexdigest() != result.sha256
            ):
                return {"state": "blocked", "reason": "ARTIFACT_INVALID"}
            version_id = stable_id("version", work["work_id"], request_id)
            await self.db(
                "record_version",
                work_id=work["work_id"],
                version_id=version_id,
                request_id=request_id,
                job_id=result.job_id,
                data=result.image,
                mime=result.mime,
                parent_id=work["parent_version"],
            )
            if work["status"] in {"finished", "abandoned"}:
                if result.state == "outcome_unknown":
                    # Keep the original hold and use persisted failure backoff;
                    # an available image does not confirm its backend turn.
                    return {"state": "blocked", "reason": "RENDER_OUTCOME_UNKNOWN"}
                return {"state": work["status"], "reason": "reconciled"}
            await self.change(
                work["work_id"],
                status="review_pending",
                reason="",
                best_version=work["best_version"] or version_id,
            )
            return {"state": "review_pending", "version_id": version_id}
        if result.state == "failed":
            if work["status"] in {"finished", "abandoned"}:
                return {"state": work["status"], "reason": "reconciled"}
            await self.change(
                work["work_id"],
                status="finished" if work["best_version"] else "abandoned",
                reason=result.error_code or "RENDER_FAILED",
            )
            return {"state": "finished" if work["best_version"] else "abandoned"}
        if result.state == "outcome_unknown":
            # 是否已有图片不改变未知回合的退避规则。
            return {"state": "blocked", "reason": "RENDER_OUTCOME_UNKNOWN"}
        return {
            "state": "creating",
            "reason": "ARTIFACT_MISSING" if result.state == "completed" else result.state,
        }
