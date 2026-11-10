"""有界的图片展示解码；缓存不承担数据库完整性或访问授权检查。"""

from functools import lru_cache
from io import BytesIO
from typing import Tuple
import warnings

from PIL import Image, UnidentifiedImageError

from ..domain.policy import StoreError


@lru_cache(maxsize=4)
def display_image(data: bytes, mime: str, pixel_limit: int, thumbnail: bool) -> Tuple[bytes, str, int, int]:
    """复用原图各分块的解码；最多保留四份受 8 MiB 上限约束的输入。"""
    # 键包含真实字节而非数据库声称的摘要；调用方每次仍需核验 scope 和 SHA256。
    # 只缓存已成功验证的不可变结果，解码器异常不会留下可复用条目。
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as original:
                actual_mime = Image.MIME.get(original.format)
                if actual_mime not in {"image/png", "image/jpeg", "image/webp"} or actual_mime != mime:
                    raise StoreError("IMAGE_INVALID", "Stored image type is invalid")
                if original.width * original.height > pixel_limit:
                    raise StoreError("IMAGE_INVALID", "Stored image dimensions exceed display limit")
                original.load()
                width, height = original.size
                if thumbnail:
                    original.thumbnail((320, 320))
                    output = BytesIO()
                    original.convert("RGB").save(output, format="JPEG", quality=80)
                    data, actual_mime = output.getvalue(), "image/jpeg"
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise StoreError("IMAGE_INVALID", "Stored image cannot be decoded") from exc
    return data, actual_mime, width, height
