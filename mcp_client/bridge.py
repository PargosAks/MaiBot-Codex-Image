"""MCP client. Only trusted plugin code constructs signed contexts."""

from datetime import timedelta
from typing import Awaitable, Callable, Optional
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


# 保留既有错误类型和私有名称的导入兼容；内部新代码使用公开契约名称。
from ..domain.errors import BackendError as BridgeError
from .contracts import (
    valid_text as _text, valid_digest as _digest, reference_args as _reference_args,
    reference_digest as _reference_digest,
    validate_asset as _validate_asset, validate_job as _validate_job,
)
from .contracts import reference_params

_reference_params = reference_params  # 兼容旧导入；内部调用使用公开契约。


class _DispatchBlocked(BridgeError):
    """本地门禁拒绝，工具尚未派发。"""


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

    async def call(
        self, operation: str, scope: dict, args: dict, *,
        before_dispatch: Optional[Callable[[], bool]] = None,
    ):
        """可选同步门禁在握手完成后、派发工具前执行；返回 False 表示拒绝派发。"""
        result = await self._checked_exchange(operation, scope, args, before_dispatch=before_dispatch)
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

    async def _checked_exchange(
        self, operation: str, scope: dict, args: dict, *,
        before_dispatch: Optional[Callable[[], bool]] = None,
    ):
        try:
            if before_dispatch is None:
                result = await self._exchange(operation, scope, args)
            else:
                result = await self._exchange(operation, scope, args, before_dispatch=before_dispatch)
        except _DispatchBlocked:
            raise
        except Exception as exc:
            # Cancellation propagates. No transport retry: a submit might have
            # been accepted even when the response/session could not be read.
            raise BridgeError("TRANSPORT_UNKNOWN", kind="transport") from exc
        if result.isError:
            raise _response_error(result)
        return result

    async def edit(
        self,
        scope: dict,
        request_id: str,
        source_job_id: str,
        source_sha256: str,
        prompt: str,
        *,
        before_dispatch: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> dict:
        if not (
            _text(request_id) and _text(source_job_id) and _digest(source_sha256) and _text(prompt, 4000)
        ):
            raise BridgeError("INVALID_ARGUMENTS", kind="local")
        if "edit_v1" not in await self.capabilities(scope):
            raise BridgeError("EDIT_UNSUPPORTED", kind="capability")
        args = {
            "request_id": request_id,
            "source_job_id": source_job_id,
            "source_sha256": source_sha256,
            "prompt": prompt,
        }
        canonical = json.dumps(
            {"operation": "edit", **{k: v for k, v in args.items() if k != "request_id"}},
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if before_dispatch is not None:
            # Capability probing awaited the network. A trusted ledger owner
            # can recheck its current authority/budget before the edit write.
            await before_dispatch()
        result = await self.call("edit", scope, args)
        if (
            not isinstance(result, dict)
            or not {"operation", "request_id", "request_digest", "job"} <= result.keys()
        ):
            raise BridgeError("INVALID_RESPONSE", kind="protocol")
        if (
            result["operation"] != "edit"
            or result["request_id"] != request_id
            or result["request_digest"] != expected
        ):
            raise BridgeError("RECEIPT_MISMATCH", kind="binding")
        _validate_job(result["job"])
        return result

    async def asset_register(
        self, scope: dict, request_id: str, *, data: bytes, mime: str, expected_sha256: str,
        before_dispatch: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> dict:
        if not (
            _text(request_id) and _digest(expected_sha256) and type(data) is bytes
            and 0 < len(data) <= 8 * 1024 * 1024
            and type(mime) is str and mime in {"image/png", "image/jpeg", "image/webp"}
        ):
            raise BridgeError("INVALID_ARGUMENTS", kind="local")
        signatures = {
            "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/jpeg": data.startswith(b"\xff\xd8\xff"),
            "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
        }
        if hashlib.sha256(data).hexdigest() != expected_sha256 or not signatures[mime]:
            raise BridgeError("INVALID_ARGUMENTS", kind="local")
        if "reference_inputs_v1" not in await self.capabilities(scope):
            raise BridgeError("REFERENCE_INPUTS_UNSUPPORTED", kind="capability")
        args = dict(
            request_id=request_id, sha256=expected_sha256, mime=mime,
            image_base64=base64.b64encode(data).decode("ascii"),
        )
        if before_dispatch is not None:
            await before_dispatch()
        result = await self.call("asset_register", scope, args)
        _validate_asset(result)
        if (result["sha256"], result["mime"], result["size_bytes"]) != (expected_sha256, mime, len(data)):
            raise BridgeError("RECEIPT_MISMATCH", kind="binding")
        return result

    async def asset_lookup(
        self, scope: dict, request_id: str, *, expected_sha256: str, expected_mime: str, expected_size: int,
    ) -> dict:
        if not (
            _text(request_id) and _digest(expected_sha256) and type(expected_mime) is str
            and expected_mime in {"image/png", "image/jpeg", "image/webp"}
            and type(expected_size) is int and 0 < expected_size <= 8 * 1024 * 1024
        ):
            raise BridgeError("INVALID_ARGUMENTS", kind="local")
        # Recover an original registration independently of current drawing
        # capability. An unsupported/missing read tool is still an error, never
        # permission to register again.
        result = await self.call("asset_lookup", scope, {"request_id": request_id})
        if type(result) is not dict or type(result.get("found")) is not bool:
            raise BridgeError("INVALID_RESPONSE", kind="protocol")
        if result.keys() != ({"found", "asset"} if result["found"] else {"found"}):
            raise BridgeError("INVALID_RESPONSE", kind="protocol")
        if result["found"]:
            _validate_asset(result["asset"])
            asset = result["asset"]
            if (asset["sha256"], asset["mime"], asset["size_bytes"]) != (
                expected_sha256, expected_mime, expected_size,
            ):
                raise BridgeError("RECEIPT_MISMATCH", kind="binding")
        return result

    async def reference_render(
        self, scope: dict, request_id: str, *, mode: str, prompt: str, source: Optional[dict], references: list,
        before_dispatch: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> dict:
        if not _text(request_id):
            raise BridgeError("INVALID_ARGUMENTS", kind="local")
        args = _reference_args(mode, prompt, source, references)
        if "reference_inputs_v1" not in await self.capabilities(scope):
            raise BridgeError("REFERENCE_INPUTS_UNSUPPORTED", kind="capability")
        expected = _reference_digest(args)
        if before_dispatch is not None:
            await before_dispatch()
        result = await self.call("render", scope, {"request_id": request_id, **args})
        if type(result) is not dict or result.keys() != {"operation", "request_id", "request_digest", "job"}:
            raise BridgeError("INVALID_RESPONSE", kind="protocol")
        if (result["operation"], result["request_id"], result["request_digest"]) != ("render", request_id, expected):
            raise BridgeError("RECEIPT_MISMATCH", kind="binding")
        _validate_job(result["job"])
        return result

    async def image(
        self, scope: dict, job_id: str, *, expected_sha256: str, expected_mime: str, expected_size: int
    ) -> dict:
        # ArtistStore versions are limited to 8 MiB, independently of the legacy
        # command path's 20 MiB result limit. Do not alter existing command behavior.
        maximum = 8 * 1024 * 1024
        if (
            not _text(job_id)
            or not _digest(expected_sha256)
            or not isinstance(expected_mime, str)
            or expected_mime not in {"image/png", "image/jpeg", "image/webp"}
            or type(expected_size) is not int
            or not 0 < expected_size <= maximum
        ):
            raise BridgeError("INVALID_ARGUMENTS", kind="local")
        result = await self._checked_exchange("result", scope, {"job_id": job_id})
        images = [block for block in result.content if block.type == "image"]
        if (
            len(images) != 1
            or images[0].mimeType != expected_mime
            or len(images[0].data) > ((maximum + 2) // 3) * 4
        ):
            raise BridgeError("ARTIFACT_INVALID", kind="protocol")
        try:
            data = base64.b64decode(images[0].data, validate=True)
        except (TypeError, ValueError) as exc:
            raise BridgeError("ARTIFACT_INVALID", kind="protocol") from exc
        if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_sha256:
            raise BridgeError("ARTIFACT_INVALID", kind="protocol")
        return {"data": data, "mime": expected_mime, "sha256": expected_sha256}

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
            or expected_operation not in {"submit", "edit", "render"}
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

    async def _exchange(
        self, operation: str, scope: dict, args: dict, *,
        before_dispatch: Optional[Callable[[], bool]] = None,
    ):
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
                    envelope = {"envelope": sign(self.secret, scope, operation, args)}
                    # 与 call_tool 之间不让出事件循环；守卫只属于这次调用。
                    if before_dispatch is not None and before_dispatch() is not True:
                        raise _DispatchBlocked("DISPATCH_BLOCKED", kind="local")
                    result = await session.call_tool("codex_image_" + operation, envelope)
        return result
