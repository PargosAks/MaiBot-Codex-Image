"""Explicit read-only MCP reconciliation; never generates, fetches images or sends.

The trusted caller supplies the Bridge for the store's recorded service binding.
All remote request identity comes from the persisted operation, never model input.
"""

from typing import Callable
import asyncio
import json

from .artist import render_digest
from .bridge import Bridge, BridgeError, _reference_digest, _reference_params, _validate_job
from .policy import Scope, StoreError
from .store import ArtistStore


_PENDING = {"submitting", "accepted", "unknown"}
_TERMINAL = {"succeeded", "failed"}


class ReadOnlyRecovery:
    def __init__(self, store: ArtistStore, bridge: Bridge):
        self.store, self.bridge = store, bridge

    @staticmethod
    async def _db(function: Callable, *args, **kwargs):
        worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError as cancelled:
            # Cancelling an asyncio waiter cannot stop SQLite in its thread.
            # Repeated cancellation must not detach that worker either.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                worker.result()
            except Exception as exc:
                raise cancelled from exc
            raise

    def _unknown(self, space_id, scope, request_id, reason):
        operation = self.store.get_operation(space_id, scope, request_id)
        if operation["state"] in _PENDING:
            try:
                self.store.mark_unknown(space_id, scope, request_id, reason=reason)
            except StoreError as exc:
                # Another connection may have settled the job while lookup was
                # in flight. Never downgrade a terminal outcome to unknown.
                if exc.code != "INVALID_STATE":
                    raise
                latest = self.store.get_operation(space_id, scope, request_id)
                if latest["state"] not in _TERMINAL:
                    raise
        return self.store.get_operation(space_id, scope, request_id)

    @staticmethod
    def _outcome(job):
        if job["status"] == "completed":
            if job["turn_status"] not in {None, "completed"}:
                raise StoreError("PROTOCOL_ERROR", "Completed job contradicts its turn status")
            return "succeeded"
        if job["status"] == "failed":
            return "failed"
        if job["status"] == "outcome_unknown":
            return "unknown"
        if job["status"] == "artifact_ready":
            if job["turn_status"] == "completed":
                return "succeeded"
            if job["turn_status"] in {"failed", "interrupted", "cancelled"}:
                return "failed"
            if job["turn_status"] not in {"queued", "starting", "running", "inProgress", "in_progress"}:
                return "unknown"
        if job["status"] not in {"queued", "starting", "running", "artifact_ready"}:
            raise StoreError("PROTOCOL_ERROR", "Unexpected lookup job state")
        return "accepted"

    def _apply(self, space_id, scope, request_id, job):
        operation = self.store.get_operation(space_id, scope, request_id)
        if operation["job_id"] not in {None, job["job_id"]}:
            raise StoreError("REQUEST_CONFLICT", "Lookup does not match the bound original job")
        if operation["state"] in _TERMINAL:
            return operation
        if operation["state"] not in _PENDING:
            raise StoreError("INVALID_STATE", "Operation is no longer an attempted render")
        outcome = self._outcome(job)
        try:
            if operation["state"] == "unknown":
                self.store.reconcile(
                    space_id,
                    scope,
                    request_id,
                    outcome="accepted",
                    evidence="Matching read-only lookup",
                    job_id=job["job_id"],
                )
            elif operation["state"] == "submitting":
                self.store.mark_accepted(space_id, scope, request_id, job_id=job["job_id"])
            if outcome == "unknown":
                return self._unknown(space_id, scope, request_id, "LOOKUP_OUTCOME_UNKNOWN")
            if outcome in _TERMINAL:
                self.store.settle(
                    space_id,
                    scope,
                    request_id,
                    outcome=outcome,
                    evidence="Matching lookup confirms terminal turn",
                    job_id=job["job_id"],
                )
        except StoreError as exc:
            if exc.code != "INVALID_STATE":
                raise
            latest = self.store.get_operation(space_id, scope, request_id)
            if latest["state"] not in _TERMINAL:
                raise
        return self.store.get_operation(space_id, scope, request_id)

    async def reconcile(self, space_id: str, scope: Scope, request_id: str) -> dict:
        """Return the local operation snapshot; Bridge failures retain holds and raise.

        Terminal operations return without querying. A not-found lookup returns an
        unknown snapshot. No lookup, generation or delivery is implicitly retried.
        Revoked owners can still reconcile their already attempted operations.
        """
        operation = await self._db(self.store.get_operation, space_id, scope, request_id)
        if operation["action"] not in {"submit", "edit"}:
            raise StoreError("INVALID_OPERATION", "Only render operations can be reconciled")
        if operation["state"] in _TERMINAL:
            return operation
        if operation["state"] not in _PENDING:
            raise StoreError("INVALID_STATE", "Only attempted render operations can be reconciled")
        try:
            frozen_scope = json.loads(operation["scope_snapshot"])
            params = json.loads(operation["params_json"])
            reference = _reference_params(operation["action"], params)
            remote_operation = "render" if reference is not None else operation["action"]
            digest = (
                _reference_digest(reference) if reference is not None else render_digest(operation["action"], params)
            )
            result = await self.bridge.lookup(
                frozen_scope,
                operation["request_id"],
                expected_operation=remote_operation,
                expected_digest=digest,
                expected_job_id=operation["job_id"],
            )
            if reference is not None:
                if type(result) is not dict or type(result.get("found")) is not bool:
                    raise BridgeError("INVALID_RESPONSE", kind="protocol")
                expected = (
                    {"found", "operation", "request_id", "request_digest", "job"} if result["found"] else {"found"}
                )
                if result.keys() != expected:
                    raise BridgeError("INVALID_RESPONSE", kind="protocol")
                if result["found"]:
                    _validate_job(result["job"])
            if result.get("found") is False:
                return await self._db(self._unknown, space_id, scope, request_id, "LOOKUP_NOT_FOUND")
            # Bridge performs the full schema check. Recheck the identity boundary
            # here before any local state change, even for an injected adapter.
            if (
                result.get("found") is not True
                or result.get("operation") != remote_operation
                or result.get("request_id") != operation["request_id"]
                or result.get("request_digest") != digest
            ):
                raise StoreError(
                    "REQUEST_CONFLICT", "Lookup request identity does not match the frozen operation"
                )
            job = result["job"]
            if not isinstance(job["job_id"], str) or not job["job_id"].strip():
                raise StoreError("PROTOCOL_ERROR", "Lookup has no valid original job_id")
            return await self._db(self._apply, space_id, scope, request_id, job)
        except asyncio.CancelledError:
            await self._db(self._unknown, space_id, scope, request_id, "LOOKUP_CANCELLED")
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, (BridgeError, StoreError)) else "LOOKUP_ERROR"
            await self._db(self._unknown, space_id, scope, request_id, code)
            raise
