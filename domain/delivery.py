"""调用 Host 前持久化发送意图；发送结果未知时禁止重发。"""

# ruff: noqa: I001 -- 继承上级 AGENTS.md 的 from-import 在前约定。
from pathlib import Path
from threading import RLock
import sqlite3


class Deliveries:
    def __init__(self, path: Path):
        # 会恢复未决发送；插件调用方必须先取得该数据目录的独占权。
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        try:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS delivery (job TEXT PRIMARY KEY, state TEXT NOT NULL)"
            )
            with self.db:
                self.db.execute(
                    "UPDATE delivery SET state='send_unknown' WHERE state='sending'"
                )
        except BaseException:
            self.db.close()
            raise

    def begin(self, job: str, manual: bool = False) -> bool:
        with self.lock, self.db:
            # 由同一条写语句判断并取得许可；进程内的锁不能保护其他连接。
            cursor = self.db.execute(
                """INSERT INTO delivery VALUES (?,'sending')
                ON CONFLICT(job) DO UPDATE SET state='sending'
                WHERE ? AND delivery.state IN ('sent', 'send_failed')""",
                (job, manual),
            )
            return cursor.rowcount == 1

    def finish(self, job: str, state: str):
        # 只有取得 begin 许可的一方可以收尾，且不能覆盖已确定的结果。
        with self.lock, self.db:
            self.db.execute("UPDATE delivery SET state=? WHERE job=? AND state='sending'", (state, job))

    def state(self, job: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT state FROM delivery WHERE job=?", (job,)).fetchone()
            return row[0] if row else None

    def close(self):
        with self.lock:
            self.db.close()
