from io import BytesIO
import asyncio

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


async def test_duplicate_and_cross_owner(settings):
    service = Service(settings)
    try:
        request = envelope("submit", {"request_id": "same", "prompt": "circle"})
        a, b = await asyncio.gather(service.submit(request), service.submit(request))
        assert a["job_id"] == b["job_id"]
        with pytest.raises(ValueError, match="REQUEST_CONFLICT"):
            await service.submit(
                envelope("submit", {"request_id": "same", "prompt": "changed"})
            )
        foreign = dict(SCOPE, stream_id="other-stream")
        with pytest.raises(ValueError, match="JOB_NOT_FOUND"):
            await service.result(envelope("result", {"job_id": a["job_id"]}, foreign))
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


def test_context_signature_and_operation(settings):
    good = envelope("status", {"job_id": "x"})
    assert unpack(good, SECRET, "status", settings.allowed_users)["scope"] == SCOPE
    with pytest.raises(ValueError):
        unpack(good, SECRET, "submit", settings.allowed_users)
    with pytest.raises(ValueError):
        unpack(good + "bad", SECRET, "status", settings.allowed_users)
    with pytest.raises(ValueError):
        unpack(good, SECRET, "status", ("qq:other",))


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
