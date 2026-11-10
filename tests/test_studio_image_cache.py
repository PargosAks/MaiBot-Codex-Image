"""原图分块复用解码结果，但每次读取仍重新检查归属和数据库字节。"""

from io import BytesIO
import hashlib
import random

from PIL import Image
import pytest

from image_plugin_under_test.domain.policy import StoreError
from image_plugin_under_test.workspace.studio import IMAGE_CHUNK_BYTES
from support.studio_data import data as data


def test_original_chunks_do_not_decode_same_image_repeatedly(data, monkeypatch):
    _, db, view = data
    output = BytesIO()
    Image.frombytes("RGB", (512, 512), random.Random(712).randbytes(512 * 512 * 3)).save(output, format="PNG")
    raw = output.getvalue()
    assert len(raw) > IMAGE_CHUNK_BYTES * 2
    db.db.execute("UPDATE versions SET data=?,sha256=? WHERE space_id='a'",
                  (raw, hashlib.sha256(raw).hexdigest()))
    opened = []
    original_open = Image.open

    def count_open(*args, **kwargs):
        opened.append(True)
        return original_open(*args, **kwargs)

    monkeypatch.setattr(Image, "open", count_open)
    offset = 0
    while offset is not None:
        offset = view.image("a", "v-a", False, offset)["next_offset"]
    assert len(opened) == 1
    # 缓存不得把手工损坏的数据库内容变成成功响应。
    db.db.execute("UPDATE versions SET data=? WHERE space_id='a'", (b"corrupt",))
    with pytest.raises(StoreError, check=lambda exc: exc.code == "IMAGE_INVALID"):
        view.image("a", "v-a", False, 0)
