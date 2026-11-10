"""画师台账共享的 SQLite 连接、锁和事务。"""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Callable, Iterator, Optional
import sqlite3

from .policy import budget_day


class SqliteUnitOfWork:
    def __init__(self, path: Path, *, clock: Optional[Callable[[], datetime]] = None):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        try:
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def transaction(self) -> Iterator[None]:
        # RLock 协调同一连接的线程；BEGIN IMMEDIATE 也让不同连接的检查与预留串行。
        # 已在事务中的业务只调用共享连接上的内部方法，不再次 BEGIN。
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def now(self) -> tuple[str, str]:
        now = self.clock()
        day = budget_day(now)
        return now.astimezone(timezone.utc).isoformat(), day

    def close(self) -> None:
        with self.lock:
            self.db.close()
