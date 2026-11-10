"""在临时账本上推进 Artist，不启动 Host 或网络客户端。"""

import pytest

from image_plugin_under_test.creation.artist import Artist
from image_plugin_under_test.creation.thinking import Thinking
from image_plugin_under_test.domain.policy import Delegation, Limits
from image_plugin_under_test.domain.store import ArtistStore
from .artist_fakes import SCOPE, Clock, Model, Renderer, Sender


@pytest.fixture
def studio(tmp_path):
    clock, model, renderer, sender = Clock(), Model(), Renderer(), Sender()
    store = ArtistStore(tmp_path / "artist.sqlite3", clock=clock)
    store.create_space("s", SCOPE, binding_id="simulation-service")
    store.grant("s", SCOPE, Delegation("explicit-host-delegation", "admin", continuous=True))
    limits = Limits(think=100, render=20, share=20, max_inflight=20, max_revisions=2)
    store.set_global_limits(limits)
    store.set_limits("s", SCOPE, limits)
    runtime = Artist(store, "s", SCOPE, Thinking(model), renderer, sender)
    yield runtime, store, clock, model, renderer, sender
    store.close()


async def step(studio, seconds=300):
    runtime, _, clock, *_ = studio
    clock.advance(seconds)
    return await runtime.tick()


async def finished(studio):
    assert (await studio[0].tick())["state"] == "planned"
    assert (await step(studio))["state"] == "review_pending"
    assert (await step(studio))["state"] == "finished"
