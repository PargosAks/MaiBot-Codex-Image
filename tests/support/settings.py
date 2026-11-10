"""仓库位置和合成协议测试数据。"""

from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
SCOPE = {"platform": "qq", "stream_id": "real-stream", "user_id": "123"}
SECRET = "local-test-secret-" + "s" * 32
