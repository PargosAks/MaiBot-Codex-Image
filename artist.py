"""Explicitly driven, simulation-only artist runtime. No default network adapters.

The Host integration is intentionally not activated by plugin.py. A trusted
adapter supplies the space/scope; models supply creative suggestions only.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol
import asyncio
import hashlib
import json
import uuid

from .policy import Scope, StoreError, canonical_json
from .store import ArtistStore
from .thinking import Thinking


@dataclass(frozen=True)
class RenderResult:
    request_id: str
    request_digest: str
    job_id: str
    state: str
    image: Optional[bytes] = None
    mime: str = "image/png"
    sha256: Optional[str] = None


class Renderer(Protocol):
    simulation: bool
    supports_edit: bool

    async def submit(
        self, *, scope: Scope, request_id: str, prompt: str, source: Optional[dict]
    ) -> RenderResult: ...

    async def status(self, *, scope: Scope, job_id: str) -> RenderResult: ...

    async def lookup(self, *, scope: Scope, request_id: str) -> Optional[RenderResult]: ...


class Sender(Protocol):
    simulation: bool

    async def send(self, *, scope: Scope, version: dict) -> bool: ...


def render_digest(action: str, params: dict) -> str:
    if action == "submit":
        payload = params["prompt"]
    else:
        payload = canonical_json({"operation": "edit", **params})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_id(kind: str, *parts: str) -> str:
    return "artist:" + kind + ":" + hashlib.sha256(canonical_json(parts).encode()).hexdigest()


class Artist:
    def __init__(
        self,
        store: ArtistStore,
        space_id: str,
        scope: Scope,
        thinking: Thinking,
        renderer: Renderer,
        sender: Sender,
        *,
        persona: str = "热爱观察与绘画的小画师",
        idle_seconds: int = 300,
        timeout_seconds: float = 30,
    ):
        if renderer.simulation is not True or sender.simulation is not True:
            raise StoreError("SIMULATION_REQUIRED", "Stage 2 requires simulated drawing and sending")
        if type(idle_seconds) is not int or not 60 <= idle_seconds <= 86400:
            raise StoreError("INVALID_ARGUMENT", "idle_seconds must be 60..86400")
        if not isinstance(persona, str) or not persona.strip() or len(persona) > 4000:
            raise StoreError("INVALID_ARGUMENT", "A bounded public persona is required")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (float, int)):
            raise StoreError("INVALID_ARGUMENT", "Invalid timeout")
        if not 0 < timeout_seconds <= 300:
            raise StoreError("INVALID_ARGUMENT", "timeout_seconds must be positive and at most 300")
        self.store, self.space_id, self.scope = store, space_id, scope
        self.thinking, self.renderer, self.sender = thinking, renderer, sender
        self.persona, self.idle_seconds, self.timeout = persona, idle_seconds, timeout_seconds
        self.lock = asyncio.Lock()

    async def _db(self, name: str, *args, **kwargs):
        # Cancellation does not stop a SQLite worker thread. Join it before
        # releasing this cycle or recording an uncertain external outcome.
        task = asyncio.create_task(
            asyncio.to_thread(
                getattr(self.store, name),
                self.space_id,
                self.scope,
                *args,
                **kwargs,
            )
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def _now(self) -> datetime:
        return self.store.clock().astimezone(timezone.utc)

    async def _epoch(self) -> int:
        grants = await self._db("audit_authorizations")
        if not grants or not grants[-1]["authorized"]:
            raise StoreError("AUTH_REQUIRED", "Continuous delegation is not active")
        return grants[-1]["version"]

    async def on_chat(self, *, message_id: str, text: str) -> bool:
        """Trusted Host event: not a model tool and not a drawing command."""
        return await self._db(
            "enqueue_wake",
            event_id="chat:" + message_id,
            kind="chat",
            text=text,
            source_ref=message_id,
        )

    async def on_completion(self, *, request_id: str) -> bool:
        # A notification can wake the flow, but never assert a result or owner.
        await self._db("get_operation", request_id)
        return await self._db(
            "enqueue_wake",
            event_id="completion:" + request_id,
            kind="completion",
            source_ref=request_id,
        )

    async def tick(self) -> dict:
        await self._db("schedule_wake", interval_seconds=self.idle_seconds)
        return await self.run_once()

    async def serve(self, stop: asyncio.Event) -> None:
        """Opt-in scheduler; never started during plugin import or load."""
        while not stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(self.idle_seconds, 5))
            except TimeoutError:
                pass

    async def recover(self, *, offline: bool = False) -> None:
        await self._db("recover_artist", offline=offline)

    async def _operation(self, request_id):
        try:
            return await self._db("get_operation", request_id)
        except StoreError as exc:
            if exc.code != "NOT_FOUND":
                raise
            return None

    async def _change(self, work_id: str, **changes):
        return await self._db("update_artist_work", work_id, changes=changes)

    async def _version(self, version_id: str) -> dict:
        version = await self._db("get_version", version_id)
        if (
            not isinstance(version["data"], bytes)
            or not version["data"]
            or hashlib.sha256(version["data"]).hexdigest() != version["sha256"]
        ):
            raise StoreError("ARTIFACT_INVALID", "Stored image failed integrity verification")
        return version

    async def _context(self, events: list) -> dict:
        ideas = [i for i in await self._db("list_inspirations") if i["consumed_by"] is None]
        works = await self._db("list_artist_works")
        visible = [w for w in works if w["status"] not in {"finished", "abandoned"}]
        visible += [w for w in works if w["status"] in {"finished", "abandoned"}][-20:]
        return {
            "persona": self.persona,
            "events": [
                {k: event.get(k) for k in ("event_id", "kind", "text", "source_ref")} for event in events
            ],
            "inspirations": [
                {k: idea.get(k) for k in ("key", "summary", "origin", "source_refs")} for idea in ideas[-50:]
            ],
            "works": [
                {
                    **{k: work.get(k) for k in ("work_id", "status", "brief", "best_version")},
                    "reason": work.get("reason") or "",
                }
                for work in visible
            ],
        }

    async def _think(self, task: str, context: dict, cycle: dict, *, work_id=None, version=None):
        identity = stable_id(
            task, self.space_id, str(cycle["through"]), work_id or "", (version or {}).get("version_id", "")
        )
        # A known failed model call may be attempted later after persisted
        # backoff. An unknown call is not repeated, even after restart.
        attempt = 0
        while True:
            request_id = identity + ":" + str(attempt)
            operation = await self._operation(request_id)
            if operation is None or operation["state"] != "failed":
                break
            attempt += 1
        if operation is None:
            operation = await self._db(
                "reserve",
                request_id=request_id,
                action="think",
                work_id=work_id,
                params={"task": task, "context": context, "version_id": (version or {}).get("version_id")},
                trigger="chat" if any(e["kind"] == "chat" for e in cycle["events"]) else "idle",
                trigger_ref=str(cycle["through"]),
                expected_auth_version=cycle["auth_version"],
            )
        if operation["auth_version"] != cycle["auth_version"] or await self._epoch() != cycle["auth_version"]:
            raise StoreError("AUTH_CHANGED", "Stored thought belongs to an older authorization")
        if operation["state"] == "succeeded":
            return json.loads(operation["evidence"]), request_id
        if operation["state"] != "reserved":
            raise StoreError("THINK_UNKNOWN", "Previous thought is unresolved; no replay")
        await self._db("mark_submitting", request_id)
        try:
            saved = operation["params"]["context"]
            if task == "review":
                result = await asyncio.wait_for(
                    self.thinking.review(saved, image=version["data"], mime=version["mime"]),
                    self.timeout,
                )
            elif task == "inspire":
                result = await asyncio.wait_for(self.thinking.inspire(saved), self.timeout)
            else:
                result = await asyncio.wait_for(self.thinking.plan(saved), self.timeout)
        except asyncio.CancelledError:
            await self._db("mark_unknown", request_id, reason="THINK_CANCELLED")
            raise
        except Exception as exc:
            if isinstance(exc, (TimeoutError, OSError)) or (
                isinstance(exc, StoreError) and exc.code == "THINKING_UNKNOWN"
            ):
                await self._db("mark_unknown", request_id, reason="THINK_UNCONFIRMED")
                raise StoreError("THINK_UNKNOWN", "Model outcome is unknown; no replay") from None
            await self._db("settle", request_id, outcome="failed", evidence="THINK_FAILED")
            raise StoreError("THINK_FAILED", "Model/validation failed; review is still pending") from None
        await self._db("settle", request_id, outcome="succeeded", evidence=canonical_json(result))
        if await self._epoch() != cycle["auth_version"]:
            raise StoreError("AUTH_CHANGED", "Discard stale model advice after an authorization change")
        return result, request_id

    async def _remember(self, result: dict, cycle: dict) -> None:
        events = {e["event_id"]: e for e in cycle["events"] if e["kind"] == "chat"}
        for idea in result["ideas"]:
            normalized = " ".join(idea["summary"].split()).casefold()
            origin = "chat" if idea["source_ids"] else "idle"
            await self._db(
                "remember_inspiration",
                # The same theme may arise independently in conversation and
                # idle thought. Keep provenance distinct rather than rewriting it.
                key=stable_id("idea", origin, normalized),
                summary=idea["summary"],
                source_refs=[events[i]["source_ref"] for i in idea["source_ids"]],
                origin=origin,
                expected_auth_version=cycle["auth_version"],
            )

    async def run_once(self) -> dict:
        async with self.lock:
            # The database claim, not this instance's lock, arbitrates different
            # runtime objects/connections. No timer-based lease stealing.
            token = uuid.uuid4().hex
            cycle = await self._db("claim_wake", token=token)
            if cycle is None:
                return {"state": "idle"}
            retry = False
            pause_after = 0
            try:
                context = await self._context(cycle["events"])
                works = await self._db("list_artist_works")
                active = next((w for w in works if w["status"] not in {"finished", "abandoned"}), None)
                if active is not None:
                    if active["status"] in {"creating", "review_pending"} and any(
                        e["kind"] == "chat" for e in cycle["events"]
                    ):
                        # Read-only reconciliation does not require thinking
                        # allowance. Persist the receipt before optional inspiration;
                        # on failure finish_wake retains the unprocessed chat batch.
                        polled = await self._poll(active)
                        response, _ = await self._think("inspire", context, cycle)
                        await self._remember(response, cycle)
                        if active["status"] == "creating":
                            return polled
                    if (
                        active["next_check_at"]
                        and datetime.fromisoformat(active["next_check_at"]) > self._now()
                    ):
                        return {"state": "waiting", "work_id": active["work_id"]}
                    if active["status"] in {"creating", "review_pending"}:
                        return await self._advance(active, context, cycle)
                else:
                    # A finished painting may still have an uncertain backend
                    # turn. Reconcile it without reopening its artistic review.
                    for work in works:
                        if work["pending_request"]:
                            operation = await self._db("get_operation", work["pending_request"])
                            if operation["state"] in {"submitting", "accepted", "unknown"}:
                                polled = await self._poll(work)
                                if any(e["kind"] == "chat" for e in cycle["events"]):
                                    response, _ = await self._think("inspire", context, cycle)
                                    await self._remember(response, cycle)
                                return polled
                response, _ = await self._think("plan", context, cycle)
                await self._remember(response, cycle)
                result = await self._decide(response["decision"], cycle)
                pause_after = result.pop("wake_after", 0)
                return result
            except asyncio.CancelledError:
                retry = True
                raise
            except StoreError as exc:
                retry = exc.code not in {"AUTH_REQUIRED", "AUTH_CHANGED"}
                return {"state": "blocked", "reason": exc.code}
            except Exception:
                retry = True
                # Unexpected defects remain visible; never report successful work.
                raise
            finally:
                await self._db(
                    "finish_wake",
                    token=token,
                    acknowledge=not retry,
                    retry_after=300 if retry else pause_after,
                )

    async def _decide(self, decision: dict, cycle: dict) -> dict:
        action = decision["action"]
        if action == "wait":
            return {
                "state": "waiting",
                "reason": decision["reason"],
                "wake_after": decision.get("delay_seconds", self.idle_seconds),
            }
        if action == "create":
            work_id = stable_id("work", self.space_id, str(cycle["through"]))
            work = await self._db(
                "plan_work",
                work_id=work_id,
                brief=decision["brief"],
                inspiration_keys=decision["inspiration_keys"],
                expected_auth_version=cycle["auth_version"],
            )
            return {"state": work["status"], "work_id": work_id}
        work = await self._db("get_artist_work", decision["work_id"])
        if action == "share":
            return await self._share(work, cycle)
        if action == "continue" and work["status"] in {"planned", "deferred"}:
            if work["next_check_at"] and datetime.fromisoformat(work["next_check_at"]) > self._now():
                return {"state": "waiting", "work_id": work["work_id"]}
            if work["status"] == "deferred":
                work = await self._change(work["work_id"], status="planned", next_check_at=None)
            return await self._start_render(work, cycle)
        if action == "abandon":
            await self._change(work["work_id"], status="abandoned", reason=decision["reason"])
            return {"state": "abandoned", "work_id": work["work_id"]}
        if action == "defer" and work["status"] in {"planned", "deferred"}:
            due = (self._now() + timedelta(seconds=decision["delay_seconds"])).isoformat()
            await self._change(
                work["work_id"], status="deferred", reason=decision["reason"], next_check_at=due
            )
            return {"state": "deferred", "work_id": work["work_id"]}
        raise StoreError("INVALID_DECISION", "Decision does not apply to current work state")

    async def _advance(self, work: dict, context: dict, cycle: dict) -> dict:
        status = work["status"]
        if status == "deferred":
            work = await self._change(work["work_id"], status="planned", next_check_at=None)
            status = "planned"
        if status == "planned":
            return await self._start_render(work, cycle)
        if status == "creating":
            return await self._poll(work)
        if status == "review_pending":
            return await self._review(work, context, cycle)
        raise StoreError("INVALID_STATE", "Unexpected active work state")

    async def _start_render(self, work: dict, cycle: dict) -> dict:
        parent = await self._version(work["parent_version"]) if work["parent_version"] else None
        if parent is not None and not self.renderer.supports_edit:
            await self._change(work["work_id"], reason="EDIT_UNSUPPORTED")
            return {"state": "blocked", "reason": "EDIT_UNSUPPORTED"}
        request_id = stable_id("render", work["work_id"], work["parent_version"] or "original")
        params = {"prompt": work["edit_prompt"] if parent else work["brief"]["prompt"]}
        action = "edit" if parent else "submit"
        if parent:
            params.update(source_job_id=parent["job_id"], source_sha256=parent["sha256"])
        try:
            operation = await self._db(
                "reserve",
                request_id=request_id,
                action=action,
                params=params,
                work_id=work["work_id"],
                trigger="completion" if parent else "idle",
                expected_auth_version=cycle["auth_version"],
            )
        except StoreError as exc:
            if exc.code == "REVISION_LIMIT" and parent:
                await self._change(
                    work["work_id"],
                    status="finished",
                    reason="REVISION_LIMIT",
                    best_version=work["best_version"] or parent["version_id"],
                )
                return {"state": "finished", "reason": "REVISION_LIMIT"}
            raise
        await self._change(work["work_id"], status="creating", pending_request=request_id, reason="")
        work = await self._db("get_artist_work", work["work_id"])
        if operation["state"] != "reserved":
            return await self._poll(work)
        await self._db("mark_submitting", request_id)
        source = None if parent is None else {k: parent[k] for k in ("job_id", "sha256", "data", "mime")}
        try:
            result = await asyncio.wait_for(
                self.renderer.submit(
                    scope=self.scope,
                    request_id=request_id,
                    prompt=params["prompt"],
                    source=source,
                ),
                self.timeout,
            )
        except BaseException as exc:
            await self._db("mark_unknown", request_id, reason="SUBMIT_UNCONFIRMED")
            if isinstance(exc, asyncio.CancelledError):
                raise
            return {"state": "blocked", "reason": "SUBMIT_UNCONFIRMED"}
        return await self._receive(work, result)

    async def _poll(self, work: dict) -> dict:
        operation = await self._db("get_operation", work["pending_request"])
        if operation["state"] == "reserved":
            # No external attempt took place. Preserve the plan for the next
            # cycle, where authorization is rechecked before dispatch.
            await self._change(work["work_id"], status="planned")
            return {"state": "planned"}
        try:
            if operation["job_id"]:
                result = await asyncio.wait_for(
                    self.renderer.status(scope=self.scope, job_id=operation["job_id"]), self.timeout
                )
            else:
                result = await asyncio.wait_for(
                    self.renderer.lookup(scope=self.scope, request_id=operation["request_id"]), self.timeout
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            return {"state": "blocked", "reason": "LOOKUP_UNAVAILABLE"}
        if result is None:
            return {"state": "blocked", "reason": "LOOKUP_NOT_FOUND"}
        return await self._receive(work, result)

    async def _receive(self, work: dict, result: RenderResult) -> dict:
        request_id = work["pending_request"]
        operation = await self._db("get_operation", request_id)
        valid = (
            isinstance(result, RenderResult)
            and result.request_id == request_id
            and result.request_digest == render_digest(operation["action"], operation["params"])
            and isinstance(result.job_id, str)
            and bool(result.job_id.strip())
            and (not operation["job_id"] or operation["job_id"] == result.job_id)
            and result.state in {"queued", "running", "completed", "failed", "outcome_unknown"}
        )
        if not valid:
            if operation["state"] in {"submitting", "accepted", "unknown"}:
                await self._db("mark_unknown", request_id, reason="RECEIPT_MISMATCH")
            return {"state": "blocked", "reason": "RECEIPT_MISMATCH"}
        if (operation["state"] == "succeeded" and result.state != "completed") or (
            operation["state"] == "failed" and result.state != "failed"
        ):
            return {"state": "blocked", "reason": "RECEIPT_MISMATCH"}
        if operation["state"] == "unknown":
            await self._db(
                "reconcile",
                request_id,
                outcome="accepted",
                evidence="Matching read-only receipt",
                job_id=result.job_id,
            )
        elif operation["state"] == "submitting":
            await self._db("mark_accepted", request_id, job_id=result.job_id)
        if result.state in {"completed", "failed"}:
            if operation["state"] not in {"succeeded", "failed"}:
                await self._db(
                    "settle",
                    request_id,
                    outcome="succeeded" if result.state == "completed" else "failed",
                    evidence="Confirmed render terminal",
                    job_id=result.job_id,
                )
        elif result.state == "outcome_unknown":
            await self._db("mark_unknown", request_id, reason="RENDER_OUTCOME_UNKNOWN")
        if result.image is not None:
            if (
                not isinstance(result.image, bytes)
                or not 0 < len(result.image) <= 8 * 1024 * 1024
                or result.mime not in {"image/png", "image/jpeg", "image/webp"}
                or hashlib.sha256(result.image).hexdigest() != result.sha256
            ):
                return {"state": "blocked", "reason": "ARTIFACT_INVALID"}
            version_id = stable_id("version", work["work_id"], request_id)
            await self._db(
                "record_version",
                work_id=work["work_id"],
                version_id=version_id,
                request_id=request_id,
                job_id=result.job_id,
                data=result.image,
                mime=result.mime,
                parent_id=work["parent_version"],
            )
            if work["status"] in {"finished", "abandoned"}:
                return {"state": work["status"], "reason": "reconciled"}
            await self._change(
                work["work_id"],
                status="review_pending",
                reason="",
                best_version=work["best_version"] or version_id,
            )
            return {"state": "review_pending", "version_id": version_id}
        if result.state == "failed":
            if work["status"] in {"finished", "abandoned"}:
                return {"state": work["status"], "reason": "reconciled"}
            await self._change(
                work["work_id"],
                status="finished" if work["best_version"] else "abandoned",
                reason="RENDER_FAILED",
            )
            return {"state": "finished" if work["best_version"] else "abandoned"}
        return {
            "state": "creating",
            "reason": "ARTIFACT_MISSING" if result.state == "completed" else result.state,
        }

    async def _review(self, work: dict, context: dict, cycle: dict) -> dict:
        original = await self._db("get_operation", work["pending_request"])
        if original["state"] in {"submitting", "accepted", "unknown"}:
            await self._poll(work)
        versions = await self._db("list_versions", work["work_id"])
        current = next(v for v in versions if v["request_id"] == work["pending_request"])
        version = await self._version(current["version_id"])
        review_context = {
            **context,
            "current_work": {k: work[k] for k in ("work_id", "brief", "best_version")},
            "current_version": {k: version[k] for k in ("version_id", "sha256", "mime")},
            "versions": [
                {k: v.get(k) for k in ("version_id", "parent_id", "sha256", "mime")} for v in versions
            ],
        }
        reviews = await self._db("list_reviews", work["work_id"])
        prior = next((r for r in reversed(reviews) if r["version_id"] == version["version_id"]), None)
        if prior is not None:
            review = prior["review"]
        else:
            review, request_id = await self._think(
                "review", review_context, cycle, work_id=work["work_id"], version=version
            )
            await self._db(
                "record_review", version_id=version["version_id"], request_id=request_id, review=review
            )
        best = review.get(
            "best_version_id",
            version["version_id"]
            if review["decision"] == "accept"
            else work["best_version"] or version["version_id"],
        )
        if review["decision"] == "revise":
            original = await self._db("get_operation", work["pending_request"])
            if original["state"] not in {"succeeded", "failed"}:
                # Evaluate available art, but reconcile the unresolved render
                # before creating another revision. Never refund its hold.
                await self._change(work["work_id"], reason="RENDER_UNRESOLVED")
                return {"state": "blocked", "reason": "RENDER_UNRESOLVED"}
            prompt = review["edit_prompt"] + "\n保留：" + review["preserve"]
            await self._change(
                work["work_id"],
                status="planned",
                parent_version=version["version_id"],
                edit_prompt=prompt,
                best_version=best,
                pending_request=None,
                reason=review["reason"],
            )
            return {"state": "planned", "reason": "revision"}
        state = "finished" if review["decision"] == "accept" else "abandoned"
        await self._change(work["work_id"], status=state, best_version=best, reason=review["reason"])
        return {"state": state, "best_version": best}

    async def _share(self, work: dict, cycle: dict) -> dict:
        if work["status"] != "finished" or not work["best_version"]:
            raise StoreError("INVALID_DECISION", "Only finished reviewed work can be shared")
        version = await self._version(work["best_version"])
        request_id = stable_id("share", work["work_id"], version["version_id"])
        operation = await self._db(
            "reserve",
            request_id=request_id,
            action="share",
            work_id=work["work_id"],
            params={"job_id": version["job_id"], "version_id": version["version_id"]},
            expected_auth_version=cycle["auth_version"],
        )
        if operation["state"] != "reserved":
            return {"state": "share_recorded", "outcome": operation["state"]}
        await self._db("mark_submitting", request_id)
        try:
            result = await asyncio.wait_for(self.sender.send(scope=self.scope, version=version), self.timeout)
        except BaseException as exc:
            await self._db("mark_unknown", request_id, reason="SEND_UNCONFIRMED")
            if isinstance(exc, asyncio.CancelledError):
                raise
            return {"state": "share_unknown"}
        if type(result) is not bool:
            await self._db("mark_unknown", request_id, reason="SEND_RECEIPT_INVALID")
            return {"state": "share_unknown"}
        await self._db(
            "settle",
            request_id,
            outcome="succeeded" if result else "failed",
            evidence="Simulated send receipt",
        )
        return {"state": "shared" if result else "share_failed"}
