"""用于 Host 媒体回归的合成图片字节。"""

from io import BytesIO

from PIL import Image


def image_bytes(fmt="PNG", *, animated=False):
    output = BytesIO()
    first = Image.new("RGB", (8, 6), "red")
    options = {"save_all": True, "append_images": [Image.new("RGB", (8, 6), "blue")]} if animated else {}
    first.save(output, format=fmt, **options)
    return output.getvalue()
