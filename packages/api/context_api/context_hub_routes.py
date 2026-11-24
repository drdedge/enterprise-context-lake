"""LangSmith Context Hub export / import endpoints (ADR-008). Release managers only."""

from __future__ import annotations

import os
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from context_core.context_hub_service import ContextHubService
from context_core.exceptions import ValidationError
from context_core.models import Principal

router = APIRouter()


async def _principal(request: Request) -> Principal:
    from context_api.routes import _current_principal

    return await _current_principal(request)


def get_context_hub(request: Request) -> ContextHubService:
    app = request.app
    svc = getattr(app.state, "context_hub", None)
    if svc is not None:
        return svc
    ctx = app.state.runtime
    versions = getattr(ctx.registry, "versions", None)
    if versions is None:
        raise ValidationError("Version control is not enabled for this runtime")
    from context_adapters.context_hub_clients import hub_client_from_env

    svc = ContextHubService(
        versions=versions,
        client=hub_client_from_env(ctx.runtime_dir),
        prefix=os.environ.get("CONTEXT_HUB_REPO_PREFIX", "context-lake"),
    )
    app.state.context_hub = svc
    return svc


class ExportBody(BaseModel):
    dry_run: bool = False
    accept_hub_heads: bool = False


class ImportBody(BaseModel):
    repo_handle: str = Field(min_length=1, max_length=100)
    kind: Literal["agent", "skill"] = "agent"
    version: str | None = Field(default=None, description="Commit hash or tag, e.g. 'production'. Default: latest.")
    read_acl_groups: list[str] | None = Field(
        default=None, description="Needed only for repos the lake did not export (no lake manifest)."
    )


@router.get("/api/v1/context-hub/releases/{release_id}/plan")
async def plan_release_export(release_id: int, request: Request, principal: Principal = Depends(_principal)) -> dict[str, Any]:
    plan = get_context_hub(request).plan_release(release_id, principal)
    return {"plan": plan.summary()}


@router.post("/api/v1/context-hub/releases/{release_id}/export")
async def export_release(
    release_id: int, body: ExportBody, request: Request, principal: Principal = Depends(_principal)
) -> dict[str, Any]:
    return get_context_hub(request).export_release(
        release_id, principal, dry_run=body.dry_run, accept_hub_heads=body.accept_hub_heads
    )


@router.post("/api/v1/context-hub/import", status_code=201)
async def import_repo(body: ImportBody, request: Request, principal: Principal = Depends(_principal)) -> dict[str, Any]:
    return await get_context_hub(request).import_repo(
        body.repo_handle,
        principal,
        kind=body.kind,
        version=body.version,
        read_acl_groups=body.read_acl_groups,
    )
