"""MCP client. Only trusted plugin code constructs signed contexts."""

from datetime import timedelta
import base64
import hashlib
import hmac
import json
import re
import time
from urllib.parse import urlparse

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class BridgeError(ValueError):
    """Machine-readable failure, never permission to retry or refund a request.

    Remote text is deliberately not copied into the user-facing message. Even
    an explicit rejection describes this call, not the fate of an earlier call.
    """

    def __init__(
        self, code: str, *, kind: str, message: str = "绘图服务拒绝请求或响应无法确认，请核对原任务。"
    ):
        self.code, self.kind = code, kind
        super().__init__(f"{message} [{code}]")


def _response_error(result) -> BridgeError:
    data = result.structuredContent
    if data is None:
        for block in result.content:
            if block.type == "text":
                try:
                    data = json.loads(block.text)
                except (TypeError, ValueError):
                    continue
                break
    error = data.get("error") if isinstance(data, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if not isinstance(code, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) is None:
        code = "REMOTE_ERROR_UNKNOWN"
    return BridgeError(code, kind="remote")


def _text(value, limit=200):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_job(job):
    valid = (
        isinstance(job, dict)
        and {"job_id", "status", "turn_status", "error_code", "artifacts"} <= job.keys()
        and _text(job["job_id"])
        and isinstance(job["status"], str)
        and job["status"]
        in {"queued", "starting", "running", "artifact_ready", "completed", "failed", "outcome_unknown"}
        and (job["turn_status"] is None or _text(job["turn_status"]))
        # Nullable legacy turns are supported; an explicit non-completed turn
        # contradicts completed and must not release an unresolved local hold.
        and (job["status"] != "completed" or job["turn_status"] in {None, "completed"})
        and (job["error_code"] is None or _text(job["error_code"]))
        and isinstance(job["artifacts"], list)
        and len(job["artifacts"]) <= 1
    )
    if valid:
        for artifact in job["artifacts"]:
            valid = (
                isinstance(artifact, dict)
                and {"asset_id", "sha256", "size_bytes", "mime_type"} <= artifact.keys()
                and _text(artifact["asset_id"])
                and _digest(artifact["sha256"])
                and type(artifact["size_bytes"]) is int
                and artifact["size_bytes"] > 0
                and isinstance(artifact["mime_type"], str)
                and artifact["mime_type"] in {"image/png", "image/jpeg", "image/webp"}
            )
            if not valid:
                break
    if not valid:
        raise BridgeError("INVALID_RESPONSE", kind="protocol")


def sign(secret: str, scope: dict, operation: str, args: dict) -> str:
    data = {"scope": scope, "op": operation, "args": args, "iat": int(time.time())}
    payload = base64.urlsafe_b64encode(json.dumps(data, sort_keys=True, separators=(",", ":")).encode())
    payload = payload.decode().rstrip("=")
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + signature


class Bridge:
    def __init__(self, url: str, secret: str):
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("请填写可访问的宿主机 MCP 地址（http 或 https）。")
        if parsed.query or parsed.fragment or len(secret) < 32:
            raise ValueError("请检查 MCP 地址，并填写与宿主机服务一致的共享密钥（至少 32 字符）。")
        self.url, self.secret = url, secret

    async def call(self, operation: str, scope: dict, args: dict):
        try:
            result = await self._exchange(operation, scope, args)
        except Exception as exc:
            # Cancellation propagates. No transport retry: a submit might have
            # been accepted even when the response/session could not be read.
            raise BridgeError("TRANSPORT_UNKNOWN", kind="transport") from exc
        if result.isError:
            raise _response_error(result)
        if operation == "result":
            for block in result.content:
                if block.type == "image":
                    data = base64.b64decode(block.data, validate=True)
                    if not data or len(data) > 20 * 1024 * 1024:
                        raise ValueError("图片大小不符合限制")
                    return block.data
            raise ValueError("服务没有返回图片")
        if result.structuredContent is not None:
            return result.structuredContent
        for block in result.content:
            if block.type == "text":
                try:
                    return json.loads(block.text)
                except (TypeError, ValueError) as exc:
                    raise BridgeError("INVALID_RESPONSE", kind="protocol") from exc
        raise BridgeError("INVALID_RESPONSE", kind="protocol", message="服务没有返回有效状态")

    async def capabilities(self, scope: dict) -> frozenset[str]:
        health = await self.call("health", scope, {})
        if not isinstance(health, dict):
            raise BridgeError("INVALID_RESPONSE", kind="protocol")
        capabilities = health.get("capabilities", [])
        if (
            not isinstance(capabilities, list)
            or len(capabilities) > 32
            or not all(_text(item, 64) for item in capabilities)
        ):
            raise BridgeError("INVALID_RESPONSE", kind="protocol")
        # No optimistic cache: a service switch/restart may restore an older
        # implementation. Missing capability means unsupported, never guessing.
        return frozenset(capabilities)

    async def lookup(
        self,
        scope: dict,
        request_id: str,
        *,
        expected_operation: str,
        expected_digest: str,
        expected_job_id: str | None = None,
    ) -> dict:
        if (
            not _text(request_id)
            or not isinstance(expected_operation, str)
            or expected_operation not in {"submit", "edit"}
            or not _digest(expected_digest)
            or (expected_job_id is not None and not _text(expected_job_id))
        ):
            raise BridgeError("INVALID_ARGUMENTS", kind="local")
        if "lookup_v1" not in await self.capabilities(scope):
            raise BridgeError("LOOKUP_UNSUPPORTED", kind="capability")
        result = await self.call("lookup", scope, {"request_id": request_id})
        if not isinstance(result, dict) or type(result.get("found")) is not bool:
            raise BridgeError("INVALID_RESPONSE", kind="protocol")
        if result["found"] is False:
            if set(result) != {"found"}:
                raise BridgeError("INVALID_RESPONSE", kind="protocol")
            return result
        if not {"operation", "request_id", "request_digest", "job"} <= result.keys():
            raise BridgeError("INVALID_RESPONSE", kind="protocol")
        if (
            result["operation"] != expected_operation
            or result["request_id"] != request_id
            or result["request_digest"] != expected_digest
        ):
            raise BridgeError("RECEIPT_MISMATCH", kind="binding")
        _validate_job(result["job"])
        if expected_job_id is not None and result["job"]["job_id"] != expected_job_id:
            raise BridgeError("RECEIPT_MISMATCH", kind="binding")
        return result

    async def _exchange(self, operation: str, scope: dict, args: dict):
        # Fresh short-lived MCP sessions avoid holding task-group contexts across plugin callbacks.
        async with httpx.AsyncClient(
            headers={"Authorization": "Bearer " + self.secret},
            timeout=30,
            follow_redirects=False,
            trust_env=False,
        ) as http:
            async with streamable_http_client(self.url, http_client=http) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "codex_image_" + operation,
                        {"envelope": sign(self.secret, scope, operation, args)},
                    )
        return result
