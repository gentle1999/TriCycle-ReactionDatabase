"""ASGI helpers for same-origin reverse-proxy deployments."""

from collections.abc import Awaitable, Callable
from typing import cast

from starlette.types import Receive, Scope, Send

ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class ForwardedPrefixMiddleware:
    """Apply the trusted proxy prefix to generated FastAPI URLs.

    Vite/Nginx strip the public ``/nexusx/...`` prefix before forwarding a
    request. Restoring it in the ASGI scope keeps Swagger, GraphiQL, and
    OpenAPI server URLs on the frontend origin without breaking direct local
    access to the individual demo services.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = cast(list[tuple[bytes, bytes]], scope.get("headers", []))
        prefix = next(
            (
                value.decode("latin-1").strip()
                for name, value in headers
                if name.lower() == b"x-forwarded-prefix"
            ),
            "",
        )
        if not prefix:
            await self.app(scope, receive, send)
            return

        normalized = "/" + prefix.strip("/") if prefix.strip("/") else ""
        forwarded_scope = dict(scope)
        forwarded_scope["root_path"] = normalized
        await self.app(forwarded_scope, receive, send)


__all__ = ["ForwardedPrefixMiddleware"]
