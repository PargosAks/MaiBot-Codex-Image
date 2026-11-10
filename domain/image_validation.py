"""图片字节的格式、像素和完整性校验；不访问 Host、网络或视觉模型。"""

from io import BytesIO
import warnings

from .policy import StoreError

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 64_000_000
IMAGE_MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def _reject(code: str) -> None:
    # Never include component fields, binary data or decoder exception text.
    raise StoreError(code, "Current Host image input was rejected")


def validate_image(data: bytes) -> str:
    # 延迟加载解码库，纯文字插件加载不承担图片解码的副作用。
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
