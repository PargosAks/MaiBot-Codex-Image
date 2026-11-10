"""冻结评审输入，并从原操作和产物重建可授权的接受证据。"""

from typing import Any, Dict, Mapping, Optional
import hashlib
import json
import sqlite3

from .ledger_repository import LedgerQueries, decode_fields, expected_authorization, require_authorized
from .policy import Scope, StoreError, canonical_json, nonempty
from .sqlite_uow import SqliteUnitOfWork


class ReviewRepository:
    def __init__(self, uow: SqliteUnitOfWork, queries: LedgerQueries):
        self._uow = uow
        self._queries = queries

    def get_review_snapshot(self, space_id, scope, version_id) -> Optional[Dict[str, Any]]:
        """Historical metadata remains readable after delegation is withdrawn."""
        with self._uow.lock:
            return self._review_snapshot(space_id, scope, version_id)

    def _review_snapshot(self, space_id, scope, version_id) -> Optional[Dict[str, Any]]:
        self._queries.space(space_id, scope)
        row = self._uow.db.execute(
            "SELECT * FROM review_snapshots WHERE space_id=? AND version_id=?", (space_id, version_id)
        ).fetchone()
        return decode_fields(row, "context") if row is not None else None

    def _validate_review_context(self, space_id, scope, version_id, context, *, check_best):
        """Validate identities and bytes without putting media in the frozen JSON.

        Only initial freezing consults the live best. Historical snapshots must
        remain valid when applying a later review changes that best.
        """
        allowed = {
            "persona", "events", "inspirations", "works", "versions", "current_work",
            "current_version", "review_contract", "review_inputs", "profiles", "series", "assets", "asset_details",
        }
        if (
            not isinstance(context, dict)
            or not context.keys() <= allowed
            or type(context.get("review_contract")) is not int
            or context["review_contract"] != 2
            or not isinstance(context.get("current_work"), dict)
            or not isinstance(context.get("current_version"), dict)
        ):
            raise StoreError("INVALID_ARGUMENT", "A contract 2 review context is required")
        current = self._queries.version(space_id, scope, version_id)
        work = self._queries.artist_work(space_id, scope, current["work_id"])
        work_meta, current_meta = context["current_work"], context["current_version"]
        if work_meta.get("work_id") != work["work_id"]:
            raise StoreError("NOT_FOUND", "Review context belongs to another work")
        if (
            not work_meta.keys() <= {"work_id", "brief", "best_version", "status", "reason"}
            or "best_version" not in work_meta
            or not current_meta.keys() <= {"version_id", "sha256", "mime", "parent_id"}
            or any(current_meta.get(key) != current[key] for key in ("version_id", "sha256", "mime"))
            or ("parent_id" in current_meta and current_meta["parent_id"] != current["parent_id"])
        ):
            raise StoreError("INVALID_ARGUMENT", "Review current metadata does not match its version")
        if "brief" in work_meta and canonical_json(work_meta["brief"]) != canonical_json(work["brief"]):
            raise StoreError("REQUEST_CONFLICT", "Review brief must match the immutable work plan")
        best = work_meta["best_version"]
        if best is not None:
            nonempty(best, "best_version")
        if check_best and best != work["best_version"]:
            raise StoreError("REQUEST_CONFLICT", "Previous best changed before freezing the review")
        expected_roles = {"current": version_id}
        if current["parent_id"] is not None:
            expected_roles["parent"] = current["parent_id"]
        if best is not None:
            expected_roles["previous_best"] = best
        inputs = context.get("review_inputs")
        if not isinstance(inputs, list) or not 1 <= len(inputs) <= 3:
            raise StoreError("INVALID_ARGUMENT", "A review requires one to three distinct image versions")
        seen_versions, roles = set(), {}
        for item in inputs:
            if not isinstance(item, dict) or item.keys() != {"version_id", "roles", "sha256", "mime"}:
                raise StoreError("INVALID_ARGUMENT", "Review inputs must contain image metadata only")
            identifier = nonempty(item["version_id"], "version_id")
            assigned = item["roles"]
            if (
                identifier in seen_versions
                or not isinstance(assigned, list)
                or not assigned
                or any(type(role) is not str or role not in expected_roles for role in assigned)
                or len(set(assigned)) != len(assigned)
            ):
                raise StoreError("INVALID_ARGUMENT", "Review versions and roles must be unique")
            seen_versions.add(identifier)
            image = self._queries.version(space_id, scope, identifier)
            if image["work_id"] != work["work_id"]:
                raise StoreError("NOT_FOUND", "All review inputs must belong to the current work")
            if (
                not isinstance(image["data"], bytes)
                or not image["data"]
                or hashlib.sha256(image["data"]).hexdigest() != image["sha256"]
                or item["sha256"] != image["sha256"]
                or item["mime"] != image["mime"]
                or image["mime"] not in {"image/png", "image/jpeg", "image/webp"}
            ):
                raise StoreError("ARTIFACT_INVALID", "Review image metadata or bytes failed verification")
            for role in assigned:
                if role in roles or expected_roles[role] != identifier:
                    raise StoreError("INVALID_ARGUMENT", "Review role does not match the original version")
                roles[role] = identifier
        if roles != expected_roles:
            raise StoreError("INVALID_ARGUMENT", "Review must include current, parent and previous best")
        return seen_versions

    def freeze_review_snapshot(
        self, space_id, scope, *, version_id, context, expected_auth_version
    ) -> Dict[str, Any]:
        payload = canonical_json(context)
        # Validate exactly the serialized inputs, detached from caller-owned
        # dictionaries that could change while the transaction waits for a lock.
        context = json.loads(payload)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self._uow.transaction():
            space = self._queries.space(space_id, scope)
            expected_authorization(space, expected_auth_version)
            require_authorized(space)
            old = self._uow.db.execute(
                "SELECT * FROM review_snapshots WHERE space_id=? AND version_id=?", (space_id, version_id)
            ).fetchone()
            if old is not None:
                if old["auth_version"] != space["auth_version"]:
                    raise StoreError("AUTH_CHANGED", "Review snapshot belongs to an older authorization")
                if old["context"] != payload or old["input_digest"] != digest:
                    raise StoreError("REQUEST_CONFLICT", "Review snapshot inputs are immutable")
            self._validate_review_context(space_id, scope, version_id, context, check_best=old is None)
            if old is None:
                self._uow.db.execute(
                    "INSERT INTO review_snapshots(space_id,version_id,auth_version,context,input_digest) "
                    "VALUES(?,?,?,?,?)", (space_id, version_id, space["auth_version"], payload, digest),
                )
            return self._review_snapshot(space_id, scope, version_id)

    def version_accepted(self, space_id, scope, version_id, expected_auth_version=None) -> bool:
        """Prove acceptance using the successful original review, never work status.

        A review row alone is audit history: older callers may record one while
        an operation is still unknown. Malformed or mismatched evidence cannot
        certify an image, but is retained unchanged for investigation.
        """
        with self._uow.transaction():
            space = self._queries.space(space_id, scope)
            expected_authorization(space, expected_auth_version)
            target = self._queries.version(space_id, scope, version_id)
            if not space["authorized"]:
                return False
            return self.accepted_review(space_id, scope, space, target) is not None

    def accepted_review(
        self, space_id: str, scope: Scope, space: sqlite3.Row, target: Mapping[str, Any] | sqlite3.Row, *,
        after_operation_id: int = 0,
    ) -> Optional[sqlite3.Row]:
        """Shared proof check for acceptance and archiving, inside the caller's transaction."""
        rows = self._uow.db.execute(
                """SELECT r.version_id,r.request_id,r.review,o.params_json,o.evidence FROM reviews AS r
                JOIN operations AS o ON o.space_id=r.space_id AND o.request_id=r.request_id
                WHERE r.space_id=? AND r.work_id=? AND o.work_id=r.work_id
                AND o.action='think' AND o.category='think' AND o.state='succeeded'
                AND o.auth_version=? AND o.scope_snapshot=? AND o.binding_id=? AND o.id>? ORDER BY r.id""",
                (
                    space_id, target["work_id"], space["auth_version"], space["scope"], space["binding_id"],
                    after_operation_id,
                ),
            ).fetchall()
        for row in rows:
            try:
                if self._review_accepts(space_id, scope, space, target, row):
                    return row
            except (StoreError, ValueError, TypeError, KeyError, RecursionError):
                # Fail closed on corrupt historical evidence; do not rewrite
                # it or interfere with any other valid review of the work.
                continue
        return None

    def _review_accepts(self, space_id, scope, space, target, row) -> bool:
        review, params = json.loads(row["review"]), json.loads(row["params_json"])
        if (
            not isinstance(review, dict)
            or review.get("decision") != "accept"
            or row["evidence"] != canonical_json(review)
            or not isinstance(params, dict)
            or params.get("task") != "review"
            or params.get("version_id") != row["version_id"]
        ):
            return False
        context = params.get("context")
        if not isinstance(context, dict):
            return False
        current = self._queries.version(space_id, scope, row["version_id"])
        work_meta, current_meta = context.get("current_work"), context.get("current_version")
        if (
            current["work_id"] != target["work_id"]
            or not isinstance(work_meta, dict)
            or work_meta.get("work_id") != target["work_id"]
            or not isinstance(current_meta, dict)
            or any(current_meta.get(key) != current[key] for key in ("version_id", "sha256", "mime"))
        ):
            return False
        contract = context.get("review_contract", 1)
        operation_contract = params.get("review_contract", 1)
        if type(contract) is not int or type(operation_contract) is not int or operation_contract != contract:
            return False
        selected = review.get("best_version_id", current["version_id"])
        if selected != target["version_id"]:
            return False
        if contract == 1:
            # Contract 1 supplied only the current image, even if its version
            # catalogue let the model name an unseen historical best.
            return (
                selected == current["version_id"]
                and isinstance(current["data"], bytes)
                and bool(current["data"])
                and hashlib.sha256(current["data"]).hexdigest() == current["sha256"]
                and current["mime"] in {"image/png", "image/jpeg", "image/webp"}
            )
        if contract != 2:
            return False
        snapshot = self._review_snapshot(space_id, scope, current["version_id"])
        digest = hashlib.sha256(canonical_json(context).encode("utf-8")).hexdigest()
        if (
            snapshot is None
            or snapshot["auth_version"] != space["auth_version"]
            or snapshot["input_digest"] != digest
            or params.get("input_digest") != digest
            or canonical_json(snapshot["context"]) != canonical_json(context)
        ):
            return False
        visible = self._validate_review_context(
            space_id, scope, current["version_id"], context, check_best=False
        )
        return selected in visible

    def record_review(self, space_id, scope, *, version_id, request_id, review):
        if not isinstance(review, dict):
            raise StoreError("INVALID_ARGUMENT", "A validated review object is required")
        payload = canonical_json(review)
        with self._uow.transaction():
            version = self._queries.version(space_id, scope, version_id)
            op = self._queries.operation(space_id, scope, request_id)
            if (
                op["action"] != "think"
                or op["work_id"] != version["work_id"]
                or op["state"] in {"reserved", "rejected", "failed"}
            ):
                raise StoreError(
                    "NOT_FOUND", "Review must belong to an attempted think operation on this work"
                )
            old = self._uow.db.execute(
                "SELECT * FROM reviews WHERE space_id=? AND request_id=?", (space_id, request_id)
            ).fetchone()
            if old and (old["version_id"], old["review"]) != (version_id, payload):
                raise StoreError("REQUEST_CONFLICT", "Review is immutable")
            if not old:
                now, _ = self._uow.now()
                self._uow.db.execute(
                    """INSERT INTO reviews(space_id,work_id,version_id,request_id,review,created_at)
                                VALUES(?,?,?,?,?,?)""",
                    (space_id, version["work_id"], version_id, request_id, payload, now),
                )
                old = self._uow.db.execute(
                    "SELECT * FROM reviews WHERE space_id=? AND request_id=?", (space_id, request_id)
                ).fetchone()
            return decode_fields(old, "review")

    def list_reviews(self, space_id, scope, work_id):
        with self._uow.lock:
            self._queries.work(space_id, scope, work_id)
            return [
                decode_fields(row, "review")
                for row in self._uow.db.execute(
                    "SELECT * FROM reviews WHERE space_id=? AND work_id=? ORDER BY id",
                    (space_id, work_id),
                )
            ]
