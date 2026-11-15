"""Offline SQLite artist ledger; no Host, MCP, timer or delivery side effects.

All authority/configuration methods are for a trusted adapter, not model tools.
Use asyncio.to_thread when integrating this synchronous module with a Host loop.
"""

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, Optional
import hashlib
import json
import os
import sqlite3
import unicodedata

from .assets import (
    MAX_ASSETS,
    MAX_ASSET_BYTES,
    REFERENCE_FIELDS,
    reference_prompt,
    validate_asset_image,
    validate_archive,
    validate_references,
)
from .ledger_repository import ACTIVE_WORK as _ACTIVE_WORK
from .ledger_repository import LedgerQueries, decode_fields, expected_authorization, require_authorized
from .policy import (
    ACTIONS,
    CONSUMED,
    Delegation,
    Limits,
    Scope,
    StoreError,
    budget_day,
    canonical_json,
    nonempty,
    parameters,
)
from .profiles import (
    CHOICE_FIELDS,
    MAX_PROFILE_VERSIONS,
    normalize_objective,
    render_prompt,
    validate_catalog,
    validate_catalog_decision,
    validate_choice,
    validate_creative,
)
from .review_repository import ReviewRepository
from .schema import (
    SCHEMA_VERSION,
    _SCHEMA,
    _SCHEMA_2,
    _SCHEMA_3,
    _SCHEMA_4,
    _SCHEMA_5,
    _SCHEMA_6,
    _SCHEMA_7,
    _SCHEMA_8,
    _SCHEMA_9,
    _SCHEMA_10,
    upgrade_schema,
)
from .sqlite_uow import SqliteUnitOfWork
from .wake_repository import WakeRepository


# 只有明确未受理的拒绝才释放预算占用；请求冲突必须保留为未知。
PREACCEPT_REJECTIONS = frozenset(
    {
        "INVALID_ARGUMENTS",
        "EDIT_UNSUPPORTED",
        "SOURCE_NOT_FOUND",
        "SOURCE_NOT_READY",
        "SOURCE_HASH_MISMATCH",
        "QUEUE_FULL",
    }
)
_VERSION_META = "space_id,version_id,work_id,request_id,job_id,sha256,mime,parent_id,created_at"
class ArtistStore:
    def __init__(self, path: Path, *, clock: Optional[Callable[[], datetime]] = None):
        self._uow = SqliteUnitOfWork(path, clock=clock)
        try:
            with self._uow.transaction():
                # 保留本模块的版本和 DDL 入口，供历史台账兼容检查复用。
                upgrade_schema(
                    self.db,
                    (_SCHEMA, _SCHEMA_2, _SCHEMA_3, _SCHEMA_4, _SCHEMA_5,
                     _SCHEMA_6, _SCHEMA_7, _SCHEMA_8, _SCHEMA_9, _SCHEMA_10),
                    target_version=SCHEMA_VERSION,
                )
        except BaseException:
            self._uow.close()
            raise
        self._queries = LedgerQueries(self.db)
        self._reviews = ReviewRepository(self._uow, self._queries)
        self._wakes = WakeRepository(self._uow, self._queries, self._reviews)

    @property
    def path(self) -> Path:
        return self._uow.path

    @property
    def db(self) -> sqlite3.Connection:
        return self._uow.db

    @property
    def lock(self) -> RLock:
        return self._uow.lock

    @property
    def clock(self) -> Callable[[], datetime]:
        return self._uow.clock

    @clock.setter
    def clock(self, clock: Callable[[], datetime]) -> None:
        self._uow.clock = clock

    def create_space(self, space_id: str, scope: Scope, *, binding_id: str) -> Dict[str, Any]:
        nonempty(space_id, "space_id")
        nonempty(binding_id, "binding_id")
        if space_id == "*" or not isinstance(scope, Scope):
            raise StoreError("INVALID_SCOPE", "Reserved space_id or untyped scope")
        with self._uow.transaction():
            old = self.db.execute("SELECT * FROM spaces WHERE space_id=?", (space_id,)).fetchone()
            if old and (old["scope"] != scope.snapshot() or old["binding_id"] != binding_id):
                raise StoreError("OWNER_CONFLICT", "Space owner and service binding are immutable")
            self._single_work_policy(space_id, scope)
            self.db.execute(
                "INSERT OR IGNORE INTO spaces(space_id,scope,binding_id) VALUES(?,?,?)",
                (space_id, scope.snapshot(), binding_id),
            )
            return dict(self._queries.space(space_id, scope))

    def get_space(self, space_id: str, scope: Scope) -> Dict[str, Any]:
        """Read authority version and immutable binding for a trusted adapter."""
        with self.lock:
            return dict(self._queries.space(space_id, scope))

    def configured_spaces(self) -> list[dict]:
        """Trusted Host administration only, for reconciling removed delegations."""
        with self.lock:
            return [dict(row) for row in self.db.execute("SELECT * FROM spaces ORDER BY space_id")]

    def grant(self, space_id: str, scope: Scope, delegation: Delegation) -> int:
        if not isinstance(delegation, Delegation):
            raise StoreError("AUTH_REQUIRED", "An explicit delegation record is required")
        return self._authorization(space_id, scope, True, delegation.source_ref, delegation.granted_by)

    def revoke(self, space_id: str, scope: Scope, *, source_ref: str, granted_by: str) -> int:
        return self._authorization(space_id, scope, False, source_ref, granted_by)

    def _authorization(self, space_id, scope, active, source_ref, granted_by):
        nonempty(source_ref, "source_ref")
        nonempty(granted_by, "granted_by")
        now, _ = self._uow.now()
        with self._uow.transaction():
            space = self._queries.space(space_id, scope)
            version = space["auth_version"] + 1
            self.db.execute(
                "INSERT INTO grants VALUES(?,?,?,?,?,?)",
                (space_id, version, int(active), source_ref, granted_by, now),
            )
            self.db.execute(
                "UPDATE spaces SET authorized=?,auth_version=? WHERE space_id=?",
                (int(active), version, space_id),
            )
            return version

    def set_global_limits(self, limits: Limits):
        """Trusted application administration; persists across all reopenings."""
        with self._uow.transaction():
            self._set_limits("*", limits)

    def set_limits(self, space_id: str, scope: Scope, limits: Limits):
        with self._uow.transaction():
            self._queries.space(space_id, scope)
            self._set_limits(space_id, limits)

    def _set_limits(self, key, limits):
        if not isinstance(limits, Limits):
            raise StoreError("INVALID_ARGUMENT", "limits must be a Limits value")
        self.db.execute(
            """INSERT INTO limits VALUES(?,?,?,?,?,?) ON CONFLICT(scope_key) DO UPDATE SET
            think=excluded.think,render=excluded.render,share=excluded.share,
            max_inflight=excluded.max_inflight,max_revisions=excluded.max_revisions""",
            (key, *asdict(limits).values()),
        )

    def _single_work_stream_policy(self, platform, stream_id):
        return self.db.execute(
            """SELECT p.* FROM single_work_policies AS p JOIN spaces AS s USING(space_id)
            WHERE p.enabled=1 AND json_extract(s.scope,'$.platform')=?
            AND json_extract(s.scope,'$.stream_id')=?""",
            (platform, stream_id),
        ).fetchone()

    def _single_work_policy(self, space_id, scope):
        policy = self._single_work_stream_policy(scope.platform, scope.stream_id)
        if policy is not None and policy["space_id"] != space_id:
            raise StoreError("SINGLE_WORK_SCOPE_CONFLICT", "This stream is locked to another space")
        return policy

    def configure_single_work(self, space_id: str, scope: Scope, enabled: bool = False) -> Dict[str, Any]:
        """Latch the policy on an unused stream; only trusted configuration can enable it."""
        with self._uow.transaction():
            return self._configure_single_work(space_id, scope, enabled)

    def configure_single_work_batch(self, items: list[tuple[str, Scope, bool]]) -> list[Dict[str, Any]]:
        """Apply the complete trusted configuration atomically, including permanent latches."""
        if not isinstance(items, list):
            raise StoreError("INVALID_ARGUMENT", "items must be a list of single-work configurations")
        entries = tuple(items)
        with self._uow.transaction():
            results = []
            for item in entries:
                if not isinstance(item, tuple) or len(item) != 3:
                    raise StoreError("INVALID_ARGUMENT", "Each configuration must be a (space_id, scope, enabled) tuple")
                results.append(self._configure_single_work(*item))
            return results

    def _configure_single_work(self, space_id: str, scope: Scope, enabled: bool) -> Dict[str, Any]:
        """Caller owns the transaction so a later configuration failure rolls back new latches."""
        if type(enabled) is not bool:
            raise StoreError("INVALID_ARGUMENT", "enabled must be a boolean")
        self._queries.space(space_id, scope)
        policy = self._single_work_policy(space_id, scope)
        if policy is not None and not enabled:
            raise StoreError("SINGLE_WORK_LATCHED", "Single-work policy cannot be disabled")
        if policy is None and enabled:
            # Include prior aliases across owners and service bindings. A
            # clean space ID cannot erase work or attempts in this stream.
            history = self.db.execute(
                """SELECT 1 FROM spaces AS s WHERE json_extract(s.scope,'$.platform')=?
                AND json_extract(s.scope,'$.stream_id')=? AND (
                    EXISTS(SELECT 1 FROM works AS w WHERE w.space_id=s.space_id)
                    OR EXISTS(SELECT 1 FROM operations AS o
                              WHERE o.space_id=s.space_id AND o.action IN ('submit','edit','share'))
                ) LIMIT 1""",
                (scope.platform, scope.stream_id),
            ).fetchone()
            if history is not None:
                raise StoreError("SINGLE_WORK_HISTORY", "Single-work policy requires a stream without work or attempts")
            self.db.execute("INSERT INTO single_work_policies(space_id,enabled) VALUES(?,1)", (space_id,))
        return self._single_work_status(space_id)

    def _single_work_status(self, space_id):
        row = self.db.execute(
            """SELECT work_id,
                EXISTS(SELECT 1 FROM operations WHERE space_id=p.space_id AND action='submit') AS create_used,
                EXISTS(SELECT 1 FROM operations WHERE space_id=p.space_id AND action='edit') AS edit_used,
                EXISTS(SELECT 1 FROM operations WHERE space_id=p.space_id AND action='share') AS share_used
            FROM single_work_policies AS p WHERE space_id=? AND enabled=1""",
            (space_id,),
        ).fetchone()
        return {
            "enabled": row is not None,
            "work_id": row["work_id"] if row is not None else None,
            **{key: bool(row[key]) if row is not None else False for key in ("create_used", "edit_used", "share_used")},
        }

    def single_work_status(self, space_id: str, scope: Scope) -> Dict[str, Any]:
        """Read the lifetime claims, regardless of day, authorization or operation outcome."""
        with self.lock:
            self._queries.space(space_id, scope)
            return self._single_work_status(space_id)

    def single_work_for_stream(self, platform: str, stream_id: str) -> bool:
        """Trusted manual-command guard: the latch applies to every owner of the stream."""
        nonempty(platform, "platform")
        nonempty(stream_id, "stream_id")
        with self.lock:
            return self._single_work_stream_policy(platform, stream_id) is not None

    def _lock_single_work(self, space_id, scope, work_id):
        policy = self._single_work_policy(space_id, scope)
        if policy is None or policy["work_id"] == work_id:
            return
        if policy["work_id"] is not None:
            raise StoreError("SINGLE_WORK_LIMIT", "This space already has its one permanent work")
        # The deferred FK permits the claim before planning. The claim and
        # work are committed together, or rolled back if any plan check fails.
        self.db.execute("UPDATE single_work_policies SET work_id=? WHERE space_id=?", (work_id, space_id))

    def create_work(self, space_id: str, scope: Scope, work_id: str) -> Dict[str, Any]:
        nonempty(work_id, "work_id")
        now, _ = self._uow.now()
        with self._uow.transaction():
            require_authorized(self._queries.space(space_id, scope))
            self._lock_single_work(space_id, scope, work_id)
            self.db.execute("INSERT OR IGNORE INTO works VALUES(?,?,?)", (space_id, work_id, now))
            return dict(
                self.db.execute(
                    "SELECT * FROM works WHERE space_id=? AND work_id=?",
                    (space_id, work_id),
                ).fetchone()
            )

    def get_work(self, space_id: str, scope: Scope, work_id: str) -> Dict[str, Any]:
        with self.lock:
            result = dict(self._queries.work(space_id, scope, work_id))
            result["revision_attempts"] = self.db.execute(
                """SELECT count(*) FROM operations WHERE space_id=? AND work_id=?
                AND action='edit' AND state!='rejected'""",
                (space_id, work_id),
            ).fetchone()[0]
            return result

    @staticmethod
    def _snapshot(row):
        result = dict(row)
        result["params"] = json.loads(result["params_json"])
        return result

    def get_operation(self, space_id: str, scope: Scope, request_id: str) -> Dict[str, Any]:
        with self.lock:
            return self._snapshot(self._queries.operation(space_id, scope, request_id))

    def get_render_operation_by_job(self, space_id: str, scope: Scope, job_id: str) -> Dict[str, Any]:
        """Resolve a persisted render job without guessing among ambiguous bindings."""
        nonempty(job_id, "job_id")
        with self.lock:
            self._queries.space(space_id, scope)
            rows = self.db.execute(
                """SELECT * FROM operations WHERE space_id=? AND job_id=?
                AND category='render' AND action IN ('submit','edit') LIMIT 2""",
                (space_id, job_id),
            ).fetchall()
            if not rows:
                raise StoreError("NOT_FOUND", "Render job not found in this space")
            if len(rows) != 1:
                raise StoreError("REQUEST_CONFLICT", "Render job has ambiguous operation bindings")
            return self._snapshot(rows[0])

    def audit_operations(self, space_id: str, scope: Scope):
        with self.lock:
            self._queries.space(space_id, scope)
            return [
                self._snapshot(row)
                for row in self.db.execute(
                    "SELECT * FROM operations WHERE space_id=? ORDER BY id",
                    (space_id,),
                )
            ]

    def audit_authorizations(self, space_id: str, scope: Scope):
        with self.lock:
            self._queries.space(space_id, scope)
            return [
                dict(row)
                for row in self.db.execute(
                    "SELECT * FROM grants WHERE space_id=? ORDER BY version",
                    (space_id,),
                )
            ]

    def _usage(self, category, day, space_id=None):
        # Holds count on every day until definitive reconciliation; terminal
        # accepted failures consume their original attempt day, just like success.
        sql = """SELECT
            COALESCE(sum(state IN ('reserved','submitting','accepted','unknown')),0) AS held,
            COALESCE(sum(state IN ('succeeded','failed') AND budget_day=?),0) AS consumed
            FROM operations WHERE category=?"""
        args = [day, category]
        if space_id is not None:
            sql += " AND space_id=?"
            args.append(space_id)
        row = self.db.execute(sql, args).fetchone()
        return {"held": row["held"], "consumed": row["consumed"], "total": row["held"] + row["consumed"]}

    def budget_usage(self, space_id: str, scope: Scope):
        """Read only this space; global usage is deliberately not exposed."""
        _, day = self._uow.now()
        with self.lock:
            self._queries.space(space_id, scope)
            return {
                "day": day,
                **{
                    category: self._usage(category, day, space_id)
                    for category in ("think", "render", "share")
                },
            }

    @staticmethod
    def _share_group_id(group_id):
        if (not isinstance(group_id, str) or len(group_id) > 200
                or group_id != group_id.strip() or any(ord(char) < 32 for char in group_id)):
            raise StoreError("INVALID_ARGUMENT", "Invalid trusted group identifier")

    def _check_group_binding(self, space_id, scope, group_id):
        # Both collection and sharing freeze the same Host-verified target.
        # Check both registries so configuring them in either order is safe.
        for table in ("group_thought_policy", "group_share_policy"):
            own = self.db.execute(f"SELECT * FROM {table} WHERE space_id=?", (space_id,)).fetchone()
            if own is not None and (own["scope_snapshot"], own["group_id"]) != (scope.snapshot(), group_id):
                raise StoreError("OWNER_CONFLICT", "Group scope and target are immutable")
            collision = self.db.execute(
                f"SELECT 1 FROM {table} WHERE space_id!=? AND platform=? AND (stream_id=? OR group_id=?)",
                (space_id, scope.platform, scope.stream_id, group_id),
            ).fetchone()
            if collision is not None:
                raise StoreError("OWNER_CONFLICT", "Group target already belongs to another space")

    def configure_share_policy(
        self, space_id, scope, *, group_id, mode, expected_auth_version
    ) -> Dict[str, Any]:
        """Persist trusted collection/share permission; never accept a model target.

        Reapplying the same configuration keeps the epoch. Revoking and granting
        it again advances the epoch, so an old reservation cannot revive.
        """
        self._share_group_id(group_id)
        if (not isinstance(mode, str) or mode not in {"collect_only", "original_group", "private"}
                or type(expected_auth_version) is not int or expected_auth_version < 0):
            raise StoreError("INVALID_ARGUMENT", "Invalid share mode or authorization version")
        if bool(group_id) != (mode != "private"):
            raise StoreError("INVALID_ARGUMENT", "Only private mode has an empty group target")
        with self._uow.transaction():
            space = self._queries.space(space_id, scope)
            expected_authorization(space, expected_auth_version)
            self._check_group_binding(space_id, scope, group_id)
            if mode == "private":
                return {"mode": "private", "allowed": bool(space["authorized"])}
            old = self.db.execute(
                "SELECT * FROM group_share_policy WHERE space_id=?", (space_id,)
            ).fetchone()
            version = 1 if old is None else old["policy_version"] + int(
                (old["mode"], old["auth_version"]) != (mode, space["auth_version"])
            )
            self.db.execute(
                """INSERT INTO group_share_policy VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(space_id) DO UPDATE SET mode=excluded.mode,
                auth_version=excluded.auth_version,policy_version=excluded.policy_version""",
                (space_id, scope.snapshot(), scope.platform, scope.stream_id, group_id,
                 mode, space["auth_version"], version),
            )
            return self._sharing_policy(space_id, space)[0]

    def _sharing_policy(self, space_id, space):
        policy = self.db.execute(
            "SELECT * FROM group_share_policy WHERE space_id=?", (space_id,)
        ).fetchone()
        if policy is not None:
            return ({"mode": policy["mode"], "allowed": bool(
                policy["mode"] == "original_group" and space["authorized"]
                and policy["auth_version"] == space["auth_version"]
            )}, policy)
        known_group = self.db.execute(
            "SELECT 1 FROM group_thought_policy WHERE space_id=?", (space_id,)
        ).fetchone()
        # Schema-8 groups never acquire sharing permission merely by upgrading.
        if known_group is not None:
            return {"mode": "collect_only", "allowed": False}, None
        return {"mode": "private", "allowed": bool(space["authorized"])}, None

    def sharing_policy(self, space_id, scope) -> Dict[str, Any]:
        """Read-only, identity-free permission summary for planning and Studio."""
        with self._uow.transaction():
            return self._sharing_policy(space_id, self._queries.space(space_id, scope))[0]

    def _require_share_policy(self, space_id, space, operation=None):
        summary, policy = self._sharing_policy(space_id, space)
        if operation is not None:
            claim = self.db.execute(
                "SELECT policy_version FROM share_policy_claims WHERE operation_id=?", (operation["id"],)
            ).fetchone()
            # An old group reservation without an epoch is evidence, never a
            # fresh dispatch permit. Its state and budget hold remain intact.
            if (summary["mode"] != "private" or claim is not None) and (
                policy is None or claim is None or claim["policy_version"] != policy["policy_version"]
            ):
                raise StoreError("SHARE_POLICY_CHANGED", "Sharing permission changed since reservation")
        if not summary["allowed"]:
            code = ("SHARE_POLICY_CHANGED" if policy is not None
                    and policy["auth_version"] != space["auth_version"] else "SHARE_NOT_ALLOWED")
            raise StoreError(code, "Sharing is not currently authorized")
        return policy

    def check_share_target(self, space_id, scope, *, group_id) -> bool:
        """Validate a sender's Host-verified target against immutable storage."""
        self._share_group_id(group_id)
        with self._uow.transaction():
            space = self._queries.space(space_id, scope)
            targets = self.db.execute(
                """SELECT group_id FROM group_share_policy WHERE space_id=?
                UNION SELECT group_id FROM group_thought_policy WHERE space_id=?""", (space_id, space_id)
            ).fetchall()
            if ((not targets and group_id) or any(row["group_id"] != group_id for row in targets)):
                raise StoreError("SHARE_TARGET_MISMATCH", "Sender target does not match the bound group")
            require_authorized(space)
            self._require_share_policy(space_id, space)
            return True

    def _check_budget(self, space_id, category, day, *, adding, work_id=None, edit=False):
        for key, space_filter in (("*", None), (space_id, space_id)):
            row = self.db.execute("SELECT * FROM limits WHERE scope_key=?", (key,)).fetchone()
            if row is None:
                raise StoreError("BUDGET_EXCEEDED", "No budget policy configured")
            if self._usage(category, day, space_filter)["total"] + adding > row[category]:
                raise StoreError("BUDGET_EXCEEDED", "Daily category budget exceeded")
            sql = "SELECT count(*) FROM operations WHERE state IN ('reserved','submitting','accepted','unknown')"
            args = ()
            if space_filter is not None:
                sql += " AND space_id=?"
                args = (space_filter,)
            if self.db.execute(sql, args).fetchone()[0] + adding > row["max_inflight"]:
                raise StoreError("BUDGET_EXCEEDED", "Inflight operation budget exceeded")
            if edit:
                revisions = self.db.execute(
                    """SELECT count(*) FROM operations WHERE space_id=? AND work_id=?
                    AND action='edit' AND state!='rejected'""",
                    (space_id, work_id),
                ).fetchone()[0]
                if revisions + adding > row["max_revisions"]:
                    raise StoreError("REVISION_LIMIT", "Work revision limit exceeded")

    def _check_single_work_operation(self, space, scope, action, work_id, payload, *, excluding=-1):
        if action not in {"submit", "edit", "share"}:
            return
        space_id = space["space_id"]
        policy = self._single_work_policy(space_id, scope)
        if policy is None:
            return
        locked_work = policy["work_id"]
        if locked_work is None or (work_id != locked_work and not (action == "share" and work_id is None)):
            raise StoreError("SINGLE_WORK_LIMIT", "Operation must belong to the one permanent work")
        if self.db.execute(
            "SELECT 1 FROM operations WHERE space_id=? AND action=? AND id!=? LIMIT 1",
            (space_id, action, excluding),
        ).fetchone() is not None:
            raise StoreError("SINGLE_WORK_LIMIT", f"Permanent {action} allowance is already used")
        if action == "submit":
            return

        # The transport sends job and digest for edits, and job and version ID
        # for shares. Resolve both through the persisted version and its render.
        params = json.loads(payload)
        edit = action == "edit"
        if edit:
            target = (params.get("source_job_id"), params.get("source_sha256"))
            condition = "v.job_id=? AND v.sha256=? AND v.parent_id IS NULL AND o.action='submit'"
            code = "SINGLE_WORK_SOURCE"
        else:
            target = (params.get("job_id"), params.get("version_id"))
            condition = "v.job_id=? AND v.version_id=? AND o.action IN ('submit','edit')"
            code = "SINGLE_WORK_VERSION"
        if not all(isinstance(value, str) and value.strip() for value in target):
            raise StoreError(code, "Operation requires a persisted image source")
        version = self.db.execute(
            f"""SELECT v.version_id, v.work_id FROM versions AS v
            JOIN operations AS o ON o.space_id=v.space_id AND o.request_id=v.request_id
                AND o.work_id=v.work_id AND o.job_id=v.job_id
            WHERE v.space_id=? AND v.work_id=? AND o.scope_snapshot=? AND o.binding_id=?
                AND o.category='render' AND o.state='succeeded' AND {condition}""",
            (space_id, locked_work, space["scope"], space["binding_id"], *target),
        ).fetchone()
        if version is None:
            raise StoreError(code, "Image does not match a successful render of the permanent work")
        if not edit and self._reviews.accepted_review(space_id, scope, space, version) is None:
            raise StoreError("REVIEW_REQUIRED", "Sharing requires explicit visual acceptance")

    def reserve(
        self,
        space_id: str,
        scope: Scope,
        *,
        request_id: str,
        action: str,
        params: Dict[str, Any],
        work_id: Optional[str] = None,
        trigger: str = "idle",
        trigger_ref: Optional[str] = None,
        units: int = 1,
        expected_auth_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        nonempty(request_id, "request_id")
        if len(request_id) > 200 or action not in ACTIONS or type(units) is not int or units != 1:
            raise StoreError("INVALID_ARGUMENT", "Invalid action, request_id or units (must be 1)")
        if trigger not in {"chat", "idle", "completion", "recovery"}:
            raise StoreError("INVALID_ARGUMENT", "Unknown trigger")
        if trigger_ref is not None:
            nonempty(trigger_ref, "trigger_ref")
        payload = parameters(params)
        with self._uow.transaction():
            now, day = self._uow.now()
            space = self._queries.space(space_id, scope)
            expected_authorization(space, expected_auth_version)
            old = self.db.execute(
                "SELECT * FROM operations WHERE binding_id=? AND scope_snapshot=? AND request_id=?",
                (space["binding_id"], space["scope"], request_id),
            ).fetchone()
            if old:
                if (old["space_id"], old["action"], old["params_json"], old["work_id"]) != (
                    space_id,
                    action,
                    payload,
                    work_id,
                ):
                    raise StoreError("REQUEST_CONFLICT", "Request identity is already bound to other content")
                # Idempotent inspection never renews permission, retries or reserves again.
                return self._snapshot(old)
            require_authorized(space)
            if action in {"submit", "edit"} and work_id is None:
                raise StoreError("INVALID_ARGUMENT", "Render operations require a work_id")
            self._check_single_work_operation(space, scope, action, work_id, payload)
            if work_id is not None:
                work = self.db.execute(
                    "SELECT 1 FROM works WHERE space_id=? AND work_id=?",
                    (space_id, work_id),
                ).fetchone()
                if work is None:
                    raise StoreError("NOT_FOUND", "Work not found in this space")
            share_policy = None
            if action == "share":
                share_policy = self._require_share_policy(space_id, space)
                self._check_share(space_id, scope, params.get("job_id"))
            self._check_budget(
                space_id, ACTIONS[action], day, adding=1, work_id=work_id, edit=action == "edit"
            )
            if (
                action in {"submit", "edit"}
                and self.db.execute(
                    """SELECT 1 FROM operations WHERE space_id=? AND work_id=? AND category='render'
                AND state IN ('reserved','submitting','accepted','unknown') LIMIT 1""",
                    (space_id, work_id),
                ).fetchone()
            ):
                raise StoreError(
                    "WORK_BUSY", "Reconcile or finish this work's pending render before another attempt"
                )
            self.db.execute(
                """INSERT INTO operations(space_id,work_id,request_id,action,category,params_json,
                scope_snapshot,binding_id,auth_version,trigger,trigger_ref,state,budget_day,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'reserved',?,?,?)""",
                (
                    space_id,
                    work_id,
                    request_id,
                    action,
                    ACTIONS[action],
                    payload,
                    space["scope"],
                    space["binding_id"],
                    space["auth_version"],
                    trigger,
                    trigger_ref,
                    day,
                    now,
                    now,
                ),
            )
            operation = self._queries.operation(space_id, scope, request_id)
            if share_policy is not None:
                self.db.execute(
                    "INSERT INTO share_policy_claims VALUES(?,?)", (operation["id"], share_policy["policy_version"])
                )
            return self._snapshot(operation)

    def mark_submitting(self, space_id: str, scope: Scope, request_id: str):
        with self._uow.transaction():
            now, day = self._uow.now()
            row = self._queries.operation(space_id, scope, request_id)
            space = self._queries.space(space_id, scope)
            require_authorized(space)
            if row["state"] != "reserved":
                raise StoreError("INVALID_STATE", "Only a reserved operation can start once")
            if row["auth_version"] != space["auth_version"]:
                raise StoreError("AUTH_CHANGED", "Authorization changed since reservation")
            self._check_single_work_operation(
                space, scope, row["action"], row["work_id"], row["params_json"], excluding=row["id"]
            )
            if row["action"] == "share":
                self._require_share_policy(space_id, space, row)
            self._check_budget(
                space_id, row["category"], day, adding=0, work_id=row["work_id"], edit=row["action"] == "edit"
            )
            if row["action"] == "share":
                self._check_share(
                    space_id, scope, json.loads(row["params_json"]).get("job_id"), excluding=row["id"]
                )
            self.db.execute(
                "UPDATE operations SET state='submitting',budget_day=?,updated_at=? WHERE id=?",
                (day, now, row["id"]),
            )
            return self._snapshot(self._queries.operation(space_id, scope, request_id))

    def validate_submitting(self, space_id: str, scope: Scope, request_id: str) -> Dict[str, Any]:
        """Read-only dispatch check in one snapshot; never renew or refund a claim."""
        with self._uow.transaction():
            _, day = self._uow.now()
            row = self._queries.operation(space_id, scope, request_id)
            space = self._queries.space(space_id, scope)
            if row["state"] != "submitting" or row["job_id"] is not None:
                raise StoreError("INVALID_STATE", "Dispatch requires an unbound submitting operation")
            require_authorized(space)
            if row["auth_version"] != space["auth_version"]:
                raise StoreError("AUTH_CHANGED", "Authorization changed since reservation")
            if row["budget_day"] != day:
                raise StoreError("BUDGET_DAY_CHANGED", "Dispatch cannot cross its recorded budget day")
            self._check_single_work_operation(
                space, scope, row["action"], row["work_id"], row["params_json"], excluding=row["id"]
            )
            if row["action"] == "share":
                self._require_share_policy(space_id, space, row)
            self._check_budget(
                space_id, row["category"], day, adding=0, work_id=row["work_id"], edit=row["action"] == "edit"
            )
            if row["action"] == "share":
                # 历史锁可能在预留之后导入；真正发送前重查，排除自己的提交行。
                self._check_share(
                    space_id, scope, json.loads(row["params_json"]).get("job_id"), excluding=row["id"]
                )
            return self._snapshot(row)

    def _update(self, row, state, evidence, job_id=None):
        now, _ = self._uow.now()
        if job_id is not None:
            nonempty(job_id, "job_id")
        if row["job_id"] is not None and job_id not in (None, row["job_id"]):
            raise StoreError("REQUEST_CONFLICT", "An operation cannot switch external job")
        self.db.execute(
            "UPDATE operations SET state=?,evidence=?,job_id=COALESCE(job_id,?),updated_at=? WHERE id=?",
            (state, evidence, job_id, now, row["id"]),
        )

    def mark_accepted(self, space_id: str, scope: Scope, request_id: str, *, job_id=None):
        with self._uow.transaction():
            row = self._queries.operation(space_id, scope, request_id)
            if row["state"] not in {"submitting", "accepted"}:
                raise StoreError("INVALID_STATE", "Acceptance requires an in-progress submission")
            if row["category"] == "render" and job_id is None:
                raise StoreError("INVALID_ARGUMENT", "Render acceptance requires the original job_id")
            self._update(row, "accepted", "accepted response", job_id)

    def mark_unknown(self, space_id: str, scope: Scope, request_id: str, *, reason: str):
        nonempty(reason, "reason")
        with self._uow.transaction():
            row = self._queries.operation(space_id, scope, request_id)
            if row["state"] not in {"submitting", "accepted", "unknown"}:
                raise StoreError("INVALID_STATE", "Only an attempted operation can become unknown")
            self._update(row, "unknown", reason)

    def reject(self, space_id: str, scope: Scope, request_id: str, *, code: str, evidence: str):
        nonempty(code, "code")
        nonempty(evidence, "evidence")
        with self._uow.transaction():
            row = self._queries.operation(space_id, scope, request_id)
            if code == "REQUEST_CONFLICT":
                if row["state"] in {"reserved", "submitting", "accepted", "unknown"}:
                    self._update(row, "unknown", evidence)
                # A conflict is never proof of nonacceptance or a refund.
                raise_error = True
            else:
                if code not in PREACCEPT_REJECTIONS:
                    raise StoreError(
                        "INVALID_ARGUMENT", "Only definitive preaccept rejection codes may refund"
                    )
                if row["state"] != "submitting":
                    raise StoreError("INVALID_STATE", "Accepted or unknown operations cannot be rejected")
                self._update(row, "rejected", f"{code}: {evidence}")
                raise_error = False
        if raise_error:
            raise StoreError("REQUEST_CONFLICT", "Keep reservation and reconcile the original request")

    def cancel_reservation(self, space_id: str, scope: Scope, request_id: str, *, reason: str):
        """Local cancellation is safe only before the first external attempt."""
        nonempty(reason, "reason")
        with self._uow.transaction():
            row = self._queries.operation(space_id, scope, request_id)
            if row["state"] != "reserved":
                raise StoreError("INVALID_STATE", "Only an unstarted reservation may be cancelled")
            self._update(row, "rejected", f"local cancellation: {reason}")

    def settle(
        self, space_id: str, scope: Scope, request_id: str, *, outcome: str, evidence: str, job_id=None
    ):
        if outcome not in CONSUMED:
            raise StoreError("INVALID_ARGUMENT", "Terminal outcome must be succeeded or failed")
        nonempty(evidence, "evidence")
        with self._uow.transaction():
            row = self._queries.operation(space_id, scope, request_id)
            if row["state"] not in {"submitting", "accepted", outcome}:
                raise StoreError(
                    "INVALID_STATE", "Unknown outcomes must be reconciled; reserved is not attempted"
                )
            if row["category"] == "render" and job_id is None and row["job_id"] is None:
                raise StoreError("INVALID_ARGUMENT", "Render settlement must bind the original job_id")
            self._update(row, outcome, evidence, job_id)

    def reconcile(
        self,
        space_id: str,
        scope: Scope,
        request_id: str,
        *,
        outcome: str,
        evidence: str,
        job_id=None,
    ):
        if outcome not in {"accepted", "succeeded", "failed"}:
            raise StoreError("INVALID_ARGUMENT", "Lookup not_found cannot refund or authorize resubmission")
        nonempty(evidence, "evidence")
        with self._uow.transaction():
            row = self._queries.operation(space_id, scope, request_id)
            if row["state"] != "unknown":
                raise StoreError("INVALID_STATE", "Reconciliation only resolves unknown operations")
            if row["category"] == "render" and job_id is None and row["job_id"] is None:
                raise StoreError("INVALID_ARGUMENT", "Render reconciliation must bind the original job_id")
            self._update(row, outcome, evidence, job_id)

    def recover_inflight(self, space_id: str, scope: Scope, *, offline: bool = False) -> int:
        """Explicit recovery after the previous worker has stopped, never on open."""
        if offline is not True:
            raise StoreError("OFFLINE_REQUIRED", "Stop previous workers before recovery")
        now, _ = self._uow.now()
        with self._uow.transaction():
            self._queries.space(space_id, scope)
            return self.db.execute(
                """UPDATE operations SET state='unknown',evidence='explicit offline recovery',updated_at=?
                WHERE space_id=? AND state IN ('submitting','accepted')""",
                (now, space_id),
            ).rowcount

    def legacy_lock(self, space_id: str, scope: Scope, job_id: str) -> bool:
        """Conservative global lock; never exposes legacy ownerless record contents."""
        nonempty(job_id, "job_id")
        with self.lock:
            self._queries.space(space_id, scope)
            return self.db.execute("SELECT 1 FROM legacy_locks WHERE job=?", (job_id,)).fetchone() is not None

    def _check_share(self, space_id, scope, job_id, *, excluding=-1):
        nonempty(job_id, "job_id")
        if self.legacy_lock(space_id, scope, job_id):
            raise StoreError(
                "LEGACY_LOCK", "Legacy delivery has no trustworthy target; automatic sharing blocked"
            )
        owned = self.db.execute(
            "SELECT 1 FROM operations WHERE space_id=? AND category='render' AND job_id=? LIMIT 1",
            (space_id, job_id),
        ).fetchone()
        if owned is None:
            raise StoreError("NOT_FOUND", "No render job binding in this space")
        old = self.db.execute(
            """SELECT 1 FROM operations WHERE space_id=? AND action='share' AND state!='rejected'
            AND json_extract(params_json,'$.job_id')=? AND id!=? LIMIT 1""",
            (space_id, job_id, excluding),
        ).fetchone()
        if old:
            raise StoreError("SHARE_LOCK", "This job already has a sharing attempt in this space")

    def check_share(self, space_id: str, scope: Scope, job_id: str) -> bool:
        with self.lock:
            space = self._queries.space(space_id, scope)
            require_authorized(space)
            self._require_share_policy(space_id, space)
            self._check_share(space_id, scope, job_id)
            return True

    def migrate_legacy_deliveries(
        self, source: Path, backup: Path, *, offline: bool = False
    ) -> Dict[str, Any]:
        """Trusted maintenance only. All legacy writers MUST already be stopped.

        A read transaction blocks rollback-journal commits. data_version is also
        checked after releasing it (including WAL). This detects races but cannot
        replace the offline prerequisite or prevent a writer starting afterwards.
        """
        if offline is not True:
            raise StoreError("OFFLINE_REQUIRED", "Stop legacy writers before migrating")
        source, backup = Path(source).resolve(), Path(backup).resolve()
        if not source.is_file():
            raise StoreError("LEGACY_MISSING", "Legacy database must already exist")
        if source == self.path.resolve() or backup in {source, self.path.resolve()}:
            raise StoreError("INVALID_ARGUMENT", "Source, backup and target must be distinct")
        # mode=ro prevents sqlite from creating a missing source; do not use
        # immutable=1, which would ignore WAL data and SQLite coordination.
        try:
            reader = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=1)
        except sqlite3.DatabaseError as exc:
            raise StoreError("LEGACY_INVALID", "Legacy database cannot be opened read-only") from exc
        reader.row_factory = sqlite3.Row
        failure_code = "LEGACY_INVALID"
        try:
            version_before = reader.execute("PRAGMA data_version").fetchone()[0]
            reader.execute("BEGIN")
            rows = self._legacy_rows(reader)
            digest = hashlib.sha256(canonical_json(rows).encode("utf-8")).hexdigest()
            failure_code = "MIGRATION_FAILED"
            backup.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation is atomic and deliberately never replaces a file.
            descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            target = sqlite3.connect(backup)
            try:
                reader.backup(target)
            finally:
                target.close()
            failure_code = "LEGACY_INVALID"
            reader.execute("ROLLBACK")
            if reader.execute("PRAGMA data_version").fetchone()[0] != version_before:
                raise StoreError("LEGACY_CHANGED", "Legacy writer changed the source during migration")
            now, _ = self._uow.now()
            failure_code = "MIGRATION_FAILED"
            with self._uow.transaction():
                prior = self.db.execute(
                    "SELECT * FROM legacy_migrations WHERE source_digest=?",
                    (digest,),
                ).fetchone()
                for item in rows:
                    state = "send_unknown" if item["state"] == "sending" else item["state"]
                    self.db.execute(
                        "INSERT OR IGNORE INTO legacy_locks VALUES(?,?,?,?)",
                        (item["job"], item["state"], state, digest),
                    )
                self.db.execute(
                    "INSERT OR IGNORE INTO legacy_migrations VALUES(?,?,?,?,?)",
                    (digest, str(source), str(backup), len(rows), now),
                )
                return {
                    "rows": len(rows),
                    "source_digest": digest,
                    "already_imported": prior is not None,
                    "backup_path": str(backup),
                    "schema_version": SCHEMA_VERSION,
                }
        except sqlite3.DatabaseError as exc:
            message = (
                "Corrupt or unreadable legacy database"
                if failure_code == "LEGACY_INVALID"
                else "Legacy backup or target import failed; no import committed"
            )
            raise StoreError(failure_code, message) from exc
        finally:
            reader.close()

    @staticmethod
    def _legacy_rows(db):
        if db.execute("PRAGMA quick_check").fetchall()[0][0] != "ok":
            raise StoreError("LEGACY_INVALID", "Legacy integrity check failed")
        if db.execute("PRAGMA user_version").fetchone()[0] != 0:
            raise StoreError("SCHEMA_UNSUPPORTED", "Unknown legacy schema version")
        objects = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        if [(item["type"], item["name"]) for item in objects] != [("table", "delivery")]:
            raise StoreError("SCHEMA_UNSUPPORTED", "Unexpected legacy schema objects")
        columns = db.execute("PRAGMA table_info(delivery)").fetchall()
        signature = [(item["name"], item["type"].upper(), item["notnull"], item["pk"]) for item in columns]
        if signature != [("job", "TEXT", 0, 1), ("state", "TEXT", 1, 0)]:
            raise StoreError("SCHEMA_UNSUPPORTED", "Unknown legacy delivery columns")
        rows = [dict(row) for row in db.execute("SELECT job,state FROM delivery ORDER BY job")]
        for row in rows:
            nonempty(row["job"], "legacy job")
            if row["state"] not in {"sending", "send_unknown", "sent", "send_failed"}:
                raise StoreError("LEGACY_INVALID", "Unknown legacy delivery state")
        return rows

    def enqueue_wake(self, space_id, scope, *, event_id, kind, text="", source_ref=None) -> bool:
        return self._wakes.enqueue_wake(space_id, scope, event_id=event_id, kind=kind, text=text, source_ref=source_ref)

    def schedule_wake(self, space_id, scope, *, interval_seconds) -> bool:
        return self._wakes.schedule_wake(space_id, scope, interval_seconds=interval_seconds)

    def schedule_progress_wake(self, space_id, scope, *, interval_seconds=15) -> bool:
        """Advance existing work without shortening the idle inspiration timer.

        State, queue and claim checks share the same transaction. The durable
        event key bounds repeated receipt polling across workers and restarts;
        a phase change can proceed on the next Host tick. No receipt is invented
        and no model/drawing/sending operation is dispatched here.
        """
        return self._wakes.schedule_progress_wake(space_id, scope, interval_seconds=interval_seconds)

    def restore_planned_wait(
        self,
        space_id: str,
        scope: Scope,
        *,
        expected_auth_version: int,
        work_id: str,
        last_wait_request_id: str,
        event_id: str,
        reason: str,
        offline: bool = False,
    ) -> bool:
        """Trusted offline repair of one proven model wait, without renewing consent.

        The caller MUST stop Host writers and hold their RuntimeLease throughout
        this call. Store does not acquire or steal that lease. A fresh explicit
        grant is still needed before the internal idle event can be consumed.
        The event's JSON text is the durable audit receipt; it is not chat or a
        completion claim. Repeating its identical key never resets newer timers.
        No work, authorization, operation, allowance or usage row is changed.
        """
        return self._wakes.restore_planned_wait(
            space_id, scope, expected_auth_version=expected_auth_version, work_id=work_id,
            last_wait_request_id=last_wait_request_id, event_id=event_id, reason=reason, offline=offline,
        )

    def claim_wake(self, space_id, scope, *, token):
        return self._wakes.claim_wake(space_id, scope, token=token)

    def finish_wake(self, space_id, scope, *, token, acknowledge=True, retry_after=0) -> None:
        self._wakes.finish_wake(space_id, scope, token=token, acknowledge=acknowledge, retry_after=retry_after)

    def recover_artist(self, space_id, scope, *, offline=False):
        return self._wakes.recover_artist(space_id, scope, offline=offline)

    @staticmethod
    def _string_list(values, field):
        if not isinstance(values, list):
            raise StoreError("INVALID_ARGUMENT", f"{field} must be a list")
        for value in values:
            nonempty(value, field)
        return list(dict.fromkeys(values))

    def remember_inspiration(
        self,
        space_id,
        scope,
        *,
        key,
        summary,
        source_refs,
        ttl_seconds=604800,
        origin="chat",
        expected_auth_version=None,
    ):
        nonempty(key, "key")
        nonempty(summary, "summary")
        if key.startswith("group-thought:"):
            raise StoreError("INVALID_ARGUMENT", "Group thought keys are reserved for the trusted synchronizer")
        refs = self._string_list(source_refs, "source_refs")
        if origin not in {"chat", "idle", "completion"} or (origin == "chat" and not refs):
            raise StoreError("INVALID_ARGUMENT", "Chat inspirations require verified source references")
        if type(ttl_seconds) is not int or not 0 < ttl_seconds <= 365 * 86400:
            raise StoreError("INVALID_ARGUMENT", "ttl_seconds must be a positive bounded integer")
        with self._uow.transaction():
            space = self._queries.space(space_id, scope)
            expected_authorization(space, expected_auth_version)
            require_authorized(space)
            now, _ = self._uow.now()
            expires = (datetime.fromisoformat(now) + timedelta(seconds=ttl_seconds)).isoformat()
            self.db.execute("DELETE FROM inspirations WHERE space_id=? AND expires_at<=?", (space_id, now))
            old = self.db.execute(
                "SELECT * FROM inspirations WHERE space_id=? AND key=?", (space_id, key)
            ).fetchone()
            if old:
                if old["origin"] != origin:
                    raise StoreError("REQUEST_CONFLICT", "Inspiration provenance cannot change origin")
                refs = list(dict.fromkeys(json.loads(old["source_refs"]) + refs))
            self.db.execute(
                """INSERT INTO inspirations VALUES(?,?,?,?,?,?,NULL,?) ON CONFLICT(space_id,key) DO UPDATE SET
                summary=excluded.summary,source_refs=excluded.source_refs,
                expires_at=excluded.expires_at,updated_at=excluded.updated_at""",
                (space_id, key, summary, canonical_json(refs), origin, expires, now),
            )
            self.db.execute(
                """DELETE FROM inspirations WHERE space_id=? AND key NOT IN
                            (SELECT key FROM inspirations WHERE space_id=? ORDER BY updated_at DESC,rowid DESC
                             LIMIT 100)""",
                (space_id, space_id),
            )
            return decode_fields(
                self.db.execute(
                    "SELECT * FROM inspirations WHERE space_id=? AND key=?", (space_id, key)
                ).fetchone(),
                "source_refs",
            )

    def sync_group_thoughts(
        self, space_id, scope, *, group_id, thoughts, enabled, expected_auth_version
    ) -> bool:
        """Import trusted notebook snapshots; never call with model-authored identity.

        A note is offered at most once, independently of the ordinary inspiration
        cache. Its adoption and the resulting work are committed together.
        """
        if (
            not isinstance(group_id, str) or len(group_id) > 200 or group_id != group_id.strip()
            or any(ord(char) < 32 for char in group_id)
            or type(enabled) is not bool
            or type(expected_auth_version) is not int or expected_auth_version < 0
            or not isinstance(thoughts, list) or len(thoughts) > 128
        ):
            raise StoreError("INVALID_ARGUMENT", "Invalid bounded group thought snapshot or authorization")
        notes = {}
        for thought in thoughts:
            if not isinstance(thought, dict) or set(thought) != {"note_id", "summary"}:
                raise StoreError("INVALID_ARGUMENT", "Thoughts contain only note_id and summary")
            note_id, summary = thought["note_id"], thought["summary"]
            if (
                not isinstance(note_id, str) or len(note_id) != 67 or not note_id.startswith("gh_")
                or any(char not in "0123456789abcdef" for char in note_id[3:])
                or not isinstance(summary, str) or not summary.strip() or summary != summary.strip()
                or len(summary) > 512 or any(ord(char) < 32 for char in summary)
            ):
                raise StoreError("INVALID_ARGUMENT", "Invalid thought identifier or summary")
            if note_id in notes and notes[note_id] != summary:
                raise StoreError("REQUEST_CONFLICT", "A thought snapshot cannot change its summary")
            notes[note_id] = summary
        with self._uow.transaction():
            space = self._queries.space(space_id, scope)
            expected_authorization(space, expected_auth_version)
            require_authorized(space)
            if not group_id:
                if enabled or thoughts:
                    raise StoreError("INVALID_ARGUMENT", "Private spaces cannot import group thoughts")
                self.db.execute(
                    "UPDATE group_thought_policy SET enabled=0,auth_version=? WHERE space_id=?",
                    (space["auth_version"], space_id),
                )
                return False
            self._check_group_binding(space_id, scope, group_id)
            policy = self.db.execute(
                "SELECT * FROM group_thought_policy WHERE space_id=?", (space_id,)
            ).fetchone()
            if policy is not None and (policy["scope_snapshot"], policy["group_id"]) != (scope.snapshot(), group_id):
                raise StoreError("OWNER_CONFLICT", "Group thought scope and group are immutable")
            collision = self.db.execute(
                """SELECT 1 FROM group_thought_policy WHERE space_id!=? AND platform=?
                    AND (stream_id=? OR group_id=?)""",
                (space_id, scope.platform, scope.stream_id, group_id),
            ).fetchone()
            if collision is not None:
                raise StoreError("OWNER_CONFLICT", "Group thought binding already belongs to another space")
            self.db.execute(
                """INSERT INTO group_thought_policy VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(space_id) DO UPDATE SET enabled=excluded.enabled,
                    auth_version=excluded.auth_version""",
                (space_id, scope.snapshot(), scope.platform, scope.stream_id, group_id,
                 int(enabled), space["auth_version"]),
            )
            existing = {row["note_id"]: row for row in self.db.execute(
                "SELECT * FROM group_thought_links WHERE space_id=?", (space_id,)
            )}
            if len(existing.keys() | notes.keys()) > 128:
                raise StoreError("INVALID_ARGUMENT", "Group thought capacity is 128 persistent notes")
            now, _ = self._uow.now()
            added = False
            for note_id, summary in notes.items():
                collision = self.db.execute(
                    "SELECT 1 FROM inspirations WHERE space_id=? AND key=?",
                    (space_id, "group-thought:" + note_id),
                ).fetchone()
                if collision is not None:
                    raise StoreError("REQUEST_CONFLICT", "Reserved thought key exists in ordinary inspirations")
                owner = self.db.execute(
                    "SELECT space_id FROM group_thought_links WHERE note_id=?", (note_id,)
                ).fetchone()
                if owner is not None and owner["space_id"] != space_id:
                    raise StoreError("OWNER_CONFLICT", "Thought already belongs to another space")
                if note_id in existing:
                    if existing[note_id]["summary"] != summary or existing[note_id]["group_id"] != group_id:
                        raise StoreError("REQUEST_CONFLICT", "Thought snapshots are immutable")
                    continue
                self.db.execute(
                    """INSERT INTO group_thought_links(space_id,note_id,inspiration_key,summary,
                        group_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)""",
                    (space_id, note_id, "group-thought:" + note_id, summary, group_id, now, now),
                )
                if enabled:
                    self._wakes.enqueue_in_transaction(space_id, "thought:" + note_id, "completion", "", None)
                added = True
            return added

    def _available_group_thoughts(self, space_id):
        return self.db.execute(
            """SELECT link.* FROM group_thought_links link
                JOIN group_thought_policy policy ON policy.space_id=link.space_id
                JOIN spaces ON spaces.space_id=link.space_id
                WHERE link.space_id=? AND link.adopted_by IS NULL AND policy.enabled=1
                    AND spaces.authorized=1 AND policy.auth_version=spaces.auth_version
                    AND policy.scope_snapshot=spaces.scope AND link.group_id=policy.group_id
                ORDER BY link.updated_at,link.inspiration_key""", (space_id,)
        ).fetchall()

    def list_inspirations(self, space_id, scope):
        with self.lock:
            require_authorized(self._queries.space(space_id, scope))
            now, _ = self._uow.now()
            ideas = [
                decode_fields(row, "source_refs")
                for row in self.db.execute(
                    "SELECT * FROM inspirations WHERE space_id=? AND expires_at>? ORDER BY updated_at,key",
                    (space_id, now),
                )
            ]
            ideas.extend(
                {"space_id": space_id, "key": row["inspiration_key"], "summary": row["summary"],
                 "source_refs": [row["note_id"]], "origin": "group_thought", "expires_at": None,
                 "consumed_by": None, "updated_at": row["updated_at"]}
                for row in self._available_group_thoughts(space_id)
            )
            return sorted(ideas, key=lambda idea: (idea["updated_at"], idea["key"]))

    def _check_active_work(self, space_id, work_id):
        row = self.db.execute(
            """SELECT 1 FROM artist_works WHERE space_id=? AND work_id!=?
                              AND status IN ('planned','deferred','creating','review_pending') LIMIT 1""",
            (space_id, work_id),
        ).fetchone()
        if row:
            raise StoreError("WORK_BUSY", "This space already has an active artist work")

    def configure_creative(self, space_id, scope, *, profiles, series, expected_auth_version):
        """Trusted Host configuration: synchronize visibility, never grant authority.

        Old revisions and closed series are durable history. A removed item can
        be configured again, but closing a series is irreversible through sync.
        """
        validate_catalog(profiles, series)
        profiles, series = json.loads(canonical_json([profiles, series]))
        with self._uow.transaction():
            space = self._queries.space(space_id, scope)
            expected_authorization(space, expected_auth_version)
            require_authorized(space)
            self._configure_creative(space_id, profiles, series, space["auth_version"])
            validate_catalog(self.list_profiles(space_id, scope), self.list_series(space_id, scope), allow_revisions=True)
            return {"profiles": self.list_profiles(space_id, scope), "series": self.list_series(space_id, scope)}

    def _configure_creative(self, space_id, profiles, series, auth_version):
        # Explicit legacy sync only owns legacy/configured data. Model-created
        # identities and the revisions pinned by their series survive it.
        self.db.execute("""UPDATE profile_versions SET active=0 WHERE space_id=? AND NOT EXISTS (
            SELECT 1 FROM creative_sources c WHERE c.space_id=profile_versions.space_id
            AND c.kind='profile' AND c.entity_id=profile_versions.profile_id AND c.origin='autonomous')""", (space_id,))
        self.db.execute("""UPDATE series SET active=0 WHERE space_id=? AND NOT EXISTS (
            SELECT 1 FROM creative_sources c WHERE c.space_id=series.space_id
            AND c.kind='series' AND c.entity_id=series.series_id AND c.origin='autonomous')""", (space_id,))
        for profile in profiles:
            identity = (space_id, profile["profile_id"], profile["revision"])
            data = canonical_json(profile["data"])
            old = self.db.execute(
                "SELECT data FROM profile_versions WHERE space_id=? AND profile_id=? AND revision=?",
                identity,
            ).fetchone()
            if old is not None and old["data"] != data:
                raise StoreError("REQUEST_CONFLICT", "Profile revision data is immutable")
            self.db.execute(
                """INSERT INTO profile_versions(space_id,profile_id,revision,data,active) VALUES(?,?,?,?,1)
                ON CONFLICT(space_id,profile_id,revision) DO UPDATE SET active=1""", (*identity, data),
            )
            self.db.execute("INSERT OR IGNORE INTO creative_sources VALUES(?,?,?,?,?,?,?,?)",
                            (space_id, "profile", profile["profile_id"], profile["revision"],
                             "configured", "host-config", auth_version, canonical_json(profile)))
        for item in series:
            old = self.db.execute(
                "SELECT title,goal,profile_id,profile_revision FROM series WHERE space_id=? AND series_id=?",
                (space_id, item["series_id"]),
            ).fetchone()
            if old is not None and (old["title"], old["goal"], old["profile_id"], old["profile_revision"]) != (
                item["title"], item["goal"], item["profile_id"], item["profile_revision"],
            ):
                raise StoreError("REQUEST_CONFLICT", "Series title, goal and profile bindings are immutable")
            self.db.execute(
                """INSERT INTO series(space_id,series_id,title,goal,profile_id,profile_revision,active)
                VALUES(?,?,?,?,?,?,1) ON CONFLICT(space_id,series_id) DO UPDATE SET active=1""",
                (space_id, item["series_id"], item["title"], item["goal"],
                 item["profile_id"], item["profile_revision"]),
            )
            self.db.execute("INSERT OR IGNORE INTO creative_sources VALUES(?,?,?,?,?,?,?,?)",
                            (space_id, "series", item["series_id"], 1, "configured", "host-config",
                             auth_version, canonical_json(item)))

    def import_creative(self, space_id, scope, *, profiles, series, expected_auth_version):
        """Host reload imports old configuration once; it never synchronizes ownership."""
        with self._uow.transaction():
            space = self._queries.space(space_id, scope)
            expected_authorization(space, expected_auth_version)
            require_authorized(space)
            if self.db.execute("SELECT 1 FROM creative_imports WHERE space_id=?", (space_id,)).fetchone():
                return False
            # Existing schema-4/6 catalogues already represent an import, including
            # disabled entries and closed series. Do not revive them on upgrade.
            existing = self.db.execute("SELECT 1 FROM creative_sources WHERE space_id=? LIMIT 1", (space_id,)).fetchone()
            if not existing:
                validate_catalog(profiles, series)
                profiles, series = json.loads(canonical_json([profiles, series]))
                self._configure_creative(space_id, profiles, series, space["auth_version"])
            snapshot = canonical_json({
                "mode": "preserved_existing" if existing else "imported_config",
                "profiles": [dict(row) for row in self.db.execute("SELECT * FROM profile_versions WHERE space_id=? ORDER BY profile_id,revision", (space_id,))],
                "series": [dict(row) for row in self.db.execute("SELECT * FROM series WHERE space_id=? ORDER BY series_id", (space_id,))],
            })
            self.db.execute("INSERT INTO creative_imports VALUES(?,?,?,?,?)", (
                space_id, hashlib.sha256(snapshot.encode()).hexdigest(), snapshot,
                space["auth_version"], self._uow.now()[0],
            ))
            return True

    def list_profiles(self, space_id, scope) -> list[dict]:
        with self.lock:
            require_authorized(self._queries.space(space_id, scope))
            return [
                decode_fields(row, "data") for row in self.db.execute(
                    """SELECT profile_id,revision,data FROM profile_versions p WHERE space_id=? AND
                    (active=1 OR EXISTS (SELECT 1 FROM series s WHERE s.space_id=p.space_id
                    AND s.profile_id=p.profile_id AND s.profile_revision=p.revision AND s.active=1 AND s.closed=0))
                    ORDER BY profile_id,revision""", (space_id,),
                )
            ]

    def creative_catalog(self, space_id, scope) -> dict:
        """Freeze both visible collections together, including across connections."""
        with self._uow.transaction():
            return {"profiles": self.list_profiles(space_id, scope), "series": self.list_series(space_id, scope)}

    def list_series(self, space_id, scope) -> list[dict]:
        with self.lock:
            require_authorized(self._queries.space(space_id, scope))
            return [
                dict(row) for row in self.db.execute(
                    """SELECT s.series_id,s.title,s.goal,s.profile_id,s.profile_revision FROM series AS s
                    JOIN profile_versions AS p ON p.space_id=s.space_id AND p.profile_id=s.profile_id
                    AND p.revision=s.profile_revision
                    WHERE s.space_id=? AND s.active=1 AND s.closed=0 ORDER BY s.series_id""",
                    (space_id,),
                )
            ]

    def apply_catalog_decision(self, space_id, scope, *, request_id, expected_auth_version):
        """Apply only a paid, successful plan's frozen evidence, atomically and once."""
        with self._uow.transaction():
            space = self._queries.space(space_id, scope)
            expected_authorization(space, expected_auth_version)
            require_authorized(space)
            op = self._queries.operation(space_id, scope, request_id)
            if (op["action"], op["category"], op["state"], op["auth_version"], op["binding_id"], op["scope_snapshot"]) != (
                "think", "think", "succeeded", space["auth_version"], space["binding_id"], space["scope"],
            ):
                raise StoreError("AUTH_CHANGED", "Catalogue advice requires a successful current authorized thought")
            params, answer = json.loads(op["params_json"]), json.loads(op["evidence"])
            if params.get("task") != "plan":
                raise StoreError("INVALID_DECISION", "Only planning may change the creative catalogue")
            context, decision = params["context"], answer["decision"]
            payload = canonical_json(decision)
            prior = self.db.execute("SELECT * FROM creative_decisions WHERE space_id=? AND request_id=?",
                                    (space_id, request_id)).fetchone()
            if prior:
                if prior["decision"] != payload or prior["operation_id"] != op["id"]:
                    raise StoreError("REQUEST_CONFLICT", "Catalogue receipt no longer matches its thought")
                return json.loads(prior["result"])
            validate_catalog_decision(decision, context.get("profiles", []), context.get("series", []))
            validate_catalog_decision(decision, self.list_profiles(space_id, scope), self.list_series(space_id, scope))
            action = decision["action"]
            generated = hashlib.sha256(canonical_json([space_id, request_id]).encode()).hexdigest()[:40]
            source = {"decision": decision, "context": context}
            if action in {"create_profile", "revise_profile"}:
                identifier, revision = "profile:" + generated, 1
                if action == "revise_profile":
                    identifier = decision["profile_id"]
                    latest = self.db.execute("SELECT max(revision),count(*) FROM profile_versions WHERE space_id=? AND profile_id=?",
                                             (space_id, identifier)).fetchone()
                    if latest[0] != decision["base_revision"]:
                        raise StoreError("REQUEST_CONFLICT", "Profile has changed since planning")
                    if latest[1] >= MAX_PROFILE_VERSIONS:
                        raise StoreError("CATALOG_LIMIT", "Profile history limit reached")
                    revision = latest[0] + 1
                    self.db.execute("UPDATE profile_versions SET active=0 WHERE space_id=? AND profile_id=?",
                                    (space_id, identifier))
                result = {"profile_id": identifier, "revision": revision, "data": decision["data"]}
                self.db.execute("INSERT INTO profile_versions VALUES(?,?,?,?,1)",
                                (space_id, identifier, revision, canonical_json(decision["data"])))
                kind = "profile"
            else:
                identifier, revision, kind = "series:" + generated, 1, "series"
                goal = normalize_objective("series", decision["goal"])
                if any(normalize_objective("series", row[0]) == goal for row in self.db.execute(
                    "SELECT goal FROM series WHERE space_id=?", (space_id,),
                )):
                    raise StoreError("REQUEST_CONFLICT", "This series goal already exists, including closed history")
                result = {key: decision[key] for key in ("title", "goal", "profile_id", "profile_revision")}
                result["series_id"] = identifier
                self.db.execute("INSERT INTO series VALUES(?,?,?,?,?,?,1,0)", (
                    space_id, identifier, result["title"], result["goal"], result["profile_id"], result["profile_revision"],
                ))
            self.db.execute("INSERT INTO creative_sources VALUES(?,?,?,?,?,?,?,?)", (
                space_id, kind, identifier, revision, "autonomous", request_id,
                space["auth_version"], canonical_json(source),
            ))
            validate_catalog(self.list_profiles(space_id, scope), self.list_series(space_id, scope), allow_revisions=True)
            self.db.execute("INSERT INTO creative_decisions VALUES(?,?,?,?,?)",
                            (space_id, request_id, op["id"], payload, canonical_json(result)))
            return result

    def close_series(self, space_id, scope, series_id, expected_auth_version):
        nonempty(series_id, "series_id")
        with self._uow.transaction():
            space = self._queries.space(space_id, scope)
            expected_authorization(space, expected_auth_version)
            require_authorized(space)
            old = self.db.execute(
                "SELECT 1 FROM series WHERE space_id=? AND series_id=?", (space_id, series_id)
            ).fetchone()
            if old is None:
                raise StoreError("NOT_FOUND", "Series not found in this space")
            self.db.execute("UPDATE series SET closed=1 WHERE space_id=? AND series_id=?", (space_id, series_id))

    def _freeze_creative(self, space_id, scope, creative):
        profiles, series = self.list_profiles(space_id, scope), self.list_series(space_id, scope)
        selected = validate_choice(creative, profiles, series)
        profile = next(item for item in profiles if (item["profile_id"], item["revision"]) ==
                       (selected["profile_id"], selected["profile_revision"]))
        item = next((item for item in series if item["series_id"] == selected["series_id"]), None)
        return {**selected, "profile": profile["data"], "series": item}

    def get_asset(self, space_id, scope, asset_id):
        """Scoped historical metadata; no image bytes or authorization renewal."""
        with self.lock:
            self._queries.space(space_id, scope)
            row = self.db.execute(
                "SELECT * FROM asset_catalog WHERE space_id=? AND asset_id=?", (space_id, asset_id)
            ).fetchone()
            if row is None:
                raise StoreError("NOT_FOUND", "Asset not found in this space")
            return decode_fields(row, "roles", "tags")

    def configure_reference_policy(self, space_id, scope, *, enabled, consent_ref, expected_auth_version):
        if (
            type(enabled) is not bool or type(consent_ref) is not str or len(consent_ref) > 500
            or type(expected_auth_version) is not int or expected_auth_version < 0
        ):
            raise StoreError("INVALID_ARGUMENT", "Reference policy needs a boolean and bounded consent reference")
        if enabled:
            nonempty(consent_ref, "consent_ref")
        with self._uow.transaction():
            space = self._queries.space(space_id, scope)
            expected_authorization(space, expected_auth_version)
            require_authorized(space)
            old = self.db.execute("SELECT * FROM reference_policy WHERE space_id=?", (space_id,)).fetchone()
            changed = old is None or (old["enabled"], old["consent_ref"], old["auth_version"]) != (
                int(enabled), consent_ref, space["auth_version"],
            )
            version = (old["version"] if old is not None else 0) + int(changed)
            self.db.execute(
                """INSERT INTO reference_policy VALUES(?,?,?,?,?) ON CONFLICT(space_id) DO UPDATE SET
                enabled=excluded.enabled,consent_ref=excluded.consent_ref,version=excluded.version,
                auth_version=excluded.auth_version""", (space_id, int(enabled), consent_ref, version, space["auth_version"]),
            )
            return {"enabled": enabled, "consent_ref": consent_ref, "version": version,
                    "auth_version": space["auth_version"]}

    def _reference_policy(self, space_id, scope, expected_auth_version):
        if type(expected_auth_version) is not int or expected_auth_version < 0:
            raise StoreError("INVALID_ARGUMENT", "An explicit current authorization version is required")
        space = self._queries.space(space_id, scope)
        expected_authorization(space, expected_auth_version)
        require_authorized(space)
        row = self.db.execute("SELECT * FROM reference_policy WHERE space_id=?", (space_id,)).fetchone()
        if row is None or not row["enabled"] or not row["consent_ref"].strip():
            raise StoreError("REFERENCE_AUTH_REQUIRED", "Explicit image-reference delegation is not active")
        if row["auth_version"] != space["auth_version"]:
            raise StoreError("AUTH_CHANGED", "Image-reference policy belongs to an older authorization")
        return {"enabled": True, "consent_ref": row["consent_ref"], "version": row["version"],
                "auth_version": row["auth_version"]}

    def validate_reference_authorization(self, space_id, scope, *, expected_auth_version):
        # Match grant and policy in one database snapshot, including writers
        # using another ArtistStore connection.
        with self._uow.transaction():
            return self._reference_policy(space_id, scope, expected_auth_version)

    def import_asset(self, space_id, scope, *, event_id, message_index, data, mime, roles, tags,
                     description, consent_ref, expected_auth_version):
        nonempty(event_id, "event_id")
        nonempty(consent_ref, "consent_ref")
        nonempty(description, "description")
        if len(event_id) > 512 or len(description) > 1000 or type(message_index) is not int or not 0 <= message_index < 2**31:
            raise StoreError("INVALID_ARGUMENT", "External image provenance and description must be bounded")
        validate_archive(roles, tags, "Host metadata import")
        roles, tags = sorted(roles), sorted(tags)
        image = validate_asset_image(data, mime)
        identifier = "asset:" + hashlib.sha256(canonical_json(
            ["authorized_chat", space_id, event_id, message_index]
        ).encode("utf-8")).hexdigest()
        metadata = ("来源元数据 source_metadata（图片内容未审阅，未进行图像分析）：" + description)[:1000]
        with self._uow.transaction():
            policy = self._reference_policy(space_id, scope, expected_auth_version)
            if consent_ref != policy["consent_ref"]:
                raise StoreError("REFERENCE_AUTH_CHANGED", "Image consent does not match the current delegation")
            old = self.db.execute(
                "SELECT * FROM external_assets WHERE space_id=? AND event_id=? AND message_index=?",
                (space_id, event_id, message_index),
            ).fetchone()
            if old is not None:
                asset = self.get_asset(space_id, scope, old["asset_id"])
                if (
                    old["data"] != data or old["consent_ref"] != consent_ref or old["source_description"] != description
                    or any(asset[key] != image[key] for key in ("mime", "sha256"))
                    or asset["roles"] != roles or asset["tags"] != tags or asset["description"] != metadata
                ):
                    raise StoreError("REQUEST_CONFLICT", "Original event image and import metadata are immutable")
                return asset
            usage = self.db.execute(
                "SELECT count(*) AS count,COALESCE(sum(size_bytes),0) AS bytes FROM asset_catalog WHERE space_id=?",
                (space_id,),
            ).fetchone()
            if usage["count"] >= MAX_ASSETS or usage["bytes"] + len(data) > MAX_ASSET_BYTES:
                raise StoreError("ASSET_QUOTA", "Space asset count or retained-image byte quota is exhausted")
            now, _ = self._uow.now()
            self.db.execute(
                """INSERT INTO asset_catalog(space_id,asset_id,source_version_id,work_id,job_id,sha256,mime,
                size_bytes,roles,tags,reason,summary,description,source_kind,review_request_id,auth_version,created_at)
                VALUES(?,?,NULL,NULL,NULL,?,?,?,?,?,?,?,?,'authorized_chat',NULL,?,?)""",
                (space_id, identifier, image["sha256"], mime, len(data), canonical_json(roles), canonical_json(tags),
                 "Host metadata import", metadata[:1000], metadata, policy["auth_version"], now),
            )
            self.db.execute("INSERT INTO external_assets VALUES(?,?,?,?,?,?,?)",
                            (space_id, identifier, event_id, message_index, data, consent_ref, description))
            return self.get_asset(space_id, scope, identifier)

    def get_asset_image(self, space_id, scope, asset_id):
        """Read the original image for scoped history/reconciliation, even after revocation."""
        with self.lock:
            asset = self.get_asset(space_id, scope, asset_id)
            source = self._asset_source(space_id, scope, asset)
            image = validate_asset_image(source["data"], asset["mime"])
            if image["sha256"] != asset["sha256"]:
                raise StoreError("ARTIFACT_INVALID", "Asset image digest changed")
            return image

    @staticmethod
    def _registration_snapshot(row):
        snapshot = {key: row[key] for key in (
            "request_id", "asset_id", "state", "binding_id", "auth_version", "sha256", "mime", "size_bytes",
            "policy_version", "consent_ref",
        )}
        if row["receipt"] is not None:
            snapshot["receipt"] = json.loads(row["receipt"])
        return snapshot

    def get_asset_registration(self, space_id, scope, asset_id):
        with self.lock:
            self._queries.space(space_id, scope)
            row = self.db.execute(
                "SELECT * FROM asset_registrations WHERE space_id=? AND asset_id=?", (space_id, asset_id)
            ).fetchone()
            return self._registration_snapshot(row) if row is not None else None

    def reserve_asset_registration(self, space_id, scope, *, asset_id, expected_auth_version):
        with self._uow.transaction():
            policy = self._reference_policy(space_id, scope, expected_auth_version)
            space = self._queries.space(space_id, scope)
            old = self.get_asset_registration(space_id, scope, asset_id)
            if old is not None:
                if old["binding_id"] != space["binding_id"]:
                    raise StoreError("REQUEST_CONFLICT", "Registration belongs to another service binding")
                # Inspection never renews authority or replaces unknown work.
                return old
            asset = self.get_asset(space_id, scope, asset_id)
            if not asset["active"]:
                raise StoreError("NOT_FOUND", "Inactive asset cannot be registered")
            if asset["source_kind"] == "authorized_chat":
                original = self.db.execute("SELECT consent_ref FROM external_assets WHERE space_id=? AND asset_id=?",
                                            (space_id, asset_id)).fetchone()
                if original is None or original["consent_ref"] != policy["consent_ref"]:
                    raise StoreError("REFERENCE_AUTH_CHANGED", "External image requires its original consent")
            self.get_asset_image(space_id, scope, asset_id)
            request = "asset-register:" + hashlib.sha256(canonical_json(
                [space["binding_id"], space_id, asset_id]
            ).encode("utf-8")).hexdigest()
            self.db.execute(
                """INSERT INTO asset_registrations(space_id,asset_id,request_id,binding_id,auth_version,
                policy_version,consent_ref,sha256,mime,size_bytes,state) VALUES(?,?,?,?,?,?,?,?,?,?,'reserved')""",
                (space_id, asset_id, request, space["binding_id"], space["auth_version"], policy["version"],
                 policy["consent_ref"], asset["sha256"], asset["mime"], asset["size_bytes"]),
            )
            return self.get_asset_registration(space_id, scope, asset_id)

    def claim_asset_registration(self, space_id, scope, asset_id, expected_auth_version):
        with self._uow.transaction():
            policy = self._reference_policy(space_id, scope, expected_auth_version)
            space = self._queries.space(space_id, scope)
            old = self.get_asset_registration(space_id, scope, asset_id)
            if old is None:
                raise StoreError("NOT_FOUND", "Registration reservation not found")
            if old["state"] != "reserved":
                raise StoreError("INVALID_STATE", "Only an undispatched reservation can be claimed once")
            if old["binding_id"] != space["binding_id"]:
                raise StoreError("REQUEST_CONFLICT", "Registration service binding changed")
            if old["auth_version"] != space["auth_version"]:
                raise StoreError("AUTH_CHANGED", "Registration belongs to an older authorization")
            if (old["policy_version"], old["consent_ref"]) != (policy["version"], policy["consent_ref"]):
                raise StoreError("REFERENCE_AUTH_CHANGED", "Reference policy changed after reservation")
            asset = self.get_asset(space_id, scope, asset_id)
            if not asset["active"]:
                raise StoreError("NOT_FOUND", "Inactive asset cannot be dispatched")
            image = self.get_asset_image(space_id, scope, asset_id)
            if (image["sha256"], image["mime"], len(image["data"])) != (
                old["sha256"], old["mime"], old["size_bytes"],
            ):
                raise StoreError("ARTIFACT_INVALID", "Reserved asset bytes changed")
            self.db.execute("UPDATE asset_registrations SET state='submitting' WHERE space_id=? AND asset_id=?",
                            (space_id, asset_id))
            return self.get_asset_registration(space_id, scope, asset_id)

    def mark_asset_registration_unknown(self, space_id, scope, asset_id, reason):
        nonempty(reason, "reason")
        if len(reason) > 500:
            raise StoreError("INVALID_ARGUMENT", "Registration uncertainty reason is too long")
        with self._uow.transaction():
            old = self.get_asset_registration(space_id, scope, asset_id)
            if old is None:
                raise StoreError("NOT_FOUND", "Registration not found")
            if old["state"] == "reserved":
                raise StoreError("INVALID_STATE", "An undispatched registration has no unknown remote outcome")
            if old["state"] == "submitting":
                self.db.execute("UPDATE asset_registrations SET state='unknown',reason=? WHERE space_id=? AND asset_id=?",
                                (reason, space_id, asset_id))
            return self.get_asset_registration(space_id, scope, asset_id)

    def complete_asset_registration(self, space_id, scope, asset_id, receipt):
        if (
            type(receipt) is not dict or receipt.keys() != {"asset_id", "sha256", "mime", "size_bytes"}
            or type(receipt.get("asset_id")) is not str or not receipt["asset_id"].strip() or len(receipt["asset_id"]) > 128
            or type(receipt.get("size_bytes")) is not int
        ):
            raise StoreError("INVALID_ARGUMENT", "Registration receipt must be exact, bounded asset metadata")
        payload = canonical_json(receipt)
        with self._uow.transaction():
            old = self.get_asset_registration(space_id, scope, asset_id)
            if old is None:
                raise StoreError("NOT_FOUND", "Registration not found")
            if any(receipt[key] != old[key] for key in ("sha256", "mime", "size_bytes")):
                raise StoreError("REQUEST_CONFLICT", "Registration receipt does not match the original image")
            if old["state"] == "succeeded":
                if old["receipt"] != receipt:
                    raise StoreError("REQUEST_CONFLICT", "Successful registration receipt is immutable")
                return old
            if old["state"] not in {"submitting", "unknown"}:
                raise StoreError("INVALID_STATE", "Only an attempted registration can be completed")
            image = self.get_asset_image(space_id, scope, asset_id)
            if (image["sha256"], image["mime"], len(image["data"])) != (old["sha256"], old["mime"], old["size_bytes"]):
                raise StoreError("ARTIFACT_INVALID", "Original registration image changed")
            self.db.execute("UPDATE asset_registrations SET state='succeeded',receipt=? WHERE space_id=? AND asset_id=?",
                            (payload, space_id, asset_id))
            return self.get_asset_registration(space_id, scope, asset_id)

    def archive_version(self, space_id, scope, *, version_id, roles, tags, reason, expected_auth_version):
        nonempty(version_id, "version_id")
        validate_archive(roles, tags, reason)
        roles, tags = sorted(roles), sorted(tags)
        asset_id = "asset:" + hashlib.sha256(canonical_json([space_id, version_id]).encode("utf-8")).hexdigest()
        with self._uow.transaction():
            space = self._queries.space(space_id, scope)
            expected_authorization(space, expected_auth_version)
            require_authorized(space)
            version = self._queries.version(space_id, scope, version_id)
            if (
                not isinstance(version["data"], bytes) or not version["data"]
                or hashlib.sha256(version["data"]).hexdigest() != version["sha256"]
                or version["mime"] not in {"image/png", "image/jpeg", "image/webp"}
            ):
                raise StoreError("ARTIFACT_INVALID", "Archive source bytes failed verification")
            proof = self._reviews.accepted_review(space_id, scope, space, version)
            if proof is None:
                raise StoreError("VERSION_NOT_ACCEPTED", "Only a version selected by a valid accepting review may be archived")
            old = self.db.execute(
                "SELECT * FROM asset_catalog WHERE space_id=? AND asset_id=?", (space_id, asset_id)
            ).fetchone()
            if old is not None:
                if (old["roles"], old["tags"], old["reason"]) != (canonical_json(roles), canonical_json(tags), reason):
                    raise StoreError("REQUEST_CONFLICT", "Archive roles, tags and reason are immutable")
                self._asset_source(space_id, scope, decode_fields(old, "roles", "tags"))
                return decode_fields(old, "roles", "tags")
            # Inactive entries still retain bytes and consume quota. Disabling
            # an asset cannot be used to evade the storage limit.
            usage = self.db.execute(
                "SELECT count(*) AS count,COALESCE(sum(size_bytes),0) AS bytes FROM asset_catalog WHERE space_id=?",
                (space_id,),
            ).fetchone()
            if usage["count"] >= MAX_ASSETS or usage["bytes"] + len(version["data"]) > MAX_ASSET_BYTES:
                raise StoreError("ASSET_QUOTA", "Space asset count or retained-image byte quota is exhausted")
            review = json.loads(proof["review"])
            review_reason, strengths = review.get("reason"), review.get("strengths", [])
            if (
                not isinstance(review_reason, str) or not review_reason.strip()
                or not isinstance(strengths, list) or any(not isinstance(item, str) for item in strengths)
            ):
                raise StoreError("INVALID_ARGUMENT", "Accepting review has no usable descriptive evidence")
            evidence_text = "评审理由：" + review_reason + "\n评审优点：" + "；".join(strengths)
            summary = evidence_text[:1000]
            description = (evidence_text + "\n入库用途说明：" + reason)[:2000]
            now, _ = self._uow.now()
            self.db.execute(
                """INSERT INTO asset_catalog(space_id,asset_id,source_version_id,work_id,job_id,sha256,mime,
                size_bytes,roles,tags,reason,summary,description,source_kind,review_request_id,auth_version,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'reviewed_work',?,?,?)""",
                (space_id, asset_id, version_id, version["work_id"], version["job_id"], version["sha256"],
                 version["mime"], len(version["data"]), canonical_json(roles), canonical_json(tags), reason,
                 summary, description, proof["request_id"], space["auth_version"], now),
            )
            return self.get_asset(space_id, scope, asset_id)

    @staticmethod
    def _asset_summary(asset):
        return {key: asset[key] for key in ("asset_id", "summary", "roles", "tags", "source_kind")}

    def list_assets(self, space_id, scope, query="", limit=8):
        if type(query) is not str or len(query) > 500 or type(limit) is not int or not 1 <= limit <= 8:
            raise StoreError("INVALID_ARGUMENT", "Asset query and candidate limit must be bounded")
        terms = unicodedata.normalize("NFKC", query).casefold().split()
        with self.lock:
            require_authorized(self._queries.space(space_id, scope))
            candidates = []
            for row in self.db.execute("SELECT * FROM asset_catalog WHERE space_id=? AND active=1", (space_id,)):
                asset = decode_fields(row, "roles", "tags")
                tags = [unicodedata.normalize("NFKC", tag).casefold() for tag in asset["tags"]]
                text = unicodedata.normalize("NFKC", asset["summary"] + " " + " ".join(asset["tags"])).casefold()
                if terms and not all(term in text for term in terms):
                    continue
                score = sum(10 if term in tags else 3 if any(term in tag for tag in tags) else 1 for term in terms)
                candidates.append((-score, asset["asset_id"], self._asset_summary(asset)))
            return [item[2] for item in sorted(candidates, key=lambda item: item[:2])[:limit]]

    def _asset_source(self, space_id, scope, asset):
        if asset["source_kind"] == "authorized_chat":
            self._queries.space(space_id, scope)
            row = self.db.execute("SELECT data FROM external_assets WHERE space_id=? AND asset_id=?",
                                  (space_id, asset["asset_id"])).fetchone()
            if (
                row is None or not isinstance(row["data"], bytes) or len(row["data"]) != asset["size_bytes"]
                or hashlib.sha256(row["data"]).hexdigest() != asset["sha256"]
            ):
                raise StoreError("ARTIFACT_INVALID", "Original external image bytes changed")
            image = validate_asset_image(row["data"], asset["mime"])
            return image
        version = self._queries.version(space_id, scope, asset["source_version_id"])
        if (
            any(version[key] != asset[key] for key in ("work_id", "job_id", "sha256", "mime"))
            or not isinstance(version["data"], bytes) or len(version["data"]) != asset["size_bytes"]
            or hashlib.sha256(version["data"]).hexdigest() != asset["sha256"]
        ):
            raise StoreError("ARTIFACT_INVALID", "Archived source identity or bytes changed")
        return version

    def _asset_details(self, assets):
        details = [{**self._asset_summary(asset), **{key: asset[key] for key in ("sha256", "mime", "description")}}
                   for asset in assets]
        # Count actual Unicode JSON characters, including labels and metadata,
        # rather than escaping Chinese into six-character ASCII sequences.
        if len(json.dumps(details, ensure_ascii=False, separators=(",", ":"))) > 6000:
            raise StoreError("ASSET_DETAILS_TOO_LONG", "Selected asset details exceed the 6000 character limit")
        return details

    def select_assets(self, space_id, scope, asset_ids, expected_auth_version):
        if (
            type(asset_ids) is not list or len(asset_ids) > 3
            or any(type(identifier) is not str or not identifier.strip() or len(identifier) > 128 for identifier in asset_ids)
            or len(set(asset_ids)) != len(asset_ids)
        ):
            raise StoreError("INVALID_ARGUMENT", "Select at most three distinct bounded asset IDs")
        asset_ids = list(asset_ids)
        with self._uow.transaction():
            space = self._queries.space(space_id, scope)
            expected_authorization(space, expected_auth_version)
            require_authorized(space)
            assets = [self.get_asset(space_id, scope, identifier) for identifier in asset_ids]
            for asset in assets:
                if not asset["active"]:
                    raise StoreError("NOT_FOUND", "Inactive asset cannot be selected")
                self._asset_source(space_id, scope, asset)
            details = self._asset_details(assets)
            self.db.execute("UPDATE asset_selection SET selected=0 WHERE space_id=?", (space_id,))
            for position, identifier in enumerate(asset_ids):
                self.db.execute(
                    """INSERT INTO asset_selection(space_id,asset_id,position,auth_version,selected) VALUES(?,?,?,?,1)
                    ON CONFLICT(space_id,asset_id) DO UPDATE SET position=excluded.position,
                    auth_version=excluded.auth_version,selected=1""", (space_id, identifier, position, space["auth_version"]),
                )
            return details

    def selected_assets(self, space_id, scope):
        with self.lock:
            self._queries.space(space_id, scope)
            assets = [decode_fields(row, "roles", "tags") for row in self.db.execute(
                """SELECT a.* FROM asset_selection AS s JOIN asset_catalog AS a
                ON a.space_id=s.space_id AND a.asset_id=s.asset_id
                WHERE s.space_id=? AND s.selected=1 AND a.active=1 ORDER BY s.position,a.asset_id""", (space_id,),
            )]
            return self._asset_details(assets)

    def deactivate_asset(self, space_id, scope, asset_id, expected_auth_version):
        with self._uow.transaction():
            space = self._queries.space(space_id, scope)
            expected_authorization(space, expected_auth_version)
            require_authorized(space)
            self.get_asset(space_id, scope, asset_id)
            self.db.execute("UPDATE asset_catalog SET active=0 WHERE space_id=? AND asset_id=?", (space_id, asset_id))

    def _freeze_references(self, space_id, scope, references, *, usage="text_context", policy=None):
        # Resolve only caller-selected, same-space asset IDs. Neither a model
        # version ID nor a filesystem path can become a new version here.
        assets = []
        for reference in references:
            if type(reference) is not dict or reference.keys() != REFERENCE_FIELDS:
                raise StoreError("INVALID_ARGUMENT", "References must be choices, not supplied snapshots")
            nonempty(reference["asset_id"], "asset_id")
            asset = self.get_asset(space_id, scope, reference["asset_id"])
            if not asset["active"]:
                raise StoreError("NOT_FOUND", "Inactive asset cannot enter a new plan")
            if asset["source_kind"] == "authorized_chat":
                active_policy = policy or self._reference_policy(
                    space_id, scope, self._queries.space(space_id, scope)["auth_version"]
                )
                original = self.db.execute("SELECT consent_ref FROM external_assets WHERE space_id=? AND asset_id=?",
                                            (space_id, asset["asset_id"])).fetchone()
                if original is None or original["consent_ref"] != active_policy["consent_ref"]:
                    raise StoreError("REFERENCE_AUTH_CHANGED", "External image belongs to another consent reference")
            self._asset_source(space_id, scope, asset)
            assets.append(asset)
        selected = validate_references(references, assets)
        frozen = [{**choice, **{key: asset[key] for key in ("summary", "source_kind", "source_version_id", "sha256", "mime")},
                   "usage": usage} for choice, asset in zip(selected, assets)]
        if usage == "image_input":
            for reference in frozen:
                reference.update(policy_version=policy["version"], consent_ref=policy["consent_ref"])
        return frozen

    def preview_plan_prompt(
        self, space_id, scope, *, prompt, creative=None, references=None,
        reference_usage="text_context", expected_auth_version,
    ):
        """只读预检最终提示词；不能代替 plan_work 入库时的事务校验。"""
        if reference_usage not in ("text_context", "image_input"):
            raise StoreError("INVALID_ARGUMENT", "Invalid reference usage")
        references = [] if references is None else references
        if not isinstance(references, list) or len(references) > 3:
            raise StoreError("INVALID_ARGUMENT", "At most three reference choices are allowed")
        with self._uow.transaction():
            space = self._queries.space(space_id, scope)
            expected_authorization(space, expected_auth_version)
            require_authorized(space)
            creative_snapshot = self._freeze_creative(space_id, scope, creative) if creative is not None else None
            policy = self._reference_policy(space_id, scope, expected_auth_version) if reference_usage == "image_input" else None
            frozen_refs = self._freeze_references(space_id, scope, references, usage=reference_usage, policy=policy)
            return reference_prompt(render_prompt(prompt, creative_snapshot), frozen_refs)

    def plan_work(
        self, space_id, scope, *, work_id, brief, inspiration_keys, creative=None, references=None,
        reference_usage="text_context", expected_auth_version=None
    ):
        nonempty(work_id, "work_id")
        if reference_usage not in ("text_context", "image_input"):
            raise StoreError("INVALID_ARGUMENT", "Reference usage is a trusted text_context or image_input value")
        if (
            not isinstance(brief, dict)
            or not {"intent", "subject", "mood", "prompt", "completion_criteria"} <= brief.keys()
            or "creative" in brief
            or "references" in brief
        ):
            raise StoreError("INVALID_ARGUMENT", "A complete validated brief is required")
        payload = canonical_json(brief)
        brief = json.loads(payload)
        selected = validate_creative(creative) if creative is not None else None
        references = [] if references is None else references
        if not isinstance(references, list) or len(references) > 3:
            raise StoreError("INVALID_ARGUMENT", "At most three reference choices are allowed")
        references = json.loads(canonical_json(references))
        keys = self._string_list(inspiration_keys, "inspiration_keys")
        with self._uow.transaction():
            space = self._queries.space(space_id, scope)
            expected_authorization(space, expected_auth_version)
            require_authorized(space)
            self._lock_single_work(space_id, scope, work_id)
            now, _ = self._uow.now()
            old = self.db.execute(
                "SELECT * FROM artist_works WHERE space_id=? AND work_id=?", (space_id, work_id)
            ).fetchone()
            if old:
                saved_brief = json.loads(old["brief"])
                saved_creative = saved_brief.pop("creative", None)
                saved_references = saved_brief.pop("references", [])
                saved_reference_choices = [{key: item[key] for key in REFERENCE_FIELDS} for item in saved_references]
                saved_choice = (
                    {key: saved_creative[key] for key in CHOICE_FIELDS} if saved_creative is not None else None
                )
                if (
                    canonical_json(saved_brief) != payload or saved_choice != selected
                    or saved_reference_choices != references
                    or any(item["usage"] != reference_usage for item in saved_references)
                    or json.loads(old["inspiration_keys"]) != keys
                ):
                    raise StoreError("REQUEST_CONFLICT", "Work brief and inspiration bindings are immutable")
                return self._queries.artist_work(space_id, scope, work_id)
            self._check_active_work(space_id, work_id)
            policy = self._reference_policy(space_id, scope, expected_auth_version) if reference_usage == "image_input" else None
            snapshot, objective = None, None
            if selected is not None:
                snapshot = self._freeze_creative(space_id, scope, selected)
                brief["creative"] = snapshot
                payload = canonical_json(brief)
                if snapshot["series_id"] is not None:
                    objective = normalize_objective(brief["intent"], brief["subject"])
                    duplicate = self.db.execute(
                        "SELECT 1 FROM series_members WHERE space_id=? AND series_id=? AND objective=?",
                        (space_id, snapshot["series_id"], objective),
                    ).fetchone()
                    if duplicate is not None:
                        raise StoreError("REQUEST_CONFLICT", "Series already contains this creative objective")
            frozen_refs = self._freeze_references(space_id, scope, references, usage=reference_usage, policy=policy)
            if frozen_refs:
                brief["references"] = frozen_refs
            payload = canonical_json(brief)
            reference_prompt(render_prompt(brief["prompt"], snapshot), frozen_refs)
            group_ideas = {row["inspiration_key"]: row for row in self._available_group_thoughts(space_id)}
            for key in keys:
                if key.startswith("group-thought:"):
                    if key not in group_ideas:
                        raise StoreError("NOT_FOUND", "Group thought unavailable in this space")
                    continue
                idea = self.db.execute(
                    "SELECT * FROM inspirations WHERE space_id=? AND key=? AND expires_at>?",
                    (space_id, key, now),
                ).fetchone()
                if idea is None or idea["consumed_by"] not in {None, work_id}:
                    raise StoreError("NOT_FOUND", "Inspiration unavailable in this space")
            self.db.execute("INSERT OR IGNORE INTO works VALUES(?,?,?)", (space_id, work_id, now))
            self.db.execute(
                """INSERT INTO artist_works(space_id,work_id,status,brief,inspiration_keys)
                            VALUES(?,?,'planned',?,?)""",
                (space_id, work_id, payload, canonical_json(keys)),
            )
            if objective is not None:
                self.db.execute(
                    "INSERT INTO series_members(space_id,series_id,work_id,objective) VALUES(?,?,?,?)",
                    (space_id, snapshot["series_id"], work_id, objective),
                )
            for key in keys:
                if key in group_ideas:
                    self.db.execute(
                        """UPDATE group_thought_links SET adopted_by=?,adopted_at=?,updated_at=?
                            WHERE space_id=? AND inspiration_key=?""",
                        (work_id, now, now, space_id, key),
                    )
                else:
                    self.db.execute(
                        "UPDATE inspirations SET consumed_by=? WHERE space_id=? AND key=?",
                        (work_id, space_id, key),
                    )
            return self._queries.artist_work(space_id, scope, work_id)

    def get_artist_work(self, space_id, scope, work_id):
        with self.lock:
            return self._queries.artist_work(space_id, scope, work_id)

    def list_artist_works(self, space_id, scope):
        with self.lock:
            self._queries.space(space_id, scope)
            return [
                decode_fields(row, "brief", "inspiration_keys")
                for row in self.db.execute(
                    "SELECT * FROM artist_works WHERE space_id=? ORDER BY rowid",
                    (space_id,),
                )
            ]

    def update_artist_work(self, space_id, scope, work_id, *, changes):
        allowed = {
            "status",
            "reason",
            "pending_request",
            "parent_version",
            "edit_prompt",
            "best_version",
            "next_check_at",
        }
        if not isinstance(changes, dict) or not changes.keys() <= allowed:
            raise StoreError("INVALID_ARGUMENT", "Unknown or immutable work fields")
        changes = dict(changes)
        with self._uow.transaction():
            current = self._queries.artist_work(space_id, scope, work_id)
            space = self._queries.space(space_id, scope)
            status = changes.get("status", current["status"])
            if status not in {*_ACTIVE_WORK, "finished", "abandoned"}:
                raise StoreError("INVALID_ARGUMENT", "Unknown artist work status")
            if not space["authorized"] and (
                ("status" in changes and status not in {"review_pending", "finished", "abandoned"})
                or changes.get("edit_prompt") is not None
                or changes.get("parent_version") is not None
                or changes.get("next_check_at") is not None
            ):
                raise StoreError("AUTH_REQUIRED", "Revoked spaces can only retain existing results")
            if status in _ACTIVE_WORK:
                self._check_active_work(space_id, work_id)
            for key in ("parent_version", "best_version"):
                if (
                    changes.get(key) is not None
                    and self._queries.version(space_id, scope, changes[key])["work_id"] != work_id
                ):
                    raise StoreError("NOT_FOUND", "Version does not belong to this work")
            if changes.get("pending_request") is not None:
                op = self._queries.operation(space_id, scope, changes["pending_request"])
                if op["work_id"] != work_id or op["category"] != "render":
                    raise StoreError("NOT_FOUND", "Pending render does not belong to this work")
            for key in ("reason", "edit_prompt"):
                if changes.get(key) is not None and not isinstance(changes[key], str):
                    raise StoreError("INVALID_ARGUMENT", f"{key} must be text or null")
            if changes.get("next_check_at") is not None:
                try:
                    when = datetime.fromisoformat(changes["next_check_at"])
                    budget_day(when)
                    changes["next_check_at"] = when.astimezone(timezone.utc).isoformat()
                except (ValueError, TypeError) as exc:
                    raise StoreError(
                        "INVALID_ARGUMENT", "next_check_at must be an aware ISO datetime"
                    ) from exc
            if changes:
                fields = ",".join(f"{key}=?" for key in changes)
                self.db.execute(
                    f"UPDATE artist_works SET {fields} WHERE space_id=? AND work_id=?",
                    (*changes.values(), space_id, work_id),
                )
            return self._queries.artist_work(space_id, scope, work_id)

    def record_version(
        self, space_id, scope, *, work_id, version_id, request_id, job_id, data, mime, parent_id=None
    ):
        nonempty(version_id, "version_id")
        if not isinstance(data, bytes) or not 0 < len(data) <= 8 * 1024 * 1024:
            raise StoreError("INVALID_ARGUMENT", "Version data must be 1 byte to 8 MiB")
        if mime not in {"image/png", "image/jpeg", "image/webp"} or parent_id == version_id:
            raise StoreError("INVALID_ARGUMENT", "Invalid image MIME or self parent")
        digest = hashlib.sha256(data).hexdigest()
        with self._uow.transaction():
            op = self._queries.operation(space_id, scope, request_id)
            if (
                op["category"] != "render"
                or op["work_id"] != work_id
                or op["job_id"] != job_id
                or op["state"] in {"reserved", "rejected"}
                or job_id is None
            ):
                raise StoreError("NOT_FOUND", "Version requires its original bound render job")
            if parent_id is not None and self._queries.version(space_id, scope, parent_id)["work_id"] != work_id:
                raise StoreError("NOT_FOUND", "Parent version belongs to another work")
            if op["action"] == "edit":
                if parent_id is None:
                    raise StoreError("REQUEST_CONFLICT", "An edit version requires its source parent")
                parent = self._queries.version(space_id, scope, parent_id)
                source = json.loads(op["params_json"])
                if (source.get("source_job_id"), source.get("source_sha256")) != (
                    parent["job_id"],
                    parent["sha256"],
                ):
                    raise StoreError("REQUEST_CONFLICT", "Edit parent does not match the frozen source")
            old = self.db.execute(
                """SELECT * FROM versions WHERE space_id=?
                                  AND (version_id=? OR request_id=?)""",
                (space_id, version_id, request_id),
            ).fetchone()
            fields = (version_id, work_id, request_id, job_id, data, digest, mime, parent_id)
            if old:
                if (
                    tuple(
                        old[key]
                        for key in (
                            "version_id",
                            "work_id",
                            "request_id",
                            "job_id",
                            "data",
                            "sha256",
                            "mime",
                            "parent_id",
                        )
                    )
                    != fields
                ):
                    raise StoreError("REQUEST_CONFLICT", "Version and request bindings are immutable")
                return dict(old)
            now, _ = self._uow.now()
            self.db.execute("INSERT INTO versions VALUES(?,?,?,?,?,?,?,?,?,?)", (space_id, *fields, now))
            return self._queries.version(space_id, scope, version_id)

    def get_version(self, space_id, scope, version_id):
        with self.lock:
            return self._queries.version(space_id, scope, version_id)

    def list_versions(self, space_id, scope, work_id):
        with self.lock:
            self.get_work(space_id, scope, work_id)
            return [
                dict(row)
                for row in self.db.execute(
                    f"SELECT {_VERSION_META} FROM versions WHERE space_id=? AND work_id=? ORDER BY rowid",
                    (space_id, work_id),
                )
            ]

    def get_review_snapshot(self, space_id, scope, version_id) -> Optional[Dict[str, Any]]:
        """Historical metadata remains readable after delegation is withdrawn."""
        return self._reviews.get_review_snapshot(space_id, scope, version_id)

    def freeze_review_snapshot(
        self, space_id, scope, *, version_id, context, expected_auth_version
    ) -> Dict[str, Any]:
        return self._reviews.freeze_review_snapshot(
            space_id, scope, version_id=version_id, context=context, expected_auth_version=expected_auth_version,
        )

    def version_accepted(self, space_id, scope, version_id, expected_auth_version=None) -> bool:
        """Prove acceptance using the successful original review, never work status.

        A review row alone is audit history: older callers may record one while
        an operation is still unknown. Malformed or mismatched evidence cannot
        certify an image, but is retained unchanged for investigation.
        """
        return self._reviews.version_accepted(space_id, scope, version_id, expected_auth_version)

    def record_review(self, space_id, scope, *, version_id, request_id, review):
        return self._reviews.record_review(space_id, scope, version_id=version_id, request_id=request_id, review=review)

    def list_reviews(self, space_id, scope, work_id):
        return self._reviews.list_reviews(space_id, scope, work_id)

    def close(self):
        self._uow.close()
