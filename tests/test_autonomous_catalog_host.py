"""Host reload through SDK 2.16 and AST-loaded public Host methods; no live Host."""

from copy import deepcopy
import os

import pytest

from image_plugin_under_test.policy import Scope
from image_plugin_under_test.thinking import Thinking
from test_autonomous_catalog import NEW
from test_host_integration import OWNER, PLATFORM, STREAM, host_chain, public_host  # noqa: F401
from test_profiles_flow import PROFILES, SERIES


pytestmark = pytest.mark.skipif(
    not (os.environ.get("CODEX_IMAGE_TEST_HOST_SOURCE") or os.environ.get("CODEX_IMAGE_TEST_HOST_REPO")),
    reason="Explicit public Host source is required",
)


async def test_reload_retains_autonomous_catalogue_and_reads_legacy_config(host_chain):  # noqa: F811
    h = host_chain
    scope = Scope(PLATFORM, STREAM, OWNER)
    config = h.config

    def legacy_config(**kwargs):
        value = config(**kwargs)
        value["autonomy"]["spaces"][0].update(profiles=deepcopy(PROFILES), series=deepcopy(SERIES))
        return value

    h.config = legacy_config
    await h.load()
    assert h.plugin.config.autonomy.spaces[0].profiles == PROFILES
    assert h.plugin.config.autonomy.spaces[0].series == SERIES

    async def scripted(request):
        return {"ideas": [], "decision": deepcopy(NEW)}

    artist = h.runtime.artists["studio"]
    artist.thinking = Thinking(scripted)
    assert (await artist.tick())["state"] == "profile_created"
    saved = h.runtime.store.list_profiles("studio", scope)
    assert len(saved) == 2
    operations = h.runtime.store.audit_operations("studio", scope)
    await h.plugin.on_unload()
    # Reload's empty legacy defaults must not turn into a visibility sync.
    h.config = config
    await h.load()
    assert h.runtime.store.list_profiles("studio", scope) == saved
    assert h.runtime.store.list_series("studio", scope) == SERIES
    assert h.runtime.store.audit_operations("studio", scope) == operations
    assert not h.renderer.calls and not h.sends
    assert [name for name, _ in h.calls] == ["chat.get_stream_by_user_id"] * 2
