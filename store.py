"""Offline SQLite artist ledger; no Host, MCP, timer or delivery side effects.

All authority/configuration methods are for a trusted adapter, not model tools.
Use asyncio.to_thread when integrating this synchronous module with a Host loop.
"""

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, Iterator, Optional
import hashlib
import json
import os
import sqlite3

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


SCHEMA_VERSION = 2
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
_SCHEMA = (
    """CREATE TABLE spaces (
        space_id TEXT PRIMARY KEY, scope TEXT NOT NULL, binding_id TEXT NOT NULL,
        auth_version INTEGER NOT NULL DEFAULT 0, authorized INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE grants (
        space_id TEXT NOT NULL REFERENCES spaces, version INTEGER NOT NULL,
        authorized INTEGER NOT NULL, source_ref TEXT NOT NULL, granted_by TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(space_id, version))""",
    """CREATE TABLE limits (
        scope_key TEXT PRIMARY KEY, think INTEGER NOT NULL, render INTEGER NOT NULL,
        share INTEGER NOT NULL, max_inflight INTEGER NOT NULL, max_revisions INTEGER NOT NULL)""",
    """CREATE TABLE works (
        space_id TEXT NOT NULL REFERENCES spaces, work_id TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(space_id, work_id))""",
    """CREATE TABLE operations (
        id INTEGER PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces, work_id TEXT,
        request_id TEXT NOT NULL, action TEXT NOT NULL, category TEXT NOT NULL,
        params_json TEXT NOT NULL, scope_snapshot TEXT NOT NULL, binding_id TEXT NOT NULL,
        auth_version INTEGER NOT NULL, trigger TEXT NOT NULL, trigger_ref TEXT,
        state TEXT NOT NULL, budget_day TEXT NOT NULL, job_id TEXT, evidence TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(binding_id, scope_snapshot, request_id),
        FOREIGN KEY(space_id, work_id) REFERENCES works(space_id, work_id))""",
    "CREATE INDEX operation_budget ON operations(category, state, budget_day, space_id)",
    """CREATE TABLE legacy_locks (
        job TEXT PRIMARY KEY, original_state TEXT NOT NULL, state TEXT NOT NULL,
        source_digest TEXT NOT NULL)""",
    """CREATE TABLE legacy_migrations (
        source_digest TEXT PRIMARY KEY, source_path TEXT NOT NULL, backup_path TEXT NOT NULL,
        row_count INTEGER NOT NULL, created_at TEXT NOT NULL)""",
)
_SCHEMA_2 = (
    """CREATE TABLE artist_state (
        space_id TEXT PRIMARY KEY REFERENCES spaces, acked_through INTEGER NOT NULL DEFAULT 0,
        next_due TEXT, backoff_until TEXT)""",
    """CREATE TABLE artist_wakes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, space_id TEXT NOT NULL REFERENCES spaces,
        event_id TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, source_ref TEXT,
        UNIQUE(space_id,event_id))""",
    """CREATE TABLE artist_claims (
        space_id TEXT NOT NULL REFERENCES spaces, token TEXT NOT NULL,
        through INTEGER NOT NULL, state TEXT NOT NULL, auth_version INTEGER NOT NULL,
        PRIMARY KEY(space_id,token))""",
    "CREATE UNIQUE INDEX artist_active_claim ON artist_claims(space_id) WHERE state='active'",
    """CREATE TABLE inspirations (
        space_id TEXT NOT NULL REFERENCES spaces, key TEXT NOT NULL, summary TEXT NOT NULL,
        source_refs TEXT NOT NULL, origin TEXT NOT NULL, expires_at TEXT NOT NULL,
        consumed_by TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(space_id,key))""",
    """CREATE TABLE artist_works (
        space_id TEXT NOT NULL, work_id TEXT NOT NULL, status TEXT NOT NULL,
        brief TEXT NOT NULL, inspiration_keys TEXT NOT NULL, reason TEXT,
        pending_request TEXT, parent_version TEXT, edit_prompt TEXT, best_version TEXT, next_check_at TEXT,
        PRIMARY KEY(space_id,work_id), FOREIGN KEY(space_id,work_id) REFERENCES works(space_id,work_id))""",
    """CREATE TABLE versions (
        space_id TEXT NOT NULL, version_id TEXT NOT NULL, work_id TEXT NOT NULL,
        request_id TEXT NOT NULL, job_id TEXT NOT NULL, data BLOB NOT NULL,
        sha256 TEXT NOT NULL, mime TEXT NOT NULL, parent_id TEXT, created_at TEXT NOT NULL,
        PRIMARY KEY(space_id,version_id), UNIQUE(space_id,request_id),
        FOREIGN KEY(space_id,work_id) REFERENCES works(space_id,work_id))""",
    """CREATE TABLE reviews (
        id INTEGER PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces,
        work_id TEXT NOT NULL, version_id TEXT NOT NULL, request_id TEXT NOT NULL,
        review TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(space_id,request_id),
        FOREIGN KEY(space_id,version_id) REFERENCES versions(space_id,version_id))""",
)
_ACTIVE_WORK = ("planned", "deferred", "creating", "review_pending")
_VERSION_META = "space_id,version_id,work_id,request_id,job_id,sha256,mime,parent_id,created_at"


class ArtistStore:
    def __init__(self, path: Path, *, clock: Optional[Callable[[], datetime]] = None):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        try:
            with self._transaction():
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                    if tables:
                        raise StoreError("SCHEMA_UNSUPPORTED", "Unversioned nonempty database")
                    for statement in _SCHEMA:
                        self.db.execute(statement)
                elif version not in {1, SCHEMA_VERSION}:
                    raise StoreError("SCHEMA_UNSUPPORTED", "Artist database schema is not supported")
                if version in {0, 1}:
                    for statement in _SCHEMA_2:
                        self.db.execute(statement)
                    self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        # BEGIN IMMEDIATE serializes checks and reservations across connections,
        # not just across threads using this instance's RLock.
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def _now(self):
        now = self.clock()
        day = budget_day(now)
        return now.astimezone(timezone.utc).isoformat(), day

    def _space(self, space_id: str, scope: Scope):
        if not isinstance(scope, Scope):
            raise StoreError("INVALID_SCOPE", "Only trusted-adapter Scope values are accepted")
        row = self.db.execute("SELECT * FROM spaces WHERE space_id=?", (space_id,)).fetchone()
        if row is None or row["scope"] != scope.snapshot():
            raise StoreError("NOT_FOUND", "Space not found for this scope")
        return row

    def _authorized(self, space):
        if not space["authorized"]:
            raise StoreError("AUTH_REQUIRED", "Continuous delegation is not active")

    @staticmethod
    def _expected_authorization(space, expected):
        if expected is not None:
            if type(expected) is not int or expected < 0:
                raise StoreError("INVALID_ARGUMENT", "expected_auth_version must be a nonnegative integer")
            if space["auth_version"] != expected:
                raise StoreError("AUTH_CHANGED", "Authorization changed while processing this wake")

    def create_space(self, space_id: str, scope: Scope, *, binding_id: str) -> Dict[str, Any]:
        nonempty(space_id, "space_id")
        nonempty(binding_id, "binding_id")
        if space_id == "*" or not isinstance(scope, Scope):
            raise StoreError("INVALID_SCOPE", "Reserved space_id or untyped scope")
        with self._transaction():
            old = self.db.execute("SELECT * FROM spaces WHERE space_id=?", (space_id,)).fetchone()
            if old and (old["scope"] != scope.snapshot() or old["binding_id"] != binding_id):
                raise StoreError("OWNER_CONFLICT", "Space owner and service binding are immutable")
            self.db.execute(
                "INSERT OR IGNORE INTO spaces(space_id,scope,binding_id) VALUES(?,?,?)",
                (space_id, scope.snapshot(), binding_id),
            )
            return dict(self._space(space_id, scope))

    def get_space(self, space_id: str, scope: Scope) -> Dict[str, Any]:
        """Read authority version and immutable binding for a trusted adapter."""
        with self.lock:
            return dict(self._space(space_id, scope))

    def grant(self, space_id: str, scope: Scope, delegation: Delegation) -> int:
        if not isinstance(delegation, Delegation):
            raise StoreError("AUTH_REQUIRED", "An explicit delegation record is required")
        return self._authorization(space_id, scope, True, delegation.source_ref, delegation.granted_by)

    def revoke(self, space_id: str, scope: Scope, *, source_ref: str, granted_by: str) -> int:
        return self._authorization(space_id, scope, False, source_ref, granted_by)

    def _authorization(self, space_id, scope, active, source_ref, granted_by):
        nonempty(source_ref, "source_ref")
        nonempty(granted_by, "granted_by")
        now, _ = self._now()
        with self._transaction():
            space = self._space(space_id, scope)
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
        with self._transaction():
            self._set_limits("*", limits)

    def set_limits(self, space_id: str, scope: Scope, limits: Limits):
        with self._transaction():
            self._space(space_id, scope)
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

    def create_work(self, space_id: str, scope: Scope, work_id: str) -> Dict[str, Any]:
        nonempty(work_id, "work_id")
        now, _ = self._now()
        with self._transaction():
            self._authorized(self._space(space_id, scope))
            self.db.execute("INSERT OR IGNORE INTO works VALUES(?,?,?)", (space_id, work_id, now))
            return dict(
                self.db.execute(
                    "SELECT * FROM works WHERE space_id=? AND work_id=?",
                    (space_id, work_id),
                ).fetchone()
            )

    def get_work(self, space_id: str, scope: Scope, work_id: str) -> Dict[str, Any]:
        with self.lock:
            self._space(space_id, scope)
            row = self.db.execute(
                "SELECT * FROM works WHERE space_id=? AND work_id=?",
                (space_id, work_id),
            ).fetchone()
            if row is None:
                raise StoreError("NOT_FOUND", "Work not found in this space")
            result = dict(row)
            result["revision_attempts"] = self.db.execute(
                """SELECT count(*) FROM operations WHERE space_id=? AND work_id=?
                AND action='edit' AND state!='rejected'""",
                (space_id, work_id),
            ).fetchone()[0]
            return result

    def _operation(self, space_id, scope, request_id):
        self._space(space_id, scope)
        row = self.db.execute(
            "SELECT * FROM operations WHERE space_id=? AND request_id=?",
            (space_id, request_id),
        ).fetchone()
        if row is None:
            raise StoreError("NOT_FOUND", "Operation not found in this space")
        return row

    @staticmethod
    def _snapshot(row):
        result = dict(row)
        result["params"] = json.loads(result["params_json"])
        return result

    def get_operation(self, space_id: str, scope: Scope, request_id: str) -> Dict[str, Any]:
        with self.lock:
            return self._snapshot(self._operation(space_id, scope, request_id))

    def audit_operations(self, space_id: str, scope: Scope):
        with self.lock:
            self._space(space_id, scope)
            return [
                self._snapshot(row)
                for row in self.db.execute(
                    "SELECT * FROM operations WHERE space_id=? ORDER BY id",
                    (space_id,),
                )
            ]

    def audit_authorizations(self, space_id: str, scope: Scope):
        with self.lock:
            self._space(space_id, scope)
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
        _, day = self._now()
        with self.lock:
            self._space(space_id, scope)
            return {
                "day": day,
                **{
                    category: self._usage(category, day, space_id)
                    for category in ("think", "render", "share")
                },
            }

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
        with self._transaction():
            now, day = self._now()
            space = self._space(space_id, scope)
            self._expected_authorization(space, expected_auth_version)
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
            self._authorized(space)
            if action in {"submit", "edit"} and work_id is None:
                raise StoreError("INVALID_ARGUMENT", "Render operations require a work_id")
            if work_id is not None:
                work = self.db.execute(
                    "SELECT 1 FROM works WHERE space_id=? AND work_id=?",
                    (space_id, work_id),
                ).fetchone()
                if work is None:
                    raise StoreError("NOT_FOUND", "Work not found in this space")
            if action == "share":
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
            return self._snapshot(self._operation(space_id, scope, request_id))

    def mark_submitting(self, space_id: str, scope: Scope, request_id: str):
        with self._transaction():
            now, day = self._now()
            row = self._operation(space_id, scope, request_id)
            space = self._space(space_id, scope)
            self._authorized(space)
            if row["state"] != "reserved":
                raise StoreError("INVALID_STATE", "Only a reserved operation can start once")
            if row["auth_version"] != space["auth_version"]:
                raise StoreError("AUTH_CHANGED", "Authorization changed since reservation")
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
            return self._snapshot(self._operation(space_id, scope, request_id))

    def _update(self, row, state, evidence, job_id=None):
        now, _ = self._now()
        if job_id is not None:
            nonempty(job_id, "job_id")
        if row["job_id"] is not None and job_id not in (None, row["job_id"]):
            raise StoreError("REQUEST_CONFLICT", "An operation cannot switch external job")
        self.db.execute(
            "UPDATE operations SET state=?,evidence=?,job_id=COALESCE(job_id,?),updated_at=? WHERE id=?",
            (state, evidence, job_id, now, row["id"]),
        )

    def mark_accepted(self, space_id: str, scope: Scope, request_id: str, *, job_id=None):
        with self._transaction():
            row = self._operation(space_id, scope, request_id)
            if row["state"] not in {"submitting", "accepted"}:
                raise StoreError("INVALID_STATE", "Acceptance requires an in-progress submission")
            if row["category"] == "render" and job_id is None:
                raise StoreError("INVALID_ARGUMENT", "Render acceptance requires the original job_id")
            self._update(row, "accepted", "accepted response", job_id)

    def mark_unknown(self, space_id: str, scope: Scope, request_id: str, *, reason: str):
        nonempty(reason, "reason")
        with self._transaction():
            row = self._operation(space_id, scope, request_id)
            if row["state"] not in {"submitting", "accepted", "unknown"}:
                raise StoreError("INVALID_STATE", "Only an attempted operation can become unknown")
            self._update(row, "unknown", reason)

    def reject(self, space_id: str, scope: Scope, request_id: str, *, code: str, evidence: str):
        nonempty(code, "code")
        nonempty(evidence, "evidence")
        with self._transaction():
            row = self._operation(space_id, scope, request_id)
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
        with self._transaction():
            row = self._operation(space_id, scope, request_id)
            if row["state"] != "reserved":
                raise StoreError("INVALID_STATE", "Only an unstarted reservation may be cancelled")
            self._update(row, "rejected", f"local cancellation: {reason}")

    def settle(
        self, space_id: str, scope: Scope, request_id: str, *, outcome: str, evidence: str, job_id=None
    ):
        if outcome not in CONSUMED:
            raise StoreError("INVALID_ARGUMENT", "Terminal outcome must be succeeded or failed")
        nonempty(evidence, "evidence")
        with self._transaction():
            row = self._operation(space_id, scope, request_id)
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
        with self._transaction():
            row = self._operation(space_id, scope, request_id)
            if row["state"] != "unknown":
                raise StoreError("INVALID_STATE", "Reconciliation only resolves unknown operations")
            if row["category"] == "render" and job_id is None and row["job_id"] is None:
                raise StoreError("INVALID_ARGUMENT", "Render reconciliation must bind the original job_id")
            self._update(row, outcome, evidence, job_id)

    def recover_inflight(self, space_id: str, scope: Scope, *, offline: bool = False) -> int:
        """Explicit recovery after the previous worker has stopped, never on open."""
        if offline is not True:
            raise StoreError("OFFLINE_REQUIRED", "Stop previous workers before recovery")
        now, _ = self._now()
        with self._transaction():
            self._space(space_id, scope)
            return self.db.execute(
                """UPDATE operations SET state='unknown',evidence='explicit offline recovery',updated_at=?
                WHERE space_id=? AND state IN ('submitting','accepted')""",
                (now, space_id),
            ).rowcount

    def legacy_lock(self, space_id: str, scope: Scope, job_id: str) -> bool:
        """Conservative global lock; never exposes legacy ownerless record contents."""
        nonempty(job_id, "job_id")
        with self.lock:
            self._space(space_id, scope)
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
            self._authorized(self._space(space_id, scope))
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
            now, _ = self._now()
            failure_code = "MIGRATION_FAILED"
            with self._transaction():
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

    def _artist_state(self, space_id):
        self.db.execute("INSERT OR IGNORE INTO artist_state(space_id) VALUES(?)", (space_id,))
        return self.db.execute("SELECT * FROM artist_state WHERE space_id=?", (space_id,)).fetchone()

    def _enqueue_wake(self, space_id, event_id, kind, text, source_ref):
        prior = self.db.execute(
            "SELECT * FROM artist_wakes WHERE space_id=? AND event_id=?",
            (space_id, event_id),
        ).fetchone()
        if prior:
            if (prior["kind"], prior["text"], prior["source_ref"]) != (kind, text, source_ref):
                raise StoreError("REQUEST_CONFLICT", "Wake event already has different content")
            return False
        self.db.execute(
            "INSERT INTO artist_wakes(space_id,event_id,kind,text,source_ref) VALUES(?,?,?,?,?)",
            (space_id, event_id, kind, text, source_ref),
        )
        return True

    def enqueue_wake(self, space_id, scope, *, event_id, kind, text="", source_ref=None) -> bool:
        nonempty(event_id, "event_id")
        if kind not in {"chat", "idle", "completion"} or not isinstance(text, str):
            raise StoreError("INVALID_ARGUMENT", "Unknown wake kind or invalid text")
        if kind == "chat" or source_ref is not None:
            nonempty(source_ref, "source_ref")
        if len(event_id) > 200 or len(text) > 4096 or (source_ref is not None and len(source_ref) > 200):
            raise StoreError(
                "INVALID_ARGUMENT", "Wake text exceeds 4096 characters or identifier exceeds 200"
            )
        with self._transaction():
            self._authorized(self._space(space_id, scope))
            return self._enqueue_wake(space_id, event_id, kind, text, source_ref)

    def schedule_wake(self, space_id, scope, *, interval_seconds) -> bool:
        if type(interval_seconds) is not int or not 0 < interval_seconds <= 365 * 86400:
            raise StoreError("INVALID_ARGUMENT", "interval_seconds must be a positive bounded integer")
        with self._transaction():
            self._authorized(self._space(space_id, scope))
            now, _ = self._now()
            current = datetime.fromisoformat(now)
            state = self._artist_state(space_id)
            if state["next_due"] and current < datetime.fromisoformat(state["next_due"]):
                return False
            pending = self.db.execute(
                "SELECT 1 FROM artist_wakes WHERE space_id=? AND kind='idle' AND id>? LIMIT 1",
                (space_id, state["acked_through"]),
            ).fetchone()
            added = False if pending else self._enqueue_wake(space_id, f"idle:{now}", "idle", "", None)
            self.db.execute(
                "UPDATE artist_state SET next_due=? WHERE space_id=?",
                ((current + timedelta(seconds=interval_seconds)).isoformat(), space_id),
            )
            return added

    def claim_wake(self, space_id, scope, *, token):
        nonempty(token, "token")
        with self._transaction():
            space = self._space(space_id, scope)
            self._authorized(space)
            state = self._artist_state(space_id)
            now, _ = self._now()
            if state["backoff_until"] and datetime.fromisoformat(now) < datetime.fromisoformat(
                state["backoff_until"]
            ):
                return None
            active = self.db.execute(
                "SELECT * FROM artist_claims WHERE space_id=? AND state='active'",
                (space_id,),
            ).fetchone()
            if active and active["token"] != token:
                return None
            if not active:
                # Never recycle tokens: a delayed old worker must not ack a new batch.
                if self.db.execute(
                    "SELECT 1 FROM artist_claims WHERE space_id=? AND token=?", (space_id, token)
                ).fetchone():
                    return None
                batch = self.db.execute(
                    "SELECT id FROM artist_wakes WHERE space_id=? AND id>? ORDER BY id LIMIT 64",
                    (space_id, state["acked_through"]),
                ).fetchall()
                if not batch:
                    return None
                through = batch[-1]["id"]
                self.db.execute(
                    "INSERT INTO artist_claims VALUES(?,?,?,'active',?)",
                    (space_id, token, through, space["auth_version"]),
                )
                active = self.db.execute(
                    "SELECT * FROM artist_claims WHERE space_id=? AND token=?", (space_id, token)
                ).fetchone()
            events = [
                dict(row)
                for row in self.db.execute(
                    """SELECT event_id,kind,text,source_ref FROM artist_wakes
                WHERE space_id=? AND id>? AND id<=? ORDER BY id""",
                    (space_id, state["acked_through"], active["through"]),
                )
            ]
            return {
                "token": token,
                "through": active["through"],
                "events": events,
                "auth_version": active["auth_version"],
            }

    def finish_wake(self, space_id, scope, *, token, acknowledge=True, retry_after=0) -> None:
        nonempty(token, "token")
        if (
            type(acknowledge) is not bool
            or type(retry_after) is not int
            or not (retry_after == 0 or 60 <= retry_after <= 86400)
        ):
            raise StoreError("INVALID_ARGUMENT", "Invalid wake acknowledgement or retry interval")
        with self._transaction():
            self._space(space_id, scope)
            claim = self.db.execute(
                "SELECT * FROM artist_claims WHERE space_id=? AND token=?", (space_id, token)
            ).fetchone()
            if claim is None or claim["state"] in {"recovered", "released"}:
                raise StoreError("CLAIM_MISMATCH", "No active claim for this token")
            if claim["state"] == "finished":
                return
            now, _ = self._now()
            backoff = (
                (datetime.fromisoformat(now) + timedelta(seconds=retry_after)).isoformat()
                if retry_after
                else None
            )
            if acknowledge:
                self.db.execute(
                    "UPDATE artist_state SET acked_through=max(acked_through,?) WHERE space_id=?",
                    (claim["through"], space_id),
                )
            self.db.execute("UPDATE artist_state SET backoff_until=? WHERE space_id=?", (backoff, space_id))
            self.db.execute(
                "UPDATE artist_claims SET state=? WHERE space_id=? AND token=?",
                ("finished" if acknowledge else "released", space_id, token),
            )

    def recover_artist(self, space_id, scope, *, offline=False):
        if offline is not True:
            raise StoreError("OFFLINE_REQUIRED", "Stop previous workers before recovery")
        with self._transaction():
            self._space(space_id, scope)
            now, _ = self._now()
            self.db.execute(
                "UPDATE artist_claims SET state='recovered' WHERE space_id=? AND state='active'", (space_id,)
            )
            return self.db.execute(
                """UPDATE operations SET state='unknown',evidence='explicit offline recovery',updated_at=?
                WHERE space_id=? AND state IN ('submitting','accepted')""",
                (now, space_id),
            ).rowcount

    @staticmethod
    def _string_list(values, field):
        if not isinstance(values, list):
            raise StoreError("INVALID_ARGUMENT", f"{field} must be a list")
        for value in values:
            nonempty(value, field)
        return list(dict.fromkeys(values))

    @staticmethod
    def _decode(row, *fields):
        result = dict(row)
        for field in fields:
            result[field] = json.loads(result[field])
        return result

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
        refs = self._string_list(source_refs, "source_refs")
        if origin not in {"chat", "idle", "completion"} or (origin == "chat" and not refs):
            raise StoreError("INVALID_ARGUMENT", "Chat inspirations require verified source references")
        if type(ttl_seconds) is not int or not 0 < ttl_seconds <= 365 * 86400:
            raise StoreError("INVALID_ARGUMENT", "ttl_seconds must be a positive bounded integer")
        with self._transaction():
            space = self._space(space_id, scope)
            self._expected_authorization(space, expected_auth_version)
            self._authorized(space)
            now, _ = self._now()
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
            return self._decode(
                self.db.execute(
                    "SELECT * FROM inspirations WHERE space_id=? AND key=?", (space_id, key)
                ).fetchone(),
                "source_refs",
            )

    def list_inspirations(self, space_id, scope):
        with self.lock:
            self._authorized(self._space(space_id, scope))
            now, _ = self._now()
            return [
                self._decode(row, "source_refs")
                for row in self.db.execute(
                    "SELECT * FROM inspirations WHERE space_id=? AND expires_at>? ORDER BY updated_at,key",
                    (space_id, now),
                )
            ]

    def _artist_work(self, space_id, scope, work_id):
        self._space(space_id, scope)
        row = self.db.execute(
            "SELECT * FROM artist_works WHERE space_id=? AND work_id=?", (space_id, work_id)
        ).fetchone()
        if row is None:
            raise StoreError("NOT_FOUND", "Artist work not found in this space")
        return self._decode(row, "brief", "inspiration_keys")

    def _check_active_work(self, space_id, work_id):
        row = self.db.execute(
            """SELECT 1 FROM artist_works WHERE space_id=? AND work_id!=?
                              AND status IN ('planned','deferred','creating','review_pending') LIMIT 1""",
            (space_id, work_id),
        ).fetchone()
        if row:
            raise StoreError("WORK_BUSY", "This space already has an active artist work")

    def plan_work(self, space_id, scope, *, work_id, brief, inspiration_keys, expected_auth_version=None):
        nonempty(work_id, "work_id")
        if (
            not isinstance(brief, dict)
            or not {"intent", "subject", "mood", "prompt", "completion_criteria"} <= brief.keys()
        ):
            raise StoreError("INVALID_ARGUMENT", "A complete validated brief is required")
        payload = canonical_json(brief)
        keys = self._string_list(inspiration_keys, "inspiration_keys")
        with self._transaction():
            space = self._space(space_id, scope)
            self._expected_authorization(space, expected_auth_version)
            self._authorized(space)
            now, _ = self._now()
            old = self.db.execute(
                "SELECT * FROM artist_works WHERE space_id=? AND work_id=?", (space_id, work_id)
            ).fetchone()
            if old:
                if old["brief"] != payload or json.loads(old["inspiration_keys"]) != keys:
                    raise StoreError("REQUEST_CONFLICT", "Work brief and inspiration bindings are immutable")
                return self._artist_work(space_id, scope, work_id)
            self._check_active_work(space_id, work_id)
            for key in keys:
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
            for key in keys:
                self.db.execute(
                    "UPDATE inspirations SET consumed_by=? WHERE space_id=? AND key=?",
                    (work_id, space_id, key),
                )
            return self._artist_work(space_id, scope, work_id)

    def get_artist_work(self, space_id, scope, work_id):
        with self.lock:
            return self._artist_work(space_id, scope, work_id)

    def list_artist_works(self, space_id, scope):
        with self.lock:
            self._space(space_id, scope)
            return [
                self._decode(row, "brief", "inspiration_keys")
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
        with self._transaction():
            current = self._artist_work(space_id, scope, work_id)
            space = self._space(space_id, scope)
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
                    and self._version(space_id, scope, changes[key])["work_id"] != work_id
                ):
                    raise StoreError("NOT_FOUND", "Version does not belong to this work")
            if changes.get("pending_request") is not None:
                op = self._operation(space_id, scope, changes["pending_request"])
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
            return self._artist_work(space_id, scope, work_id)

    def _version(self, space_id, scope, version_id):
        self._space(space_id, scope)
        row = self.db.execute(
            "SELECT * FROM versions WHERE space_id=? AND version_id=?", (space_id, version_id)
        ).fetchone()
        if row is None:
            raise StoreError("NOT_FOUND", "Version not found in this space")
        return dict(row)

    def record_version(
        self, space_id, scope, *, work_id, version_id, request_id, job_id, data, mime, parent_id=None
    ):
        nonempty(version_id, "version_id")
        if not isinstance(data, bytes) or not 0 < len(data) <= 8 * 1024 * 1024:
            raise StoreError("INVALID_ARGUMENT", "Version data must be 1 byte to 8 MiB")
        if mime not in {"image/png", "image/jpeg", "image/webp"} or parent_id == version_id:
            raise StoreError("INVALID_ARGUMENT", "Invalid image MIME or self parent")
        digest = hashlib.sha256(data).hexdigest()
        with self._transaction():
            op = self._operation(space_id, scope, request_id)
            if (
                op["category"] != "render"
                or op["work_id"] != work_id
                or op["job_id"] != job_id
                or op["state"] in {"reserved", "rejected"}
                or job_id is None
            ):
                raise StoreError("NOT_FOUND", "Version requires its original bound render job")
            if parent_id is not None and self._version(space_id, scope, parent_id)["work_id"] != work_id:
                raise StoreError("NOT_FOUND", "Parent version belongs to another work")
            if op["action"] == "edit":
                if parent_id is None:
                    raise StoreError("REQUEST_CONFLICT", "An edit version requires its source parent")
                parent = self._version(space_id, scope, parent_id)
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
            now, _ = self._now()
            self.db.execute("INSERT INTO versions VALUES(?,?,?,?,?,?,?,?,?,?)", (space_id, *fields, now))
            return self._version(space_id, scope, version_id)

    def get_version(self, space_id, scope, version_id):
        with self.lock:
            return self._version(space_id, scope, version_id)

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

    def record_review(self, space_id, scope, *, version_id, request_id, review):
        if not isinstance(review, dict):
            raise StoreError("INVALID_ARGUMENT", "A validated review object is required")
        payload = canonical_json(review)
        with self._transaction():
            version = self._version(space_id, scope, version_id)
            op = self._operation(space_id, scope, request_id)
            if (
                op["action"] != "think"
                or op["work_id"] != version["work_id"]
                or op["state"] in {"reserved", "rejected", "failed"}
            ):
                raise StoreError(
                    "NOT_FOUND", "Review must belong to an attempted think operation on this work"
                )
            old = self.db.execute(
                "SELECT * FROM reviews WHERE space_id=? AND request_id=?", (space_id, request_id)
            ).fetchone()
            if old and (old["version_id"], old["review"]) != (version_id, payload):
                raise StoreError("REQUEST_CONFLICT", "Review is immutable")
            if not old:
                now, _ = self._now()
                self.db.execute(
                    """INSERT INTO reviews(space_id,work_id,version_id,request_id,review,created_at)
                                VALUES(?,?,?,?,?,?)""",
                    (space_id, version["work_id"], version_id, request_id, payload, now),
                )
                old = self.db.execute(
                    "SELECT * FROM reviews WHERE space_id=? AND request_id=?", (space_id, request_id)
                ).fetchone()
            return self._decode(old, "review")

    def list_reviews(self, space_id, scope, work_id):
        with self.lock:
            self.get_work(space_id, scope, work_id)
            return [
                self._decode(row, "review")
                for row in self.db.execute(
                    "SELECT * FROM reviews WHERE space_id=? AND work_id=? ORDER BY id",
                    (space_id, work_id),
                )
            ]

    def close(self):
        with self.lock:
            self.db.close()
