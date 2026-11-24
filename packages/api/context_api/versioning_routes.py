"""Version control REST endpoints (ADR-007): change sets, history, redline diffs, releases, channels.

Attribution: callers say who is writing with optional headers, which the middleware below copies
into every version recorded during the request:
  X-Change-Source: ui | api | harness | agent | upload
  X-Agent-Id:      the agent or harness identity
  X-Run-Id:        the run (trace) id, e.g. a LangSmith run
Without headers the source is inferred from the path (MCP -> harness, agent/curator -> agent,
uploads -> upload, other REST -> api).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from context_core.exceptions import ValidationError
from context_core.models import Principal
from context_core.versioning import RECORD_TYPES, Attribution, ChangeItemInput, current_attribution

router = APIRouter()

_SOURCES = {"ui", "api", "harness", "agent", "upload"}


def _infer_source(path: str) -> str:
    if path.startswith("/mcp"):
        return "harness"
    if path.startswith(("/api/v1/agent", "/api/v1/curator")):
        return "agent"
    if path.startswith(("/api/v1/uploads", "/api/v1/sources/upload")):
        return "upload"
    return "api"


class AttributionMiddleware:
    """Pure ASGI middleware setting the change attribution context for the request."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        source = (headers.get("x-change-source") or "").strip().lower()
        if source not in _SOURCES:
            source = _infer_source(scope.get("path", ""))
        attr = Attribution(
            source=source,
            agent_id=(headers.get("x-agent-id") or None),
            run_id=(headers.get("x-run-id") or None),
        )
        token = current_attribution.set(attr)
        try:
            await self.app(scope, receive, send)
        finally:
            current_attribution.reset(token)


def _ctx(request: Request) -> Any:
    return request.app.state.runtime


def _svc(request: Request) -> Any:
    svc = getattr(_ctx(request).registry, "versions", None)
    if svc is None:
        raise ValidationError("Version control is not enabled for this runtime")
    return svc


async def _principal(request: Request) -> Principal:
    from context_api.routes import _current_principal

    return await _current_principal(request)


def _check_type(record_type: str) -> str:
    if record_type not in RECORD_TYPES:
        raise ValidationError(f"Unknown record type {record_type}; use one of {', '.join(RECORD_TYPES)}")
    return record_type


async def _after_write(request: Request, reason: str) -> int:
    ctx = _ctx(request)
    epochs = await ctx.registry.store.get_epochs()
    ctx.bundle_cache.clear()
    ctx.events.publish("epoch_updated", {"reason": reason, "data_epoch": epochs.data_epoch})
    return epochs.data_epoch


# ---------------------------------------------------------------------------
# Change sets
# ---------------------------------------------------------------------------


class ChangeSetCreateBody(BaseModel):
    title: str
    reason: str = ""
    items: list[ChangeItemInput] = Field(min_length=1, max_length=200)
    agent_id: str | None = None
    run_id: str | None = None


class DecisionBody(BaseModel):
    comment: str = ""


class RevertBody(BaseModel):
    reason: str = ""


@router.post("/api/v1/change-sets", status_code=201)
async def create_change_set(body: ChangeSetCreateBody, request: Request, principal: Principal = Depends(_principal)) -> dict[str, Any]:
    svc = _svc(request)
    cs = await svc.propose(
        title=body.title, reason=body.reason, items=body.items, principal=principal,
        agent_id=body.agent_id, run_id=body.run_id,
    )
    if cs.status == "merged":
        await _after_write(request, "change_set_merged")
    return {"change_set": svc.view(cs, principal)}


@router.get("/api/v1/change-sets")
async def list_change_sets(
    request: Request,
    status: str | None = Query(default=None, description="Comma-separated: proposed,merged,rejected,conflict,failed"),
    include_direct: bool = Query(default=False, description="Include writes recorded from direct (unreviewed) paths"),
    limit: int = Query(default=50, ge=1, le=500),
    principal: Principal = Depends(_principal),
) -> dict[str, Any]:
    svc = _svc(request)
    statuses = [s.strip() for s in status.split(",")] if status else None
    items = svc.list_visible(principal, statuses=statuses, include_implicit=include_direct, limit=limit)
    return {
        "change_sets": [
            {k: v for k, v in svc.view(cs, principal).items() if k not in ("items",)} | {"item_count": len(cs.items)}
            for cs in items
        ]
    }


@router.get("/api/v1/change-sets/{change_id}")
async def get_change_set(change_id: str, request: Request, principal: Principal = Depends(_principal)) -> dict[str, Any]:
    svc = _svc(request)
    return {"change_set": svc.view(svc.get(change_id, principal), principal)}


@router.post("/api/v1/change-sets/{change_id}/approve")
async def approve_change_set(change_id: str, body: DecisionBody, request: Request, principal: Principal = Depends(_principal)) -> dict[str, Any]:
    svc = _svc(request)
    cs = await svc.approve(change_id, principal, body.comment)
    if cs.status == "merged":
        await _after_write(request, "change_set_merged")
    return {"change_set": svc.view(cs, principal)}


@router.post("/api/v1/change-sets/{change_id}/reject")
async def reject_change_set(change_id: str, body: DecisionBody, request: Request, principal: Principal = Depends(_principal)) -> dict[str, Any]:
    svc = _svc(request)
    cs = await svc.reject(change_id, principal, body.comment)
    return {"change_set": svc.view(cs, principal)}


@router.post("/api/v1/change-sets/{change_id}/revert", status_code=201)
async def revert_change_set(change_id: str, body: RevertBody, request: Request, principal: Principal = Depends(_principal)) -> dict[str, Any]:
    svc = _svc(request)
    cs = await svc.revert(change_id, principal, body.reason)
    if cs.status == "merged":
        await _after_write(request, "change_set_reverted")
    return {"change_set": svc.view(cs, principal)}


@router.get("/api/v1/approval-rules")
async def get_approval_rules(request: Request, principal: Principal = Depends(_principal)) -> dict[str, Any]:
    svc = _svc(request)
    return svc.rules.model_dump()


# ---------------------------------------------------------------------------
# History, versions and redline diffs
# ---------------------------------------------------------------------------


@router.get("/api/v1/history/{record_type}/{record_id:path}/diff")
async def get_diff(
    record_type: str,
    record_id: str,
    request: Request,
    from_version: int | None = Query(default=None, alias="from"),
    to_version: int | None = Query(default=None, alias="to"),
    principal: Principal = Depends(_principal),
) -> dict[str, Any]:
    return _svc(request).diff(_check_type(record_type), record_id, from_version, to_version, principal)


@router.get("/api/v1/history/{record_type}/{record_id:path}/versions/{version_no}")
async def get_version(record_type: str, record_id: str, version_no: int, request: Request, principal: Principal = Depends(_principal)) -> dict[str, Any]:
    return _svc(request).get_version(_check_type(record_type), record_id, version_no, principal)


@router.get("/api/v1/history/{record_type}/{record_id:path}/at")
async def get_record_at(
    record_type: str,
    record_id: str,
    request: Request,
    channel: str | None = Query(default=None),
    release: int | None = Query(default=None),
    principal: Principal = Depends(_principal),
) -> dict[str, Any]:
    return _svc(request).record_at(_check_type(record_type), record_id, principal, channel=channel, release=release)


@router.get("/api/v1/history/claim-slots/{subject_id:path}")
async def get_claim_slots(subject_id: str, request: Request, principal: Principal = Depends(_principal)) -> dict[str, Any]:
    return {"slots": _svc(request).subject_claim_slots(subject_id, principal)}


@router.get("/api/v1/history/{record_type}/{record_id:path}")
async def get_history(record_type: str, record_id: str, request: Request, principal: Principal = Depends(_principal)) -> dict[str, Any]:
    return _svc(request).history(_check_type(record_type), record_id, principal)


# ---------------------------------------------------------------------------
# Releases and channels
# ---------------------------------------------------------------------------


class ReleaseBody(BaseModel):
    notes: str = ""


class ChannelMoveBody(BaseModel):
    release_id: int
    reason: str = ""


@router.get("/api/v1/releases")
async def list_releases(request: Request, principal: Principal = Depends(_principal)) -> dict[str, Any]:
    svc = _svc(request)
    return {"releases": svc.releases(principal), **svc.channels()}


@router.post("/api/v1/releases", status_code=201)
async def cut_release(body: ReleaseBody, request: Request, principal: Principal = Depends(_principal)) -> dict[str, Any]:
    svc = _svc(request)
    rel = await svc.cut_release(principal, body.notes)
    return {"release": rel.model_dump(), **svc.channels()}


@router.get("/api/v1/channels")
async def get_channels(request: Request, principal: Principal = Depends(_principal)) -> dict[str, Any]:
    return _svc(request).channels()


@router.put("/api/v1/channels/{name}")
async def move_channel(name: str, body: ChannelMoveBody, request: Request, principal: Principal = Depends(_principal)) -> dict[str, Any]:
    svc = _svc(request)
    out = svc.move_channel(name, body.release_id, principal, body.reason)
    ctx = _ctx(request)
    ctx.bundle_cache.clear()
    ctx.events.publish("channel_moved", {"channel": name, "release_id": body.release_id})
    return out


@router.get("/api/v1/channels/{name}/as-of")
async def channel_as_of(name: str, request: Request, principal: Principal = Depends(_principal)) -> dict[str, Any]:
    """The system time to pass as ``recorded_as_of`` on claim and bundle reads to read this channel."""
    return {"channel": name, "recorded_as_of": _svc(request).resolve_as_of(channel=name)}
