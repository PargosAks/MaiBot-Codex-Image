"""Group sharing metadata and timeout boundaries; no external effects."""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from image_plugin_under_test.host_runtime import ArtistSpace
from image_plugin_under_test.policy import StoreError
from image_plugin_under_test.thinking import Thinking
from test_artist_flow import SCOPE, finished, step, studio as studio


def test_group_collection_and_sharing_are_independent_and_default_to_collection():
    space = ArtistSpace(group_inspiration=True)
    assert space.group_share_mode == "collect_only"
    space = ArtistSpace(group_inspiration=False, group_share_mode="original_group")
    assert space.group_inspiration is False and space.group_share_mode == "original_group"
    with pytest.raises(ValidationError):
        space.group_share_mode = "collect_only"


@pytest.mark.parametrize("value", [True, False, None, "", "private", "any_group", " original_group"])
def test_share_mode_rejects_invalid_configuration(value):
    with pytest.raises(ValidationError):
        ArtistSpace(group_share_mode=value)


@pytest.mark.parametrize("sharing", [
    {"mode": "collect_only", "allowed": True}, {"mode": "original_group", "allowed": "yes"},
    {"mode": "foreign", "allowed": True}, {"mode": "private", "allowed": True, "target": "foreign"},
])
async def test_malformed_permission_never_reaches_model(sharing):
    model = AsyncMock()
    with pytest.raises(StoreError, check=lambda exc: exc.code == "INVALID_CONTEXT"):
        await Thinking(model).plan({"sharing": sharing})
    model.assert_not_awaited()


async def test_model_receives_permission_without_target_identity():
    model = AsyncMock(return_value={"ideas": [], "decision": {"action": "wait", "reason": "收藏"}})
    permission = {"mode": "collect_only", "allowed": False}
    context = {"sharing": deepcopy(permission)}
    await Thinking(model).plan(context)
    request = model.call_args.args[0]
    assert request["context"]["sharing"] == permission and context["sharing"] == permission
    assert "不表示必须分享" in request["instructions"]


@pytest.mark.parametrize("with_preflight", [False, True])
async def test_group_target_check_has_its_own_outer_wait_budget(studio, monkeypatch, with_preflight):
    artist, store, _, model, _, sender = studio
    await finished(studio)
    model.share = True
    artist.send_timeout_seconds = 1
    artist.share_preflight_seconds = 30 if with_preflight else 0
    original_send = sender.send
    original_wait = asyncio.wait_for

    async def scaled_wait(awaitable, timeout):
        return await original_wait(awaitable, timeout / 100)

    async def delayed_send(**kwargs):
        # Simulates a 12s group preflight before a short successful upload.
        await asyncio.sleep(0.12)
        return await original_send(**kwargs)

    monkeypatch.setattr(asyncio, "wait_for", scaled_wait)
    monkeypatch.setattr(sender, "send", delayed_send)
    result = await step(studio)
    assert result["state"] == ("shared" if with_preflight else "share_unknown")
    assert len(sender.calls) == int(with_preflight)
    operation = next(row for row in store.audit_operations("s", SCOPE) if row["action"] == "share")
    assert operation["state"] == ("succeeded" if with_preflight else "unknown")
