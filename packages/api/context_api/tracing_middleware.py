"""Pure-ASGI middleware opening one root span per HTTP request (ADR-006).

The span's trace is marked with the caller's wall side during principal resolution, and
receipts built while serving the request add their metadata to it. Query strings and
bodies are never recorded.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from context_core.tracing import TRACER_NAME

_SKIP_PREFIXES = ("/assets", "/healthz")


class RequestTracingMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or str(scope.get("path", "")).startswith(_SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        path = scope.get("path", "")
        tracer = trace.get_tracer(TRACER_NAME)
        status_holder: dict[str, int] = {}

        async def _send(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                status_holder["status"] = int(message.get("status", 0))
            await send(message)

        with tracer.start_as_current_span(
            f"{method} {path}",
            kind=SpanKind.SERVER,
            attributes={"http.request.method": method, "url.path": path},
        ) as span:
            try:
                await self.app(scope, receive, _send)
            finally:
                status = status_holder.get("status")
                if status is not None:
                    span.set_attribute("http.response.status_code", status)
                    if status >= 500:
                        span.set_status(Status(StatusCode.ERROR))
