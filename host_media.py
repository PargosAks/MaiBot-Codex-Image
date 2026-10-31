"""Bounded current-event image decoding. No URL, path, history or vision access."""

from dataclasses import dataclass
from io import BytesIO
from typing import List
import base64
import binascii
import hashlib
import re
import warnings

from .policy import StoreError

MAX_IMAGES = 3
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_EVENT_BYTES = 24 * 1024 * 1024
MAX_PIXELS = 64_000_000
IMAGE_ONLY_TEXT = "[收到当前消息中的参考图片；内容尚未审阅]"
UNREVIEWED_DESCRIPTION = "Unreviewed Host image metadata only; image content has not been reviewed."
IMAGE_MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


@dataclass(frozen=True)
class HostImage:
    message_index: int
    data: bytes
    mime: str
    sha256: str


def _reject(code: str) -> None:
    # Never include component fields, binary data or decoder exception text.
    raise StoreError(code, "Current Host image input was rejected")


def _validate_image(data: bytes) -> str:
    # Lazy import keeps text-only loading independent of the optional decoder.
    from PIL import Image

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data), formats=list(IMAGE_MIMES)) as image:
                mime = IMAGE_MIMES.get(image.format)
                if mime is None:
                    _reject("MEDIA_FORMAT_INVALID")
                if not 0 < image.width * image.height <= MAX_PIXELS:
                    _reject("MEDIA_PIXELS_EXCEEDED")
                if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) != 1:
                    _reject("MEDIA_ANIMATION_UNSUPPORTED")
                image.verify()
            # Header/CRC verification alone does not reject truncated JPEG/WebP.
            with Image.open(BytesIO(data), formats=list(IMAGE_MIMES)) as image:
                image.load()
        return mime
    except StoreError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        _reject("MEDIA_PIXELS_EXCEEDED")
    except Exception:  # noqa: BLE001 - decoder failures must not expose component data
        _reject("MEDIA_FORMAT_INVALID")


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
