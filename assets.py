"""Reference metadata and prompt validation; no network or image analysis."""

from copy import deepcopy
from typing import Any, List
import hashlib
import re

from .policy import StoreError


ASSET_ROLES = frozenset({"character", "style", "composition", "palette"})
REFERENCE_FIELDS = {"asset_id", "role", "reason"}
FROZEN_REFERENCE_FIELDS = REFERENCE_FIELDS | {
    "summary", "source_kind", "source_version_id", "sha256", "mime", "usage",
}
MAX_ASSETS = 100
MAX_ASSET_BYTES = 64 * 1024 * 1024


def validate_asset_image(data: Any, mime: Any) -> dict:
    """Reuse the Host decoder to verify original bytes without interpreting them."""
    from .host_media import MAX_IMAGE_BYTES, _validate_image

    _require(type(data) is bytes and 0 < len(data) <= MAX_IMAGE_BYTES, "Image bytes exceed the 8 MiB limit")
    _require(type(mime) is str and mime in {"image/png", "image/jpeg", "image/webp"}, "Invalid image MIME")
    if _validate_image(data) != mime:
        raise StoreError("ARTIFACT_INVALID", "Image MIME does not match the original format")
    return {"data": data, "mime": mime, "sha256": hashlib.sha256(data).hexdigest()}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise StoreError("INVALID_ARGUMENT", message)


def _text(value: Any, limit: int) -> None:
    _require(type(value) is str and bool(value.strip()) and len(value) <= limit,
             "Asset text must be nonempty and bounded")
    _require(not any((ord(char) < 32 and char not in "\n\t") or ord(char) == 127 for char in value),
             "Asset text cannot contain control characters")


def validate_archive(roles: Any, tags: Any, reason: Any) -> None:
    _require(type(roles) is list and 1 <= len(roles) <= 4, "Archive requires one to four asset roles")
    _require(all(type(role) is str and role in ASSET_ROLES for role in roles), "Unknown asset role")
    _require(len(set(roles)) == len(roles), "Asset roles must be unique")
    _require(type(tags) is list and len(tags) <= 8, "At most eight asset tags are allowed")
    for tag in tags:
        _text(tag, 80)
    _require(len(set(tags)) == len(tags), "Asset tags must be unique")
    _text(reason, 500)


def validate_references(references: Any, available: Any) -> List[dict]:
    """Validate choices against metadata; duplicate summary/detail rows are normal.

    Duplicated identities never expand the permitted roles: a role must be
    allowed by every supplied row for that identity. Source lookup and active
    status belong to ArtistStore, not this model-facing function.
    """
    _require(type(references) is list and len(references) <= 3, "At most three text references are allowed")
    _require(type(available) is list, "Available assets must be a list")
    roles_by_id = {}
    for item in available:
        _require(type(item) is dict, "Available asset must be metadata")
        _text(item.get("asset_id"), 128)
        roles = item.get("roles")
        _require(type(roles) is list and all(type(role) is str and role in ASSET_ROLES for role in roles),
                 "Available asset roles are invalid")
        identifier = item["asset_id"]
        if identifier not in roles_by_id:
            roles_by_id[identifier] = set(roles)
        else:
            roles_by_id[identifier].intersection_update(roles)
    seen = set()
    for reference in references:
        _require(type(reference) is dict and reference.keys() == REFERENCE_FIELDS,
                 "Reference choices contain only asset_id, role and reason")
        _text(reference["asset_id"], 128)
        _text(reference["reason"], 500)
        role = reference["role"]
        _require(type(role) is str and role in ASSET_ROLES, "Unknown reference role")
        _require(reference["asset_id"] in roles_by_id and role in roles_by_id[reference["asset_id"]],
                 "Reference role is not available for this asset")
        identity = (reference["asset_id"], role)
        _require(identity not in seen, "Duplicate asset role reference")
        seen.add(identity)
    return deepcopy(references)


def reference_prompt(prompt: str, frozen_refs: Any) -> str:
    """Add bounded, frozen text inspiration without claiming image input."""
    _require(type(prompt) is str, "Render prompt must be text")
    _require(type(frozen_refs) is list and len(frozen_refs) <= 3, "At most three frozen references are allowed")
    choices, available, lines = [], [], [prompt]
    for reference in frozen_refs:
        _require(type(reference) is dict and reference.keys() in (
            FROZEN_REFERENCE_FIELDS, FROZEN_REFERENCE_FIELDS | {"policy_version", "consent_ref"}),
                 "A complete frozen text reference is required")
        _require(reference["source_kind"] in ("reviewed_work", "authorized_chat")
                 and reference["usage"] in ("text_context", "image_input"), "Unsupported reference source or usage")
        if reference["source_kind"] == "reviewed_work":
            _text(reference["source_version_id"], 256)
        else:
            _require(reference["source_version_id"] is None, "Chat images cannot pretend to be work versions")
        if reference["usage"] == "image_input":
            _require(type(reference.get("policy_version")) is int and reference["policy_version"] > 0,
                     "Image input requires a frozen policy version")
            _text(reference.get("consent_ref"), 500)
        else:
            _require(reference.keys() == FROZEN_REFERENCE_FIELDS, "Text references retain the original contract")
        _text(reference["summary"], 1000)
        _require(type(reference["sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", reference["sha256"]) is not None,
                 "Frozen reference requires a SHA256 digest")
        _require(type(reference["mime"]) is str and reference["mime"] in {"image/png", "image/jpeg", "image/webp"},
                 "Invalid reference MIME")
        _require(type(reference["role"]) is str and reference["role"] in ASSET_ROLES, "Invalid frozen reference role")
        choices.append({key: reference[key] for key in REFERENCE_FIELDS})
        available.append({"asset_id": reference["asset_id"], "roles": [reference["role"]]})
    # Roles for repeated assets may differ between references; validate each
    # reference against the union of that asset's frozen usage roles.
    merged = {}
    for item in available:
        merged.setdefault(item["asset_id"], set()).update(item["roles"])
    validate_references(choices, [{"asset_id": key, "roles": sorted(roles)} for key, roles in merged.items()])
    if frozen_refs:
        if all(reference["usage"] == "text_context" for reference in frozen_refs):
            lines.extend(["", "素材引用（仅文字灵感，未输入参考图）："])
        else:
            lines.extend(["", "素材引用（image_input 项作为实际参考图片输入；text_context 项仅文字灵感）："])
        for reference in frozen_refs:
            label = "评审摘要：" if reference["source_kind"] == "reviewed_work" else "来源元数据（尚未审阅图片内容）："
            lines.extend(["用途：" + reference["role"], label + reference["summary"],
                          "使用理由：" + reference["reason"]])
    result = "\n".join(lines)
    if len(result) > 4000:
        raise StoreError("PROMPT_TOO_LONG", "Text-reference render prompt exceeds the MCP 4000 character limit")
    return result
