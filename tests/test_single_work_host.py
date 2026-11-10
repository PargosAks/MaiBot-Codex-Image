"""Host standby and resume over temporary SQLite with synthetic Host RPC effects."""

from contextlib import closing

import pytest
from image_plugin_under_test.domain.policy import Scope, StoreError
from image_plugin_under_test.domain.store import ArtistStore
from image_plugin_under_test.host import host_runtime
from image_plugin_under_test.host.host_runtime import ArtistSpace
from pydantic import ValidationError
from support.host_chain import OWNER, PLATFORM, STREAM, host_chain as host_chain, public_host as public_host
from support.media_samples import image_bytes

SCOPE = Scope(PLATFORM, STREAM, OWNER)


def test_single_work_host_config_flags_are_strict_and_default_off():
    defaults = ArtistSpace()
    assert defaults.single_work is defaults.paused is False
    for field in ("single_work", "paused"):
        for value in ("false", 1, None):
            with pytest.raises(ValidationError, match=field):
                ArtistSpace.model_validate({field: value})


async def test_new_paused_space_verifies_and_persists_policies_without_collecting(host_chain, monkeypatch):
    h = host_chain
    config = h.config(group=True, media=True)
    item = config["autonomy"]["spaces"][0]
    item.update(single_work=True, paused=True, group_inspiration=True)
    monkeypatch.setattr(
        h.plugin, "_artist_renderer", lambda *args: pytest.fail("standby constructed a renderer"),
    )
    h.plugin.set_plugin_config(config)
    await h.plugin.on_load()
    assert not h.plugin.startup_error, h.plugin.startup_error

    runtime, space_id = h.runtime, item["space_id"]
    store = runtime.store
    space = store.get_space(space_id, SCOPE)
    assert space["authorized"] and space["auth_version"] == 1
    assert store.single_work_status(space_id, SCOPE) == {
        "enabled": True, "work_id": None, "create_used": False, "edit_used": False, "share_used": False,
    }
    assert store.single_work_for_stream(PLATFORM, STREAM)
    assert store.sharing_policy(space_id, SCOPE) == {"mode": "original_group", "allowed": True}
    reference = store.db.execute("SELECT enabled,consent_ref,auth_version FROM reference_policy").fetchone()
    assert tuple(reference) == (1, item["media_consent_ref"], space["auth_version"])
    notebook = store.db.execute("SELECT group_id,enabled,auth_version FROM group_thought_policy").fetchone()
    assert tuple(notebook) == (item["group_id"], 1, space["auth_version"])
    limits = store.db.execute("SELECT * FROM limits WHERE scope_key=?", (space_id,)).fetchone()
    assert dict(limits) == {"scope_key": space_id, **item["limits"]}

    assert not any((runtime.artists, runtime.signals, runtime.tasks, runtime.verified_notebook_groups,
                    runtime.grant_versions, runtime.media_policies))
    image = h.host.ImageComponent(binary_hash="", binary_data=image_bytes())
    await h.message("standby must ignore this", components=[image])
    assert not store.list_assets(space_id, SCOPE)
    assert not store.db.execute("SELECT 1 FROM artist_wakes").fetchone()
    assert not store.list_artist_works(space_id, SCOPE) and not store.audit_operations(space_id, SCOPE)
    assert h.calls == [("chat.get_stream_by_group_id", {"platform": PLATFORM, "group_id": item["group_id"]})]
    assert not h.renderer.calls and not h.sends


async def test_resume_and_pause_keep_grant_and_enforce_lifetime_work_limit(host_chain):
    h = host_chain
    config = h.config()
    item = config["autonomy"]["spaces"][0]
    item.update(single_work=True, paused=True)
    space_id = item["space_id"]
    h.plugin.set_plugin_config(config)
    await h.plugin.on_load()
    assert not h.plugin.startup_error, h.plugin.startup_error
    grants = h.runtime.store.audit_authorizations(space_id, SCOPE)

    item["paused"] = False
    h.plugin.set_plugin_config(config)
    await h.plugin.on_config_update("plugin", {}, "resume")
    assert not h.plugin.startup_error, h.plugin.startup_error
    assert h.runtime.grant_versions == {space_id: grants[-1]["version"]}
    assert h.runtime.store.audit_authorizations(space_id, SCOPE) == grants
    await h.message("a fresh wake after resume")
    h.gate.set()
    await h.until(lambda: h.state() == "planned")
    await h.advance("review_pending")

    previous = h.runtime
    tasks = tuple(previous.tasks.values())
    store = previous.store
    work_id = store.list_artist_works(space_id, SCOPE)[0]["work_id"]
    status = store.single_work_status(space_id, SCOPE)
    assert status["work_id"] == work_id and status["create_used"]
    assert len(h.renderer.calls) == 1 and not h.sends
    with pytest.raises(StoreError) as second_work:
        store.create_work(space_id, SCOPE, "second-work")
    assert second_work.value.code == "SINGLE_WORK_LIMIT"
    with pytest.raises(StoreError) as second_submit:
        store.reserve(space_id, SCOPE, request_id="second-submit", action="submit",
                      params={"prompt": "second render"}, work_id=work_id)
    assert second_submit.value.code == "SINGLE_WORK_LIMIT"

    effects = (len(h.calls), len(h.renderer.calls), len(h.sends))
    item["paused"] = True
    h.plugin.set_plugin_config(config)
    await h.plugin.on_config_update("plugin", {}, "pause-again")
    assert not h.plugin.startup_error, h.plugin.startup_error
    assert previous.store is None and all(task.done() for task in tasks)
    assert not any((h.runtime.artists, h.runtime.tasks, h.runtime.signals))
    await h.message("standby after the first render", message_id="while-paused")
    assert (len(h.calls), len(h.renderer.calls), len(h.sends)) == (effects[0] + 1, effects[1], effects[2])
    assert h.calls[-1][0] == "chat.get_stream_by_user_id"
    assert h.runtime.store.audit_authorizations(space_id, SCOPE) == grants
    assert h.runtime.store.single_work_status(space_id, SCOPE) == status
    assert not h.runtime.store.db.execute(
        "SELECT 1 FROM artist_wakes WHERE event_id='chat:while-paused'",
    ).fetchone()


async def test_standby_keeps_legacy_space_active_and_rejects_unlatch(host_chain, monkeypatch):
    h = host_chain
    await h.load()
    h.runtime.store.create_work("studio", SCOPE, "existing-work")
    grants = h.runtime.store.audit_authorizations("studio", SCOPE)
    config = h.config()
    standby_stream = "standby-stream"
    standby_scope = Scope(PLATFORM, standby_stream, OWNER)
    standby = {**config["autonomy"]["spaces"][0], "space_id": "standby", "stream_id": standby_stream,
               "group_id": "standby-group", "single_work": True, "paused": True}
    config["autonomy"]["spaces"].append(standby)

    async def standby_lookup(*args):
        receipt = await h.capability(*args)
        receipt["stream"].update(stream_id=standby_stream, session_id=standby_stream)
        return receipt

    h.capabilities.register_capability("chat.get_stream_by_group_id", standby_lookup)
    h.plugin.set_plugin_config(config)
    await h.plugin.on_config_update("plugin", {}, "add-standby")
    assert not h.plugin.startup_error, h.plugin.startup_error
    runtime = h.runtime
    assert set(runtime.artists) == set(runtime.tasks) == {"studio"}
    assert runtime.store.audit_authorizations("studio", SCOPE) == grants
    assert not runtime.store.single_work_status("studio", SCOPE)["enabled"]
    assert runtime.store.single_work_status("standby", standby_scope)["enabled"]
    assert runtime.store.create_work("studio", SCOPE, "another-work")["work_id"] == "another-work"
    await h.message("existing space still collects")
    assert runtime.store.db.execute("SELECT space_id FROM artist_wakes").fetchone()["space_id"] == "studio"

    standby_grants = runtime.store.audit_authorizations("standby", standby_scope)
    # A new, valid latch before an invalid existing one must roll back as part
    # of the batch, without undoing existing latches or authorization history.
    fresh = {**standby, "space_id": "fresh", "stream_id": "fresh-stream", "group_id": "fresh-group"}
    config["autonomy"]["spaces"].insert(0, fresh)

    async def multiple_group_lookup(*args):
        receipt = await h.capability(*args)
        stream = "fresh-stream" if receipt["stream"]["group_id"] == "fresh-group" else standby_stream
        receipt["stream"].update(stream_id=stream, session_id=stream)
        return receipt

    h.capabilities.register_capability("chat.get_stream_by_group_id", multiple_group_lookup)
    standby.pop("single_work")

    async def unexpected_recovery(self, **kwargs):
        pytest.fail("artist recovered before batch succeeded")

    monkeypatch.setattr(host_runtime.Artist, "recover", unexpected_recovery)
    h.plugin.set_plugin_config(config)
    await h.plugin.on_config_update("plugin", {}, "omit-latched-flag")
    assert "Single-work policy cannot be disabled" in h.plugin.startup_error
    assert h.runtime is None and not h.renderer.calls and not h.sends
    with closing(ArtistStore(h.data_paths.data_dir / "artist.sqlite3")) as persisted:
        assert [row["space_id"] for row in persisted.db.execute("SELECT * FROM single_work_policies")] == [
            "standby",
        ]
        assert persisted.audit_authorizations("studio", SCOPE) == grants
        assert persisted.audit_authorizations("standby", standby_scope) == standby_grants
        fresh_scope = Scope(PLATFORM, "fresh-stream", OWNER)
        assert not persisted.single_work_status("fresh", fresh_scope)["enabled"]
        # Only the latch transaction rolls back; the grant written before it
        # remains an auditable fact, with no worker allowed to spend against it.
        assert persisted.audit_authorizations("fresh", fresh_scope)[-1]["authorized"]


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("consent", ["", "   "])
async def test_missing_media_consent_never_latches_or_grants(host_chain, monkeypatch, paused, consent):
    h = host_chain
    config = h.config(media=True, media_consent=consent)
    config["autonomy"]["spaces"][0].update(single_work=True, paused=paused)
    monkeypatch.setattr(
        h.plugin, "_artist_renderer", lambda *args: pytest.fail("artist started with invalid media consent"),
    )
    h.plugin.set_plugin_config(config)
    await h.plugin.on_load()
    assert h.plugin.startup_error == "Separate media consent is required for image inputs"
    assert h.runtime is None and not h.calls and not h.renderer.calls and not h.sends
    with closing(ArtistStore(h.data_paths.data_dir / "artist.sqlite3")) as persisted:
        assert not persisted.configured_spaces()
        assert not persisted.db.execute("SELECT 1 FROM grants").fetchone()
        assert not persisted.db.execute("SELECT 1 FROM single_work_policies").fetchone()


@pytest.mark.parametrize("failure", ["media", "stream", "policy", "removed-stream"])
async def test_later_invalid_space_never_leaves_new_latches(host_chain, monkeypatch, failure):
    h = host_chain
    config = h.config()
    existing = config["autonomy"]["spaces"][0]
    existing.update(single_work=True, paused=True)
    h.plugin.set_plugin_config(config)
    await h.plugin.on_load()
    assert not h.plugin.startup_error, h.plugin.startup_error
    old_grants = h.runtime.store.audit_authorizations("studio", SCOPE)
    old_space = h.runtime.store.get_space("studio", SCOPE)
    old_latch = h.runtime.store.single_work_status("studio", SCOPE)
    await h.plugin.on_unload()
    h.calls.clear()

    first = {**existing, "space_id": "first", "stream_id": "first-stream", "group_id": "first-group",
             "paused": False}
    second = {**existing, "space_id": "second", "stream_id": "second-stream", "group_id": "second-group"}
    config["autonomy"]["spaces"] = [first, second] + ([] if failure == "removed-stream" else [existing])
    if failure == "media":
        second.update(reference_inputs=True, media_consent_ref="")
    elif failure == "policy":
        second["profiles"] = [{}]

    async def group_lookup(*args):
        receipt = await h.capability(*args)
        group = receipt["stream"]["group_id"]
        if group == "second-group" and failure in {"stream", "removed-stream"}:
            return {"success": False, "stream": receipt["stream"]}
        stream = "first-stream" if group == "first-group" else "second-stream"
        receipt["stream"].update(stream_id=stream, session_id=stream)
        return receipt

    h.capabilities.register_capability("chat.get_stream_by_group_id", group_lookup)
    monkeypatch.setattr(
        h.plugin, "_artist_renderer", lambda *args: pytest.fail("artist started before all spaces were valid"),
    )
    h.plugin.set_plugin_config(config)
    await h.plugin.on_load()
    expected = {
        "media": "Separate media consent is required for image inputs",
        "stream": "Configured Host stream is unavailable",
        "removed-stream": "Configured Host stream is unavailable",
        "policy": "Unexpected creative data fields",
    }
    assert h.plugin.startup_error == expected[failure]
    assert h.runtime is None and not h.renderer.calls and not h.sends
    assert all(name.startswith("chat.get_stream_by_") for name, _ in h.calls)
    with closing(ArtistStore(h.data_paths.data_dir / "artist.sqlite3")) as persisted:
        assert [row["space_id"] for row in persisted.db.execute("SELECT * FROM single_work_policies")] == [
            "studio",
        ]
        assert persisted.single_work_status("studio", SCOPE) == old_latch
        grants = persisted.audit_authorizations("studio", SCOPE)
        if failure == "removed-stream":
            # Reconciliation still revokes removed authority before stream lookup.
            assert grants[:-1] == old_grants and not grants[-1]["authorized"]
            assert grants[-1]["source_ref"] == "host-config:disabled-or-removed"
        else:
            assert grants == old_grants
            assert persisted.get_space("studio", SCOPE) == old_space
        if failure != "policy":
            assert [space["space_id"] for space in persisted.configured_spaces()] == ["studio"]
        else:
            # Both streams passed before policy configuration began; earlier
            # ordinary writes remain, but no permanent latch has been applied.
            assert persisted.audit_authorizations("first", Scope(PLATFORM, "first-stream", OWNER))
        assert not persisted.db.execute("SELECT 1 FROM operations").fetchone()


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("field", ["persona", "plan_task", "review_task"])
async def test_blank_runtime_text_fails_before_any_latch(host_chain, monkeypatch, field, paused):
    h = host_chain
    config = h.config()
    config["autonomy"][field] = " \t "
    config["autonomy"]["spaces"][0].update(single_work=True, paused=paused)
    monkeypatch.setattr(
        h.plugin, "_artist_renderer", lambda *args: pytest.fail("blank configuration reached a constructor"),
    )
    h.plugin.set_plugin_config(config)  # Whitespace satisfies the SDK's min_length constraint.
    await h.plugin.on_load()
    expected = "A bounded public persona is required" if field == "persona" else f"{field} must be a nonempty string"
    assert h.plugin.startup_error == expected
    assert h.runtime is None and not h.calls and not h.renderer.calls and not h.sends
    with closing(ArtistStore(h.data_paths.data_dir / "artist.sqlite3")) as persisted:
        assert not persisted.configured_spaces()
        assert not persisted.db.execute("SELECT 1 FROM single_work_policies").fetchone()


async def test_later_constructor_failure_never_latches_or_recovers_first_artist(host_chain, monkeypatch):
    h = host_chain
    config = h.config()
    first = config["autonomy"]["spaces"][0]
    first["single_work"] = True
    second = {**first, "space_id": "second", "stream_id": "second-stream", "group_id": "second-group"}
    config["autonomy"]["spaces"].append(second)

    async def second_lookup(*args):
        receipt = await h.capability(*args)
        receipt["stream"].update(stream_id="second-stream", session_id="second-stream")
        return receipt

    h.capabilities.register_capability("chat.get_stream_by_group_id", second_lookup)
    constructed, runtimes = [], []

    class ConstructionProbe(host_runtime.Artist):
        def __init__(self, store, space_id, *args, **kwargs):
            runtimes.append(h.runtime)
            if space_id == "second":
                # Inject a late validation failure in the real constructor,
                # after the first Artist and all policies have been prepared.
                kwargs["persona"] = "   "
            super().__init__(store, space_id, *args, **kwargs)
            constructed.append(self)

        async def recover(self, **kwargs):
            pytest.fail("recovery ran before all constructors succeeded")

    monkeypatch.setattr(host_runtime, "Artist", ConstructionProbe)
    h.plugin.set_plugin_config(config)
    await h.plugin.on_load()
    assert h.plugin.startup_error == "A bounded public persona is required"
    assert [artist.space_id for artist in constructed] == ["studio"]
    assert len(runtimes) == 2 and runtimes[0] is runtimes[1]
    runtime = runtimes[0]
    assert not any((runtime.artists, runtime.tasks, runtime.signals, runtime.grant_versions,
                    runtime.verified_notebook_groups, runtime.media_policies))
    assert h.runtime is None and not h.renderer.calls and not h.sends
    assert all(name.startswith("chat.get_stream_by_") for name, _ in h.calls)
    with closing(ArtistStore(h.data_paths.data_dir / "artist.sqlite3")) as persisted:
        assert not persisted.db.execute("SELECT 1 FROM single_work_policies").fetchone()
        assert not persisted.db.execute("SELECT 1 FROM operations").fetchone()
        assert not persisted.db.execute("SELECT 1 FROM artist_state").fetchone()
        assert persisted.audit_authorizations("studio", SCOPE)[-1]["authorized"]
        assert persisted.audit_authorizations("second", Scope(PLATFORM, "second-stream", OWNER))[-1]["authorized"]
