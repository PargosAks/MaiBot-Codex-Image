from io import BytesIO
import asyncio
import base64
import hashlib
import hmac
import json
import time

from PIL import Image, UnidentifiedImageError
import pytest

from codex_image_mcp.auth import unpack
from codex_image_mcp.images import copy_output, validate
from codex_image_mcp.service import Service
from codex_image_mcp.store import Store
from conftest import SECRET, SCOPE, envelope


async def wait_job(service, job_id, states):
    async with asyncio.timeout(5):
        while True:
            result = await service.status(envelope("status", {"job_id": job_id}))
            if result["status"] in states:
                return result
            await asyncio.sleep(0.02)


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("ok", "completed"),
        ("disconnect", "artifact_ready"),
        ("no-image", "failed"),
        ("timeout", "outcome_unknown"),
        ("api", "outcome_unknown"),
    ],
)
async def test_process_protocol_and_failure_boundaries(
    settings, driver_factory, mode, expected
):
    service = Service(settings, driver_factory(mode))
    await service.start()
    try:
        job = await service.submit(
            envelope("submit", {"request_id": "message-1", "prompt": "circle"})
        )
        final = await wait_job(service, job["job_id"], {expected})
        if mode in {"ok", "disconnect"}:
            data, mime = await service.result(
                envelope("result", {"job_id": job["job_id"]})
            )
            assert mime == "image/png" and Image.open(BytesIO(data)).size == (32, 32)
            assert len(final["artifacts"]) == 1
        if mode == "ok":
            assert final["turn_status"] == "completed"
        if mode == "api":
            assert (
                service.store.db.execute("SELECT thread_id FROM jobs").fetchone()[0]
                is None
            )
    finally:
        await service.close()


@pytest.mark.parametrize(
    "changed_scope", [{"user_id": "other"}, {"stream_id": "other-stream"}, {"platform": "other"}]
)
async def test_duplicate_and_cross_owner(settings, changed_scope):
    service = Service(settings)
    try:
        request = envelope("submit", {"request_id": "same", "prompt": "circle"})
        a, b = await asyncio.gather(service.submit(request), service.submit(request))
        assert a["job_id"] == b["job_id"]
        with pytest.raises(ValueError, match="REQUEST_CONFLICT"):
            await service.submit(
                envelope("submit", {"request_id": "same", "prompt": "changed"})
            )
        foreign = dict(SCOPE, **changed_scope)
        for operation in ("status", "result"):
            with pytest.raises(ValueError, match="JOB_NOT_FOUND"):
                await getattr(service, operation)(envelope(operation, {"job_id": a["job_id"]}, foreign))
    finally:
        await service.close()


async def test_signed_users_can_submit_without_service_allowlist(settings):
    service = Service(settings, driver=object())
    try:
        args = {"request_id": "same-message", "prompt": "circle"}
        other = dict(SCOPE, user_id="not-in-legacy-list")
        original_job = await service.submit(envelope("submit", args))
        other_job = await service.submit(envelope("submit", args, other))
        assert original_job["job_id"] != other_job["job_id"]
        assert await service.status(envelope("status", {"job_id": other_job["job_id"]}, other)) == other_job
        for scope, foreign_job in ((SCOPE, other_job), (other, original_job)):
            for operation in ("status", "result"):
                with pytest.raises(ValueError, match="JOB_NOT_FOUND"):
                    await getattr(service, operation)(
                        envelope(operation, {"job_id": foreign_job["job_id"]}, scope)
                    )
    finally:
        await service.close()


def test_restart_never_replays_active(settings):
    store = Store(settings.state / "jobs.sqlite3")
    job = store.submit("owner", "message", "test", 10)
    assert store.claim()["id"] == job["job_id"]
    store.close()
    restored = Store(settings.state / "jobs.sqlite3")
    try:
        assert restored.get("owner", job["job_id"])["status"] == "outcome_unknown"
        assert restored.claim() is None
    finally:
        restored.close()


def test_context_signature_and_operation():
    good = envelope("status", {"job_id": "x"})
    assert unpack(good, SECRET, "status")["scope"] == SCOPE
    with pytest.raises(ValueError, match="Unauthorized request context"):
        unpack(good, SECRET, "submit")
    with pytest.raises(ValueError, match="Unauthorized request context"):
        unpack(good + "bad", SECRET, "status")


@pytest.mark.parametrize("field", ["platform", "stream_id", "user_id"])
@pytest.mark.parametrize("value", [None, "", " \t", 123])
def test_context_rejects_empty_or_invalid_scope_fields(field, value):
    with pytest.raises(ValueError, match="Unauthorized request context"):
        unpack(envelope("status", {}, dict(SCOPE, **{field: value})), SECRET, "status")


@pytest.mark.parametrize("field", ["platform", "stream_id", "user_id"])
def test_context_rejects_missing_scope_fields(field):
    scope = {key: value for key, value in SCOPE.items() if key != field}
    with pytest.raises(ValueError, match="Unauthorized request context"):
        unpack(envelope("status", {}, scope), SECRET, "status")


@pytest.mark.parametrize("scope", [{}, [], "invalid", dict(SCOPE, extra="unexpected")])
def test_context_rejects_invalid_scope(scope):
    with pytest.raises(ValueError, match="Unauthorized request context"):
        unpack(envelope("status", {}, scope), SECRET, "status")


@pytest.mark.parametrize("args", [None, [], "invalid", 123])
def test_context_requires_dict_arguments(args):
    with pytest.raises(ValueError, match="Unauthorized request context"):
        unpack(envelope("status", args), SECRET, "status")


def signed_payload(data):
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    signature = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + signature


@pytest.mark.parametrize("issued_at", [879, 1121, None, "1000", True, float("nan"), float("inf")])
def test_context_rejects_invalid_or_expired_timestamp(monkeypatch, issued_at):
    monkeypatch.setattr(time, "time", lambda: 1000)
    signed = signed_payload({"scope": SCOPE, "op": "status", "args": {}, "iat": issued_at})
    with pytest.raises(ValueError, match="Unauthorized request context"):
        unpack(signed, SECRET, "status")


@pytest.mark.parametrize("field", ["scope", "op", "args", "iat"])
def test_context_rejects_missing_envelope_fields(field):
    data = {"scope": SCOPE, "op": "status", "args": {}, "iat": time.time()}
    del data[field]
    with pytest.raises(ValueError, match="Unauthorized request context"):
        unpack(signed_payload(data), SECRET, "status")


def test_image_validation_and_paths(tmp_path):
    outside = tmp_path / "outside.png"
    Image.new("RGB", (16, 16)).save(outside)
    with pytest.raises(ValueError, match="OUTPUT_PATH_REJECTED"):
        copy_output(str(outside), tmp_path / "allowed", tmp_path / "dest", 10000)
    with pytest.raises(ValueError):
        validate(outside.read_bytes(), 1)
    with pytest.raises(UnidentifiedImageError):
        validate(b"not an image", 1000)


async def test_shutdown_during_generation(settings, driver_factory):
    service = Service(settings, driver_factory("timeout"))
    await service.start()
    job = await service.submit(envelope("submit", {"request_id": "m", "prompt": "p"}))
    await wait_job(service, job["job_id"], {"running"})
    await service.close()
    store = Store(settings.state / "jobs.sqlite3")
    try:
        assert (
            store.get(
                service.authorize(envelope("health", {}), "health")[0], job["job_id"]
            )["status"]
            == "outcome_unknown"
        )
    finally:
        store.close()
