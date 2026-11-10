-- schema 7 的历史 DDL，取自 161d6e408d42cf865a750d4639e2b55cfe10ad6e:store.py。
-- 这里固定源格式，测试当前迁移时不会把当前实现用作旧版账本的生成器。

-- v1
CREATE TABLE spaces (
        space_id TEXT PRIMARY KEY, scope TEXT NOT NULL, binding_id TEXT NOT NULL,
        auth_version INTEGER NOT NULL DEFAULT 0, authorized INTEGER NOT NULL DEFAULT 0);

CREATE TABLE grants (
        space_id TEXT NOT NULL REFERENCES spaces, version INTEGER NOT NULL,
        authorized INTEGER NOT NULL, source_ref TEXT NOT NULL, granted_by TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(space_id, version));

CREATE TABLE limits (
        scope_key TEXT PRIMARY KEY, think INTEGER NOT NULL, render INTEGER NOT NULL,
        share INTEGER NOT NULL, max_inflight INTEGER NOT NULL, max_revisions INTEGER NOT NULL);

CREATE TABLE works (
        space_id TEXT NOT NULL REFERENCES spaces, work_id TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(space_id, work_id));

CREATE TABLE operations (
        id INTEGER PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces, work_id TEXT,
        request_id TEXT NOT NULL, action TEXT NOT NULL, category TEXT NOT NULL,
        params_json TEXT NOT NULL, scope_snapshot TEXT NOT NULL, binding_id TEXT NOT NULL,
        auth_version INTEGER NOT NULL, trigger TEXT NOT NULL, trigger_ref TEXT,
        state TEXT NOT NULL, budget_day TEXT NOT NULL, job_id TEXT, evidence TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(binding_id, scope_snapshot, request_id),
        FOREIGN KEY(space_id, work_id) REFERENCES works(space_id, work_id));

CREATE INDEX operation_budget ON operations(category, state, budget_day, space_id);

CREATE TABLE legacy_locks (
        job TEXT PRIMARY KEY, original_state TEXT NOT NULL, state TEXT NOT NULL,
        source_digest TEXT NOT NULL);

CREATE TABLE legacy_migrations (
        source_digest TEXT PRIMARY KEY, source_path TEXT NOT NULL, backup_path TEXT NOT NULL,
        row_count INTEGER NOT NULL, created_at TEXT NOT NULL);

-- v2
CREATE TABLE artist_state (
        space_id TEXT PRIMARY KEY REFERENCES spaces, acked_through INTEGER NOT NULL DEFAULT 0,
        next_due TEXT, backoff_until TEXT);

CREATE TABLE artist_wakes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, space_id TEXT NOT NULL REFERENCES spaces,
        event_id TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, source_ref TEXT,
        UNIQUE(space_id,event_id));

CREATE TABLE artist_claims (
        space_id TEXT NOT NULL REFERENCES spaces, token TEXT NOT NULL,
        through INTEGER NOT NULL, state TEXT NOT NULL, auth_version INTEGER NOT NULL,
        PRIMARY KEY(space_id,token));

CREATE UNIQUE INDEX artist_active_claim ON artist_claims(space_id) WHERE state='active';

CREATE TABLE inspirations (
        space_id TEXT NOT NULL REFERENCES spaces, key TEXT NOT NULL, summary TEXT NOT NULL,
        source_refs TEXT NOT NULL, origin TEXT NOT NULL, expires_at TEXT NOT NULL,
        consumed_by TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(space_id,key));

CREATE TABLE artist_works (
        space_id TEXT NOT NULL, work_id TEXT NOT NULL, status TEXT NOT NULL,
        brief TEXT NOT NULL, inspiration_keys TEXT NOT NULL, reason TEXT,
        pending_request TEXT, parent_version TEXT, edit_prompt TEXT, best_version TEXT, next_check_at TEXT,
        PRIMARY KEY(space_id,work_id), FOREIGN KEY(space_id,work_id) REFERENCES works(space_id,work_id));

CREATE TABLE versions (
        space_id TEXT NOT NULL, version_id TEXT NOT NULL, work_id TEXT NOT NULL,
        request_id TEXT NOT NULL, job_id TEXT NOT NULL, data BLOB NOT NULL,
        sha256 TEXT NOT NULL, mime TEXT NOT NULL, parent_id TEXT, created_at TEXT NOT NULL,
        PRIMARY KEY(space_id,version_id), UNIQUE(space_id,request_id),
        FOREIGN KEY(space_id,work_id) REFERENCES works(space_id,work_id));

CREATE TABLE reviews (
        id INTEGER PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces,
        work_id TEXT NOT NULL, version_id TEXT NOT NULL, request_id TEXT NOT NULL,
        review TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(space_id,request_id),
        FOREIGN KEY(space_id,version_id) REFERENCES versions(space_id,version_id));

-- v3
CREATE TABLE review_snapshots (
        space_id TEXT NOT NULL, version_id TEXT NOT NULL, auth_version INTEGER NOT NULL,
        context TEXT NOT NULL, input_digest TEXT NOT NULL, PRIMARY KEY(space_id,version_id),
        FOREIGN KEY(space_id,version_id) REFERENCES versions(space_id,version_id));

-- v4
CREATE TABLE profile_versions (
        space_id TEXT NOT NULL REFERENCES spaces, profile_id TEXT NOT NULL,
        revision INTEGER NOT NULL, data TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(space_id,profile_id,revision));

CREATE TABLE series (
        space_id TEXT NOT NULL REFERENCES spaces, series_id TEXT NOT NULL, title TEXT NOT NULL,
        goal TEXT NOT NULL, profile_id TEXT NOT NULL, profile_revision INTEGER NOT NULL,
        active INTEGER NOT NULL DEFAULT 1, closed INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(space_id,series_id), FOREIGN KEY(space_id,profile_id,profile_revision)
        REFERENCES profile_versions(space_id,profile_id,revision));

CREATE TABLE series_members (
        space_id TEXT NOT NULL, series_id TEXT NOT NULL, work_id TEXT NOT NULL, objective TEXT NOT NULL,
        PRIMARY KEY(space_id,work_id), UNIQUE(space_id,series_id,objective),
        FOREIGN KEY(space_id,series_id) REFERENCES series(space_id,series_id),
        FOREIGN KEY(space_id,work_id) REFERENCES artist_works(space_id,work_id));

-- v5
CREATE TABLE asset_catalog (
        space_id TEXT NOT NULL REFERENCES spaces, asset_id TEXT NOT NULL,
        source_version_id TEXT NOT NULL, work_id TEXT NOT NULL, job_id TEXT NOT NULL,
        sha256 TEXT NOT NULL, mime TEXT NOT NULL, size_bytes INTEGER NOT NULL,
        roles TEXT NOT NULL, tags TEXT NOT NULL, reason TEXT NOT NULL, summary TEXT NOT NULL,
        description TEXT NOT NULL, source_kind TEXT NOT NULL, review_request_id TEXT NOT NULL,
        auth_version INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
        PRIMARY KEY(space_id,asset_id), UNIQUE(space_id,source_version_id),
        FOREIGN KEY(space_id,source_version_id) REFERENCES versions(space_id,version_id));

CREATE TABLE asset_selection (
        space_id TEXT NOT NULL, asset_id TEXT NOT NULL, position INTEGER NOT NULL,
        auth_version INTEGER NOT NULL, selected INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(space_id,asset_id),
        FOREIGN KEY(space_id,asset_id) REFERENCES asset_catalog(space_id,asset_id));

-- v6
CREATE TABLE asset_catalog_v6 (
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
                AND job_id IS NULL AND review_request_id IS NULL)));

INSERT INTO asset_catalog_v6 SELECT * FROM asset_catalog;

CREATE TABLE asset_selection_v6 (
        space_id TEXT NOT NULL, asset_id TEXT NOT NULL, position INTEGER NOT NULL,
        auth_version INTEGER NOT NULL, selected INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(space_id,asset_id),
        FOREIGN KEY(space_id,asset_id) REFERENCES asset_catalog_v6(space_id,asset_id));

INSERT INTO asset_selection_v6 SELECT * FROM asset_selection;

DROP TABLE asset_selection;

DROP TABLE asset_catalog;

ALTER TABLE asset_catalog_v6 RENAME TO asset_catalog;

ALTER TABLE asset_selection_v6 RENAME TO asset_selection;

CREATE TABLE external_assets (
        space_id TEXT NOT NULL, asset_id TEXT NOT NULL, event_id TEXT NOT NULL,
        message_index INTEGER NOT NULL, data BLOB NOT NULL, consent_ref TEXT NOT NULL,
        source_description TEXT NOT NULL,
        PRIMARY KEY(space_id,asset_id), UNIQUE(space_id,event_id,message_index),
        FOREIGN KEY(space_id,asset_id) REFERENCES asset_catalog(space_id,asset_id));

CREATE TABLE reference_policy (
        space_id TEXT PRIMARY KEY REFERENCES spaces, enabled INTEGER NOT NULL,
        consent_ref TEXT NOT NULL, version INTEGER NOT NULL, auth_version INTEGER NOT NULL);

CREATE TABLE asset_registrations (
        space_id TEXT NOT NULL, asset_id TEXT NOT NULL, request_id TEXT NOT NULL,
        binding_id TEXT NOT NULL, auth_version INTEGER NOT NULL, policy_version INTEGER NOT NULL,
        consent_ref TEXT NOT NULL, sha256 TEXT NOT NULL, mime TEXT NOT NULL, size_bytes INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('reserved','submitting','unknown','succeeded','failed')),
        receipt TEXT, reason TEXT, PRIMARY KEY(space_id,asset_id), UNIQUE(binding_id,space_id,request_id),
        FOREIGN KEY(space_id,asset_id) REFERENCES asset_catalog(space_id,asset_id));

-- v7
CREATE TABLE creative_imports (
        space_id TEXT PRIMARY KEY REFERENCES spaces, source_digest TEXT NOT NULL,
        source_snapshot TEXT NOT NULL, auth_version INTEGER NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE creative_sources (
        space_id TEXT NOT NULL REFERENCES spaces, kind TEXT NOT NULL, entity_id TEXT NOT NULL,
        revision INTEGER NOT NULL, origin TEXT NOT NULL, source_ref TEXT, auth_version INTEGER,
        snapshot TEXT NOT NULL, PRIMARY KEY(space_id,kind,entity_id,revision));

INSERT INTO creative_sources SELECT space_id,'profile',profile_id,revision,
        'legacy',NULL,NULL,data FROM profile_versions;

INSERT INTO creative_sources SELECT space_id,'series',series_id,1,
        'legacy',NULL,NULL,json_object('series_id',series_id,'title',title,'goal',goal,
        'profile_id',profile_id,'profile_revision',profile_revision) FROM series;

CREATE TABLE creative_decisions (
        space_id TEXT NOT NULL REFERENCES spaces, request_id TEXT NOT NULL,
        operation_id INTEGER NOT NULL REFERENCES operations,
        decision TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(space_id,request_id));

PRAGMA user_version=7;
