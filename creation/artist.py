"""Artist state machine; Host adapters explicitly opt into external effects."""

from datetime import datetime, timedelta, timezone
import asyncio
import hashlib
import json
import math
import uuid

from ..domain.errors import BackendError
from ..domain.async_work import join_worker
from ..domain.policy import Scope, StoreError, canonical_json
from ..domain.profiles import CATALOG_ACTIONS, render_prompt
from ..domain.assets import reference_prompt
from ..domain.store import ArtistStore
from .thinking import Thinking
# 这些名称保留原导入入口；实际契约由独立模块维护。
from .contracts import ReceiptRenderer, Renderer, RenderResult, Sender, render_digest as render_digest, stable_id
from .receipts import ReceiptService
from .reviewing import ReviewService
from .sharing import ShareService


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
        send_timeout_seconds: float = 240.0,
        share_preflight_seconds: float = 0.0,
        allow_network_renderer: bool = False,
        allow_host_sender: bool = False,
        reference_inputs: bool = False,
    ):
        if any(type(value) is not bool for value in (allow_network_renderer, allow_host_sender, reference_inputs)):
            raise StoreError("INVALID_ARGUMENT", "Network rendering requires an explicit boolean opt-in")
        if (renderer.simulation is not True and not allow_network_renderer) or (
            sender.simulation is not True and not allow_host_sender
        ):
            raise StoreError(
                "SIMULATION_REQUIRED",
                "Use simulated drawing and sending unless each is explicitly opted in",
            )
        if type(idle_seconds) is not int or not 60 <= idle_seconds <= 86400:
            raise StoreError("INVALID_ARGUMENT", "idle_seconds must be 60..86400")
        if not isinstance(persona, str) or not persona.strip() or len(persona) > 4000:
            raise StoreError("INVALID_ARGUMENT", "A bounded public persona is required")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (float, int)):
            raise StoreError("INVALID_ARGUMENT", "Invalid timeout")
        if not 0 < timeout_seconds <= 300:
            raise StoreError("INVALID_ARGUMENT", "timeout_seconds must be positive and at most 300")
        if (isinstance(send_timeout_seconds, bool) or not isinstance(send_timeout_seconds, (float, int))
                or not 0 < send_timeout_seconds <= 600 or not math.isfinite(send_timeout_seconds)):
            raise StoreError("INVALID_ARGUMENT", "send_timeout_seconds must be finite, positive and at most 600")
        if (type(share_preflight_seconds) not in (float, int) or not math.isfinite(share_preflight_seconds)
                or not 0 <= share_preflight_seconds <= 30):
            raise StoreError("INVALID_ARGUMENT", "share_preflight_seconds must be finite and within 0..30")
        self.share_preflight_seconds = share_preflight_seconds
        self.store, self.space_id, self.scope = store, space_id, scope
        self.thinking, self.renderer, self.sender = thinking, renderer, sender
        self.persona, self.idle_seconds, self.timeout = persona, idle_seconds, timeout_seconds
        self.send_timeout_seconds = send_timeout_seconds
        self.reference_inputs = reference_inputs
        self.lock = asyncio.Lock()

    async def _db(self, name: str, *args, **kwargs):
        # 按持久空间绑定调用存储；取消后仍等线程收尾，不释放半笔操作。
        return await join_worker(getattr(self.store, name), self.space_id, self.scope, *args, **kwargs)

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
        await self._db("schedule_progress_wake")
        return await self.run_once()

    async def serve(self, stop: asyncio.Event) -> None:
        """Standalone opt-in scheduler; HostRuntime owns plugin workers instead."""
        while not stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(self.idle_seconds, 5))
            except TimeoutError:
                pass

    async def recover(self, *, offline: bool = False, reconcile: bool = False) -> list:
        """After stopping old workers, optionally read original render results.

        Revocation does not erase results. This path never thinks, generates or
        sends, and does not acknowledge queued chat or make a new plan.
        """
        if type(reconcile) is not bool:
            raise StoreError("INVALID_ARGUMENT", "reconcile must be a boolean")
        async with self.lock:
            await self._db("recover_artist", offline=offline)
            results = []
            if reconcile:
                for work in await self._db("list_artist_works"):
                    if work["pending_request"]:
                        operation = await self._db("get_operation", work["pending_request"])
                        if operation["state"] in {"submitting", "accepted", "unknown", "succeeded", "failed"}:
                            results.append(await self._poll(work))
            return results

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
        context = {
            "persona": self.persona,
            "sharing": await self._db("sharing_policy"),
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
        single_work = await self._db("single_work_status")
        if single_work["enabled"]:
            context["single_work"] = single_work
        catalog = await self._db("creative_catalog")
        profiles, series = catalog["profiles"], catalog["series"]
        if profiles:
            context["profiles"] = profiles
        if series:
            context["series"] = series
        assets = await self._db("list_assets", query=" ".join(e.get("text", "") for e in events)[-500:])
        if not assets and events:
            # Ordinary chat is not a search expression. A full-sentence miss
            # must not hide the bounded catalogue, especially for image-only events.
            assets = await self._db("list_assets")
        details = await self._db("selected_assets")
        if assets:
            context["assets"] = assets
        if details:
            context["asset_details"] = details
        return context

    async def _think(self, task: str, context: dict, cycle: dict, *, work_id=None, version=None, images=None):
        operations = await self._db("audit_operations")
        if any(
            op["action"] == "think" and op["state"] in {"submitting", "accepted", "unknown"}
            for op in operations
        ):
            # A new chat or timer changes the wake watermark, not the certainty
            # of a previous model call. Do not evade its hold with another key.
            raise StoreError("THINK_UNKNOWN", "An earlier model call remains unresolved")
        contract = context.get("review_contract", 1) if task == "review" else None
        input_digest = (
            hashlib.sha256(canonical_json(context).encode("utf-8")).hexdigest()
            if contract == 2 else None
        )
        cached_review = next(
            (
                op
                for op in reversed(operations)
                if task == "review"
                and op["action"] == "think"
                and op["work_id"] == work_id
                and op["params"].get("task") == "review"
                and op["params"].get("version_id") == version["version_id"]
                and op["params"].get("review_contract", 1) == contract
                and (contract != 2 or op["params"].get("input_digest") == input_digest)
                and op["state"] == "succeeded"
            ),
            None,
        )
        if cached_review is not None:
            if (
                cached_review["auth_version"] != cycle["auth_version"]
                or await self._epoch() != cycle["auth_version"]
            ):
                raise StoreError("AUTH_CHANGED", "Stored review belongs to an older authorization")
            return json.loads(cached_review["evidence"]), cached_review["request_id"]
        identity = stable_id(
            task,
            self.space_id,
            "version" if task == "review" else str(cycle["through"]),
            work_id or "",
            (version or {}).get("version_id", ""),
        )
        if contract == 2:
            identity = stable_id("review-v2", self.space_id, work_id, version["version_id"], input_digest)
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
            params = {"task": task, "context": context, "version_id": (version or {}).get("version_id")}
            if contract == 2:
                params.update(review_contract=2, input_digest=input_digest)
            operation = await self._db(
                "reserve",
                request_id=request_id,
                action="think",
                work_id=work_id,
                params=params,
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
                media = (
                    {"images": images} if contract == 2
                    else {"image": version["data"], "mime": version["mime"]}
                )
                result = await asyncio.wait_for(
                    self.thinking.review(saved, **media),
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

            def outcome(result):
                nonlocal pause_after
                if result.get("state") == "blocked":
                    # Progress ticks must not turn a known transport/capability
                    # failure into a rapid sequence of new planning calls.
                    pause_after = max(pause_after, 300)
                return result

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
                            return outcome(polled)
                    if (
                        active["next_check_at"]
                        and datetime.fromisoformat(active["next_check_at"]) > self._now()
                    ):
                        return {"state": "waiting", "work_id": active["work_id"]}
                    if active["status"] in {"creating", "review_pending"}:
                        return outcome(await self._advance(active, context, cycle))
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
                                return outcome(polled)
                single_work = context.get("single_work", {})
                if single_work.get("enabled") and active is None and (
                    single_work.get("share_used") or any(w["status"] == "abandoned" for w in works)
                ):
                    return {"state": "single_work_stopped", "reason": "SINGLE_WORK_SPENT"}
                response, thought_request = await self._think("plan", context, cycle)
                await self._remember(response, cycle)
                result = await self._decide(response["decision"], cycle, thought_request=thought_request)
                pause_after = result.pop("wake_after", 0)
                return outcome(result)
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

    async def _decide(self, decision: dict, cycle: dict, *, thought_request=None) -> dict:
        action = decision["action"]
        if action in CATALOG_ACTIONS:
            if thought_request is None:
                raise StoreError("INVALID_DECISION", "Catalogue writes require a persisted planning operation")
            entry = await self._db("apply_catalog_decision", request_id=thought_request,
                                   expected_auth_version=cycle["auth_version"])
            return {"state": {"create_profile": "profile_created", "revise_profile": "profile_revised",
                              "create_series": "series_created"}[action], "entry": entry}
        if action == "close_series":
            await self._db("close_series", decision["series_id"], expected_auth_version=cycle["auth_version"])
            return {"state": "series_closed", "series_id": decision["series_id"]}
        if action == "inspect_assets":
            await self._db("select_assets", decision["asset_ids"], expected_auth_version=cycle["auth_version"])
            return {"state": "assets_selected"}
        if action == "wait":
            return {
                "state": "waiting",
                "reason": decision["reason"],
                "wake_after": decision.get("delay_seconds", self.idle_seconds),
            }
        if action == "create":
            if cycle["events"] and all(
                event["kind"] == "completion" and event["event_id"].startswith("progress:")
                for event in cycle["events"]
            ):
                # Completion advances this painting; new creative work still
                # needs the normal idle/chat wake and its existing budget.
                return {"state": "waiting", "reason": "PROGRESS_EXISTING_WORK_ONLY"}
            work_id = stable_id("work", self.space_id, str(cycle["through"]))
            work = await self._db(
                "plan_work",
                work_id=work_id,
                brief=decision["brief"],
                inspiration_keys=decision["inspiration_keys"],
                expected_auth_version=cycle["auth_version"],
                creative=decision.get("creative"),
                references=decision.get("references"),
                reference_usage="image_input" if self.reference_inputs else "text_context",
            )
            return {"state": work["status"], "work_id": work_id}
        work = await self._db("get_artist_work", decision["work_id"])
        if action == "share":
            return await self._share(work, cycle)
        if action == "archive" and work["status"] == "finished" and work["best_version"]:
            asset = await self._db(
                "archive_version", version_id=work["best_version"], roles=decision["roles"],
                tags=decision["tags"], reason=decision["reason"], expected_auth_version=cycle["auth_version"],
            )
            return {"state": "archived", "asset_id": asset["asset_id"]}
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
        params = {"prompt": reference_prompt(render_prompt(
            work["edit_prompt"] if parent else work["brief"]["prompt"], work["brief"].get("creative")
        ), work["brief"].get("references", []))}
        action = "edit" if parent else "submit"
        if parent:
            params.update(source_job_id=parent["job_id"], source_sha256=parent["sha256"])
        references = work["brief"].get("references", [])
        if any(ref["usage"] == "image_input" for ref in references):
            if not self.reference_inputs or not all(ref["usage"] == "image_input" for ref in references):
                return {"state": "blocked", "reason": "REFERENCE_INPUTS_DISABLED"}
            prepare = getattr(self.renderer, "prepare_references", None)
            if prepare is None:
                return {"state": "blocked", "reason": "REFERENCE_INPUTS_UNSUPPORTED"}
            try:
                inputs = await asyncio.wait_for(prepare(
                    scope=self.scope, references=references, expected_auth_version=cycle["auth_version"],
                    source_size=0 if parent is None else len(parent["data"]),
                ), self.timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return {"state": "blocked", "reason": getattr(exc, "code", "REFERENCE_INPUT_UNKNOWN")}
            params.update(transport_operation="render", reference_contract=1, reference_inputs=inputs)
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
            # 新适配器先交付回执；兼容原 Renderer 返回已经取得的图片字节。
            submit = self.renderer.submit_receipt if isinstance(self.renderer, ReceiptRenderer) else self.renderer.submit
            result = await asyncio.wait_for(
                submit(
                    scope=self.scope,
                    request_id=request_id,
                    prompt=params["prompt"],
                    source=source,
                ),
                self.timeout,
            )
        except BaseException as exc:
            reason = exc.code if isinstance(exc, (BackendError, StoreError)) else "SUBMIT_UNCONFIRMED"
            await self._db("mark_unknown", request_id, reason=reason)
            if isinstance(exc, asyncio.CancelledError):
                raise
            return {"state": "blocked", "reason": reason}
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
                status = self.renderer.status_receipt if isinstance(self.renderer, ReceiptRenderer) else self.renderer.status
                result = await asyncio.wait_for(
                    status(scope=self.scope, job_id=operation["job_id"]), self.timeout
                )
            else:
                lookup = self.renderer.lookup_receipt if isinstance(self.renderer, ReceiptRenderer) else self.renderer.lookup
                result = await asyncio.wait_for(
                    lookup(scope=self.scope, request_id=operation["request_id"]), self.timeout
                )
        except asyncio.CancelledError:
            raise
        except (BackendError, StoreError) as exc:
            return {"state": "blocked", "reason": exc.code}
        except Exception:
            return {"state": "blocked", "reason": "LOOKUP_UNAVAILABLE"}
        if result is None:
            return {"state": "blocked", "reason": "LOOKUP_NOT_FOUND"}
        return await self._receive(work, result)

    async def _receive(self, work: dict, result: RenderResult) -> dict:
        return await ReceiptService(db=self._db, change=self._change, renderer=self.renderer,
                              scope=self.scope, timeout=self.timeout).receive(work, result)

    async def _review(self, work: dict, context: dict, cycle: dict) -> dict:
        return await ReviewService(db=self._db, version=self._version, think=self._think,
                             poll=self._poll, epoch=self._epoch, change=self._change).review(work, context, cycle)

    async def _share(self, work: dict, cycle: dict) -> dict:
        return await ShareService(db=self._db, version=self._version, sender=self.sender, scope=self.scope,
                            timeout=self.send_timeout_seconds + self.share_preflight_seconds + 5).share(work, cycle)
