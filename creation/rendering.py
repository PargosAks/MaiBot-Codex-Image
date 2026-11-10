"""Explicit network Renderer; Artist alone owns ledger writes and dispatch claims.

The trusted application supplies a Bridge belonging to binding_id. This adapter
does not discover endpoints, grant authority, retry submissions or persist results.
"""

from dataclasses import replace
from typing import Optional
import hashlib
import json
import re

from .contracts import ArtifactRef, RenderResult, render_digest
from ..mcp_client.bridge import Bridge, BridgeError
from ..mcp_client.contracts import job_outcome, reference_digest, reference_params, validate_asset, validate_job
from ..domain.async_work import join_worker
from ..domain.policy import Scope, StoreError, nonempty
from ..domain.store import ArtistStore


class MCPRenderer:
    simulation = False
    supports_edit = True  # Adapter support only; Bridge.edit probes edit_v1 each time.

    def __init__(self, store: ArtistStore, space_id: str, scope: Scope, bridge: Bridge, *, binding_id: str):
        if not isinstance(scope, Scope):
            raise StoreError("INVALID_SCOPE", "A trusted adapter Scope is required")
        nonempty(binding_id, "binding_id")
        self.store, self.space_id, self.scope = store, space_id, scope
        self.bridge, self.binding_id = bridge, binding_id

    def _space(self, scope):
        if not isinstance(scope, Scope) or scope != self.scope:
            raise StoreError("INVALID_SCOPE", "Renderer scope cannot change")
        space = self.store.get_space(self.space_id, scope)
        if space["binding_id"] != self.binding_id:
            raise StoreError("BINDING_MISMATCH", "Renderer does not match the recorded service binding")
        return space

    def _operation(self, scope, request_id=None, *, job_id=None):
        self._space(scope)
        if job_id is None:
            op = self.store.get_operation(self.space_id, scope, request_id)
        else:
            op = self.store.get_render_operation_by_job(self.space_id, scope, job_id)
        if op["binding_id"] != self.binding_id or op["scope_snapshot"] != self.scope.snapshot():
            raise StoreError("BINDING_MISMATCH", "Operation identity differs from the renderer")
        if op["action"] not in {"submit", "edit"} or op["state"] not in {
            "submitting",
            "accepted",
            "unknown",
            "succeeded",
            "failed",
        }:
            raise StoreError("INVALID_STATE", "Only an attempted render can use this adapter")
        try:
            reference = reference_params(op["action"], op["params"])
        except BridgeError as exc:
            raise StoreError("REQUEST_CONFLICT", "Invalid frozen reference transport parameters") from exc
        expected = {"prompt"} if op["action"] == "submit" else {"prompt", "source_job_id", "source_sha256"}
        if reference is not None:
            expected |= {"transport_operation", "reference_inputs", "reference_contract"}
        if set(op["params"]) != expected:
            raise StoreError("REQUEST_CONFLICT", "Unexpected frozen render parameters")
        return op

    def _submission(self, scope, request_id, prompt, source):
        op = self._operation(scope, request_id)
        op = self.store.validate_submitting(self.space_id, scope, request_id)
        if prompt != op["params"]["prompt"]:
            raise StoreError("REQUEST_CONFLICT", "Prompt differs from the frozen operation")
        work = self.store.get_artist_work(self.space_id, scope, op["work_id"])
        if work["pending_request"] != request_id:
            raise StoreError("REQUEST_CONFLICT", "Work does not own this pending request")
        if reference_params(op["action"], op["params"]) is not None:
            self._reference_submission(scope, op, work)
        if op["action"] == "submit":
            if source is not None or work["parent_version"] is not None:
                raise StoreError("REQUEST_CONFLICT", "An original render cannot take an edit source")
            return op
        if not isinstance(source, dict) or set(source) != {"job_id", "sha256", "data", "mime"}:
            raise StoreError("REQUEST_CONFLICT", "Edit source must be a persisted parent image")
        if work["parent_version"] is None:
            raise StoreError("REQUEST_CONFLICT", "Edit work has no parent version")
        parent = self.store.get_version(self.space_id, scope, work["parent_version"])
        original = self.store.get_render_operation_by_job(self.space_id, scope, parent["job_id"])
        if (
            parent["work_id"] != op["work_id"]
            or original["work_id"] != op["work_id"]
            or original["request_id"] != parent["request_id"]
            or original["binding_id"] != self.binding_id
            or any(source[key] != parent[key] for key in source)
            or parent["job_id"] != op["params"]["source_job_id"]
            or parent["sha256"] != op["params"]["source_sha256"]
            or hashlib.sha256(parent["data"]).hexdigest() != parent["sha256"]
        ):
            raise StoreError("REQUEST_CONFLICT", "Edit source differs from the work's frozen parent")
        return op

    def _reference_submission(self, scope: Scope, op: dict, work: dict) -> None:
        policy = self.store.validate_reference_authorization(
            self.space_id, scope, expected_auth_version=op["auth_version"],
        )
        references = work["brief"].get("references")
        if type(references) is not list or not 1 <= len(references) <= 3:
            raise StoreError("REQUEST_CONFLICT", "Render requires the original frozen image references")
        handles = []
        for reference in references:
            if type(reference) is not dict or reference.get("usage") != "image_input":
                raise StoreError("REQUEST_CONFLICT", "Render cannot replace text-only reference usage")
            if type(reference.get("policy_version")) is not int or (
                reference.get("policy_version"), reference.get("consent_ref")
            ) != (policy["version"], policy["consent_ref"]):
                raise StoreError("REFERENCE_AUTH_CHANGED", "Frozen image delegation no longer matches")
            registration = self.store.get_asset_registration(self.space_id, scope, reference["asset_id"])
            if (
                not isinstance(registration, dict) or registration.get("state") != "succeeded"
                or registration.get("binding_id") != self.binding_id
                or registration.get("asset_id") != reference["asset_id"]
            ):
                raise StoreError("REQUEST_CONFLICT", "Reference registration is not confirmed for this service")
            receipt = registration.get("receipt")
            validate_asset(receipt)
            image = self.store.get_asset_image(self.space_id, scope, reference["asset_id"])
            identity = (reference["sha256"], reference["mime"], len(image["data"]))
            if (image["sha256"], image["mime"], len(image["data"])) != identity or any(
                (item.get("sha256"), item.get("mime"), item.get("size_bytes")) != identity
                for item in (registration, receipt)
            ):
                raise StoreError("ARTIFACT_INVALID", "Frozen reference and registered source image differ")
            handles.append({"asset_id": receipt["asset_id"], "sha256": reference["sha256"],
                            "role": reference["role"]})
        if handles != op["params"]["reference_inputs"]:
            raise StoreError("REQUEST_CONFLICT", "Remote handles differ from the ordered frozen references")

    async def prepare_references(
        self, *, scope: Scope, references: list, expected_auth_version: int, source_size: int = 0,
    ) -> list:
        # The registry owns durable registration intent, policy and aggregate limits.
        await join_worker(self._space, scope)
        from .references import ReferenceInputs

        return await ReferenceInputs(
            self.store, self.space_id, self.scope, self.bridge, binding_id=self.binding_id,
        ).prepare(references, expected_auth_version=expected_auth_version, source_size=source_size)

    @staticmethod
    def _job(job):
        validate_job(job)
        code = job["error_code"]
        if code is not None and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) is None:
            raise BridgeError("INVALID_RESPONSE", kind="protocol")
        return job

    @staticmethod
    def _transport(op):
        reference = reference_params(op["action"], op["params"])
        if reference is not None:
            return "render", reference_digest(reference)
        return op["action"], render_digest(op["action"], op["params"])

    @staticmethod
    def _receipt(op, receipt, *, expected_job_id=None):
        if not isinstance(receipt, dict):
            raise BridgeError("INVALID_RESPONSE", kind="protocol")
        operation, digest = MCPRenderer._transport(op)
        if (
            receipt.get("operation") != operation
            or receipt.get("request_id") != op["request_id"]
            or receipt.get("request_digest") != digest
        ):
            raise BridgeError("RECEIPT_MISMATCH", kind="binding")
        job = receipt.get("job")
        MCPRenderer._job(job)
        if (op["job_id"] is not None and job["job_id"] != op["job_id"]) or (
            expected_job_id is not None and job["job_id"] != expected_job_id
        ):
            raise BridgeError("RECEIPT_MISMATCH", kind="binding")
        return job

    async def _lookup(self, op, *, expected_job_id=None):
        operation, digest = self._transport(op)
        result = await self.bridge.lookup(
            json.loads(op["scope_snapshot"]),
            op["request_id"],
            expected_operation=operation,
            expected_digest=digest,
            expected_job_id=op["job_id"] if expected_job_id is None else expected_job_id,
        )
        if not isinstance(result, dict) or type(result.get("found")) is not bool:
            raise BridgeError("INVALID_RESPONSE", kind="protocol")
        if result["found"] is False:
            if set(result) != {"found"}:
                raise BridgeError("INVALID_RESPONSE", kind="protocol")
            return None
        return self._receipt(op, result, expected_job_id=expected_job_id)

    @staticmethod
    def _image(image, artifact):
        if (
            not isinstance(image, dict)
            or not isinstance(image.get("data"), bytes)
            or len(image["data"]) != artifact["size_bytes"]
            or image.get("mime") != artifact["mime_type"]
            or image.get("sha256") != artifact["sha256"]
            or hashlib.sha256(image["data"]).hexdigest() != artifact["sha256"]
        ):
            raise BridgeError("ARTIFACT_INVALID", kind="protocol")
        return image

    def _receipt_result(self, op, job) -> RenderResult:
        """只投影已经校验的回执；这里不得因下载失败丢失后端终态。"""
        outcome = job_outcome(job)
        if outcome == "accepted":
            state = "queued" if job["status"] in {"queued", "starting"} else "running"
        else:
            state = {"succeeded": "completed", "failed": "failed", "unknown": "outcome_unknown"}[outcome]
        artifact_ref = None
        if job["artifacts"]:
            artifact = job["artifacts"][0]
            artifact_ref = ArtifactRef(artifact["sha256"], artifact["mime_type"], artifact["size_bytes"])
        return RenderResult(
            op["request_id"],
            self._transport(op)[1],
            job["job_id"],
            state,
            error_code=job["error_code"],
            artifact=artifact_ref,
        )

    async def fetch_artifact(self, *, scope: Scope, receipt: RenderResult) -> RenderResult:
        """取回原任务的字节，重新核对归属，不创建新任务或修改操作账本。"""
        op = await join_worker(self._operation, scope, receipt.request_id)
        if receipt.request_digest != self._transport(op)[1] or (
            op["job_id"] is not None and op["job_id"] != receipt.job_id
        ):
            raise BridgeError("RECEIPT_MISMATCH", kind="binding")
        artifact = receipt.artifact
        if artifact is None:
            return receipt
        if not isinstance(artifact, ArtifactRef) or not 0 < artifact.size_bytes <= 8 * 1024 * 1024:
            raise BridgeError("ARTIFACT_INVALID", kind="protocol")
        image = await self.bridge.image(
            json.loads(op["scope_snapshot"]), receipt.job_id,
            expected_sha256=artifact.sha256, expected_mime=artifact.mime,
            expected_size=artifact.size_bytes,
        )
        image = await join_worker(self._image, image, {
            "sha256": artifact.sha256, "mime_type": artifact.mime, "size_bytes": artifact.size_bytes,
        })
        return replace(receipt, image=image["data"], mime=image["mime"], sha256=image["sha256"], artifact=None)

    async def submit_receipt(
        self, *, scope: Scope, request_id: str, prompt: str, source: Optional[dict]
    ) -> RenderResult:
        op = await join_worker(self._submission, scope, request_id, prompt, source)
        if "lookup_v1" not in await self.bridge.capabilities(json.loads(op["scope_snapshot"])):
            raise BridgeError("LOOKUP_UNSUPPORTED", kind="capability")
        # Health awaits network I/O. Recheck the dispatch claim and authority
        # before generation rather than using a pre-health ledger snapshot.
        op = await join_worker(self._submission, scope, request_id, prompt, source)
        reference = reference_params(op["action"], op["params"])
        if reference is not None:

            async def before_dispatch():
                await join_worker(self._submission, scope, request_id, prompt, source)

            result = await self.bridge.reference_render(
                json.loads(op["scope_snapshot"]), op["request_id"], **reference,
                before_dispatch=before_dispatch,
            )
            job = self._receipt(op, result)
        elif op["action"] == "edit":

            async def before_dispatch():
                await join_worker(self._submission, scope, request_id, prompt, source)

            result = await self.bridge.edit(
                json.loads(op["scope_snapshot"]),
                op["request_id"],
                **op["params"],
                before_dispatch=before_dispatch,
            )
            job = self._receipt(op, result)
        else:
            result = await self.bridge.call(
                "submit", json.loads(op["scope_snapshot"]), {"request_id": op["request_id"], **op["params"]}
            )
            if not isinstance(result, dict):
                raise BridgeError("INVALID_RESPONSE", kind="protocol")
            legacy = {"turn_status": None, "error_code": None, "artifacts": [], **result}
            self._job(legacy)
            job = await self._lookup(op, expected_job_id=legacy["job_id"])
            if job is None:
                raise BridgeError("LOOKUP_NOT_FOUND", kind="binding")
        return self._receipt_result(op, job)

    async def lookup_receipt(self, *, scope: Scope, request_id: str) -> Optional[RenderResult]:
        op = await join_worker(self._operation, scope, request_id)
        job = await self._lookup(op)
        return None if job is None else self._receipt_result(op, job)

    async def status_receipt(self, *, scope: Scope, job_id: str) -> RenderResult:
        op = await join_worker(self._operation, scope, job_id=job_id)
        job = await self._lookup(op)
        if job is None:
            raise BridgeError("LOOKUP_NOT_FOUND", kind="binding")
        return self._receipt_result(op, job)

    # 保留带图接口供旧调用方使用；Artist 通过 ReceiptRenderer 显式选择两阶段路径。
    async def submit(self, *, scope: Scope, request_id: str, prompt: str, source: Optional[dict]) -> RenderResult:
        receipt = await self.submit_receipt(scope=scope, request_id=request_id, prompt=prompt, source=source)
        return await self.fetch_artifact(scope=scope, receipt=receipt)

    async def lookup(self, *, scope: Scope, request_id: str) -> Optional[RenderResult]:
        receipt = await self.lookup_receipt(scope=scope, request_id=request_id)
        return None if receipt is None else await self.fetch_artifact(scope=scope, receipt=receipt)

    async def status(self, *, scope: Scope, job_id: str) -> RenderResult:
        receipt = await self.status_receipt(scope=scope, job_id=job_id)
        return await self.fetch_artifact(scope=scope, receipt=receipt)
