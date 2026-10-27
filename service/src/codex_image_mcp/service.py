from pathlib import Path
import asyncio
import hashlib

from .auth import owner_key, unpack
from .codex import Codex
from .config import Settings
from .images import copy_output
from .lease import StateLease
from .store import Store


class Service:
    def __init__(self, settings: Settings, driver=None):
        self.cfg = settings.validated()
        self.worker = None
        self._close_task = None
        # Store initialization recovers active jobs. Claim ownership before it can
        # touch the database, even when create_app has not entered its lifespan yet.
        self.lease = StateLease(self.cfg.state)
        self.store = None
        try:
            self.store = Store(self.cfg.state / "jobs.sqlite3")
            self.driver = driver if driver is not None else Codex(self.cfg)
        except BaseException:
            self._close_resources()
            raise

    async def start(self):
        if self._close_task is not None:
            raise RuntimeError("SERVICE_CLOSED")
        if self.worker is None:
            self.worker = asyncio.create_task(self._work())

    async def close(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        # One cancelled caller must not abandon shutdown or release ownership while
        # the worker is still writing. Concurrent/repeated close calls share cleanup.
        await asyncio.shield(self._close_task)

    async def _close(self):
        try:
            if self.worker:
                self.worker.cancel()
                await asyncio.gather(self.worker, return_exceptions=True)
        finally:
            await asyncio.to_thread(self._close_resources)

    def _close_resources(self):
        try:
            if self.store is not None:
                self.store.close()
        finally:
            self.lease.close()

    async def _owned_io(self, operation):
        # Cancellation of to_thread only stops its awaiter. Keep the worker alive
        # until its owned write/commit finishes, before close can release the lease.
        task = asyncio.create_task(operation)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # A timeout and shutdown can each cancel the same worker. Every
                # wait must be shielded, including waits after the first cancel.
                cancelled = True
            except Exception:
                break
        if cancelled:
            if not task.cancelled():
                task.exception()  # Retrieve a possible failure before propagating cancellation.
            raise asyncio.CancelledError
        return task.result()

    def authorize(self, envelope: str, operation: str):
        request = unpack(envelope, self.cfg.secret, operation, self.cfg.allowed_users)
        return owner_key(request["scope"]), request["args"]

    async def submit(self, envelope: str) -> dict:
        owner, args = self.authorize(envelope, "submit")
        if set(args) != {"request_id", "prompt"}:
            raise ValueError("INVALID_ARGUMENTS")
        prompt, key = args["prompt"], args["request_id"]
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000:
            raise ValueError("INVALID_PROMPT")
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise ValueError("INVALID_REQUEST_ID")
        return await asyncio.to_thread(
            self.store.submit, owner, key, prompt, self.cfg.max_queue
        )

    async def status(self, envelope: str) -> dict:
        owner, args = self.authorize(envelope, "status")
        return await asyncio.to_thread(self.store.get, owner, args["job_id"])

    async def result(self, envelope: str) -> tuple[bytes, str]:
        owner, args = self.authorize(envelope, "result")
        row = await asyncio.to_thread(self.store.result, owner, args["job_id"])
        path = Path(row["path"]).resolve(strict=True)
        if not path.is_relative_to((self.cfg.state / "artifacts").resolve()):
            raise ValueError("OUTPUT_PATH_REJECTED")
        data = await asyncio.to_thread(path.read_bytes)
        if (
            len(data) != row["size"]
            or hashlib.sha256(data).hexdigest() != row["sha256"]
        ):
            raise ValueError("ARTIFACT_CHANGED")
        return data, row["mime"]

    async def health(self, envelope: str) -> dict:
        self.authorize(envelope, "health")
        return {
            "status": "ready",
            "worker_running": bool(self.worker and not self.worker.done()),
            "generation_probe": False,
            "login_verified": False,
        }

    async def _work(self):
        while True:
            job = await self._owned_io(asyncio.to_thread(self.store.claim))
            if job is None:
                await asyncio.sleep(0.1)
                continue
            artifact = False

            async def ids(job=job, **fields):
                await self._owned_io(asyncio.to_thread(self.store.update, job["id"], **fields))

            async def image(saved_path, job=job):
                nonlocal artifact
                if artifact:
                    return
                target = self.cfg.state / "artifacts" / (job["id"] + ".image")

                async def persist_image():
                    nonlocal artifact
                    data, mime = await asyncio.to_thread(
                        copy_output, saved_path, self.driver.output_root, target,
                        self.cfg.max_image_bytes,
                    )
                    await asyncio.to_thread(self.store.artifact, job["id"], target, data, mime)
                    artifact = True

                # Copy and registration form one owned operation. On shutdown,
                # retain a successfully copied image as a retrievable artifact.
                await self._owned_io(persist_image())

            try:
                terminal = await self.driver.generate(job, ids, image)
                state = (
                    "completed"
                    if terminal == "completed" and artifact
                    else ("artifact_ready" if artifact else "failed")
                )
                await ids(
                    status=state,
                    turn_status=terminal,
                    error_code=None
                    if state == "completed"
                    else "TURN_WITHOUT_COMPLETE_IMAGE",
                )
            except asyncio.CancelledError:
                await ids(
                    status="artifact_ready" if artifact else "outcome_unknown",
                    error_code="SERVICE_STOPPED",
                )
                raise
            except Exception:
                # Do not expose provider errors, paths or secrets. Do not retry an uncertain write.
                await ids(
                    status="artifact_ready" if artifact else "outcome_unknown",
                    error_code="GENERATION_UNCONFIRMED",
                )
