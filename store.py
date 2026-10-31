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
    normalize_objective,
    render_prompt,
    validate_catalog,
    validate_choice,
    validate_creative,
)


SCHEMA_VERSION = 6
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
_SCHEMA_3 = (
    """CREATE TABLE review_snapshots (
        space_id TEXT NOT NULL, version_id TEXT NOT NULL, auth_version INTEGER NOT NULL,
        context TEXT NOT NULL, input_digest TEXT NOT NULL, PRIMARY KEY(space_id,version_id),
        FOREIGN KEY(space_id,version_id) REFERENCES versions(space_id,version_id))""",
)
_SCHEMA_4 = (
    """CREATE TABLE profile_versions (
        space_id TEXT NOT NULL REFERENCES spaces, profile_id TEXT NOT NULL,
        revision INTEGER NOT NULL, data TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(space_id,profile_id,revision))""",
    """CREATE TABLE series (
        space_id TEXT NOT NULL REFERENCES spaces, series_id TEXT NOT NULL, title TEXT NOT NULL,
        goal TEXT NOT NULL, profile_id TEXT NOT NULL, profile_revision INTEGER NOT NULL,
        active INTEGER NOT NULL DEFAULT 1, closed INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(space_id,series_id), FOREIGN KEY(space_id,profile_id,profile_revision)
        REFERENCES profile_versions(space_id,profile_id,revision))""",
    """CREATE TABLE series_members (
        space_id TEXT NOT NULL, series_id TEXT NOT NULL, work_id TEXT NOT NULL, objective TEXT NOT NULL,
        PRIMARY KEY(space_id,work_id), UNIQUE(space_id,series_id,objective),
        FOREIGN KEY(space_id,series_id) REFERENCES series(space_id,series_id),
        FOREIGN KEY(space_id,work_id) REFERENCES artist_works(space_id,work_id))""",
)
_SCHEMA_5 = (
    """CREATE TABLE asset_catalog (
        space_id TEXT NOT NULL REFERENCES spaces, asset_id TEXT NOT NULL,
        source_version_id TEXT NOT NULL, work_id TEXT NOT NULL, job_id TEXT NOT NULL,
        sha256 TEXT NOT NULL, mime TEXT NOT NULL, size_bytes INTEGER NOT NULL,
        roles TEXT NOT NULL, tags TEXT NOT NULL, reason TEXT NOT NULL, summary TEXT NOT NULL,
        description TEXT NOT NULL, source_kind TEXT NOT NULL, review_request_id TEXT NOT NULL,
        auth_version INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
        PRIMARY KEY(space_id,asset_id), UNIQUE(space_id,source_version_id),
        FOREIGN KEY(space_id,source_version_id) REFERENCES versions(space_id,version_id))""",
    """CREATE TABLE asset_selection (
        space_id TEXT NOT NULL, asset_id TEXT NOT NULL, position INTEGER NOT NULL,
        auth_version INTEGER NOT NULL, selected INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(space_id,asset_id),
        FOREIGN KEY(space_id,asset_id) REFERENCES asset_catalog(space_id,asset_id))""",
)
_SCHEMA_6 = (
    # Rebuild both ends of the FK in one transaction. Renaming the replacement
    # parent updates the replacement child's FK without disabling constraints.
    """CREATE TABLE asset_catalog_v6 (
        space_id TEXT NOT NULL REFERENCES spaces, asset_id TEXT NOT NULL,
        source_version_id TEXT, work_id TEXT, job_id TEXT,
        sha256 TEXT NOT NULL, mime TEXT NOT NULL, size_bytes INTEGER NOT NULL,
        roles TEXT NOT NULL, tags TEXT NOT NULL, reason TEXT NOT NULL, summary TEXT NOT NULL,
        description TEXT NOT NULL, source_kind TEXT NOT NULL, review_request_id TEXT,
        auth_version INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
        PRIMARY KEY(space_id,asset_id), UNIQUE(space_id,source_version_id),
        FOREIGN KEY(space_id,source_version_id) REFERENCES versions(space_id,version_id),
        CHECK ((source_kind='reviewed_work' AND source_version_id IS NOT NULL AND work_id IS NOT NULL
                AND job_id IS NOT NULL AND review_request_id IS NOT NULL)
            OR (source_kind='authorized_chat' AND source_version_id IS NULL AND work_id IS NULL
                AND job_id IS NULL AND review_request_id IS NULL)))""",
    "INSERT INTO asset_catalog_v6 SELECT * FROM asset_catalog",
    """CREATE TABLE asset_selection_v6 (
        space_id TEXT NOT NULL, asset_id TEXT NOT NULL, position INTEGER NOT NULL,
        auth_version INTEGER NOT NULL, selected INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(space_id,asset_id),
        FOREIGN KEY(space_id,asset_id) REFERENCES asset_catalog_v6(space_id,asset_id))""",
    "INSERT INTO asset_selection_v6 SELECT * FROM asset_selection",
    "DROP TABLE asset_selection",
    "DROP TABLE asset_catalog",
    "ALTER TABLE asset_catalog_v6 RENAME TO asset_catalog",
    "ALTER TABLE asset_selection_v6 RENAME TO asset_selection",
    """CREATE TABLE external_assets (
        space_id TEXT NOT NULL, asset_id TEXT NOT NULL, event_id TEXT NOT NULL,
        message_index INTEGER NOT NULL, data BLOB NOT NULL, consent_ref TEXT NOT NULL,
        source_description TEXT NOT NULL,
        PRIMARY KEY(space_id,asset_id), UNIQUE(space_id,event_id,message_index),
        FOREIGN KEY(space_id,asset_id) REFERENCES asset_catalog(space_id,asset_id))""",
    """CREATE TABLE reference_policy (
        space_id TEXT PRIMARY KEY REFERENCES spaces, enabled INTEGER NOT NULL,
        consent_ref TEXT NOT NULL, version INTEGER NOT NULL, auth_version INTEGER NOT NULL)""",
    """CREATE TABLE asset_registrations (
        space_id TEXT NOT NULL, asset_id TEXT NOT NULL, request_id TEXT NOT NULL,
        binding_id TEXT NOT NULL, auth_version INTEGER NOT NULL, policy_version INTEGER NOT NULL,
        consent_ref TEXT NOT NULL, sha256 TEXT NOT NULL, mime TEXT NOT NULL, size_bytes INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('reserved','submitting','unknown','succeeded','failed')),
        receipt TEXT, reason TEXT, PRIMARY KEY(space_id,asset_id), UNIQUE(binding_id,space_id,request_id),
        FOREIGN KEY(space_id,asset_id) REFERENCES asset_catalog(space_id,asset_id))""",
)


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
                elif version not in {1, 2, 3, 4, 5, SCHEMA_VERSION}:
                    raise StoreError("SCHEMA_UNSUPPORTED", "Artist database schema is not supported")
                if version in {0, 1}:
                    for statement in _SCHEMA_2:
                        self.db.execute(statement)
                if version in {0, 1, 2}:
                    for statement in _SCHEMA_3:
                        self.db.execute(statement)
                if version in {0, 1, 2, 3}:
                    for statement in _SCHEMA_4:
                        self.db.execute(statement)
                if version in {0, 1, 2, 3, 4}:
                    for statement in _SCHEMA_5:
                        self.db.execute(statement)
                if version in {0, 1, 2, 3, 4, 5}:
                    for statement in _SCHEMA_6:
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

    def get_render_operation_by_job(self, space_id: str, scope: Scope, job_id: str) -> Dict[str, Any]:
        """Resolve a persisted render job without guessing among ambiguous bindings."""
        nonempty(job_id, "job_id")
        with self.lock:
            self._space(space_id, scope)
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

    def validate_submitting(self, space_id: str, scope: Scope, request_id: str) -> Dict[str, Any]:
        """Read-only dispatch check in one snapshot; never renew or refund a claim."""
        with self._transaction():
            _, day = self._now()
            row = self._operation(space_id, scope, request_id)
            space = self._space(space_id, scope)
            if row["state"] != "submitting" or row["job_id"] is not None:
                raise StoreError("INVALID_STATE", "Dispatch requires an unbound submitting operation")
            self._authorized(space)
            if row["auth_version"] != space["auth_version"]:
                raise StoreError("AUTH_CHANGED", "Authorization changed since reservation")
            if row["budget_day"] != day:
                raise StoreError("BUDGET_DAY_CHANGED", "Dispatch cannot cross its recorded budget day")
            self._check_budget(
                space_id, row["category"], day, adding=0, work_id=row["work_id"], edit=row["action"] == "edit"
            )
            return self._snapshot(row)

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

    def _chat_interrupts_wait(self, space, state) -> bool:
        # Infer only a successful model wait from the existing durable ledger.
        # A later released/recovered/active claim, or any later operation, must
        # not borrow an old wait's evidence to bypass a failure backoff.
        space_id = space["space_id"]
        if self.db.execute(
            """SELECT 1 FROM (SELECT kind FROM artist_wakes
            WHERE space_id=? AND id>? ORDER BY id LIMIT 64) WHERE kind='chat' LIMIT 1""",
            (space_id, state["acked_through"]),
        ).fetchone() is None:
            # Never wake a model for chat beyond the batch it will actually see,
            # or skip/ack the preceding backlog merely to reach that chat.
            return False
        claim = self.db.execute(
            "SELECT * FROM artist_claims WHERE space_id=? ORDER BY rowid DESC LIMIT 1", (space_id,)
        ).fetchone()
        if claim is None or claim["state"] != "finished" or claim["through"] != state["acked_through"]:
            return False
        operation = self.db.execute(
            "SELECT * FROM operations WHERE space_id=? ORDER BY id DESC LIMIT 1", (space_id,)
        ).fetchone()
        if operation is None or (
            operation["action"], operation["category"], operation["state"], operation["trigger_ref"],
            operation["auth_version"], operation["scope_snapshot"], operation["binding_id"],
        ) != (
            "think", "think", "succeeded", str(claim["through"]),
            claim["auth_version"], space["scope"], space["binding_id"],
        ):
            return False
        try:
            params, result = json.loads(operation["params_json"]), json.loads(operation["evidence"])
        except (TypeError, ValueError):
            return False
        # The old claim's epoch proves the wait's origin, not current authority.
        # claim_wake checks the live grant; dispatch still checks budgets/epochs
        # and the existing unknown-operation locks, including after re-grant.
        return (
            isinstance(params, dict) and params.get("task") == "plan"
            and isinstance(result, dict) and isinstance(result.get("decision"), dict)
            and result["decision"].get("action") == "wait"
        )

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
                if not self._chat_interrupts_wait(space, state):
                    return None
                # Proof, interruption and the new claim share BEGIN IMMEDIATE:
                # chat arriving during model await/finish is never lost, and a
                # second connection cannot clear a competing worker's backoff.
                self.db.execute("UPDATE artist_state SET backoff_until=NULL WHERE space_id=?", (space_id,))
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

    def configure_creative(self, space_id, scope, *, profiles, series, expected_auth_version):
        """Trusted Host configuration: synchronize visibility, never grant authority.

        Old revisions and closed series are durable history. A removed item can
        be configured again, but closing a series is irreversible through sync.
        """
        validate_catalog(profiles, series)
        profiles, series = json.loads(canonical_json([profiles, series]))
        with self._transaction():
            space = self._space(space_id, scope)
            self._expected_authorization(space, expected_auth_version)
            self._authorized(space)
            self.db.execute("UPDATE profile_versions SET active=0 WHERE space_id=?", (space_id,))
            self.db.execute("UPDATE series SET active=0 WHERE space_id=?", (space_id,))
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
            return {"profiles": self.list_profiles(space_id, scope), "series": self.list_series(space_id, scope)}

    def list_profiles(self, space_id, scope) -> list[dict]:
        with self.lock:
            self._authorized(self._space(space_id, scope))
            return [
                self._decode(row, "data") for row in self.db.execute(
                    "SELECT profile_id,revision,data FROM profile_versions WHERE space_id=? AND active=1 "
                    "ORDER BY profile_id,revision", (space_id,),
                )
            ]

    def list_series(self, space_id, scope) -> list[dict]:
        with self.lock:
            self._authorized(self._space(space_id, scope))
            return [
                dict(row) for row in self.db.execute(
                    """SELECT s.series_id,s.title,s.goal,s.profile_id,s.profile_revision FROM series AS s
                    JOIN profile_versions AS p ON p.space_id=s.space_id AND p.profile_id=s.profile_id
                    AND p.revision=s.profile_revision
                    WHERE s.space_id=? AND s.active=1 AND s.closed=0 AND p.active=1 ORDER BY s.series_id""",
                    (space_id,),
                )
            ]

    def close_series(self, space_id, scope, series_id, expected_auth_version):
        nonempty(series_id, "series_id")
        with self._transaction():
            space = self._space(space_id, scope)
            self._expected_authorization(space, expected_auth_version)
            self._authorized(space)
            old = self.db.execute(
                "SELECT 1 FROM series WHERE space_id=? AND series_id=?", (space_id, series_id)
            ).fetchone()
            if old is None:
                raise StoreError("NOT_FOUND", "Series not found in this space")
            self.db.execute("UPDATE series SET closed=1 WHERE space_id=? AND series_id=?", (space_id, series_id))

    def _freeze_creative(self, space_id, scope, creative):
        profiles, series = self.list_profiles(space_id, scope), self.list_series(space_id, scope)
        selected = validate_choice(creative, profiles, series)
        profile = next(item for item in profiles if item["profile_id"] == selected["profile_id"])
        item = next((item for item in series if item["series_id"] == selected["series_id"]), None)
        return {**selected, "profile": profile["data"], "series": item}

    def get_asset(self, space_id, scope, asset_id):
        """Scoped historical metadata; no image bytes or authorization renewal."""
        with self.lock:
            self._space(space_id, scope)
            row = self.db.execute(
                "SELECT * FROM asset_catalog WHERE space_id=? AND asset_id=?", (space_id, asset_id)
            ).fetchone()
            if row is None:
                raise StoreError("NOT_FOUND", "Asset not found in this space")
            return self._decode(row, "roles", "tags")

    def configure_reference_policy(self, space_id, scope, *, enabled, consent_ref, expected_auth_version):
        if (
            type(enabled) is not bool or type(consent_ref) is not str or len(consent_ref) > 500
            or type(expected_auth_version) is not int or expected_auth_version < 0
        ):
            raise StoreError("INVALID_ARGUMENT", "Reference policy needs a boolean and bounded consent reference")
        if enabled:
            nonempty(consent_ref, "consent_ref")
        with self._transaction():
            space = self._space(space_id, scope)
            self._expected_authorization(space, expected_auth_version)
            self._authorized(space)
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
        space = self._space(space_id, scope)
        self._expected_authorization(space, expected_auth_version)
        self._authorized(space)
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
        with self._transaction():
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
        with self._transaction():
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
            now, _ = self._now()
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
            self._space(space_id, scope)
            row = self.db.execute(
                "SELECT * FROM asset_registrations WHERE space_id=? AND asset_id=?", (space_id, asset_id)
            ).fetchone()
            return self._registration_snapshot(row) if row is not None else None

    def reserve_asset_registration(self, space_id, scope, *, asset_id, expected_auth_version):
        with self._transaction():
            policy = self._reference_policy(space_id, scope, expected_auth_version)
            space = self._space(space_id, scope)
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
        with self._transaction():
            policy = self._reference_policy(space_id, scope, expected_auth_version)
            space = self._space(space_id, scope)
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
        with self._transaction():
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
        with self._transaction():
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
        with self._transaction():
            space = self._space(space_id, scope)
            self._expected_authorization(space, expected_auth_version)
            self._authorized(space)
            version = self._version(space_id, scope, version_id)
            if (
                not isinstance(version["data"], bytes) or not version["data"]
                or hashlib.sha256(version["data"]).hexdigest() != version["sha256"]
                or version["mime"] not in {"image/png", "image/jpeg", "image/webp"}
            ):
                raise StoreError("ARTIFACT_INVALID", "Archive source bytes failed verification")
            proof = self._accepted_review(space_id, scope, space, version)
            if proof is None:
                raise StoreError("VERSION_NOT_ACCEPTED", "Only a version selected by a valid accepting review may be archived")
            old = self.db.execute(
                "SELECT * FROM asset_catalog WHERE space_id=? AND asset_id=?", (space_id, asset_id)
            ).fetchone()
            if old is not None:
                if (old["roles"], old["tags"], old["reason"]) != (canonical_json(roles), canonical_json(tags), reason):
                    raise StoreError("REQUEST_CONFLICT", "Archive roles, tags and reason are immutable")
                self._asset_source(space_id, scope, self._decode(old, "roles", "tags"))
                return self._decode(old, "roles", "tags")
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
            now, _ = self._now()
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
            self._authorized(self._space(space_id, scope))
            candidates = []
            for row in self.db.execute("SELECT * FROM asset_catalog WHERE space_id=? AND active=1", (space_id,)):
                asset = self._decode(row, "roles", "tags")
                tags = [unicodedata.normalize("NFKC", tag).casefold() for tag in asset["tags"]]
                text = unicodedata.normalize("NFKC", asset["summary"] + " " + " ".join(asset["tags"])).casefold()
                if terms and not all(term in text for term in terms):
                    continue
                score = sum(10 if term in tags else 3 if any(term in tag for tag in tags) else 1 for term in terms)
                candidates.append((-score, asset["asset_id"], self._asset_summary(asset)))
            return [item[2] for item in sorted(candidates, key=lambda item: item[:2])[:limit]]

    def _asset_source(self, space_id, scope, asset):
        if asset["source_kind"] == "authorized_chat":
            self._space(space_id, scope)
            row = self.db.execute("SELECT data FROM external_assets WHERE space_id=? AND asset_id=?",
                                  (space_id, asset["asset_id"])).fetchone()
            if (
                row is None or not isinstance(row["data"], bytes) or len(row["data"]) != asset["size_bytes"]
                or hashlib.sha256(row["data"]).hexdigest() != asset["sha256"]
            ):
                raise StoreError("ARTIFACT_INVALID", "Original external image bytes changed")
            image = validate_asset_image(row["data"], asset["mime"])
            return image
        version = self._version(space_id, scope, asset["source_version_id"])
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
        with self._transaction():
            space = self._space(space_id, scope)
            self._expected_authorization(space, expected_auth_version)
            self._authorized(space)
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
            self._space(space_id, scope)
            assets = [self._decode(row, "roles", "tags") for row in self.db.execute(
                """SELECT a.* FROM asset_selection AS s JOIN asset_catalog AS a
                ON a.space_id=s.space_id AND a.asset_id=s.asset_id
                WHERE s.space_id=? AND s.selected=1 AND a.active=1 ORDER BY s.position,a.asset_id""", (space_id,),
            )]
            return self._asset_details(assets)

    def deactivate_asset(self, space_id, scope, asset_id, expected_auth_version):
        with self._transaction():
            space = self._space(space_id, scope)
            self._expected_authorization(space, expected_auth_version)
            self._authorized(space)
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
                    space_id, scope, self._space(space_id, scope)["auth_version"]
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
        with self._transaction():
            space = self._space(space_id, scope)
            self._expected_authorization(space, expected_auth_version)
            self._authorized(space)
            now, _ = self._now()
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
                return self._artist_work(space_id, scope, work_id)
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
            if objective is not None:
                self.db.execute(
                    "INSERT INTO series_members(space_id,series_id,work_id,objective) VALUES(?,?,?,?)",
                    (space_id, snapshot["series_id"], work_id, objective),
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

    def get_review_snapshot(self, space_id, scope, version_id) -> Optional[Dict[str, Any]]:
        """Historical metadata remains readable after delegation is withdrawn."""
        with self.lock:
            self._space(space_id, scope)
            row = self.db.execute(
                "SELECT * FROM review_snapshots WHERE space_id=? AND version_id=?", (space_id, version_id)
            ).fetchone()
            return self._decode(row, "context") if row is not None else None

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
        current = self._version(space_id, scope, version_id)
        work = self._artist_work(space_id, scope, current["work_id"])
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
            image = self._version(space_id, scope, identifier)
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
        with self._transaction():
            space = self._space(space_id, scope)
            self._expected_authorization(space, expected_auth_version)
            self._authorized(space)
            old = self.db.execute(
                "SELECT * FROM review_snapshots WHERE space_id=? AND version_id=?", (space_id, version_id)
            ).fetchone()
            if old is not None:
                if old["auth_version"] != space["auth_version"]:
                    raise StoreError("AUTH_CHANGED", "Review snapshot belongs to an older authorization")
                if old["context"] != payload or old["input_digest"] != digest:
                    raise StoreError("REQUEST_CONFLICT", "Review snapshot inputs are immutable")
            self._validate_review_context(space_id, scope, version_id, context, check_best=old is None)
            if old is None:
                self.db.execute(
                    "INSERT INTO review_snapshots(space_id,version_id,auth_version,context,input_digest) "
                    "VALUES(?,?,?,?,?)", (space_id, version_id, space["auth_version"], payload, digest),
                )
            return self.get_review_snapshot(space_id, scope, version_id)

    def version_accepted(self, space_id, scope, version_id, expected_auth_version=None) -> bool:
        """Prove acceptance using the successful original review, never work status.

        A review row alone is audit history: older callers may record one while
        an operation is still unknown. Malformed or mismatched evidence cannot
        certify an image, but is retained unchanged for investigation.
        """
        with self._transaction():
            space = self._space(space_id, scope)
            self._expected_authorization(space, expected_auth_version)
            target = self._version(space_id, scope, version_id)
            if not space["authorized"]:
                return False
            return self._accepted_review(space_id, scope, space, target) is not None

    def _accepted_review(self, space_id, scope, space, target):
        """Shared proof check for acceptance and archiving, inside the caller's transaction."""
        rows = self.db.execute(
                """SELECT r.version_id,r.request_id,r.review,o.params_json,o.evidence FROM reviews AS r
                JOIN operations AS o ON o.space_id=r.space_id AND o.request_id=r.request_id
                WHERE r.space_id=? AND r.work_id=? AND o.work_id=r.work_id
                AND o.action='think' AND o.category='think' AND o.state='succeeded'
                AND o.auth_version=? AND o.scope_snapshot=? AND o.binding_id=? ORDER BY r.id""",
                (space_id, target["work_id"], space["auth_version"], space["scope"], space["binding_id"]),
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
        current = self._version(space_id, scope, row["version_id"])
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
        snapshot = self.get_review_snapshot(space_id, scope, current["version_id"])
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
