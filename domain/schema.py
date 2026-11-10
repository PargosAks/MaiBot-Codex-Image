"""画师台账的历史 DDL 和原子升级入口。"""

from typing import Sequence
import sqlite3

from .policy import StoreError


# schema 10 加入单作品的永久占位；拆分模块不改变磁盘格式。
SCHEMA_VERSION = 10

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

_SCHEMA_7 = (
    """CREATE TABLE creative_imports (
        space_id TEXT PRIMARY KEY REFERENCES spaces, source_digest TEXT NOT NULL,
        source_snapshot TEXT NOT NULL, auth_version INTEGER NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE creative_sources (
        space_id TEXT NOT NULL REFERENCES spaces, kind TEXT NOT NULL, entity_id TEXT NOT NULL,
        revision INTEGER NOT NULL, origin TEXT NOT NULL, source_ref TEXT, auth_version INTEGER,
        snapshot TEXT NOT NULL, PRIMARY KEY(space_id,kind,entity_id,revision))""",
    """INSERT INTO creative_sources SELECT space_id,'profile',profile_id,revision,
        'legacy',NULL,NULL,data FROM profile_versions""",
    """INSERT INTO creative_sources SELECT space_id,'series',series_id,1,
        'legacy',NULL,NULL,json_object('series_id',series_id,'title',title,'goal',goal,
        'profile_id',profile_id,'profile_revision',profile_revision) FROM series""",
    """CREATE TABLE creative_decisions (
        space_id TEXT NOT NULL REFERENCES spaces, request_id TEXT NOT NULL,
        operation_id INTEGER NOT NULL REFERENCES operations,
        decision TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(space_id,request_id))""",
)

_SCHEMA_8 = (
    """CREATE TABLE group_thought_policy (
        space_id TEXT PRIMARY KEY REFERENCES spaces, scope_snapshot TEXT NOT NULL,
        platform TEXT NOT NULL, stream_id TEXT NOT NULL, group_id TEXT NOT NULL,
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), auth_version INTEGER NOT NULL,
        UNIQUE(platform,stream_id), UNIQUE(platform,group_id))""",
    """CREATE TABLE group_thought_links (
        space_id TEXT NOT NULL REFERENCES group_thought_policy, note_id TEXT NOT NULL,
        inspiration_key TEXT NOT NULL, summary TEXT NOT NULL, group_id TEXT NOT NULL,
        adopted_by TEXT, adopted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(space_id,note_id), UNIQUE(note_id), UNIQUE(space_id,inspiration_key),
        FOREIGN KEY(space_id,adopted_by) REFERENCES artist_works(space_id,work_id),
        CHECK((adopted_by IS NULL AND adopted_at IS NULL)
            OR (adopted_by IS NOT NULL AND adopted_at IS NOT NULL)))""",
)

_SCHEMA_9 = (
    """CREATE TABLE group_share_policy (
        space_id TEXT PRIMARY KEY REFERENCES spaces, scope_snapshot TEXT NOT NULL,
        platform TEXT NOT NULL, stream_id TEXT NOT NULL, group_id TEXT NOT NULL,
        mode TEXT NOT NULL CHECK(mode IN ('collect_only','original_group')),
        auth_version INTEGER NOT NULL, policy_version INTEGER NOT NULL CHECK(policy_version>0),
        UNIQUE(platform,stream_id), UNIQUE(platform,group_id))""",
    """CREATE TABLE share_policy_claims (
        operation_id INTEGER PRIMARY KEY REFERENCES operations,
        policy_version INTEGER NOT NULL CHECK(policy_version>0))""",
)

_SCHEMA_10 = (
    """CREATE TABLE single_work_policies (
        space_id TEXT NOT NULL PRIMARY KEY REFERENCES spaces, work_id TEXT,
        enabled INTEGER NOT NULL CHECK(enabled=1),
        FOREIGN KEY(space_id,work_id) REFERENCES works(space_id,work_id) DEFERRABLE INITIALLY DEFERRED)""",
    "CREATE INDEX operation_single_work ON operations(space_id,action)",
)


def upgrade_schema(db: sqlite3.Connection, steps: Sequence[Sequence[str]], *, target_version: int) -> None:
    """调用方在同一写事务内执行全部 DDL 和版本标记。"""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if tables:
            raise StoreError("SCHEMA_UNSUPPORTED", "Unversioned nonempty database")
    elif not 1 <= version <= target_version:
        raise StoreError("SCHEMA_UNSUPPORTED", "Artist database schema is not supported")
    if version == target_version:
        return

    # 下标 0 是创建 v1；已有版本只顺序执行其后的 DDL。
    for statements in steps[version:target_version]:
        for statement in statements:
            db.execute(statement)
    db.execute(f"PRAGMA user_version={target_version}")
