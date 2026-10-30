"""Test-only MCP protocol fixture, explicitly not a production image service.

FastMCP and uvicorn exercise the plugin's real HTTP/MCP client. The small
independent verifier describes the external protocol expected by this client;
these tests do not validate any external service's authorization implementation.
Jobs live only in memory and return one synthetic, static 1x1 PNG.
"""

from contextlib import asynccontextmanager
import base64
import hashlib
import hmac
import json
import math
import time

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ImageContent, TextContent
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAX+XDSwAAAABJRU5ErkJggg=="
)


class ProtocolFixture:
    def __init__(self, secret: str):
        self.secret = secret
        self.url = ""
        self.jobs = {}
        self.requests = {}
        self.calls = []
        self.health_mode = "structured"
        self.result_mode = "image"
        self.capabilities = None
        mcp = FastMCP(
            "Static image protocol fixture (not a production service)",
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=["127.0.0.1:*"],
                allowed_origins=[],
            ),
        )

        @mcp.tool()
        async def codex_image_submit(envelope: str) -> dict:
            scope, args = self.verify(envelope, "submit")
            key = (scope, args["request_id"])
            if key in self.requests:
                job = self.requests[key]
                if self.jobs[job]["prompt"] != args["prompt"]:
                    raise ValueError("The request_id is already bound to a different prompt")
            else:
                job = f"img_{len(self.jobs) + 1:032x}"
                self.requests[key] = job
                self.jobs[job] = {"scope": scope, "prompt": args["prompt"]}
            return {"job_id": job, "status": "completed"}

        @mcp.tool()
        async def codex_image_status(envelope: str) -> dict:
            self.owned_job(envelope, "status")
            return {"status": "completed", "artifacts": 1, "error_code": None}

        @mcp.tool()
        async def codex_image_result(envelope: str) -> CallToolResult:
            self.owned_job(envelope, "result")
            if self.result_mode == "error":
                return CallToolResult(isError=True, content=[TextContent(type="text", text="fixture error")])
            if self.result_mode == "text":
                return CallToolResult(content=[TextContent(type="text", text="no image")])
            data = base64.b64encode(PNG).decode()
            if self.result_mode == "empty":
                data = ""
            elif self.result_mode == "invalid_base64":
                data = "not base64!"
            return CallToolResult(content=[ImageContent(type="image", data=data, mimeType="image/png")])

        @mcp.tool()
        async def codex_image_lookup(envelope: str) -> dict:
            scope, args = self.verify(envelope, "lookup")
            job_id = self.requests.get((scope, args["request_id"]))
            if job_id is None:
                return {"found": False}
            return {
                "found": True,
                "operation": "submit",
                "request_id": args["request_id"],
                "request_digest": hashlib.sha256(self.jobs[job_id]["prompt"].encode()).hexdigest(),
                "job": {
                    "job_id": job_id,
                    "status": "completed",
                    "turn_status": "completed",
                    "error_code": None,
                    "artifacts": [],
                },
            }

        @mcp.tool()
        async def codex_image_health(envelope: str) -> CallToolResult:
            self.verify(envelope, "health")
            data = {"worker_running": True, "generation_probe": False, "login_verified": False}
            if self.capabilities is not None:
                data["capabilities"] = self.capabilities
            if self.health_mode == "empty":
                return CallToolResult(content=[])
            content = [TextContent(type="text", text=json.dumps(data))]
            if self.health_mode == "text":
                return CallToolResult(content=content)
            return CallToolResult(content=content, structuredContent=data)

        transport = mcp.streamable_http_app()

        @asynccontextmanager
        async def lifespan(app):
            async with mcp.session_manager.run():
                yield

        application = Starlette(routes=[Mount("/", app=transport)], lifespan=lifespan)

        async def bearer_guard(scope, receive, send):
            if scope["type"] == "http":
                auth = dict(scope["headers"]).get(b"authorization", b"")
                if not hmac.compare_digest(auth, ("Bearer " + secret).encode()):
                    await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                    return
            await application(scope, receive, send)

        self.app = bearer_guard

    def verify(self, envelope: str, operation: str) -> tuple[tuple[str, ...], dict]:
        """Independently check the signed protocol fields expected from Bridge."""
        try:
            payload, signature = envelope.split(".")
            expected = hmac.new(self.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature")
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            scope, issued_at, args = data["scope"], data["iat"], data["args"]
            if (
                data["op"] != operation
                or isinstance(issued_at, bool)
                or not isinstance(issued_at, (int, float))
                or not math.isfinite(issued_at)
                or abs(time.time() - issued_at) > 120
                or not isinstance(scope, dict)
                or set(scope) != {"platform", "stream_id", "user_id"}
                or not all(isinstance(value, str) and value.strip() for value in scope.values())
                or not isinstance(args, dict)
            ):
                raise ValueError("context")
            owner = tuple(scope[key] for key in ("platform", "stream_id", "user_id"))
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
            raise ValueError("Protocol fixture rejected the signed context") from exc
        self.calls.append((operation, dict(scope), dict(args)))
        return owner, args

    def owned_job(self, envelope: str, operation: str):
        owner, args = self.verify(envelope, operation)
        job = self.jobs[args["job_id"]]
        if owner != job["scope"]:
            raise ValueError("Protocol fixture rejected a different owner")
        return job
