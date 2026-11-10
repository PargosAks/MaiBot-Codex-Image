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

from ..creation.contracts import stable_id
from ..domain.async_work import join_worker
from ..creation.thinking import _context, _review_images
from ..domain.delivery import Deliveries
from ..domain.policy import Scope, StoreError, canonical_json, nonempty
from ..domain.store import ArtistStore
from .delivery_runtime import DeliveryBlocked, capability_denied, dispatch_delivery

_IMAGE_LIMIT = 8 * 1024 * 1024
_JSON_LIMIT = 32768
SHARE_TARGET_TIMEOUT_SECONDS = 30.0


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
    def __init__(self, ctx, *, plan_task: str, review_task: str,
                 timeout_seconds: float = 30, max_tokens: int | None = None):
        nonempty(plan_task, "plan_task")
        nonempty(review_task, "review_task")
        if max_tokens is not None and (type(max_tokens) is not int or not 1 <= max_tokens <= 4096):
            raise StoreError("INVALID_ARGUMENT", "max_tokens must be between 1 and 4096")
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300):
            raise StoreError("INVALID_ARGUMENT", "timeout_seconds must be positive and at most 300")
        self.ctx, self.task_name, self.review_task_name = ctx, plan_task, review_task
        self.max_tokens = max_tokens
        self.timeout_ms = math.ceil(timeout_seconds * 1000)

    def _messages(self, request):
        if (type(request) is not dict or type(request.get("task")) is not str
                or request["task"] not in {"plan", "inspire", "review"}):
            raise StoreError("INVALID_ARGUMENT", "Unexpected thinking task")
        required = {"task", "instructions", "context"}
        if not required <= request.keys():
            raise StoreError("INVALID_ARGUMENT", "Missing thinking request fields")
        frozen = _context(request["context"])
        v2 = request["task"] == "review" and frozen.get("review_contract") == 2
        if request["task"] == "review":
            required |= {"images"} if v2 else {"image", "mime"}
        if set(request) != required:
            raise StoreError("INVALID_ARGUMENT", "Unexpected thinking request fields")
        instructions = nonempty(request["instructions"], "instructions")
        context = canonical_json(frozen)
        if len(instructions) > 16384 or len(context) > 512 * 1024:
            raise StoreError("INVALID_CONTEXT", "Thinking context exceeds the adapter limit")
        content = [{"type": "text", "text": context}]
        if v2:
            for image in _review_images(frozen, request["images"]):
                # Keep each version/role label adjacent to its own image. Never
                # reconstruct the order from roles, hashes or historical rows.
                label = canonical_json({"roles": image["roles"], "version_id": image["version_id"]})
                content.append({"type": "text", "text": label})
                encoded = _image(image["data"], image["mime"])
                content.append({"type": "image_url", "image_url": {
                    "url": f"data:{image['mime']};base64,{encoded}",
                }})
        elif request["task"] == "review":
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
        messages = await join_worker(self._messages, request)
        task = self.review_task_name if request["task"] == "review" else self.task_name
        args = {"prompt": messages, "task_name": task}
        # Omit the override so Host uses this task's configured output limit.
        if self.max_tokens is not None:
            args["max_tokens"] = self.max_tokens
        try:
            # LLMCapability rewrites a non-dict response as success=False. Keep
            # the raw receipt so a lost/malformed result stays uncertain.
            result = await self.ctx.call_host_method(
                "cap.call", payload={"capability": "llm.generate", "args": args},
                # Raw cap.call otherwise falls back to the Runner's 30s timer,
                # independently of Artist's configured outer timeout.
                timeout_ms=self.timeout_ms,
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
        return await join_worker(_json_response, result.get("response"))


async def send_image_receipt(
    ctx, image_base64: str, stream_id: str, *, timeout_seconds: float = 240.0
) -> bool:
    """Send to a trusted caller's target and validate the uncoerced Host receipt.

    The caller owns persistent intent and failure classification. Capability
    denial, transport exceptions and cancellation propagate without rewriting.
    """
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= 600 or not math.isfinite(timeout_seconds)):
        raise StoreError("INVALID_ARGUMENT", "timeout_seconds must be finite, positive and at most 600")
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
        # Image uploads can outlast the Runner's default 30-second RPC wait.
        timeout_ms=math.ceil(timeout_seconds * 1000),
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


class _SharePreflightRejected(Exception):
    """本次自主分享已取得意图，但群或委托复核未通过，尚未派发。"""


class HostSender:
    simulation = False

    def __init__(self, ctx, deliveries: Deliveries, *, store: ArtistStore, space_id: str, scope: Scope,
                 timeout_seconds: float = 240.0, group_id: str = ""):
        if not isinstance(scope, Scope):
            raise StoreError("INVALID_SCOPE", "A trusted Host scope is required")
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not 0 < timeout_seconds <= 600 or not math.isfinite(timeout_seconds)):
            raise StoreError("INVALID_ARGUMENT", "timeout_seconds must be finite, positive and at most 600")
        if not isinstance(group_id, str) or len(group_id) > 200 or group_id != group_id.strip():
            raise StoreError("INVALID_ARGUMENT", "A trusted bounded group identifier is required")
        self.ctx, self.store, self.space_id, self.scope = ctx, store, space_id, scope
        self.deliveries = deliveries
        self.timeout_seconds = timeout_seconds
        self.group_id = group_id

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
        self.store.check_share_target(self.space_id, scope, group_id=self.group_id)
        if (
            operation["action"] != "share"
            or operation["work_id"] != stored["work_id"]
            or operation["params"] != {"job_id": stored["job_id"], "version_id": stored["version_id"]}
        ):
            raise StoreError("REQUEST_CONFLICT", "Share operation does not match the stored version")
        if self.store.legacy_lock(self.space_id, scope, stored["job_id"]):
            raise StoreError("LEGACY_LOCK", "Historical delivery blocks sharing this job")
        return stored, _image(stored["data"], stored["mime"])

    async def _verify_group(self):
        if not self.group_id:
            return
        response = await self.ctx.call_host_method(
            "cap.call", payload={"capability": "chat.get_stream_by_group_id",
                                  "args": {"group_id": self.group_id, "platform": self.scope.platform}},
            timeout_ms=int(SHARE_TARGET_TIMEOUT_SECONDS * 1000),
        )
        stream = response.get("stream") if isinstance(response, dict) and response.get("success") is True else None
        if not isinstance(stream, dict) or (
            stream.get("platform") != self.scope.platform or stream.get("group_id") != self.group_id
            or stream.get("stream_id") != self.scope.stream_id
            or stream.get("session_id", self.scope.stream_id) != self.scope.stream_id
            or stream.get("is_group_session") is not True
        ):
            raise StoreError("SHARE_TARGET_MISMATCH", "Original group no longer matches the trusted Host stream")

    async def send(self, *, scope: Scope, version: dict) -> bool:
        try:
            stored, encoded = await join_worker(self._version, scope, version)
        except Exception as exc:
            if capability_denied(exc) or (isinstance(exc, StoreError) and exc.code in {
                "SHARE_NOT_ALLOWED", "SHARE_POLICY_CHANGED", "SHARE_TARGET_MISMATCH",
            }):
                return False
            raise

        async def recheck() -> None:
            try:
                await self._verify_group()
                # SQLite 争用期间，委托或预算可能变化；派发前再次核对。
                await join_worker(self._version, scope, version)
            except Exception as exc:
                # 群定位与账本复核都是只读的；此时尚未调用 send.image。
                raise _SharePreflightRejected() from exc

        async def dispatch() -> bool:
            return await send_image_receipt(
                self.ctx, encoded, self.scope.stream_id, timeout_seconds=self.timeout_seconds
            )

        job = stored["job_id"]
        try:
            return await dispatch_delivery(self.deliveries, job, before_dispatch=recheck, dispatch=dispatch)
        except DeliveryBlocked:
            old = await join_worker(self.deliveries.state, job)
            if old in {"sent", "send_failed"}:
                return old == "sent"
            raise
        except _SharePreflightRejected:
            return False
        except Exception as exc:
            if capability_denied(exc):
                return False
            raise
