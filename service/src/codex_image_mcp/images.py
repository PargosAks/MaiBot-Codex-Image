from io import BytesIO
from pathlib import Path
import warnings

from PIL import Image


def validate(data: bytes, maximum: int) -> str:
    if not data or len(data) > maximum:
        raise ValueError("IMAGE_SIZE_INVALID")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(data)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("IMAGE_TYPE_INVALID")
            if image.width * image.height > 25_000_000:
                raise ValueError("IMAGE_PIXELS_EXCEEDED")
            mime = Image.MIME[image.format]
            image.verify()
    return mime


def copy_output(
    saved_path: str, generated_root: Path, destination: Path, maximum: int
) -> tuple[bytes, str]:
    source = Path(saved_path).resolve(strict=True)
    if not source.is_relative_to(generated_root.resolve()):
        raise ValueError("OUTPUT_PATH_REJECTED")
    with source.open("rb") as stream:
        data = stream.read(maximum + 1)
    mime = validate(data, maximum)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents overwriting any previously accepted artifact.
    with destination.open("xb") as stream:
        stream.write(data)
    return data, mime
