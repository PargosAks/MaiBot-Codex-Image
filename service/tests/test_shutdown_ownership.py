"""Shutdown must retain directory ownership until background writes have finished."""

import asyncio
import threading

from PIL import Image
import pytest

from codex_image_mcp.service import Service
import codex_image_mcp.service as service_module
from conftest import envelope


@pytest.mark.parametrize("cancel_before_close", [False, True])
async def test_cancelled_close_waits_for_image_copy_and_registration(settings, monkeypatch, cancel_before_close):
    started, release = threading.Event(), threading.Event()
    original = service_module.copy_output
    settings.state.mkdir(parents=True)
    source = settings.state / "source.png"
    Image.new("RGB", (16, 16), "orange").save(source)

    def blocked_copy(*args):
        started.set()
        assert release.wait(5), "Test did not release the image writer"
        return original(*args)

    class Driver:
        output_root = settings.state

        async def generate(self, job, ids, image):
            await ids(status="running")
            await image(str(source))
            return "completed"

    monkeypatch.setattr(service_module, "copy_output", blocked_copy)
    service = Service(settings, Driver())
    await service.start()
    job = await service.submit(envelope("submit", {"request_id": "shutdown-copy", "prompt": "square"}))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        if cancel_before_close:
            # Model a generation deadline cancelling the worker before close()
            # cancels it again while the image transaction is still blocked.
            service.worker.cancel()
            await asyncio.sleep(.02)
        closing = asyncio.create_task(service.close())
        await asyncio.sleep(.05)
        assert not closing.done()
        if cancel_before_close:
            service.worker.cancel()
            await asyncio.sleep(.02)
            assert not service.worker.done()
        with pytest.raises(RuntimeError, match="STATE_DIRECTORY_IN_USE"):
            Service(settings)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        with pytest.raises(RuntimeError, match="STATE_DIRECTORY_IN_USE"):
            Service(settings)
    finally:
        release.set()
        await service.close()
    restored = Service(settings)
    try:
        status = await restored.status(envelope("status", {"job_id": job["job_id"]}))
        assert status["status"] == "artifact_ready" and len(status["artifacts"]) == 1
        data, mime = await restored.result(envelope("result", {"job_id": job["job_id"]}))
        assert data == source.read_bytes() and mime == "image/png"
    finally:
        await restored.close()


async def test_close_waits_for_pending_claim_before_releasing_state(settings, monkeypatch):
    service = Service(settings)
    started, release = threading.Event(), threading.Event()
    original = service.store.claim

    def blocked_claim():
        started.set()
        assert release.wait(5)
        return original()

    monkeypatch.setattr(service.store, "claim", blocked_claim)
    job = await service.submit(envelope("submit", {"request_id": "shutdown-claim", "prompt": "square"}))
    await service.start()
    try:
        assert await asyncio.to_thread(started.wait, 3)
        closing = asyncio.create_task(service.close())
        await asyncio.sleep(.05)
        assert not closing.done()
        with pytest.raises(RuntimeError, match="STATE_DIRECTORY_IN_USE"):
            Service(settings)
    finally:
        release.set()
        await service.close()
    await closing
    restored = Service(settings)
    try:
        status = await restored.status(envelope("status", {"job_id": job["job_id"]}))
        assert status["status"] == "outcome_unknown"
        assert restored.store.claim() is None
    finally:
        await restored.close()
