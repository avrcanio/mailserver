import contextlib

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .auth import BearerTokenMiddleware
from .server import get_settings, mcp


async def healthz(_request):
    settings = get_settings()
    return JSONResponse(
        {
            "ok": True,
            "service": "mail-mcp",
            "mailbox": settings.mailbox_email,
        }
    )


@contextlib.asynccontextmanager
async def lifespan(app: Starlette):
    async with mcp.session_manager.run():
        yield


settings = get_settings()
transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=list(settings.allowed_hosts),
    allowed_origins=list(settings.allowed_origins),
)

# Allow a 10 MiB decoded attachment as base64 (~13.3 MiB) plus JSON framing.
MCP_MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024

app = Starlette(
    routes=[
        Route("/healthz", healthz),
        Mount(
            "/",
            app=mcp.streamable_http_app(
                streamable_http_path="/mcp",
                stateless_http=True,
                json_response=True,
                transport_security=transport_security,
                max_request_body_size=MCP_MAX_REQUEST_BODY_BYTES,
            ),
        ),
    ],
    lifespan=lifespan,
)
app.add_middleware(BearerTokenMiddleware, token=settings.bearer_token, public_paths={"/healthz"})
