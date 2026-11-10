"""MCP 请求与回执的纯校验契约；无网络、数据库或隐式重试。"""

from copy import deepcopy
from typing import Optional
import hashlib
import json
import re

from ..domain.policy import StoreError
from ..domain.errors import BackendError as BridgeError


def valid_text(value, limit=200):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def valid_digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def reference_args(mode: str, prompt: str, source: Optional[dict], references: list) -> dict:
    valid = (
        type(mode) is str and mode in {"create", "edit"} and valid_text(prompt, 4000)
        and type(references) is list and 1 <= len(references) <= 3
    )
    if valid and mode == "create":
        valid = source is None
    elif valid:
        valid = (
            type(source) is dict and source.keys() == {"job_id", "sha256"}
            and valid_text(source["job_id"]) and valid_digest(source["sha256"])
        )
    if valid:
        valid = all(
            type(item) is dict and item.keys() == {"asset_id", "sha256", "role"}
            and valid_text(item["asset_id"]) and valid_digest(item["sha256"])
            and type(item["role"]) is str and item["role"] in {"character", "style", "composition", "palette"}
            for item in references
        )
    if not valid:
        raise BridgeError("INVALID_ARGUMENTS", kind="local")
    return deepcopy(dict(mode=mode, prompt=prompt, source=source, references=references))


def reference_params(action: str, params: dict) -> Optional[dict]:
    """Translate only the explicit frozen reference transport, never infer a fallback."""
    if type(params) is not dict:
        raise BridgeError("INVALID_ARGUMENTS", kind="local")
    if not {"transport_operation", "reference_inputs", "reference_contract"}.intersection(params):
        return None
    expected = {"prompt", "reference_inputs", "transport_operation", "reference_contract"}
    if action == "edit":
        expected |= {"source_job_id", "source_sha256"}
    if action not in {"submit", "edit"} or params.keys() != expected or params["transport_operation"] != "render":
        raise BridgeError("INVALID_ARGUMENTS", kind="local")
    if type(params["reference_contract"]) is not int or params["reference_contract"] != 1:
        raise BridgeError("INVALID_ARGUMENTS", kind="local")
    source = None if action == "submit" else {
        "job_id": params["source_job_id"], "sha256": params["source_sha256"],
    }
    return reference_args(
        "create" if action == "submit" else "edit", params["prompt"], source, params["reference_inputs"],
    )


def reference_digest(args: dict) -> str:
    canonical = json.dumps({"operation": "render", **args}, ensure_ascii=True, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_asset(asset: dict) -> None:
    if not (
        type(asset) is dict and asset.keys() == {"asset_id", "sha256", "mime", "size_bytes"}
        and valid_text(asset["asset_id"]) and valid_digest(asset["sha256"])
        and type(asset["mime"]) is str and asset["mime"] in {"image/png", "image/jpeg", "image/webp"}
        and type(asset["size_bytes"]) is int and 0 < asset["size_bytes"] <= 8 * 1024 * 1024
    ):
        raise BridgeError("INVALID_RESPONSE", kind="protocol")


def validate_job(job):
    valid = (
        isinstance(job, dict)
        and {"job_id", "status", "turn_status", "error_code", "artifacts"} <= job.keys()
        and valid_text(job["job_id"])
        and isinstance(job["status"], str)
        and job["status"]
        in {"queued", "starting", "running", "artifact_ready", "completed", "failed", "outcome_unknown"}
        and (job["turn_status"] is None or valid_text(job["turn_status"]))
        # Nullable legacy turns are supported; an explicit non-completed turn
        # contradicts completed and must not release an unresolved local hold.
        and (job["status"] != "completed" or job["turn_status"] in {None, "completed"})
        and (job["error_code"] is None or valid_text(job["error_code"]))
        and isinstance(job["artifacts"], list)
        and len(job["artifacts"]) <= 1
    )
    if valid:
        for artifact in job["artifacts"]:
            valid = (
                isinstance(artifact, dict)
                and {"asset_id", "sha256", "size_bytes", "mime_type"} <= artifact.keys()
                and valid_text(artifact["asset_id"])
                and valid_digest(artifact["sha256"])
                and type(artifact["size_bytes"]) is int
                and artifact["size_bytes"] > 0
                and isinstance(artifact["mime_type"], str)
                and artifact["mime_type"] in {"image/png", "image/jpeg", "image/webp"}
            )
            if not valid:
                break
    if not valid:
        raise BridgeError("INVALID_RESPONSE", kind="protocol")




# 产物就绪与任务终态分别判断；有图不能证明整个绘图调用成功。
def job_outcome(job):
    if job["status"] == "completed":
        if job["turn_status"] not in {None, "completed"}:
            raise StoreError("PROTOCOL_ERROR", "Completed job contradicts its turn status")
        return "succeeded"
    if job["status"] == "failed":
        return "failed"
    if job["status"] == "outcome_unknown":
        return "unknown"
    if job["status"] == "artifact_ready":
        if job["turn_status"] == "completed":
            return "succeeded"
        if job["turn_status"] in {"failed", "interrupted", "cancelled"}:
            return "failed"
        if job["turn_status"] not in {"queued", "starting", "running", "inProgress", "in_progress"}:
            return "unknown"
    if job["status"] not in {"queued", "starting", "running", "artifact_ready"}:
        raise StoreError("PROTOCOL_ERROR", "Unexpected lookup job state")
    return "accepted"
