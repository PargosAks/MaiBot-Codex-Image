"""持久化唤醒队列、批次领取、进度判断及离线恢复。"""

from datetime import datetime, timedelta
from typing import Any, Mapping, Optional, Protocol
import hashlib
import json
import sqlite3

from .ledger_repository import ACTIVE_WORK, LedgerQueries, expected_authorization, require_authorized
from .policy import Scope, StoreError, canonical_json, nonempty
from .sqlite_uow import SqliteUnitOfWork


class ReviewEvidence(Protocol):
    def accepted_review(
        self, space_id: str, scope: Scope, space: sqlite3.Row, target: Mapping[str, Any] | sqlite3.Row, *,
        after_operation_id: int = 0,
    ) -> Optional[sqlite3.Row]: ...


class WakeRepository:
    def __init__(self, uow: SqliteUnitOfWork, queries: LedgerQueries, review_evidence: ReviewEvidence):
        self._uow = uow
        self._queries = queries
        self._review_evidence = review_evidence

    def _artist_state(self, space_id):
        self._uow.db.execute("INSERT OR IGNORE INTO artist_state(space_id) VALUES(?)", (space_id,))
        return self._uow.db.execute("SELECT * FROM artist_state WHERE space_id=?", (space_id,)).fetchone()

    def enqueue_in_transaction(
        self, space_id: str, event_id: str, kind: str, text: str, source_ref: str | None,
    ) -> bool:
        """由持有写事务的调用方入队，例如与群灵感导入一起提交。"""
        prior = self._uow.db.execute(
            "SELECT * FROM artist_wakes WHERE space_id=? AND event_id=?",
            (space_id, event_id),
        ).fetchone()
        if prior:
            if (prior["kind"], prior["text"], prior["source_ref"]) != (kind, text, source_ref):
                raise StoreError("REQUEST_CONFLICT", "Wake event already has different content")
            return False
        self._uow.db.execute(
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
        with self._uow.transaction():
            require_authorized(self._queries.space(space_id, scope))
            return self.enqueue_in_transaction(space_id, event_id, kind, text, source_ref)

    def schedule_wake(self, space_id, scope, *, interval_seconds) -> bool:
        if type(interval_seconds) is not int or not 0 < interval_seconds <= 365 * 86400:
            raise StoreError("INVALID_ARGUMENT", "interval_seconds must be a positive bounded integer")
        with self._uow.transaction():
            require_authorized(self._queries.space(space_id, scope))
            now, _ = self._uow.now()
            current = datetime.fromisoformat(now)
            state = self._artist_state(space_id)
            if state["next_due"] and current < datetime.fromisoformat(state["next_due"]):
                # A shorter configured interval must not inherit an old, distant
                # deadline. Clamp once; subsequent ticks leave the earlier due
                # time intact. Claims still enforce backoff and Artist enforces
                # each work's next_check_at independently of this idle timer.
                due = (current + timedelta(seconds=interval_seconds)).isoformat()
                if datetime.fromisoformat(state["next_due"]) > datetime.fromisoformat(due):
                    self._uow.db.execute("UPDATE artist_state SET next_due=? WHERE space_id=?", (due, space_id))
                return False
            pending = self._uow.db.execute(
                "SELECT 1 FROM artist_wakes WHERE space_id=? AND kind='idle' AND id>? LIMIT 1",
                (space_id, state["acked_through"]),
            ).fetchone()
            added = False if pending else self.enqueue_in_transaction(space_id, f"idle:{now}", "idle", "", None)
            self._uow.db.execute(
                "UPDATE artist_state SET next_due=? WHERE space_id=?",
                ((current + timedelta(seconds=interval_seconds)).isoformat(), space_id),
            )
            return added

    def schedule_progress_wake(self, space_id, scope, *, interval_seconds=15) -> bool:
        """Advance existing work without shortening the idle inspiration timer.

        State, queue and claim checks share the same transaction. The durable
        event key bounds repeated receipt polling across workers and restarts;
        a phase change can proceed on the next Host tick. No receipt is invented
        and no model/drawing/sending operation is dispatched here.
        """
        if type(interval_seconds) is not int or not 1 <= interval_seconds <= 60:
            raise StoreError("INVALID_ARGUMENT", "Progress interval must be 1..60 seconds")
        with self._uow.transaction():
            space = self._queries.space(space_id, scope)
            require_authorized(space)
            now, _ = self._uow.now()
            current = datetime.fromisoformat(now)
            state = self._artist_state(space_id)
            if state["backoff_until"] and current < datetime.fromisoformat(state["backoff_until"]):
                return False
            if self._uow.db.execute(
                "SELECT 1 FROM artist_claims WHERE space_id=? AND state='active'", (space_id,)
            ).fetchone() or self._uow.db.execute(
                "SELECT 1 FROM artist_wakes WHERE space_id=? AND id>? LIMIT 1",
                (space_id, state["acked_through"]),
            ).fetchone():
                return False
            phase = "advance"
            work = self._uow.db.execute(
                """SELECT * FROM artist_works WHERE space_id=?
                AND status IN (?,?,?,?) ORDER BY rowid LIMIT 1""",
                (space_id, *ACTIVE_WORK),
            ).fetchone()
            if work is None:
                # A retained image can precede backend turn completion. Only
                # reconcile that original render, even after sharing/abandoning.
                phase = "reconcile"
                work = self._uow.db.execute(
                    """SELECT w.* FROM artist_works w JOIN operations o
                    ON o.space_id=w.space_id AND o.request_id=w.pending_request
                    WHERE w.space_id=? AND o.category='render'
                    AND o.state IN ('submitting','accepted','unknown') ORDER BY w.rowid LIMIT 1""",
                    (space_id,),
                ).fetchone()
            if work is None:
                phase = "post-review"
                work = self._pending_post_review_work(space_id, scope, space)
            if work is None or (
                work["next_check_at"] and current < datetime.fromisoformat(work["next_check_at"])
            ):
                return False
            signature = {key: work[key] for key in (
                "work_id", "status", "pending_request", "parent_version", "best_version", "next_check_at",
            )}
            digest = hashlib.sha256(canonical_json(signature).encode("utf-8")).hexdigest()
            source_ref = f"progress:{phase}:{digest}"
            previous = self._uow.db.execute(
                """SELECT event_id,source_ref FROM artist_wakes WHERE space_id=?
                AND kind='completion' AND event_id GLOB 'progress:*' ORDER BY id DESC LIMIT 1""",
                (space_id,),
            ).fetchone()
            if previous is not None and previous["source_ref"] == source_ref:
                last_wake = datetime.fromisoformat(previous["event_id"].removeprefix(source_ref + ":"))
                if current - last_wake < timedelta(seconds=interval_seconds):
                    return False
            return self.enqueue_in_transaction(space_id, f"{source_ref}:{now}", "completion", "", source_ref)

    def _pending_post_review_work(self, space_id, scope, space):
        # Artist exposes only the latest twenty terminal works to the planner.
        # Count only a successful plan that saw this work as finished; seeing
        # it as planned or under review did not answer what to do with the art.
        # 没有 work_id 的分享优先核对版本和 job；只有缺少 version_id 的旧记录才按 job 归属。
        # 查同空间的原始渲染操作，即使版本还未落库也能识别歧义。所有匹配行须指向同一作品。
        candidates = self._uow.db.execute(
            """SELECT w.*, COALESCE((SELECT MAX(p.id) FROM operations p,
                json_each(p.params_json,'$.context.works') visible
                WHERE p.space_id=w.space_id AND p.action='think' AND p.category='think' AND p.state='succeeded'
                AND p.auth_version=? AND p.scope_snapshot=? AND p.binding_id=?
                AND json_extract(p.params_json,'$.task')='plan'
                AND CASE WHEN visible.type='object' THEN
                    json_extract(visible.value,'$.work_id')=w.work_id
                    AND json_extract(visible.value,'$.status')='finished' END),0) AS last_plan
            FROM (SELECT rowid AS work_order,* FROM artist_works
                WHERE space_id=? AND status IN ('finished','abandoned')
                ORDER BY rowid DESC LIMIT 20) w
            JOIN versions v ON v.space_id=w.space_id AND v.work_id=w.work_id AND v.version_id=w.best_version
            WHERE w.status='finished' AND NOT EXISTS (
                SELECT 1 FROM operations s WHERE s.space_id=w.space_id AND s.action='share'
                AND (s.work_id=w.work_id OR (s.work_id IS NULL AND CASE
                    WHEN json_type(s.params_json,'$.version_id') IS NULL THEN (
                        SELECT COUNT(*)=COUNT(r.work_id) AND MIN(r.work_id)=w.work_id AND MAX(r.work_id)=w.work_id
                        FROM operations r WHERE r.space_id=s.space_id AND r.category='render'
                        AND r.action IN ('submit','edit') AND r.job_id=json_extract(s.params_json,'$.job_id')
                    )
                    ELSE EXISTS (
                        SELECT 1 FROM versions shared
                        WHERE shared.space_id=w.space_id AND shared.work_id=w.work_id
                        AND shared.version_id=json_extract(s.params_json,'$.version_id')
                        AND shared.job_id=json_extract(s.params_json,'$.job_id')
                    ) END))
            )
            ORDER BY w.work_order DESC""",
            (space["auth_version"], space["scope"], space["binding_id"], space_id),
        )
        for work in candidates:
            target = {"work_id": work["work_id"], "version_id": work["best_version"]}
            if self._review_evidence.accepted_review(space_id, scope, space, target, after_operation_id=work["last_plan"]) is not None:
                return work
        return None

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
        if offline is not True:
            raise StoreError("OFFLINE_REQUIRED", "Stop Host writers and hold RuntimeLease before recovery")
        if type(expected_auth_version) is not int or expected_auth_version < 1:
            raise StoreError("INVALID_ARGUMENT", "expected_auth_version must be a positive integer")
        for name, value in (
            ("work_id", work_id), ("last_wait_request_id", last_wait_request_id),
            ("event_id", event_id), ("reason", reason),
        ):
            nonempty(value, name)
            if len(value) > (500 if name == "reason" else 200):
                raise StoreError("INVALID_ARGUMENT", "Recovery reason or identifier is too long")
        request = {
            "operation": "restore_planned_wait", "expected_auth_version": expected_auth_version,
            "work_id": work_id, "last_wait_request_id": last_wait_request_id, "reason": reason,
        }
        with self._uow.transaction():
            space = self._queries.space(space_id, scope)
            expected_authorization(space, expected_auth_version)
            if space["authorized"]:
                raise StoreError("RECOVERY_UNSAFE", "Revoke delegation before restoring a planned wait")
            if self._uow.db.execute(
                "SELECT 1 FROM artist_claims WHERE space_id=? AND state='active'", (space_id,)
            ).fetchone() or self._uow.db.execute(
                """SELECT 1 FROM operations WHERE space_id=?
                AND state IN ('reserved','submitting','accepted','unknown') LIMIT 1""", (space_id,)
            ).fetchone() or self._uow.db.execute(
                """SELECT 1 FROM asset_registrations WHERE space_id=?
                AND state IN ('reserved','submitting','unknown') LIMIT 1""", (space_id,)
            ).fetchone():
                raise StoreError("RECOVERY_UNSAFE", "Active claims or unresolved operations prohibit recovery")
            work = self._uow.db.execute(
                "SELECT * FROM artist_works WHERE space_id=? AND work_id=?", (space_id, work_id)
            ).fetchone()
            if work is None:
                raise StoreError("NOT_FOUND", "Planned work not found in this space")
            if work["status"] != "planned" or work["pending_request"] or work["next_check_at"]:
                raise StoreError("RECOVERY_UNSAFE", "Only an undeferred plan without a pending request can recover")
            operation = self._queries.operation(space_id, scope, last_wait_request_id)
            work_digest = hashlib.sha256(canonical_json(dict(work)).encode("utf-8")).hexdigest()
            wait_digest = hashlib.sha256(canonical_json(dict(operation)).encode("utf-8")).hexdigest()
            prior = self._uow.db.execute(
                "SELECT * FROM artist_wakes WHERE space_id=? AND event_id=?", (space_id, event_id)
            ).fetchone()
            if prior:
                try:
                    receipt = json.loads(prior["text"])
                except (TypeError, ValueError):
                    receipt = None
                if (
                    prior["kind"] != "idle" or prior["source_ref"] != last_wait_request_id
                    or not isinstance(receipt, dict) or receipt.get("request") != request
                ):
                    raise StoreError("REQUEST_CONFLICT", "Recovery event already has different content")
                if receipt.get("work_digest") != work_digest or receipt.get("wait_digest") != wait_digest:
                    raise StoreError("RECOVERY_UNSAFE", "Recovered work or wait has since changed")
                return False
            # A different key must not repeat a repair already recorded for this
            # wait, even if no worker has consumed the first internal event yet.
            if self._uow.db.execute(
                "SELECT 1 FROM artist_wakes WHERE space_id=? AND kind='idle' AND source_ref=?",
                (space_id, last_wait_request_id),
            ).fetchone():
                raise StoreError("REQUEST_CONFLICT", "This wait already has a recovery event")
            state = self._uow.db.execute("SELECT * FROM artist_state WHERE space_id=?", (space_id,)).fetchone()
            claim = self._uow.db.execute(
                "SELECT * FROM artist_claims WHERE space_id=? ORDER BY rowid DESC LIMIT 1", (space_id,)
            ).fetchone()
            latest = self._uow.db.execute(
                "SELECT id FROM operations WHERE space_id=? ORDER BY id DESC LIMIT 1", (space_id,)
            ).fetchone()
            grants = self._uow.db.execute(
                "SELECT version,authorized FROM grants WHERE space_id=? AND version IN (?,?) ORDER BY version",
                (space_id, expected_auth_version - 1, expected_auth_version),
            ).fetchall()
            if (
                state is None or claim is None or latest is None
                or claim["state"] != "finished" or claim["through"] != state["acked_through"]
                or claim["auth_version"] != expected_auth_version - 1
                or [(row["version"], row["authorized"]) for row in grants]
                != [(expected_auth_version - 1, 1), (expected_auth_version, 0)]
                or latest["id"] != operation["id"]
                or (operation["action"], operation["category"], operation["state"], operation["trigger_ref"],
                    operation["auth_version"], operation["scope_snapshot"], operation["binding_id"])
                != ("think", "think", "succeeded", str(claim["through"]), claim["auth_version"],
                    space["scope"], space["binding_id"])
            ):
                raise StoreError("RECOVERY_UNSAFE", "Latest finished claim and old authorization do not prove this wait")
            now, _ = self._uow.now()
            try:
                params = json.loads(operation["params_json"])
                result = json.loads(operation["evidence"])
                decision = result["decision"]
                delay = decision["delay_seconds"]
                known_work = next(w for w in params["context"]["works"] if w["work_id"] == work_id)
                backoff = datetime.fromisoformat(state["backoff_until"])
                started = backoff - timedelta(seconds=delay)
                proven = (
                    params["task"] == "plan" and decision["action"] == "wait"
                    and type(delay) is int and 60 <= delay <= 86400
                    and known_work["status"] == "planned" and known_work["brief"] == json.loads(work["brief"])
                    and datetime.fromisoformat(operation["updated_at"]) <= started <= datetime.fromisoformat(now)
                    and datetime.fromisoformat(now) < backoff
                )
            except (KeyError, TypeError, ValueError, StopIteration, OverflowError):
                proven = False
            if not proven:
                raise StoreError("RECOVERY_UNSAFE", "A successful explicit wait for the retained plan is required")
            receipt = canonical_json({
                "request": request, "work_digest": work_digest, "wait_digest": wait_digest,
                "claim_token": claim["token"], "through": claim["through"],
                "old_auth_version": claim["auth_version"], "operation_id": operation["id"],
                "previous_next_due": state["next_due"], "previous_backoff_until": state["backoff_until"],
                "restored_at": now,
            })
            if len(receipt) > 4096:
                raise StoreError("INVALID_ARGUMENT", "Encoded recovery receipt exceeds 4096 characters")
            self.enqueue_in_transaction(space_id, event_id, "idle", receipt, last_wait_request_id)
            self._uow.db.execute(
                "UPDATE artist_state SET next_due=?,backoff_until=NULL WHERE space_id=?", (now, space_id)
            )
            return True

    def _chat_interrupts_wait(self, space, state) -> bool:
        # Infer only a successful model wait from the existing durable ledger.
        # A later released/recovered/active claim, or any later operation, must
        # not borrow an old wait's evidence to bypass a failure backoff.
        space_id = space["space_id"]
        if self._uow.db.execute(
            """SELECT 1 FROM (SELECT kind FROM artist_wakes
            WHERE space_id=? AND id>? ORDER BY id LIMIT 64) WHERE kind='chat' LIMIT 1""",
            (space_id, state["acked_through"]),
        ).fetchone() is None:
            # Never wake a model for chat beyond the batch it will actually see,
            # or skip/ack the preceding backlog merely to reach that chat.
            return False
        claim = self._uow.db.execute(
            "SELECT * FROM artist_claims WHERE space_id=? ORDER BY rowid DESC LIMIT 1", (space_id,)
        ).fetchone()
        if claim is None or claim["state"] != "finished" or claim["through"] != state["acked_through"]:
            return False
        operation = self._uow.db.execute(
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
        with self._uow.transaction():
            space = self._queries.space(space_id, scope)
            require_authorized(space)
            state = self._artist_state(space_id)
            now, _ = self._uow.now()
            if state["backoff_until"] and datetime.fromisoformat(now) < datetime.fromisoformat(
                state["backoff_until"]
            ):
                if not self._chat_interrupts_wait(space, state):
                    return None
                # Proof, interruption and the new claim share BEGIN IMMEDIATE:
                # chat arriving during model await/finish is never lost, and a
                # second connection cannot clear a competing worker's backoff.
                self._uow.db.execute("UPDATE artist_state SET backoff_until=NULL WHERE space_id=?", (space_id,))
            active = self._uow.db.execute(
                "SELECT * FROM artist_claims WHERE space_id=? AND state='active'",
                (space_id,),
            ).fetchone()
            if active and active["token"] != token:
                return None
            if not active:
                # Never recycle tokens: a delayed old worker must not ack a new batch.
                if self._uow.db.execute(
                    "SELECT 1 FROM artist_claims WHERE space_id=? AND token=?", (space_id, token)
                ).fetchone():
                    return None
                # A progress plan can already have a paid result keyed by this
                # batch's watermark. Resume its exact batch with a new token;
                # newly queued idle/chat must not create a second planning key.
                prior = self._uow.db.execute(
                    """SELECT c.through FROM (SELECT through,state FROM artist_claims
                        WHERE space_id=? ORDER BY rowid DESC LIMIT 1) c
                    WHERE c.state IN ('recovered','released') AND EXISTS (
                        SELECT 1 FROM artist_wakes WHERE space_id=? AND id>? AND id<=c.through
                        AND kind='completion' AND event_id GLOB 'progress:*')""",
                    (space_id, space_id, state["acked_through"]),
                ).fetchone()
                if prior is not None:
                    through = prior["through"]
                else:
                    batch = self._uow.db.execute(
                        "SELECT id FROM artist_wakes WHERE space_id=? AND id>? ORDER BY id LIMIT 64",
                        (space_id, state["acked_through"]),
                    ).fetchall()
                    if not batch:
                        return None
                    through = batch[-1]["id"]
                self._uow.db.execute(
                    "INSERT INTO artist_claims VALUES(?,?,?,'active',?)",
                    (space_id, token, through, space["auth_version"]),
                )
                active = self._uow.db.execute(
                    "SELECT * FROM artist_claims WHERE space_id=? AND token=?", (space_id, token)
                ).fetchone()
            events = [
                dict(row)
                for row in self._uow.db.execute(
                    """SELECT event_id,kind,text,source_ref FROM artist_wakes
                WHERE space_id=? AND id>? AND id<=? ORDER BY id""",
                    (space_id, state["acked_through"], active["through"]),
                )
            ]
            for event in events:
                if event["kind"] == "idle" and event["source_ref"] is not None:
                    try:
                        audit = json.loads(event["text"])
                    except (TypeError, ValueError):
                        continue
                    if (
                        isinstance(audit, dict) and isinstance(audit.get("request"), dict)
                        and audit["request"].get("operation") == "restore_planned_wait"
                        and audit["request"].get("last_wait_request_id") == event["source_ref"]
                    ):
                        # The durable repair receipt is administrative evidence,
                        # never fresh drawing inspiration supplied to a model.
                        event["text"] = ""
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
        with self._uow.transaction():
            self._queries.space(space_id, scope)
            claim = self._uow.db.execute(
                "SELECT * FROM artist_claims WHERE space_id=? AND token=?", (space_id, token)
            ).fetchone()
            if claim is None or claim["state"] in {"recovered", "released"}:
                raise StoreError("CLAIM_MISMATCH", "No active claim for this token")
            if claim["state"] == "finished":
                return
            now, _ = self._uow.now()
            backoff = (
                (datetime.fromisoformat(now) + timedelta(seconds=retry_after)).isoformat()
                if retry_after
                else None
            )
            if acknowledge:
                self._uow.db.execute(
                    "UPDATE artist_state SET acked_through=max(acked_through,?) WHERE space_id=?",
                    (claim["through"], space_id),
                )
            self._uow.db.execute("UPDATE artist_state SET backoff_until=? WHERE space_id=?", (backoff, space_id))
            self._uow.db.execute(
                "UPDATE artist_claims SET state=? WHERE space_id=? AND token=?",
                ("finished" if acknowledge else "released", space_id, token),
            )

    def recover_artist(self, space_id, scope, *, offline=False):
        if offline is not True:
            raise StoreError("OFFLINE_REQUIRED", "Stop previous workers before recovery")
        with self._uow.transaction():
            self._queries.space(space_id, scope)
            now, _ = self._uow.now()
            self._uow.db.execute(
                "UPDATE artist_claims SET state='recovered' WHERE space_id=? AND state='active'", (space_id,)
            )
            return self._uow.db.execute(
                """UPDATE operations SET state='unknown',evidence='explicit offline recovery',updated_at=?
                WHERE space_id=? AND state IN ('submitting','accepted')""",
                (now, space_id),
            ).rowcount
