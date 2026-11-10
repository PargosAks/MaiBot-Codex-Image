"""同一 SQLite 连接上的内部查询；调用方持锁或持有事务。"""

from typing import Any
import json
import sqlite3

from .policy import Scope, StoreError


# 有进行中作品时先推进作品，再考虑终态作品的回执或分享。
ACTIVE_WORK = ("planned", "deferred", "creating", "review_pending")


def require_authorized(space: sqlite3.Row) -> None:
    if not space["authorized"]:
        raise StoreError("AUTH_REQUIRED", "Continuous delegation is not active")


def expected_authorization(space: sqlite3.Row, expected: int | None) -> None:
    if expected is not None:
        if type(expected) is not int or expected < 0:
            raise StoreError("INVALID_ARGUMENT", "expected_auth_version must be a nonnegative integer")
        if space["auth_version"] != expected:
            raise StoreError("AUTH_CHANGED", "Authorization changed while processing this wake")


def decode_fields(row: sqlite3.Row, *fields: str) -> dict[str, Any]:
    result = dict(row)
    for field in fields:
        result[field] = json.loads(result[field])
    return result


class LedgerQueries:
    """提供身份、作品、版本和操作的作用域查询，不启动事务。"""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def space(self, space_id: str, scope: Scope) -> sqlite3.Row:
        if not isinstance(scope, Scope):
            raise StoreError("INVALID_SCOPE", "Only trusted-adapter Scope values are accepted")
        row = self.db.execute("SELECT * FROM spaces WHERE space_id=?", (space_id,)).fetchone()
        if row is None or row["scope"] != scope.snapshot():
            raise StoreError("NOT_FOUND", "Space not found for this scope")
        return row

    def operation(self, space_id: str, scope: Scope, request_id: str) -> sqlite3.Row:
        self.space(space_id, scope)
        row = self.db.execute(
            "SELECT * FROM operations WHERE space_id=? AND request_id=?",
            (space_id, request_id),
        ).fetchone()
        if row is None:
            raise StoreError("NOT_FOUND", "Operation not found in this space")
        return row

    def artist_work(self, space_id: str, scope: Scope, work_id: str) -> dict[str, Any]:
        self.space(space_id, scope)
        row = self.db.execute(
            "SELECT * FROM artist_works WHERE space_id=? AND work_id=?", (space_id, work_id)
        ).fetchone()
        if row is None:
            raise StoreError("NOT_FOUND", "Artist work not found in this space")
        return decode_fields(row, "brief", "inspiration_keys")

    def version(self, space_id: str, scope: Scope, version_id: str) -> dict[str, Any]:
        self.space(space_id, scope)
        row = self.db.execute(
            "SELECT * FROM versions WHERE space_id=? AND version_id=?", (space_id, version_id)
        ).fetchone()
        if row is None:
            raise StoreError("NOT_FOUND", "Version not found in this space")
        return dict(row)

    def work(self, space_id: str, scope: Scope, work_id: str) -> sqlite3.Row:
        self.space(space_id, scope)
        row = self.db.execute(
            "SELECT * FROM works WHERE space_id=? AND work_id=?",
            (space_id, work_id),
        ).fetchone()
        if row is None:
            raise StoreError("NOT_FOUND", "Work not found in this space")
        return row
