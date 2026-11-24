"""FastAPI application factory mounting REST/SSE/AX routes, dual FastMCP servers, CORS, and static UI."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from context_core.exceptions import ContextLakeError
from context_core.models import ActorMode, ChannelMode
from context_core.registry import LlmMode
from context_mcp.server import create_codemode_mcp_server, create_primitive_mcp_server

from context_api.bootstrap import AppRuntimeContext, build_runtime_context_sync
from context_api.routes import router as api_router
from context_api.versioning_routes import AttributionMiddleware
from context_api.versioning_routes import router as versioning_router
from context_api.context_hub_routes import router as context_hub_router


def create_app(
    *,
    runtime: AppRuntimeContext | None = None,
    repo_root: Path | str | None = None,
    db_path: Path | str | None = None,
    runtime_dir: Path | str | None = None,
    active_store: Literal["sqlite_bitemporal", "okf_markdown_vault"] = "sqlite_bitemporal",
    active_retrieval: Literal["lexical_manifest", "facet_graph"] = "lexical_manifest",
    llm_mode: LlmMode = "auto",
    auth_mode: Literal["mock", "entra_jwt"] | None = None,
    default_user_id: str | None = "user:fund-steward",
    default_actor_mode: ActorMode = "obo",
    default_channel_mode: ChannelMode = "direct",
    seed_fixtures: bool = True,
    compile_initial_snapshots: bool = True,
    ephemeral: bool = False,
) -> FastAPI:
    """Create the configured Enterprise Context Lake FastAPI application (OpenAPI 3.1)."""
    ctx = runtime or build_runtime_context_sync(
        repo_root=repo_root,
        db_path=db_path,
        runtime_dir=runtime_dir,
        active_store=active_store,
        active_retrieval=active_retrieval,
        llm_mode=llm_mode,
        auth_mode=auth_mode,
        default_user_id=default_user_id,
        default_actor_mode=default_actor_mode,
        default_channel_mode=default_channel_mode,
        seed_fixtures=seed_fixtures,
        compile_initial_snapshots=compile_initial_snapshots,
        ephemeral=ephemeral,
    )

    primitive_mcp = create_primitive_mcp_server(ctx.mcp_runtime)
    codemode_mcp = create_codemode_mcp_server(ctx.mcp_runtime)

    # Stateless Streamable HTTP JSON responses work cleanly across multi-worker Uvicorn, httpx, and FastMCP clients.
    codemode_http = codemode_mcp.http_app(path="/", stateless_http=True, json_response=True)
    primitive_http = primitive_mcp.http_app(path="/", stateless_http=True, json_response=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            await ctx.initialize()
            await stack.enter_async_context(codemode_http.lifespan(app))
            await stack.enter_async_context(primitive_http.lifespan(app))
            try:
                yield
            finally:
                await ctx.close()

    app = FastAPI(
        title="Enterprise Context Lake",
        version="0.1.0",
        description=(
            "Bitemporal institutional knowledge substrate with fail-closed MNPI wall barriers, "
            "decoupled claim/evidence ACLs, N-way conflicts, dual FastMCP + Server-Side Code Mode, "
            "and DeepAgents harness."
        ),
        lifespan=lifespan,
    )

    app.state.runtime = ctx
    app.state.primitive_mcp = primitive_mcp
    app.state.codemode_mcp = codemode_mcp

    # CORS for local Vite dev server (localhost:5173) and local tooling
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
            "http://127.0.0.1:4173",
            "http://localhost:8000",
            "http://127.0.0.1:8000",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["ETag", "Content-Disposition"],
    )

    # Authoritative ADR-005 error envelope: { "error": { "code": "...", "message": "..." } }
    @app.exception_handler(ContextLakeError)
    async def _handle_domain_error(request: Request, exc: ContextLakeError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_error_dict())

    @app.exception_handler(PermissionError)
    async def _handle_permission_error(request: Request, exc: PermissionError) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={"error": {"code": "forbidden", "message": str(exc) or "Permission denied"}},
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "; ".join(
                        f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg', 'invalid')}"
                        for err in exc.errors()[:5]
                    ),
                }
            },
        )

    app.include_router(api_router)
    app.include_router(versioning_router)
    app.include_router(context_hub_router)
    app.add_middleware(AttributionMiddleware)

    # Mount /mcp/codemode BEFORE /mcp so prefix routing resolves unambiguously.
    app.mount("/mcp/codemode", codemode_http)
    app.mount("/mcp", primitive_http)

    # Enforce HTTP 401/503 auth & revocation on /mcp* and eliminate trailing-slash 307 redirects on POST /mcp
    @app.middleware("http")
    async def _mcp_gate_and_slash_middleware(request: Request, call_next):
        path = request.scope.get("path", "")
        if path in ("/mcp", "/mcp/codemode"):
            request.scope["path"] = path + "/"
            path = request.scope["path"]
        if path.startswith("/mcp/"):
            try:
                await ctx.auth.resolve_principal(request.headers, store=ctx.registry.store)
            except ContextLakeError as exc:
                return JSONResponse(status_code=exc.status_code, content=exc.to_error_dict())
        return await call_next(request)

    # Optionally serve built frontend (`apps/web/dist`) if present
    dist_dir = ctx.repo_root / "apps" / "web" / "dist"
    index_html = dist_dir / "index.html"
    if dist_dir.is_dir() and index_html.is_file():
        assets_dir = dist_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="web-assets")

        @app.get("/", include_in_schema=False, response_model=None)
        async def serve_spa_root() -> FileResponse:
            return FileResponse(str(index_html))

        @app.get("/{spa_path:path}", include_in_schema=False, response_model=None)
        async def serve_spa_fallback(spa_path: str) -> FileResponse | JSONResponse:
            reserved = ("api/", "mcp", "healthz", "llms.txt", "agents", "docs", "openapi.json", "redoc")
            if spa_path.startswith(reserved):
                return JSONResponse(
                    status_code=404,
                    content={"error": {"code": "not_found", "message": "Resource not found"}},
                )
            candidate = (dist_dir / spa_path).resolve()
            try:
                candidate.relative_to(dist_dir.resolve())
                if candidate.is_file():
                    return FileResponse(str(candidate))
            except ValueError:
                pass
            return FileResponse(str(index_html))

    return app
