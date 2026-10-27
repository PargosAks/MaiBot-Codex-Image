from pathlib import Path
import asyncio
import hashlib

from .auth import owner_key, unpack
from .codex import Codex
from .config import Settings
from .images import copy_output
from .store import Store


class Service:
    def __init__(self, settings: Settings, driver=None):
        self.cfg = settings.validated()
        self.store = Store(settings.state / "jobs.sqlite3")
        self.driver = driver or Codex(settings)
        self.worker = None

    async def start(self):
        self.worker = asyncio.create_task(self._work())

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await asyncio.to_thread(self.store.close)

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
            job = await asyncio.to_thread(self.store.claim)
            if job is None:
                await asyncio.sleep(0.1)
                continue
            artifact = False

            async def ids(job=job, **fields):
                await asyncio.to_thread(self.store.update, job["id"], **fields)

            async def image(saved_path, job=job):
                nonlocal artifact
                if artifact:
                    return
                target = self.cfg.state / "artifacts" / (job["id"] + ".image")
                data, mime = await asyncio.to_thread(
                    copy_output,
                    saved_path,
                    self.cfg.home / "generated_images",
                    target,
                    self.cfg.max_image_bytes,
                )
                await asyncio.to_thread(
                    self.store.artifact, job["id"], target, data, mime
                )
                artifact = True

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
