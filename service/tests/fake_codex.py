"""Test subprocess speaking real JSON-RPC framing. No OpenAI requests."""

from pathlib import Path
import json
import os
import sys

from PIL import Image

mode = sys.argv[1]
for line in sys.stdin:
    msg = json.loads(line)
    method, key = msg["method"], msg.get("id")
    if key is None:
        continue
    result = {}
    if method == "account/read":
        result = {"account": {"type": "apiKey" if mode == "api" else "chatgpt"}}
    elif method == "thread/start":
        result = {"thread": {"id": "thread-test"}}
    elif method == "turn/start":
        result = {"turn": {"id": "turn-test"}}
    print(json.dumps({"id": key, "result": result}), flush=True)
    if method != "turn/start":
        continue
    if mode == "timeout":
        continue
    if mode != "no-image":
        path = Path(os.environ["CODEX_HOME"]) / "generated_images" / "test.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (32, 32), "green").save(path)
        print(
            json.dumps(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-test",
                        "turnId": "turn-test",
                        "item": {
                            "type": "imageGeneration",
                            "status": "completed",
                            "savedPath": str(path),
                            "failure": None,
                            "result": "a" * 100_000,
                        },
                    },
                }
            ),
            flush=True,
        )
    if mode == "disconnect":
        sys.exit(0)
    print(
        json.dumps(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-test",
                    "turn": {"id": "turn-test", "status": "completed"},
                },
            }
        ),
        flush=True,
    )
