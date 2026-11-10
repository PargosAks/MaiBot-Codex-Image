"""Bounded current-event image decoding. No URL, path, history or vision access."""

from dataclasses import dataclass
from typing import List
import base64
import binascii
import hashlib
import re

# 常量及私有名称继续保留，以兼容既有调用方；实现属于纯图片边界。
from ..domain.image_validation import (
    MAX_IMAGE_BYTES as MAX_IMAGE_BYTES, MAX_PIXELS as MAX_PIXELS, IMAGE_MIMES as IMAGE_MIMES,
    _reject, validate_image as _validate_image,
)

MAX_IMAGES = 3
MAX_EVENT_BYTES = 24 * 1024 * 1024
IMAGE_ONLY_TEXT = "[收到当前消息中的参考图片；内容尚未审阅]"
UNREVIEWED_DESCRIPTION = "Unreviewed Host image metadata only; image content has not been reviewed."


@dataclass(frozen=True)
class HostImage:
    message_index: int
    data: bytes
    mime: str
    sha256: str


def current_images(message: dict) -> List[HostImage]:
    """Decode only direct image components in the supplied trusted event.

    Validate the complete batch before any import. Emoji, nested forwards,
    replies, URLs, component content and historical references are never read.
    """
    raw = message.get("raw_message")
    if not isinstance(raw, list):
        return []
    components = []
    for index, component in enumerate(raw):
        if isinstance(component, dict) and component.get("type") == "image":
            components.append((index, component))
            if len(components) > MAX_IMAGES:
                _reject("MEDIA_COUNT_EXCEEDED")
    result, total = [], 0
    for index, component in components:
        encoded = component.get("binary_data_base64")
        if not isinstance(encoded, str) or not encoded:
            _reject("MEDIA_BYTES_REQUIRED")
        if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            _reject("MEDIA_SIZE_EXCEEDED")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            _reject("MEDIA_BASE64_INVALID")
        if not data or len(data) > MAX_IMAGE_BYTES:
            _reject("MEDIA_SIZE_EXCEEDED")
        total += len(data)
        if total > MAX_EVENT_BYTES:
            _reject("MEDIA_TOTAL_EXCEEDED")
        host_hash = component.get("hash")
        # Public Host commonly uses MD5; unknown hash schemes assert nothing.
        if isinstance(host_hash, str):
            if (re.fullmatch(r"[0-9a-fA-F]{32}", host_hash)
                    and hashlib.md5(data, usedforsecurity=False).hexdigest() != host_hash.lower()):
                _reject("MEDIA_HASH_MISMATCH")
            if (re.fullmatch(r"[0-9a-fA-F]{64}", host_hash)
                    and hashlib.sha256(data).hexdigest() != host_hash.lower()):
                _reject("MEDIA_HASH_MISMATCH")
        mime = _validate_image(data)
        result.append(HostImage(index, data, mime, hashlib.sha256(data).hexdigest()))
    return result
