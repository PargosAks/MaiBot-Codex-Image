"""评审快照、历史兼容和作品修订；模型调用及事务由显式依赖提供。"""

import hashlib

from ..domain.policy import StoreError, canonical_json
from .contracts import AsyncCall


class ReviewService:
    def __init__(self, *, db: AsyncCall, version: AsyncCall, think: AsyncCall,
                 poll: AsyncCall, epoch: AsyncCall, change: AsyncCall):
        # 依赖只暴露本用例所需能力，不把整个 Artist 传入组件。
        self.db = db
        self.version = version
        self.think = think
        self.poll = poll
        self.epoch = epoch
        self.change = change

    async def review(self, work: dict, context: dict, cycle: dict) -> dict:
        original = await self.db("get_operation", work["pending_request"])
        if original["state"] in {"submitting", "accepted", "unknown"}:
            polled = await self.poll(work)
            if polled.get("state") == "blocked":
                return polled
            original = await self.db("get_operation", work["pending_request"])
        versions = await self.db("list_versions", work["work_id"])
        current = next(v for v in versions if v["request_id"] == work["pending_request"])
        version = await self.version(current["version_id"])
        review_context = {
            # 规划时的方向与权限不覆盖原作品；评审标准已经冻结在该作品的 brief 中。
            **{key: value for key, value in context.items()
               if key not in {"sharing", "single_work", "creative_direction"}},
            "current_work": {k: work[k] for k in ("work_id", "brief", "best_version")},
            "current_version": {k: version[k] for k in ("version_id", "sha256", "mime")},
            "versions": [
                {k: v.get(k) for k in ("version_id", "parent_id", "sha256", "mime")} for v in versions
            ],
        }
        snapshot = await self.db("get_review_snapshot", version["version_id"])
        operations = await self.db("audit_operations")
        # An upgrade must finish the original single-image request, including a
        # reserved call that never dispatched. Unknown calls keep their identity.
        legacy = next((op for op in reversed(operations) if (
            op["action"] == "think" and op["work_id"] == work["work_id"]
            and op["params"].get("task") == "review"
            and op["params"].get("version_id") == version["version_id"]
            and op["params"].get("review_contract", 1) == 1
            and op["state"] in {"reserved", "submitting", "accepted", "unknown", "succeeded"}
        )), None)
        images = None
        if snapshot is None and legacy is not None:
            review_context = legacy["params"]["context"]
        else:
            if snapshot is None:
                inputs = {}
                for role, version_id in (
                    ("current", version["version_id"]),
                    ("parent", version["parent_id"]),
                    ("previous_best", work["best_version"]),
                ):
                    if version_id is None:
                        continue
                    item = await self.version(version_id)
                    if item["work_id"] != work["work_id"]:
                        raise StoreError("ARTIFACT_INVALID", "Review input belongs to another work")
                    if version_id not in inputs:
                        inputs[version_id] = {
                            **{k: item[k] for k in ("version_id", "sha256", "mime")}, "roles": [],
                        }
                    inputs[version_id]["roles"].append(role)
                review_context.update(review_contract=2, review_inputs=list(inputs.values()))
                review_context["versions"] = [
                    row for row in review_context["versions"] if row["version_id"] in inputs
                ]
                snapshot = await self.db(
                    "freeze_review_snapshot", version_id=version["version_id"], context=review_context,
                    expected_auth_version=cycle["auth_version"],
                )
            if snapshot["auth_version"] != cycle["auth_version"]:
                raise StoreError("AUTH_CHANGED", "Frozen review belongs to an older authorization")
            review_context = snapshot["context"]
            if hashlib.sha256(canonical_json(review_context).encode("utf-8")).hexdigest() != snapshot["input_digest"]:
                raise StoreError("ARTIFACT_INVALID", "Review snapshot digest mismatch")
            images = []
            for item in review_context["review_inputs"]:
                picture = await self.version(item["version_id"])
                if (picture["work_id"], picture["sha256"], picture["mime"]) != (
                    work["work_id"], item["sha256"], item["mime"],
                ):
                    raise StoreError("ARTIFACT_INVALID", "Frozen review image changed")
                images.append({**item, "data": picture["data"]})
        reviews = await self.db("list_reviews", work["work_id"])
        prior = next((r for r in reversed(reviews) if r["version_id"] == version["version_id"]), None)
        if prior is not None:
            review = prior["review"]
            request_id = prior["request_id"]
        else:
            try:
                review, request_id = await self.think(
                    "review", review_context, cycle, work_id=work["work_id"], version=version, images=images
                )
            except StoreError as exc:
                if exc.code != "BUDGET_EXCEEDED" or original["state"] not in {"accepted", "unknown"}:
                    raise
                # A render can expose an image before its turn releases the
                # sole inflight slot. Continue querying that same receipt;
                # do not impose model-failure backoff on a healthy render.
                return {
                    "state": "blocked" if original["state"] == "unknown" else "creating",
                    "reason": "RENDER_RECEIPT_PENDING",
                }
            await self.db(
                "record_review", version_id=version["version_id"], request_id=request_id, review=review
            )
        # A review row is audit history, not renewed authority. Both persisted
        # reviews and newly recorded advice must still belong to this grant.
        reviewed_operation = await self.db("get_operation", request_id)
        if (
            reviewed_operation["auth_version"] != cycle["auth_version"]
            or await self.epoch() != cycle["auth_version"]
        ):
            raise StoreError("AUTH_CHANGED", "Review cannot cross authorization versions")
        if reviewed_operation["state"] != "succeeded" or reviewed_operation["evidence"] != canonical_json(review):
            raise StoreError("INVALID_DECISION", "Review must match its successful original operation")
        if snapshot is not None and (
            reviewed_operation["params"].get("review_contract") != 2
            or reviewed_operation["params"].get("input_digest") != snapshot["input_digest"]
        ):
            raise StoreError("REQUEST_CONFLICT", "Review does not match its frozen inputs")
        best = review.get(
            "best_version_id",
            version["version_id"]
            if review["decision"] == "accept"
            else work["best_version"] or version["version_id"],
        )
        if review["decision"] == "revise":
            single_work = await self.db("single_work_status")
            if single_work["enabled"] and single_work["edit_used"]:
                # A second suggestion cannot create a permanently blocked plan.
                # Preserve review evidence; never invent acceptance for sharing.
                accepted = await self.db("version_accepted", best,
                                          expected_auth_version=cycle["auth_version"])
                state = "finished" if accepted else "abandoned"
                await self.change(work["work_id"], status=state, best_version=best,
                                   reason="SINGLE_WORK_REVISION_LIMIT")
                return {"state": state, "reason": "SINGLE_WORK_REVISION_LIMIT"}
            original = await self.db("get_operation", work["pending_request"])
            if original["state"] not in {"succeeded", "failed"}:
                # Evaluate available art, but reconcile the unresolved render
                # before creating another revision. Never refund its hold.
                await self.change(work["work_id"], reason="RENDER_UNRESOLVED")
                return {"state": "blocked", "reason": "RENDER_UNRESOLVED"}
            prompt = review["edit_prompt"] + "\n保留：" + review["preserve"]
            await self.change(
                work["work_id"],
                status="planned",
                parent_version=version["version_id"],
                edit_prompt=prompt,
                best_version=best,
                pending_request=None,
                reason=review["reason"],
            )
            return {"state": "planned", "reason": "revision"}
        state = "finished" if review["decision"] == "accept" else "abandoned"
        await self.change(work["work_id"], status=state, best_version=best, reason=review["reason"])
        return {"state": state, "best_version": best}
