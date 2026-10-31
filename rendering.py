"""Explicit network Renderer; Artist alone owns ledger writes and dispatch claims.

The trusted application supplies a Bridge belonging to binding_id. This adapter
does not discover endpoints, grant authority, retry submissions or persist results.
"""

from typing import Optional
import hashlib
import json
import re

from .artist import RenderResult, render_digest
from .bridge import Bridge, BridgeError, _reference_digest, _reference_params, _validate_asset, _validate_job
from .policy import Scope, StoreError, nonempty
from .recovery import ReadOnlyRecovery
from .store import ArtistStore


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
            reference = _reference_params(op["action"], op["params"])
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
        if _reference_params(op["action"], op["params"]) is not None:
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
            _validate_asset(receipt)
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
        await ReadOnlyRecovery._db(self._space, scope)
        from .references import ReferenceInputs

        return await ReferenceInputs(
            self.store, self.space_id, self.scope, self.bridge, binding_id=self.binding_id,
        ).prepare(references, expected_auth_version=expected_auth_version, source_size=source_size)

    @staticmethod
    def _job(job):
        _validate_job(job)
        code = job["error_code"]
        if code is not None and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) is None:
            raise BridgeError("INVALID_RESPONSE", kind="protocol")
        return job

    @staticmethod
    def _transport(op):
        reference = _reference_params(op["action"], op["params"])
        if reference is not None:
            return "render", _reference_digest(reference)
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

    async def _project(self, op, job):
        outcome = ReadOnlyRecovery._outcome(job)
        if outcome == "accepted":
            state = "queued" if job["status"] in {"queued", "starting"} else "running"
        else:
            state = {"succeeded": "completed", "failed": "failed", "unknown": "outcome_unknown"}[outcome]
        image = {}
        if job["artifacts"]:
            artifact = job["artifacts"][0]
            if artifact["size_bytes"] > 8 * 1024 * 1024:
                raise BridgeError("ARTIFACT_INVALID", kind="protocol")
            image = await self.bridge.image(
                json.loads(op["scope_snapshot"]),
                job["job_id"],
                expected_sha256=artifact["sha256"],
                expected_mime=artifact["mime_type"],
                expected_size=artifact["size_bytes"],
            )
            image = await ReadOnlyRecovery._db(self._image, image, artifact)
        return RenderResult(
            op["request_id"],
            self._transport(op)[1],
            job["job_id"],
            state,
            image.get("data"),
            image.get("mime", "image/png"),
            image.get("sha256"),
            error_code=job["error_code"],
        )

    async def submit(
        self, *, scope: Scope, request_id: str, prompt: str, source: Optional[dict]
    ) -> RenderResult:
        op = await ReadOnlyRecovery._db(self._submission, scope, request_id, prompt, source)
        if "lookup_v1" not in await self.bridge.capabilities(json.loads(op["scope_snapshot"])):
            raise BridgeError("LOOKUP_UNSUPPORTED", kind="capability")
        # Health awaits network I/O. Recheck the dispatch claim and authority
        # before generation rather than using a pre-health ledger snapshot.
        op = await ReadOnlyRecovery._db(self._submission, scope, request_id, prompt, source)
        reference = _reference_params(op["action"], op["params"])
        if reference is not None:

            async def before_dispatch():
                await ReadOnlyRecovery._db(self._submission, scope, request_id, prompt, source)

            result = await self.bridge.reference_render(
                json.loads(op["scope_snapshot"]), op["request_id"], **reference,
                before_dispatch=before_dispatch,
            )
            job = self._receipt(op, result)
        elif op["action"] == "edit":

            async def before_dispatch():
                await ReadOnlyRecovery._db(self._submission, scope, request_id, prompt, source)

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
        return await self._project(op, job)

    async def lookup(self, *, scope: Scope, request_id: str) -> Optional[RenderResult]:
        op = await ReadOnlyRecovery._db(self._operation, scope, request_id)
        job = await self._lookup(op)
        return None if job is None else await self._project(op, job)

    async def status(self, *, scope: Scope, job_id: str) -> RenderResult:
        op = await ReadOnlyRecovery._db(self._operation, scope, job_id=job_id)
        job = await self._lookup(op)
        if job is None:
            raise BridgeError("LOOKUP_NOT_FOUND", kind="binding")
        return await self._project(op, job)
