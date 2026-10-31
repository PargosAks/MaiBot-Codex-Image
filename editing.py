"""Explicit original-image edits; trusted callers supply the recorded service binding.

No Host registration, automatic retries, text-only fallback or delivery lives here.
Scope must come from the trusted adapter, never a model-produced identity.
"""

from typing import Optional
import asyncio
import hashlib
import json

from .artist import render_digest, stable_id
from .bridge import Bridge, BridgeError, _validate_job
from .policy import Scope, StoreError, nonempty
from .recovery import ReadOnlyRecovery
from .store import ArtistStore


def _receipt(operation, result):
    if (
        not isinstance(result, dict)
        or result.get("operation") != operation["action"]
        or result.get("request_id") != operation["request_id"]
        or result.get("request_digest") != render_digest("edit", operation["params"])
    ):
        raise StoreError("REQUEST_CONFLICT", "Receipt does not match the frozen edit")
    _validate_job(result.get("job"))
    job = result["job"]
    if operation["job_id"] not in {None, job["job_id"]}:
        raise StoreError("REQUEST_CONFLICT", "Receipt changed the bound job")
    return job


class _CaptureLookup:
    """Retain metadata from the same lookup used to reconcile, not a second poll."""

    def __init__(self, bridge, operation):
        self.bridge, self.operation, self.result = bridge, operation, None

    async def lookup(self, *args, **kwargs):
        result = await self.bridge.lookup(*args, **kwargs)
        if not isinstance(result, dict) or type(result.get("found")) is not bool:
            raise StoreError("PROTOCOL_ERROR", "Lookup has no explicit found flag")
        if result.get("found") is not False:
            _receipt(self.operation, result)
        self.result = result
        return result


class ImageEdits:
    def __init__(self, store: ArtistStore, bridge: Bridge):
        self.store, self.bridge = store, bridge
        self.recovery = ReadOnlyRecovery(store, bridge)

    def _source(self, space_id, scope, work_id, version_id):
        version = self.store.get_version(space_id, scope, version_id)
        if version["work_id"] != work_id:
            raise StoreError("NOT_FOUND", "Source version does not belong to this work")
        original = self.store.get_operation(space_id, scope, version["request_id"])
        if (
            original["action"] not in {"submit", "edit"}
            or original["work_id"] != work_id
            or original["job_id"] != version["job_id"]
            or original["state"] not in {"accepted", "unknown", "succeeded", "failed"}
        ):
            raise StoreError("REQUEST_CONFLICT", "Source is not bound to its original render")
        if hashlib.sha256(version["data"]).hexdigest() != version["sha256"]:
            raise StoreError("SOURCE_HASH_MISMATCH", "Stored source bytes do not match their digest")
        return version

    def _reserve(self, space_id, scope, work_id, source_version_id, request_id, prompt):
        space = self.store.get_space(space_id, scope)
        source = self._source(space_id, scope, work_id, source_version_id)
        return self.store.reserve(
            space_id,
            scope,
            request_id=request_id,
            action="edit",
            work_id=work_id,
            params={"prompt": prompt, "source_job_id": source["job_id"], "source_sha256": source["sha256"]},
            expected_auth_version=space["auth_version"],
        )

    async def submit(
        self,
        space_id: str,
        scope: Scope,
        work_id: str,
        source_version_id: str,
        request_id: str,
        prompt: str,
    ) -> dict:
        """Return an operation snapshot; an attempted request is never resubmitted."""
        nonempty(prompt, "prompt")
        if len(prompt) > 4000:
            raise StoreError("INVALID_ARGUMENT", "Edit prompt exceeds 4000 characters")
        operation = await self.recovery._db(
            self._reserve, space_id, scope, work_id, source_version_id, request_id, prompt
        )
        if operation["state"] != "reserved":
            return operation
        # Only the connection that changes reserved -> submitting may dispatch.
        # Cancellation waits for the DB worker, then retains any attempted hold.
        try:
            operation = await self.recovery._db(self.store.mark_submitting, space_id, scope, request_id)
        except asyncio.CancelledError:
            await self.recovery._db(self.recovery._unknown, space_id, scope, request_id, "EDIT_CANCELLED")
            raise
        try:

            async def before_dispatch():
                await self.recovery._db(self.store.validate_submitting, space_id, scope, request_id)

            result = await self.bridge.edit(
                json.loads(operation["scope_snapshot"]),
                operation["request_id"],
                **operation["params"],
                before_dispatch=before_dispatch,
            )
            job = _receipt(operation, result)
            return await self.recovery._db(self.recovery._apply, space_id, scope, request_id, job)
        except asyncio.CancelledError:
            await self.recovery._db(self.recovery._unknown, space_id, scope, request_id, "EDIT_CANCELLED")
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, (BridgeError, StoreError)) else "EDIT_ERROR"
            await self.recovery._db(self.recovery._unknown, space_id, scope, request_id, code)
            raise

    def _collection(self, space_id, scope, request_id, source_version_id):
        operation = self.store.get_operation(space_id, scope, request_id)
        if operation["action"] != "edit" or operation["state"] not in {
            "submitting",
            "accepted",
            "unknown",
            "succeeded",
            "failed",
        }:
            raise StoreError("INVALID_STATE", "Only an attempted edit can be collected")
        source = self._source(space_id, scope, operation["work_id"], source_version_id)
        if (operation["params"].get("source_job_id"), operation["params"].get("source_sha256")) != (
            source["job_id"],
            source["sha256"],
        ):
            raise StoreError("REQUEST_CONFLICT", "Parent does not match the frozen source")
        for version in self.store.list_versions(space_id, scope, operation["work_id"]):
            if version["request_id"] == request_id:
                if version["parent_id"] != source_version_id:
                    raise StoreError("REQUEST_CONFLICT", "Collected parent is immutable")
                return operation, self.store.get_version(space_id, scope, version["version_id"])
        return operation, None

    def _save(self, space_id, scope, operation, source_version_id, artifact, image):
        data = image.get("data")
        if (
            not isinstance(data, bytes)
            or len(data) != artifact["size_bytes"]
            or hashlib.sha256(data).hexdigest() != artifact["sha256"]
            or image.get("sha256") != artifact["sha256"]
            or image.get("mime") != artifact["mime_type"]
        ):
            raise StoreError("SOURCE_HASH_MISMATCH", "Returned image does not match the original artifact")
        return self.store.record_version(
            space_id,
            scope,
            work_id=operation["work_id"],
            version_id=stable_id("version", space_id, operation["request_id"]),
            request_id=operation["request_id"],
            job_id=operation["job_id"],
            data=data,
            mime=image["mime"],
            parent_id=source_version_id,
        )

    async def collect(
        self,
        space_id: str,
        scope: Scope,
        request_id: str,
        source_version_id: str,
    ) -> Optional[dict]:
        """Reconcile once and import the original job image, independently of turn outcome.

        Return the immutable version or None if lookup has no available artifact.
        A stored version is never fetched again; its unresolved turn is still
        reconciled so image availability cannot strand or clear an unknown hold.
        """
        operation, saved = await self.recovery._db(
            self._collection, space_id, scope, request_id, source_version_id
        )
        if saved is not None:
            await self.recovery.reconcile(space_id, scope, request_id)
            return saved
        capture = _CaptureLookup(self.bridge, operation)
        recovery = ReadOnlyRecovery(self.store, capture)
        operation = await recovery.reconcile(space_id, scope, request_id)
        if capture.result is None:
            # Recovery intentionally skips terminal operations. A terminal turn
            # can still have an uncollected image, so read its original metadata.
            await capture.lookup(
                json.loads(operation["scope_snapshot"]),
                operation["request_id"],
                expected_operation="edit",
                expected_digest=render_digest("edit", operation["params"]),
                expected_job_id=operation["job_id"],
            )
        result = capture.result
        if result.get("found") is False or not result["job"]["artifacts"]:
            return None
        artifact = result["job"]["artifacts"][0]
        if artifact["size_bytes"] > 8 * 1024 * 1024:
            raise StoreError("INVALID_ARGUMENT", "Version image exceeds 8 MiB")
        try:
            image = await self.bridge.image(
                json.loads(operation["scope_snapshot"]),
                operation["job_id"],
                expected_sha256=artifact["sha256"],
                expected_mime=artifact["mime_type"],
                expected_size=artifact["size_bytes"],
            )
            return await self.recovery._db(
                self._save, space_id, scope, operation, source_version_id, artifact, image
            )
        except asyncio.CancelledError:
            await self.recovery._db(self.recovery._unknown, space_id, scope, request_id, "COLLECT_CANCELLED")
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, (BridgeError, StoreError)) else "COLLECT_ERROR"
            await self.recovery._db(self.recovery._unknown, space_id, scope, request_id, code)
            raise
