"""构造指定 schema 的历史账本，避免用当前数据库降级冒充旧格式。"""

from contextlib import closing
from pathlib import Path
import hashlib
import sqlite3

from image_plugin_under_test.domain import store as store_module
from image_plugin_under_test.domain.policy import Scope, canonical_json
from .reviewed_assets import SCOPE, BRIEF


def make_schema_4(path):
    db = sqlite3.connect(path)
    for statements in (store_module._SCHEMA, store_module._SCHEMA_2, store_module._SCHEMA_3, store_module._SCHEMA_4):
        for statement in statements:
            db.execute(statement)
    db.execute("INSERT INTO spaces VALUES('s',?,'old-binding',1,1)", (SCOPE.snapshot(),))
    db.execute("INSERT INTO grants VALUES('s',1,1,'consent','adapter','then')")
    db.execute("INSERT INTO works VALUES('s','w','then')")
    db.execute("INSERT INTO artist_works(space_id,work_id,status,brief,inspiration_keys) "
               "VALUES('s','w','finished',?,'[]')", (canonical_json(BRIEF),))
    digest = hashlib.sha256(b"image").hexdigest()
    db.execute("INSERT INTO versions VALUES('s','v','w','render','job',?,?,'image/png',NULL,'then')", (b"image", digest))
    context = {"current_work": {"work_id": "w", "brief": BRIEF, "best_version": None},
               "current_version": {"version_id": "v", "sha256": digest, "mime": "image/png"},
               "review_contract": 2,
               "review_inputs": [{"version_id": "v", "roles": ["current"], "sha256": digest, "mime": "image/png"}]}
    input_digest = hashlib.sha256(canonical_json(context).encode()).hexdigest()
    db.execute("INSERT INTO review_snapshots VALUES('s','v',1,?,?)", (canonical_json(context), input_digest))
    params = {"task": "review", "version_id": "v", "context": context, "review_contract": 2, "input_digest": input_digest}
    review = {"decision": "accept", "reason": "historically accepted", "strengths": ["lighting"], "issues": []}
    for index, state in enumerate(("reserved", "unknown", "succeeded"), 1):
        db.execute("""INSERT INTO operations VALUES(?,'s','w',?,'think','think',?,?,'old-binding',1,'idle',NULL,
                   ?,'2026-10-31',NULL,?,'then','then')""",
                   (index, state, canonical_json(params), SCOPE.snapshot(), state,
                    canonical_json(review) if state == "succeeded" else None))
    db.execute("INSERT INTO reviews VALUES(1,'s','w','v','succeeded',?,'then')", (canonical_json(review),))
    db.execute("PRAGMA user_version=4")
    db.commit()
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    before = {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    db.close()
    return before



def make_schema_7(path: Path, scope: Scope):
    # 使用历史发布版 DDL；当前 ArtistStore 会自动迁移，不能用于创建源账本。
    ddl = Path(__file__).with_name("schema_v7.sql").read_text(encoding="utf-8")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(ddl)
        stamp = "2026-11-04T00:00:00+00:00"
        db.execute("INSERT INTO spaces VALUES('s',?,'backend',1,1)", (scope.snapshot(),))
        db.execute("INSERT INTO grants VALUES('s',1,1,'explicit-consent','admin',?)", (stamp,))
        db.executemany("INSERT INTO limits VALUES(?,3,2,1,1,1)", [("*",), ("s",)])
        db.execute("INSERT INTO works VALUES('s','old-work',?)", (stamp,))
        db.execute(
            "INSERT INTO inspirations VALUES('s','normal','retained','[]','idle',?,NULL,?)",
            ("2026-11-11T00:00:00+00:00", stamp),
        )
        # 未知提交仍持有额度；同一天已完成的模型调用仍计入已消耗额度。
        db.executemany(
            """INSERT INTO operations(
                space_id, work_id, request_id, action, category, params_json, scope_snapshot, binding_id,
                auth_version, trigger, state, budget_day, evidence, created_at, updated_at
            ) VALUES('s','old-work',?,?,?,'{}',?,'backend',1,'idle',?,'2026-11-04',?,?,?)""",
            [
                ("old-unknown", "submit", "render", scope.snapshot(), "unknown", "receipt uncertain", stamp, stamp),
                ("old-thought", "think", "think", scope.snapshot(), "succeeded", "reviewed earlier", stamp, stamp),
            ],
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
