"""MCP client. Only trusted plugin code constructs signed contexts."""

from datetime import timedelta
import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlparse

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def sign(secret: str, scope: dict, operation: str, args: dict) -> str:
    data = {"scope": scope, "op": operation, "args": args, "iat": int(time.time())}
    payload = base64.urlsafe_b64encode(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    )
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
                async with ClientSession(
                    read, write, read_timeout_seconds=timedelta(seconds=30)
                ) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "codex_image_" + operation,
                        {"envelope": sign(self.secret, scope, operation, args)},
                    )
        if result.isError:
            raise ValueError("绘图服务拒绝请求或暂不可用，请查询任务状态后再操作。")
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
                return json.loads(block.text)
        raise ValueError("服务没有返回有效状态")
