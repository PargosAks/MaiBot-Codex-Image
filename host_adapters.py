"""Explicit Host capabilities; lifecycle, consent and model routing are trusted inputs.

The caller owns ctx/store/deliveries and closes them only after active calls stop.
In particular, reuse the plugin's Deliveries instance: opening another live writer
would invoke its startup recovery. Neither adapter registers a Host action.
"""

import asyncio
import base64
import hashlib
import json
import math

from .artist import stable_id
from .delivery import Deliveries
from .policy import Scope, StoreError, canonical_json, nonempty
from .recovery import ReadOnlyRecovery
from .store import ArtistStore


_IMAGE_LIMIT = 8 * 1024 * 1024
_JSON_LIMIT = 32768


def _image(data, mime):
    if not isinstance(data, bytes) or not 0 < len(data) <= _IMAGE_LIMIT:
        raise StoreError("ARTIFACT_INVALID", "Image must contain bounded original bytes")
    if mime not in {"image/png", "image/jpeg", "image/webp"}:
        raise StoreError("ARTIFACT_INVALID", "Unsupported image MIME")
    return base64.b64encode(data).decode("ascii")


def _json_response(text):
    def invalid(*args):
        raise ValueError("Invalid JSON value")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                invalid()
            result[key] = value
        return result

    try:
        if not isinstance(text, str) or len(text) > _JSON_LIMIT:
            invalid()
        # Bound nesting before parsing, respecting escaped quotes in JSON strings.
        depth, quoted, escaped = 0, False, False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                if depth > 16:
                    invalid()
            elif char in "]}":
                depth -= 1
        answer = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)
        if type(answer) is not dict:
            invalid()
        pending, count = [answer], 0
        while pending:
            value = pending.pop()
            count += 1
            if count > 2048 or (type(value) is float and not math.isfinite(value)):
                invalid()
            if isinstance(value, dict):
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        return answer
    except (ValueError, TypeError, RecursionError) as exc:
        raise StoreError("INVALID_DECISION", "Host model must return one bounded JSON object") from exc


class HostThinking:
    def __init__(self, ctx, *, plan_task: str, review_task: str, max_tokens: int = 2048):
        nonempty(plan_task, "plan_task")
        nonempty(review_task, "review_task")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 4096:
            raise StoreError("INVALID_ARGUMENT", "max_tokens must be between 1 and 4096")
        self.ctx, self.task_name, self.review_task_name = ctx, plan_task, review_task
        self.max_tokens = max_tokens

    def _messages(self, request):
        if not isinstance(request, dict) or request.get("task") not in {"plan", "inspire", "review"}:
            raise StoreError("INVALID_ARGUMENT", "Unexpected thinking task")
        required = {"task", "instructions", "context"}
        if request["task"] == "review":
            required |= {"image", "mime"}
        if set(request) != required:
            raise StoreError("INVALID_ARGUMENT", "Unexpected thinking request fields")
        instructions = nonempty(request["instructions"], "instructions")
        context = canonical_json(request["context"])
        if len(instructions) > 16384 or len(context) > 512 * 1024:
            raise StoreError("INVALID_CONTEXT", "Thinking context exceeds the adapter limit")
        content = [{"type": "text", "text": context}]
        if request["task"] == "review":
            encoded = _image(request["image"], request["mime"])
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{request['mime']};base64,{encoded}",
                    },
                }
            )
        return [
            {"role": "system", "content": instructions + "\n只输出一个JSON对象，不使用Markdown或工具调用。"},
            {"role": "user", "content": content},
        ]

    async def __call__(self, request: dict) -> dict:
        messages = await ReadOnlyRecovery._db(self._messages, request)
        task = self.review_task_name if request["task"] == "review" else self.task_name
        try:
            # LLMCapability rewrites a non-dict response as success=False. Keep
            # the raw receipt so a lost/malformed result stays uncertain.
            result = await self.ctx.call_host_method(
                "cap.call", payload={"capability": "llm.generate", "args": {
                    "prompt": messages, "task_name": task, "max_tokens": self.max_tokens,
                }},
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = (
                "THINKING_FAILED"
                if getattr(exc, "code", None) == "E_CAPABILITY_DENIED"
                else "THINKING_UNKNOWN"
            )
            raise StoreError(code, "Host model call failed; no automatic retry") from exc
        if not isinstance(result, dict) or type(result.get("success")) is not bool:
            raise StoreError("THINKING_UNKNOWN", "Host model returned an unrecognized receipt")
        if result.get("error") not in (None, ""):
            # Host catches provider exceptions into this unstructured envelope;
            # it cannot establish whether the remote model already executed.
            raise StoreError("THINKING_UNKNOWN", "Host model outcome cannot be established")
        if result["success"] is False:
            raise StoreError("THINKING_UNKNOWN", "Host failure does not establish whether the model executed")
        if result.get("tool_calls"):
            raise StoreError("INVALID_DECISION", "Tool calls are not accepted")
        return await ReadOnlyRecovery._db(_json_response, result.get("response"))


async def send_image_receipt(ctx, image_base64: str, stream_id: str) -> bool:
    """Send to a trusted caller's target and validate the uncoerced Host receipt.

    The caller owns persistent intent and failure classification. Capability
    denial, transport exceptions and cancellation propagate without rewriting.
    """
    # SDK send.image coerces sent with bool() and falls back to success. The
    # public raw capability API preserves malformed and contradictory receipts.
    result = await ctx.call_host_method(
        "cap.call",
        payload={
            "capability": "send.image",
            "args": {
                "image_base64": image_base64,
                "stream_id": stream_id,
                "return_details": True,
            },
        },
    )
    sent = result.get("sent") if isinstance(result, dict) else result
    if (
        sent is not True
        or (isinstance(result, dict) and result.get("error") not in (None, ""))
        or (
            isinstance(result, dict)
            and "success" in result
            and (type(result["success"]) is not bool or result["success"] is not sent)
        )
    ):
        raise StoreError("SEND_UNKNOWN", "Host did not confirm the send outcome")
    # Host send_service can swallow a transport exception and return None,
    # yielding sent=False without error. Only an explicit permission denial is
    # provably pre-dispatch; false receipts must retain the unknown-send lock.
    return True


class HostSender:
    simulation = False

    def __init__(self, ctx, deliveries: Deliveries, *, store: ArtistStore, space_id: str, scope: Scope):
        if not isinstance(scope, Scope):
            raise StoreError("INVALID_SCOPE", "A trusted Host scope is required")
        self.ctx, self.store, self.space_id, self.scope = ctx, store, space_id, scope
        self.deliveries = deliveries

    def _version(self, scope, version):
        if not isinstance(scope, Scope) or scope != self.scope:
            raise StoreError("INVALID_SCOPE", "Sender target cannot change")
        if not isinstance(version, dict) or not isinstance(version.get("version_id"), str):
            raise StoreError("INVALID_ARGUMENT", "A stored version is required")
        stored = self.store.get_version(self.space_id, scope, version["version_id"])
        for key in ("work_id", "request_id", "job_id", "data", "mime", "sha256"):
            if version.get(key) != stored[key]:
                raise StoreError("REQUEST_CONFLICT", "Send image differs from the persisted version")
        if hashlib.sha256(stored["data"]).hexdigest() != stored["sha256"]:
            raise StoreError("ARTIFACT_INVALID", "Stored image digest mismatch")
        request_id = stable_id("share", stored["work_id"], stored["version_id"])
        operation = self.store.validate_submitting(self.space_id, scope, request_id)
        if (
            operation["action"] != "share"
            or operation["work_id"] != stored["work_id"]
            or operation["params"] != {"job_id": stored["job_id"], "version_id": stored["version_id"]}
        ):
            raise StoreError("REQUEST_CONFLICT", "Share operation does not match the stored version")
        if self.store.legacy_lock(self.space_id, scope, stored["job_id"]):
            raise StoreError("LEGACY_LOCK", "Historical delivery blocks sharing this job")
        return stored, _image(stored["data"], stored["mime"])

    def _prepare(self, scope, version, claim):
        stored, encoded = self._version(scope, version)
        if not self.deliveries.begin(stored["job_id"]):
            with self.deliveries.lock:
                old = self.deliveries.db.execute(
                    "SELECT state FROM delivery WHERE job=?", (stored["job_id"],)
                ).fetchone()
            if old is not None and old[0] in {"sent", "send_failed"}:
                return old[0] == "sent"
            raise StoreError("SEND_UNKNOWN", "Original send is in progress or its outcome is unknown")
        claim["job_id"] = stored["job_id"]
        return encoded

    def _finish(self, claim, state):
        if claim["job_id"] is not None:
            # Never change another call's lock or overwrite a known result during
            # cancellation cleanup; ownership is acquired only by begin=True.
            with self.deliveries.lock, self.deliveries.db:
                self.deliveries.db.execute(
                    "UPDATE delivery SET state=? WHERE job=? AND state='sending'",
                    (state, claim["job_id"]),
                )

    async def send(self, *, scope: Scope, version: dict) -> bool:
        claim = {"job_id": None}
        try:
            encoded = await ReadOnlyRecovery._db(self._prepare, scope, version, claim)
            if type(encoded) is bool:
                return encoded
            # Acquiring a delivery intent may wait behind SQLite. Recheck the
            # latest delegation/budget before calling the Host capability.
            await ReadOnlyRecovery._db(self._version, scope, version)
            sent = await send_image_receipt(self.ctx, encoded, self.scope.stream_id)
            await ReadOnlyRecovery._db(self._finish, claim, "sent" if sent else "send_failed")
            return sent
        except asyncio.CancelledError:
            await ReadOnlyRecovery._db(self._finish, claim, "send_unknown")
            raise
        except Exception as exc:
            denied = getattr(exc, "code", None) == "E_CAPABILITY_DENIED"
            await ReadOnlyRecovery._db(self._finish, claim, "send_failed" if denied else "send_unknown")
            if denied:
                return False
            raise
