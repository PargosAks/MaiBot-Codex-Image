"""Test subprocess speaking real JSON-RPC framing. No OpenAI requests."""

from pathlib import Path
import json
import base64
import os
import sys

from PIL import Image

mode = sys.argv[1]
role = sys.argv[2] if len(sys.argv) > 2 else "worker"
token_reads = 0
authenticated = False
for line in sys.stdin:
    msg = json.loads(line)
    method, key = msg["method"], msg.get("id")
    if key is None:
        continue
    result = {}
    if role == "broker" and method in {"thread/start", "turn/start"}:
        raise RuntimeError("Auth broker must never create a thread or start inference")
    if method == "account/read":
        result = {"account": {"type": "apiKey" if mode == "api" else "chatgpt"}}
    elif method == "getAuthStatus":
        token_reads += 1
        identity = "changed-account" if mode == "account-change" and token_reads > 1 else "account-test"
        claims = {"https://api.openai.com/auth": {"chatgpt_account_id": identity}}
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        result = {"authMethod": "chatgpt", "authToken": "test." + payload + ".fake"}
    elif method == "account/login/start":
        assert role == "worker" and msg["params"]["type"] == "chatgptAuthTokens"
        authenticated = True
        result = {"type": "chatgptAuthTokens"}
    elif method == "thread/start":
        assert authenticated
        result = {"thread": {"id": "thread-test"}}
    elif method == "turn/start":
        result = {"turn": {"id": "turn-test"}}
    print(json.dumps({"id": key, "result": result}), flush=True)
    if method != "turn/start":
        continue
    if mode in {"refresh", "account-change"}:
        print(json.dumps({"id": "refresh", "method": "account/chatgptAuthTokens/refresh",
                          "params": {"reason": "unauthorized", "previousAccountId": "account-test"}}), flush=True)
        reply = json.loads(sys.stdin.readline())
        assert reply["id"] == "refresh"
        if "error" in reply:
            print(json.dumps({"method": "error", "params": {}}), flush=True)
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
