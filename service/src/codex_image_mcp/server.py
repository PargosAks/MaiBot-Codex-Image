from contextlib import asynccontextmanager
from pathlib import Path
import argparse
import base64
import hmac

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ImageContent
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount
import uvicorn

from .config import Settings
from .service import Service


class BearerGuard:
    def __init__(self, app, secret, service=None):
        self.app, self.secret = app, secret
        self.service = service

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(auth, ("Bearer " + self.secret).encode()):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


def create_app(settings: Settings, driver=None):
    service = Service(settings, driver)
    try:
        return _create_app(settings, service)
    except BaseException:
        # App construction is synchronous and precedes the lifespan. Release the
        # database and ownership here if transport setup fails before startup.
        service._close_resources()
        raise


def _create_app(settings: Settings, service: Service):
    mcp = FastMCP(
        "Codex image jobs",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                f"{settings.host}:{settings.port}",
                "127.0.0.1:*",
                "localhost:*",
            ],
            allowed_origins=[],
        ),
    )

    @mcp.tool()
    async def codex_image_submit(envelope: str) -> dict:
        """Submit one image using signed host identity and a stable request_id. Consumes usage."""
        return await service.submit(envelope)

    @mcp.tool()
    async def codex_image_status(envelope: str) -> dict:
        """Read an owned job. Does not generate images."""
        return await service.status(envelope)

    @mcp.tool()
    async def codex_image_result(envelope: str) -> list[ImageContent]:
        """Read a previously generated owned image. Does not generate or resend it."""
        data, mime = await service.result(envelope)
        return [
            ImageContent(
                type="image", data=base64.b64encode(data).decode(), mimeType=mime
            )
        ]

    @mcp.tool()
    async def codex_image_health(envelope: str) -> dict:
        """Service health without a paid generation probe. Login is verified when a job starts."""
        return await service.health(envelope)

    transport = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        try:
            await service.start()
            async with mcp.session_manager.run():
                yield
        finally:
            await service.close()

    application = Starlette(routes=[Mount("/", app=transport)], lifespan=lifespan)
    return BearerGuard(application, settings.secret, service)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings.load(args.config)
    uvicorn.run(
        create_app(settings), host=settings.host, port=settings.port, access_log=False
    )


if __name__ == "__main__":
    main()
