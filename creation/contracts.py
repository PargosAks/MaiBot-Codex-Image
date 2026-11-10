"""创作侧的公开类型和稳定标识；不依赖 Host 或 MCP 传输实现。"""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable
import hashlib

from ..domain.policy import Scope, StoreError, canonical_json


AsyncCall = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class ArtifactRef:
    """服务确认的不可变产物描述；字节获取失败不改变任务回执。"""

    sha256: str
    mime: str
    size_bytes: int


@dataclass(frozen=True)
class RenderResult:
    request_id: str
    request_digest: str
    job_id: str
    state: str
    image: Optional[bytes] = None
    mime: str = "image/png"
    sha256: Optional[str] = None
    error_code: Optional[str] = None
    artifact: Optional[ArtifactRef] = None


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
    if params.get("transport_operation") == "render":
        if type(params.get("reference_contract")) is not int or params["reference_contract"] != 1:
            raise StoreError("REQUEST_CONFLICT", "Unknown frozen reference contract")
        payload = canonical_json({
            "operation": "render", "mode": "create" if action == "submit" else "edit",
            "prompt": params["prompt"],
            "source": None if action == "submit" else {
                "job_id": params["source_job_id"], "sha256": params["source_sha256"],
            },
            "references": params["reference_inputs"],
        })
    elif action == "submit":
        payload = params["prompt"]
    else:
        payload = canonical_json({"operation": "edit", **params})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_id(kind: str, *parts: str) -> str:
    return "artist:" + kind + ":" + hashlib.sha256(canonical_json(parts).encode()).hexdigest()


@runtime_checkable
class ReceiptRenderer(Protocol):
    """先取得回执，再下载产物。旧 Renderer 的带图回执接口继续可用。"""

    async def submit_receipt(self, *, scope: Scope, request_id: str, prompt: str, source: Optional[dict]) -> RenderResult: ...

    async def lookup_receipt(self, *, scope: Scope, request_id: str) -> Optional[RenderResult]: ...

    async def status_receipt(self, *, scope: Scope, job_id: str) -> RenderResult: ...

    async def fetch_artifact(self, *, scope: Scope, receipt: RenderResult) -> RenderResult: ...
