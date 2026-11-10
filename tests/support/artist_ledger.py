"""供调度与账本测试复用的授权空间和时钟。"""

from datetime import datetime, timezone

import pytest

from image_plugin_under_test.domain.policy import Delegation, Limits, Scope
from image_plugin_under_test.domain.store import ArtistStore


SCOPE = Scope("test", "verified-stream", "verified-owner")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 31, 4, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def ledger(tmp_path):
    clock = Clock()
    db = ArtistStore(tmp_path / "artist.sqlite3", clock=clock)
    db.create_space("s", SCOPE, binding_id="synthetic-binding")
    db.grant("s", SCOPE, Delegation("explicit-ongoing-consent", "trusted-adapter", continuous=True))
    limits = Limits(think=50, render=50, share=0, max_inflight=50, max_revisions=5)
    db.set_global_limits(limits)
    db.set_limits("s", SCOPE, limits)
    yield db, clock
    db.close()
