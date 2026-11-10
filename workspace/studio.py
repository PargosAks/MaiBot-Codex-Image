"""Read-only Studio projection; importing/opening it never starts the artist.

Integration contract (private WebUI APIs, never model tools):
    @API("studio_spaces", version="1", public=False)
    async def studio_spaces(self, offset: int = 0, limit: int = 20): ...
    @API("studio_summary", version="1", public=False)
    async def studio_summary(self, space_id: str): ...
    @API("studio_records", version="1", public=False)
    async def studio_records(self, space_id: str, section: str,
                             offset: int = 0, limit: int = 20, work_id: str = ""): ...
    @API("studio_image", version="1", public=False)
    async def studio_image(self, space_id: str, version_id: str,
                           thumbnail: bool = True, offset: int = 0): ...

Each wrapper returns await asyncio.to_thread(Studio.for_plugin(self).METHOD, ...).
The Host WebUI gateway authenticates administrators and invokes private APIs as
the owning plugin. Do not make these APIs public or expose for_plugin to tools.
The group_thoughts and experimental group_inspirations records sections require
for_plugin's configured group binding and matching persisted ledger and database
ownership. They are administrative projections, never chat/model summaries.
Chat integration uses Studio(data_dir, scopes={space_id: verified_scope}), then
await asyncio.to_thread(view.summary, space_id), followed by
snapshot_from_summary(space_id, verified_scope, summary) from studio_chat.py.
Hold the plugin lifecycle lock around this scoped read. Never use for_plugin's
administrative scope list in chat. The adapter verifies Host identity/consent and
adds only bounded text data to planner/replyer context; this module never sends.
For a Host-verified incoming-message acknowledgement, inspiration_receipt reads
only its exact wake key and returns the same minimal authorization envelope.
It is not a WebUI API and proves receipt only, never model processing or artwork.
All methods are synchronous: keep filesystem work off Host loops.
Bundle integration must include workspace/studio.py, webui.json and assets/studio.html explicitly.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping, Optional
import base64
import hashlib
import json
import sqlite3
import time


from ..domain.policy import Scope, StoreError, budget_day
from .images import display_image


MAX_PAGE = 50
MAX_OFFSET = 10000
MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_CHUNK_BYTES = 192 * 1024  # base64 + JSON stays below Host's 512 KiB result cap
MAX_IMAGE_PIXELS = 16 * 1024 * 1024
MAX_TEXT = 4000
MAX_PAGE_BYTES = 384 * 1024
HELD = ("reserved", "submitting", "accepted", "unknown")
SECTIONS = {
    "works": ("artist_works", "work_id,status,brief,reason,pending_request,parent_version,best_version,next_check_at"),
    "tasks": ("operations", "request_id,work_id,action,category,state,job_id,budget_day,created_at,updated_at"),
    "versions": ("versions", "version_id,work_id,job_id,sha256,mime,parent_id,created_at"),
    "reviews": ("reviews", "work_id,version_id,request_id,review,created_at"),
    "profiles": ("profile_versions", "profile_id,revision,data,active"),
    "series": ("series", "series_id,title,goal,profile_id,profile_revision,active,closed"),
    "assets": ("asset_catalog", "asset_id,source_version_id,work_id,sha256,mime,roles,tags,summary,source_kind,active"),
}
JSON_FIELDS = frozenset({"brief", "review", "data", "roles", "tags"})
JSON_TEXT_FIELDS = {
    "brief": {"subject": 80, "intent": 400, "mood": 200, "completion_criteria": 400},
    "review": {"decision": 40, "reason": 500, "edit_prompt": 500, "preserve": 400, "best_version_id": 200},
    "data": {"title": 120, "palette": 400, "technique": 400, "source": 400},
}
REVIEW_DECISIONS = frozenset({"accept", "revise", "discard"})
WAIT_TEXT = {
    "AUTHORIZATION_REVOKED": "持续委托已撤销",
    "AUTONOMY_DISABLED": "自主创作未启用",
    "STATE_NOT_CREATED": "尚未建立创作状态",
    "THINK_UNKNOWN": "模型结果未知，等待核实原调用",
    "RENDER_UNKNOWN": "绘图结果未知，等待核实原任务",
    "SEND_UNKNOWN": "发送结果未知，保留原记录且不重发",
    "INFLIGHT": "已有在途操作",
    "SPACE_BUDGET_EXHAUSTED": "当前空间当日预算已用尽",
    "SPACE_INFLIGHT_LIMIT": "当前空间并发额度已占满",
    "SCHEDULED_WAIT": "等待计划检查时间",
    "BACKOFF": "等待退避时间",
    "RUNTIME_BLOCKED": "运行器报告阻塞，需管理员检查",
    "SHARED_THINK_LIMIT": "共享思考预算已达上限",
    "SHARED_RENDER_LIMIT": "共享绘图预算已达上限",
    "SHARED_SHARE_LIMIT": "共享分享预算已达上限",
    "SHARED_INFLIGHT_LIMIT": "共享并发额度已占满",
    "SHARED_POLICY_MISSING": "共享预算尚未配置",
}


def _page(offset, limit):
    if type(offset) is not int or not 0 <= offset <= MAX_OFFSET:
        raise StoreError("INVALID_ARGUMENT", "offset must be 0..10000")
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise StoreError("INVALID_ARGUMENT", "limit must be 1..50")


def _identifier(value):
    if not isinstance(value, str) or not value.strip() or value == "*" or len(value) > 200:
        raise StoreError("INVALID_ARGUMENT", "A bounded explicit identifier is required")
    return value


def _text(value, limit=MAX_TEXT):
    return value[:limit] if isinstance(value, str) else value


class Studio:
    """A trusted adapter supplies a finite per-space read allowlist, never '*'.

    Read authority is distinct from permission to draw/send: a revoked grant can
    still be inspected by its configured administrator or exact original owner.
    Scope equality is checked again against the persisted space on every read.
    """

    def __init__(self, data_dir: Path, *, scopes: Mapping[str, Scope],
                 clock: Optional[Callable[[], datetime]] = None,
                 enabled: Optional[bool] = None, blocked: Optional[Mapping[str, bool]] = None,
                 group_bindings: Optional[Mapping[str, str]] = None):
        if not isinstance(scopes, Mapping) or len(scopes) > 32:
            raise StoreError("INVALID_SCOPE", "A finite trusted read allowlist is required")
        self.scopes = dict(scopes)
        for space_id, scope in self.scopes.items():
            _identifier(space_id)
            if not isinstance(scope, Scope):
                raise StoreError("INVALID_SCOPE", "Only trusted-adapter Scope values are accepted")
        # Administrative group reads require an explicit configured binding.
        # Chat adapters pass scopes only and cannot acquire this read authority.
        if group_bindings is not None and not isinstance(group_bindings, Mapping):
            raise StoreError("INVALID_SCOPE", "Trusted group bindings must be a mapping")
        self.administrative = group_bindings is not None
        self.group_bindings = dict(group_bindings or {})
        for space_id, group_id in self.group_bindings.items():
            if space_id not in self.scopes:
                raise StoreError("INVALID_SCOPE", "Group binding requires a configured read scope")
            _identifier(group_id)
        self.root = Path(data_dir).resolve()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.enabled = enabled
        self.blocked = dict(blocked or {})

    @classmethod
    def for_plugin(cls, plugin):
        """Administrative adapter, for private authenticated WebUI wrappers only."""
        scopes, group_bindings = {}, {}
        for item in plugin.config.autonomy.spaces:
            # Blank defaults have no read authority. Duplicate IDs fail closed.
            if not item.space_id.strip():
                continue
            if item.space_id in scopes:
                raise StoreError("INVALID_SCOPE", "Duplicate configured space")
            scopes[item.space_id] = Scope(item.platform, item.stream_id, item.user_id)
            if item.group_id.strip():
                group_bindings[item.space_id] = item.group_id
        runtime = plugin.artist_runtime
        blocked = {key: bool(value) for key, value in runtime.errors.items()} if runtime else {}
        return cls(Path(plugin.ctx.paths.data_dir), scopes=scopes,
                   enabled=plugin.config.plugin.enabled and plugin.config.autonomy.enabled, blocked=blocked,
                   group_bindings=group_bindings)

    def _scope(self, space_id):
        _identifier(space_id)
        if space_id not in self.scopes:
            raise StoreError("NOT_FOUND", "Space not available for this reader")
        return self.scopes[space_id]

    def _path(self, filename):
        path = self.root / filename
        # Neither persisted image paths nor caller paths are accepted. Reject
        # links before SQLite can follow the DB or its journal sidecars.
        for candidate in (path, *(self.root / (filename + suffix) for suffix in ("-wal", "-shm", "-journal"))):
            if candidate.is_symlink() or candidate.is_junction():
                raise StoreError("UNSAFE_STATE_PATH", "Linked state files are not readable")
        if path.exists() and not path.is_file():
            raise StoreError("UNSAFE_STATE_PATH", "State must be a regular file")
        return path

    @contextmanager
    def _open(self) -> Iterator[Optional[sqlite3.Connection]]:
        path = self._path("artist.sqlite3")
        if not path.exists():
            yield None
            return
        db = None
        try:
            db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            deadline = time.monotonic() + 2
            db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in range(1, 11):
                raise StoreError("SCHEMA_UNSUPPORTED", "Read-only Studio supports ledger schemas 1..10")
            db.execute("BEGIN")
            yield db
        except sqlite3.Error as exc:
            raise StoreError("STATE_READ_FAILED", "Read-only ledger query failed") from exc
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _tables(db):
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def single_work_for_stream(self, platform: str, stream_id: str) -> bool:
        """Internal command guard; no authority or counters are exposed to chat."""
        with self._open() as db:
            if db is None or "single_work_policies" not in self._tables(db):
                return False
            for row in db.execute("SELECT s.scope FROM single_work_policies p JOIN spaces s USING(space_id) WHERE p.enabled=1"):
                scope = json.loads(row[0])
                if scope["platform"] == platform and scope["stream_id"] == stream_id:
                    return True
            return False

    def _space(self, db, space_id):
        scope = self._scope(space_id)
        if db is None:
            return None
        row = db.execute("SELECT authorized,auth_version,scope FROM spaces WHERE space_id=?", (space_id,)).fetchone()
        if row is None or row["scope"] != scope.snapshot():
            raise StoreError("NOT_FOUND", "Space not available for this reader")
        return row

    def spaces(self, offset: int = 0, limit: int = 20) -> dict:
        _page(offset, limit)
        # Iterate the trusted finite list, never enumerate all database spaces.
        with self._open() as db:
            items = []
            for key in sorted(self.scopes):
                try:
                    row = self._space(db, key)
                except StoreError as exc:
                    if exc.code != "NOT_FOUND":
                        raise
                    continue
                items.append({"space_id": key, "authorized": bool(row["authorized"]) if row else False,
                              "state_exists": row is not None})
            return {"items": items[offset:offset + limit], "offset": offset, "limit": limit,
                    "has_more": len(items) > offset + limit}

    def inspiration_receipt(self, space_id: str, message_id: str, *, expected_auth_version: int) -> dict:
        """Confirm one exact Host message was enqueued, without reading its text.

        Call only on a Studio restricted to the verified chat Scope. Unlike the
        historical summary, this receipt requires active continuous delegation.
        The plugin guarantees a newly committed Host ingress before requesting
        this receipt and supplies its runtime's authorization version. Schemas
        1..7 do not bind wakes to an authorization epoch: this read verifies the
        expected current grant, not message freshness. Reauthorization permits
        new ingress; stale runtime versions fail closed. Never infer an epoch
        from wake IDs, claims, message IDs or grant times.
        Repeated reads confirm the same receipt, not a fresh ingestion event.
        """
        self._scope(space_id)
        if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 195:
            raise StoreError("INVALID_ARGUMENT", "Host message_id must fit the exact bounded chat wake key")
        if type(expected_auth_version) is not int or expected_auth_version < 1:
            raise StoreError("INVALID_ARGUMENT", "Expected authorization version must be a positive integer")
        with self._open() as db:
            space = self._space(db, space_id)
            if space is None:
                raise StoreError("NOT_FOUND", "Message receipt not available for this reader")
            version = space["auth_version"]
            if self.enabled is False or space["authorized"] != 1 or type(version) is not int or version < 1:
                raise StoreError("AUTH_REQUIRED", "Continuous delegation is not active")
            if version != expected_auth_version:
                raise StoreError("AUTH_CHANGED", "Message receipt authorization version changed")
            grant = db.execute("SELECT authorized FROM grants WHERE space_id=? AND version=?",
                               (space_id, version)).fetchone()
            if grant is None or grant["authorized"] != 1:
                raise StoreError("AUTH_REQUIRED", "Continuous delegation has no matching active grant")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artist_wakes' LIMIT 1").fetchone() is None:
                raise StoreError("NOT_FOUND", "Message receipt not available for this reader")
            receipt = db.execute("""SELECT 1 FROM artist_wakes
                WHERE space_id=? AND event_id=? AND kind='chat' AND source_ref=? LIMIT 1""",
                                 (space_id, "chat:" + message_id, message_id)).fetchone()
            if receipt is None:
                raise StoreError("NOT_FOUND", "Message receipt not available for this reader")
            return {"space_id": space_id, "read_only": True,
                    "authorization": {"authorized": True, "version": version}, "inspiration_received": True}

    def summary(self, space_id: str) -> dict:
        self._scope(space_id)
        now = self.clock()
        day = budget_day(now)
        result = {"space_id": space_id, "day": day, "read_only": True, "state_exists": False,
                  "authorization": {"authorized": False, "version": None, "enabled": self.enabled},
                  "budget": {}, "counts": {}, "waiting": [], "global_budget": "availability_only",
                  "shared_constraints": {},
                  "recent_works": [], "current_work": None,
                  "coverage": "仅含当前空间账本的近期作品；未见记录不代表从未创作，不可猜测题材或分享结果。"}
        reasons = []
        if self.enabled is False:
            reasons.append("AUTONOMY_DISABLED")
        with self._open() as db:
            space = self._space(db, space_id)
            # Only the administrative view knows which scopes are groups.
            # Chat summaries never acquire group-policy read authority.
            if self.administrative:
                result["sharing"] = self._sharing(db, space_id, space)
            if space is None:
                reasons.append("STATE_NOT_CREATED")
            else:
                result["state_exists"] = True
                if "single_work_policies" in self._tables(db):
                    single = db.execute("SELECT work_id FROM single_work_policies WHERE space_id=? AND enabled=1",
                                        (space_id,)).fetchone()
                    if single is not None:
                        counts = dict(db.execute("SELECT action,count(*) FROM operations WHERE space_id=? GROUP BY action",
                                                 (space_id,)))
                        result["single_work"] = {"enabled": True, "work_id": single[0],
                                                 "create_used": bool(counts.get("submit", 0)),
                                                 "edit_used": bool(counts.get("edit", 0)),
                                                 "share_used": bool(counts.get("share", 0))}
                result["authorization"].update(authorized=bool(space["authorized"]), version=space["auth_version"])
                if not space["authorized"]:
                    reasons.append("AUTHORIZATION_REVOKED")
                policy = db.execute("SELECT * FROM limits WHERE scope_key=?", (space_id,)).fetchone()
                usage = db.execute("""SELECT category,
                    sum(state IN ('reserved','submitting','accepted','unknown')) held,
                    sum(state IN ('succeeded','failed') AND budget_day=?) consumed
                    FROM operations WHERE space_id=? GROUP BY category""", (day, space_id)).fetchall()
                usages = {row["category"]: row for row in usage}
                for category in ("think", "render", "share"):
                    row = usages.get(category)
                    held, consumed = (row["held"], row["consumed"]) if row else (0, 0)
                    cap = policy[category] if policy else None
                    result["budget"][category] = {"limit": cap, "held": held, "consumed": consumed,
                                                   "remaining": max(0, cap - held - consumed) if cap is not None else None}
                result["budget"]["max_inflight"] = policy["max_inflight"] if policy else None
                result["budget"]["max_revisions"] = policy["max_revisions"] if policy else None
                # Only classify the shared constraint. Never return its policy,
                # usage, remaining count, space list or identities to this view.
                shared = db.execute("SELECT * FROM limits WHERE scope_key='*'").fetchone()
                shared_usage = db.execute("""SELECT category,
                    sum(state IN ('reserved','submitting','accepted','unknown')) held,
                    sum(state IN ('succeeded','failed') AND budget_day=?) consumed
                    FROM operations GROUP BY category""", (day,)).fetchall()
                shared_totals = {row["category"]: row["held"] + row["consumed"] for row in shared_usage}
                shared_inflight = sum(row["held"] for row in shared_usage)
                blocked = {category: shared is None or shared_totals.get(category, 0) >= shared[category]
                           for category in ("think", "render", "share")}
                blocked["inflight"] = shared is None or shared_inflight >= shared["max_inflight"]
                result["shared_constraints"] = blocked
                if shared is None:
                    reasons.append("SHARED_POLICY_MISSING")
                else:
                    reasons.extend("SHARED_" + category.upper() + "_LIMIT" for category, value in blocked.items() if value)
                if any(item["remaining"] == 0 for item in result["budget"].values() if isinstance(item, dict)):
                    reasons.append("SPACE_BUDGET_EXHAUSTED")
                inflight = sum(result["budget"][key]["held"] for key in ("think", "render", "share"))
                for category in ("think", "render", "share"):
                    item = result["budget"][category]
                    item["space_remaining"] = item["remaining"]
                    item["available_now"] = bool(item["remaining"] and policy and inflight < policy["max_inflight"]
                                                  and not blocked[category] and not blocked["inflight"])
                if policy and inflight >= policy["max_inflight"]:
                    reasons.append("SPACE_INFLIGHT_LIMIT")
                unknown = db.execute("""SELECT DISTINCT category FROM operations WHERE space_id=?
                    AND state='unknown'""", (space_id,)).fetchall()
                reasons.extend({"think": "THINK_UNKNOWN", "render": "RENDER_UNKNOWN", "share": "SEND_UNKNOWN"}
                               [row[0]] for row in unknown if row[0] in {"think", "render", "share"})
                if inflight:
                    reasons.append("INFLIGHT")
                tables = self._tables(db)
                for label, table in (("works", "artist_works"), ("versions", "versions"), ("reviews", "reviews")):
                    result["counts"][label] = db.execute(
                        f"SELECT count(*) FROM {table} WHERE space_id=?", (space_id,)).fetchone()[0] if table in tables else 0
                if "artist_state" in tables:
                    state = db.execute("SELECT next_due,backoff_until FROM artist_state WHERE space_id=?",
                                       (space_id,)).fetchone()
                    if state:
                        for field, code in (("next_due", "SCHEDULED_WAIT"), ("backoff_until", "BACKOFF")):
                            if self._future(state[field], now):
                                reasons.append(code)
                if "artist_works" in tables and db.execute("""SELECT 1 FROM artist_works
                    WHERE space_id=? AND next_check_at>? LIMIT 1""", (space_id, now.astimezone(timezone.utc).isoformat())).fetchone():
                    reasons.append("SCHEDULED_WAIT")
                # The legacy journal has no owner column. Only join job IDs
                # already proven to belong to this exact space, never list it.
                if self._unknown_delivery(db, space_id):
                    reasons.append("SEND_UNKNOWN")
                if "artist_works" in tables:
                    recent = db.execute("""SELECT work_id,status,
                        CASE WHEN json_valid(brief) THEN CASE WHEN json_type(brief,'$.subject')='text'
                        THEN substr(json_extract(brief,'$.subject'),1,80) END END AS subject
                        FROM artist_works WHERE space_id=? ORDER BY rowid DESC LIMIT 5""", (space_id,)).fetchall()
                    result["recent_works"] = [self._work_summary(db, space_id, row) for row in recent]
                    current = db.execute("""SELECT work_id,status,
                        CASE WHEN json_valid(brief) THEN CASE WHEN json_type(brief,'$.subject')='text'
                        THEN substr(json_extract(brief,'$.subject'),1,80) END END AS subject
                        FROM artist_works WHERE space_id=? AND status IN ('planned','deferred','creating','review_pending')
                        ORDER BY rowid DESC LIMIT 1""", (space_id,)).fetchone()
                    result["current_work"] = self._work_summary(db, space_id, current) if current else None
        if self.blocked.get(space_id):
            reasons.append("RUNTIME_BLOCKED")
        result["waiting"] = [{"code": code, "text": WAIT_TEXT[code]} for code in dict.fromkeys(reasons)]
        return result

    def _sharing(self, db, space_id, space):
        group_id = self.group_bindings.get(space_id)
        denied = {"mode": "collect_only", "allowed": False}
        if group_id is None:
            if space is not None:
                # Removing a configured group binding cannot turn its saved
                # policy into permission for an apparently private space.
                tables = self._tables(db)
                for table in ("group_share_policy", "group_thought_policy"):
                    if table in tables and db.execute(
                            f"SELECT 1 FROM {table} WHERE space_id=? AND group_id!='' LIMIT 1",
                            (space_id,)).fetchone():
                        return denied
            return {"mode": "private", "allowed": bool(space and space["authorized"])}
        if space is None or db.execute("PRAGMA user_version").fetchone()[0] < 9 or (
                "group_share_policy" not in self._tables(db)):
            return denied
        scope = self.scopes[space_id]
        policy = db.execute("SELECT * FROM group_share_policy WHERE space_id=?", (space_id,)).fetchone()
        # A configured group is denied until its own complete binding and
        # current grant have a persisted policy. Legacy groups never inherit
        # the permissive private-chat behavior just because the table is absent.
        if policy is None or (policy["scope_snapshot"], policy["platform"], policy["stream_id"],
                              policy["group_id"], policy["auth_version"]) != (
                scope.snapshot(), scope.platform, scope.stream_id, group_id, space["auth_version"]):
            return denied
        if policy["mode"] not in {"collect_only", "original_group"}:
            return denied
        return {"mode": policy["mode"],
                "allowed": bool(space["authorized"] and policy["mode"] == "original_group")}

    @staticmethod
    def _future(value, now):
        if not isinstance(value, str):
            return False
        try:
            when = datetime.fromisoformat(value)
            return when.utcoffset() is not None and when > now
        except ValueError:
            return False

    def _unknown_delivery(self, db, space_id):
        path = self._path("delivery.sqlite3")
        if not path.exists():
            return False
        # Only attach the fixed, validated read-only receipt file. Drive this
        # indexed lookup from scoped operation IDs; never scan other receipts.
        db.execute("ATTACH DATABASE ? AS receipts", (path.as_uri() + "?mode=ro",))
        return bool(db.execute("""SELECT 1 FROM operations AS op
            JOIN receipts.delivery AS receipt ON receipt.job=op.job_id
            WHERE op.space_id=? AND receipt.state IN ('sending','send_unknown') LIMIT 1""",
                               (space_id,)).fetchone())

    def _work_summary(self, db, space_id, row):
        """All evidence stays in the caller's one read transaction and scope."""
        work_id = row["work_id"]
        generated = db.execute("SELECT count(*) FROM versions WHERE space_id=? AND work_id=?",
                               (space_id, work_id)).fetchone()[0]
        review = db.execute("""SELECT CASE WHEN json_valid(review) THEN
            CASE WHEN json_type(review,'$.decision')='text' THEN substr(json_extract(review,'$.decision'),1,40) END END
            FROM reviews WHERE space_id=? AND work_id=? ORDER BY id DESC LIMIT 1""",
                            (space_id, work_id)).fetchone()
        decision = "pending" if generated else "not_generated"
        if review:
            decision = review[0] or "unknown"
            if decision not in REVIEW_DECISIONS:
                decision = "unknown"
        operations = db.execute("""SELECT state FROM operations WHERE space_id=? AND work_id=?
            AND category='share' ORDER BY id DESC LIMIT 1""", (space_id, work_id)).fetchall()
        unknown = db.execute("""SELECT 1 FROM operations WHERE space_id=? AND work_id=?
            AND category='share' AND state='unknown' LIMIT 1""", (space_id, work_id)).fetchone()
        receipts = []
        if any(item[1] == "receipts" for item in db.execute("PRAGMA database_list")):
            receipts = db.execute("""SELECT DISTINCT receipt.state FROM versions AS v
                JOIN receipts.delivery AS receipt ON receipt.job=v.job_id
                WHERE v.space_id=? AND v.work_id=?
                AND receipt.state IN ('sending','send_unknown','sent','send_failed')""", (space_id, work_id)).fetchall()
        states = {item[0] for item in receipts}
        if unknown or "send_unknown" in states:
            share = "send_unknown"
        elif "sending" in states:
            share = "sending"
        elif operations:
            share = {"succeeded": "sent", "failed": "send_failed", "submitting": "sending",
                     "accepted": "sending", "reserved": "queued"}.get(operations[0][0], "not_shared")
        elif "sent" in states:
            share = "sent"
        elif "send_failed" in states:
            share = "send_failed"
        else:
            share = "not_shared"
        return {"work_id": _text(work_id, 200), "subject": row["subject"], "status": _text(row["status"], 40),
                "generated": generated > 0, "version_count": generated, "review_state": decision, "share_state": share}

    def records(self, space_id: str, section: str, offset: int = 0, limit: int = 20, work_id: str = "") -> dict:
        self._scope(space_id)
        _page(offset, limit)
        if section == "group_thoughts":
            if work_id:
                raise StoreError("INVALID_ARGUMENT", "Group thoughts have no work filter")
            return self._group_thoughts(space_id, offset, limit)
        if section == "group_inspirations":
            if work_id:
                raise StoreError("INVALID_ARGUMENT", "Group inspirations have no work filter")
            return self._group_inspirations(space_id, offset, limit)
        if section not in SECTIONS:
            raise StoreError("INVALID_ARGUMENT", "Unknown Studio section")
        if work_id:
            _identifier(work_id)
        table, fields = SECTIONS[section]
        if work_id and "work_id" not in fields.split(","):
            raise StoreError("INVALID_ARGUMENT", "This section has no work filter")
        with self._open() as db:
            self._space(db, space_id)
            if db is None or table not in self._tables(db):
                return {"items": [], "offset": offset, "limit": limit, "has_more": False,
                        "next_offset": None, "available": False}
            where, args = "space_id=?", [space_id]
            if work_id:
                where += " AND work_id=?"
                args.append(work_id)
            # Restrict both text and rows in SQL, before fetching any payload.
            numeric = {"revision", "profile_revision", "active", "closed"}
            projection = ",".join(key if key in numeric else f"substr({key},1,{MAX_TEXT}) AS {key}"
                                  for key in fields.split(",") if key not in JSON_FIELDS)
            rows = db.execute(f"SELECT rowid AS _rowid,{projection} FROM {table} WHERE {where} ORDER BY rowid DESC LIMIT ? OFFSET ?",
                              (*args, limit + 1, offset)).fetchall()
            if section == "works":
                self._unknown_delivery(db, space_id)
            items = []
            size = 0
            for row in rows[:limit]:
                item = dict(row)
                rowid = item.pop("_rowid")
                for key in JSON_FIELDS.intersection(fields.split(",")):
                    item[key] = self._json_summary(db, table, key, space_id, rowid)
                if section == "works":
                    subject = item["brief"].get("subject") if isinstance(item["brief"], dict) else None
                    item.update(self._work_summary(db, space_id, {"work_id": item["work_id"],
                                                                "status": item["status"], "subject": _text(subject, 80)}))
                if section in {"profiles", "series"} and "creative_sources" in self._tables(db):
                    identity = item["profile_id"] if section == "profiles" else item["series_id"]
                    revision = item["revision"] if section == "profiles" else 1
                    source = db.execute("""SELECT substr(origin,1,80) AS origin,substr(source_ref,1,160) AS source_ref
                        FROM creative_sources WHERE space_id=? AND kind=? AND entity_id=? AND revision=?""",
                                        (space_id, "profile" if section == "profiles" else "series", identity, revision)).fetchone()
                    item["provenance"] = dict(source) if source else None
                item_size = len(json.dumps(item, ensure_ascii=True).encode("ascii"))
                if size + item_size > MAX_PAGE_BYTES:
                    break
                items.append(item)
                size += item_size
            more = len(rows) > len(items) and offset + len(items) <= MAX_OFFSET
            return {"items": items, "offset": offset, "limit": limit,
                    "has_more": more, "next_offset": offset + len(items) if more else None, "available": True}

    def _group_thoughts(self, space_id: str, offset: int, limit: int) -> dict:
        """Read MaiBot's own themes, with ledger and configured group ownership.

        Adoption belongs to ledger v8 and means a plan was created, not that
        rendering succeeded. Do not import writers, read request tokens or
        expose record_count as a member/message count. Opening this
        view must never create/migrate either database or wake the artist.
        """
        empty = {"items": [], "offset": offset, "limit": limit, "has_more": False,
                 "next_offset": None, "available": False}
        with self._open() as db:
            space = self._space(db, space_id)
            group_id = self.group_bindings.get(space_id)
            if space is None or group_id is None:
                return empty
            path = self._path("group_thoughts.sqlite3")
            if not path.exists():
                return empty
            # The writer owns a DELETE-journal DB. Even mode=ro can create WAL
            # sidecars, so reject that storage format before SQLite attaches it.
            if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm")):
                raise StoreError("UNSAFE_STATE_PATH", "Unexpected thoughts WAL sidecars")
            try:
                with path.open("rb") as source:
                    header = source.read(20)
            except OSError as exc:
                raise StoreError("STATE_READ_FAILED", "Read-only thoughts header query failed") from exc
            if header[18:20] == b"\x02\x02":
                raise StoreError("SCHEMA_UNSUPPORTED", "Read-only Studio supports DELETE-journal thoughts only")
            db.execute("ATTACH DATABASE ? AS thoughts", (path.as_uri() + "?mode=ro",))
            if db.execute("PRAGMA thoughts.user_version").fetchone()[0] != 1:
                raise StoreError("SCHEMA_UNSUPPORTED", "Read-only Studio supports thoughts schema 1")
            binding = db.execute("""SELECT owner_id,platform,stream_id,group_id
                FROM thoughts.thought_groups WHERE space_id=?""", (space_id,)).fetchone()
            scope = self.scopes[space_id]
            if binding is None or (binding["owner_id"], binding["platform"], binding["stream_id"], binding["group_id"]) != (
                    scope.user_id, scope.platform, scope.stream_id, group_id):
                raise StoreError("NOT_FOUND", "Group thoughts not available for this reader")
            # Older ledgers have no source-link table. Never migrate them just
            # to view notes. A v8 link is visible only through its complete
            # persisted group/scope binding and a work in that exact space.
            adoption_join, adoption_args = "", ()
            adoption_status, associated_work = "'unadopted'", "NULL"
            if db.execute("PRAGMA main.user_version").fetchone()[0] >= 8:
                adoption_join = """LEFT JOIN (
                    SELECT link.space_id,link.note_id,link.adopted_by
                    FROM main.group_thought_links link
                    JOIN main.group_thought_policy policy
                        ON policy.space_id=link.space_id AND policy.group_id=link.group_id
                    JOIN main.spaces owner
                        ON owner.space_id=policy.space_id AND owner.scope=policy.scope_snapshot
                    JOIN main.artist_works work
                        ON work.space_id=link.space_id AND work.work_id=link.adopted_by
                    WHERE link.space_id=? AND policy.scope_snapshot=? AND policy.platform=?
                        AND policy.stream_id=? AND policy.group_id=?
                        AND link.adopted_at IS NOT NULL
                    ) adopted ON adopted.space_id=topic.space_id AND adopted.note_id=topic.note_id"""
                adoption_args = (space_id, scope.snapshot(), scope.platform, scope.stream_id, group_id)
                adoption_status = "CASE WHEN adopted.adopted_by IS NULL THEN 'unadopted' ELSE 'adopted' END"
                associated_work = "substr(adopted.adopted_by,1,200)"
            rows = db.execute(f"""SELECT
                substr(topic.note_id,1,200) AS note_id,substr(topic.title,1,1000) AS summary,
                'MaiBot 群聊构思' AS source,
                strftime('%Y-%m-%dT%H:%M:%SZ',topic.first_seen,'unixepoch') AS created_at,
                strftime('%Y-%m-%dT%H:%M:%SZ',topic.last_seen,'unixepoch') AS updated_at,
                {adoption_status} AS adoption_status,{associated_work} AS work_id
                FROM thoughts.thought_topics topic {adoption_join} WHERE topic.space_id=?
                ORDER BY topic.last_seen DESC,topic.theme_key ASC LIMIT ? OFFSET ?""",
                              (*adoption_args, space_id, limit + 1, offset)).fetchall()
            items, size = [], 0
            for row in rows[:limit]:
                item = dict(row)
                item_size = len(json.dumps(item, ensure_ascii=True).encode("ascii"))
                if size + item_size > MAX_PAGE_BYTES:
                    break
                items.append(item)
                size += item_size
            more = len(rows) > len(items) and offset + len(items) <= MAX_OFFSET
            return {"items": items, "offset": offset, "limit": limit, "available": True,
                    "has_more": more, "next_offset": offset + len(items) if more else None}

    def _group_inspirations(self, space_id: str, offset: int, limit: int) -> dict:
        """Read the experimental notebook; never evidence of production ingress.

        Notebook v1 persists its immutable binding in notebook_groups and
        aggregate themes in notebook_topics. This phase has no adoption path.
        Never read notebook_seen or notebook_topic_members, even their hashes.
        """
        empty = {"items": [], "offset": offset, "limit": limit, "has_more": False,
                 "next_offset": None, "available": False}
        with self._open() as db:
            space = self._space(db, space_id)
            group_id = self.group_bindings.get(space_id)
            if space is None or group_id is None:
                return empty
            path = self._path("group_notebook.sqlite3")
            if not path.exists():
                return empty
            # Use the existing query-only transaction and fixed, validated path.
            # Both the ledger owner and notebook binding must match this reader.
            db.execute("ATTACH DATABASE ? AS notebook", (path.as_uri() + "?mode=ro",))
            if db.execute("PRAGMA notebook.user_version").fetchone()[0] != 1:
                raise StoreError("SCHEMA_UNSUPPORTED", "Read-only Studio supports notebook schema 1")
            binding = db.execute("""SELECT owner_id,platform,stream_id,group_id
                FROM notebook.notebook_groups WHERE space_id=?""",
                                 (space_id,)).fetchone()
            scope = self.scopes[space_id]
            if binding is None or (binding["owner_id"], binding["platform"], binding["stream_id"], binding["group_id"]) != (
                    scope.user_id, scope.platform, scope.stream_id, group_id):
                raise StoreError("NOT_FOUND", "Group notebook not available for this reader")
            rows = db.execute("""SELECT
                substr(theme_key,1,200) AS inspiration_id,substr(title,1,1000) AS summary,
                CASE WHEN typeof(member_count)='integer' AND member_count>=0
                    THEN member_count END AS participant_count,
                CASE WHEN typeof(source_count)='integer' AND source_count>=0
                    THEN source_count END AS message_count,
                strftime('%Y-%m-%dT%H:%M:%SZ',first_seen,'unixepoch') AS created_at,
                strftime('%Y-%m-%dT%H:%M:%SZ',last_seen,'unixepoch') AS updated_at,
                'unadopted' AS adoption_status
                FROM notebook.notebook_topics WHERE space_id=?
                ORDER BY last_seen DESC,theme_key ASC LIMIT ? OFFSET ?""", (space_id, limit + 1, offset)).fetchall()
            items, size = [], 0
            for row in rows[:limit]:
                item = dict(row)
                item_size = len(json.dumps(item, ensure_ascii=True).encode("ascii"))
                if size + item_size > MAX_PAGE_BYTES:
                    break
                items.append(item)
                size += item_size
            more = len(rows) > len(items) and offset + len(items) <= MAX_OFFSET
            return {"items": items, "offset": offset, "limit": limit, "available": True,
                    "has_more": more, "next_offset": offset + len(items) if more else None}

    @staticmethod
    def _json_summary(db, table, column, space_id, rowid):
        """Extract independent bounded fields from valid, potentially long JSON.

        SQL identifiers come only from SECTIONS, not from request parameters.
        No full frozen prompt/profile/reference snapshot is fetched or parsed.
        """
        where = " WHERE space_id=? AND rowid=?"
        valid = db.execute(f"SELECT json_valid({column}) FROM {table}" + where, (space_id, rowid)).fetchone()[0]
        if not valid:
            return {"unavailable": "历史内容格式无效"}
        definitions = JSON_TEXT_FIELDS.get(column, {})
        result = {}
        if definitions:
            projection = ",".join(
                f"CASE WHEN json_type({column},'$.{name}')='text' THEN substr(json_extract({column},'$.{name}'),1,{limit}) END AS {name}"
                for name, limit in definitions.items())
            result = dict(db.execute(f"SELECT {projection} FROM {table}" + where, (space_id, rowid)).fetchone())
        array_paths = {"review": ["strengths", "issues", "comparison.improvements", "comparison.regressions", "comparison.preserved"],
                       "roles": [""], "tags": [""]}.get(column, [])
        for name in array_paths:
            path = "$" + ("." + name if name else "")
            rows = db.execute(f"""SELECT substr(part.value,1,200) FROM {table} AS source,
                json_each(source.{column}, ?) AS part WHERE source.space_id=? AND source.rowid=?
                AND part.type='text' LIMIT 8""", (path, space_id, rowid)).fetchall()
            values = [row[0] for row in rows]
            if not name:
                return values
            if "." in name:
                parent, child = name.split(".")
                result.setdefault(parent, {})[child] = values
            else:
                result[name] = values
        if column == "data":
            for name in ("stable", "variable"):
                rows = db.execute(f"""SELECT substr(part.key,1,64),substr(part.value,1,400)
                    FROM {table} AS source,json_each(source.{column},?) AS part
                    WHERE source.space_id=? AND source.rowid=? AND part.type='text' LIMIT 12""",
                                  ("$." + name, space_id, rowid)).fetchall()
                result[name] = dict(rows)
        if column == "brief":
            row = db.execute(f"""SELECT
                substr(json_extract({column},'$.creative.profile_id'),1,64) AS profile_id,
                substr(json_extract({column},'$.creative.series_id'),1,64) AS series_id
                FROM {table}""" + where, (space_id, rowid)).fetchone()
            result["creative"] = dict(row)
        if column == "review" and result["decision"] not in REVIEW_DECISIONS:
            result["decision"] = "unknown"
        return result

    def image(self, space_id: str, version_id: str, thumbnail: bool = True, offset: int = 0) -> dict:
        self._scope(space_id)
        _identifier(version_id)
        if type(thumbnail) is not bool:
            raise StoreError("INVALID_ARGUMENT", "thumbnail must be boolean")
        if type(offset) is not int or not 0 <= offset < MAX_IMAGE_BYTES or (thumbnail and offset != 0):
            raise StoreError("INVALID_ARGUMENT", "Invalid image byte offset")
        with self._open() as db:
            self._space(db, space_id)
            if db is None or "versions" not in self._tables(db):
                raise StoreError("NOT_FOUND", "Version not available for this reader")
            row = db.execute("""SELECT data,sha256,mime FROM versions
                WHERE space_id=? AND version_id=? AND length(data) BETWEEN 1 AND ?""",
                             (space_id, version_id, MAX_IMAGE_BYTES)).fetchone()
            if row is None:
                raise StoreError("NOT_FOUND", "Version not available for this reader")
            data = row["data"]
            if not isinstance(data, bytes) or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise StoreError("IMAGE_INVALID", "Stored image digest is invalid")
            # 每次仍从当前授权的数据库读取并核验摘要；仅复用昂贵的完整解码。
            data, actual_mime, width, height = display_image(data, row["mime"], MAX_IMAGE_PIXELS, thumbnail)
            result = {"version_id": version_id, "thumbnail": thumbnail, "width": width, "height": height,
                      "sha256": row["sha256"], "mime": actual_mime}
            if thumbnail:
                result["data_url"] = f"data:{actual_mime};base64," + base64.b64encode(data).decode("ascii")
            else:
                if offset >= len(data):
                    raise StoreError("INVALID_ARGUMENT", "Image offset exceeds saved image")
                end = min(offset + IMAGE_CHUNK_BYTES, len(data))
                result.update(chunk_base64=base64.b64encode(data[offset:end]).decode("ascii"),
                              offset=offset, next_offset=end if end < len(data) else None, total_bytes=len(data))
            return result
