"""Entity page endpoints (ADR-009): page list, page reader, cited answers and page templates."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from context_core.exceptions import ValidationError
from context_core.models import Principal, WallSide
from context_core.versioning import ChangeItemInput
from context_core.wiki import (
    BUILTIN_TEMPLATE,
    PAGE_TEMPLATE_KIND,
    SectionSpec,
    WikiService,
    render_markdown,
    render_template_body,
)

router = APIRouter()


async def _principal(request: Request) -> Principal:
    from context_api.routes import _current_principal

    return await _current_principal(request)


def _wiki(request: Request) -> WikiService:
    svc = getattr(request.app.state.runtime, "wiki", None)
    if svc is None:
        raise ValidationError("Entity pages are not enabled for this runtime")
    return svc


def _at(request: Request, channel: str | None, release: int | None, recorded_as_of: datetime | None) -> datetime | None:
    from context_api.routes import _channel_recorded_as_of

    return _channel_recorded_as_of(request, channel, release, recorded_as_of)


@router.get("/api/v1/wiki/pages")
async def list_pages(
    request: Request,
    side: WallSide | None = Query(default=None, description="Defaults to your own side"),
    entity_types: str | None = Query(default=None, description="Comma-separated entity types"),
    topic: str | None = Query(default=None, description="Topic slug"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(_principal),
) -> dict[str, Any]:
    types = [t.strip() for t in entity_types.split(",") if t.strip()] if entity_types else None
    return await _wiki(request).structure(
        principal, side=side, entity_types=types, topic_slug=topic, limit=limit, offset=offset
    )


@router.get("/api/v1/wiki/pages/{key:path}", response_model=None)
async def read_page(
    key: str,
    request: Request,
    side: WallSide | None = Query(default=None),
    as_of: datetime | None = Query(default=None, description="Business time"),
    recorded_as_of: datetime | None = Query(default=None, description="System time"),
    channel: str | None = Query(default=None, description="dev, staging or production"),
    release: int | None = Query(default=None),
    format: Literal["json", "markdown"] = Query(default="json"),
    principal: Principal = Depends(_principal),
) -> JSONResponse | PlainTextResponse:
    rec = _at(request, channel, release, recorded_as_of)
    page = await _wiki(request).build_page(key, principal, side=side, as_of=as_of, recorded_as_of=rec)
    if format == "markdown":
        return PlainTextResponse(render_markdown(page), media_type="text/markdown; charset=utf-8")
    return JSONResponse({"page": page.model_dump(mode="json"), "markdown": render_markdown(page)})


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    side: WallSide | None = None
    entity_keys: list[str] | None = Field(default=None, max_length=10)
    as_of: datetime | None = None
    recorded_as_of: datetime | None = None
    channel: str | None = None
    release: int | None = None


@router.post("/api/v1/wiki/ask")
async def ask(body: AskBody, request: Request, principal: Principal = Depends(_principal)) -> dict[str, Any]:
    rec = _at(request, body.channel, body.release, body.recorded_as_of)
    return await _wiki(request).ask(
        body.question, principal, side=body.side, entity_keys=body.entity_keys, as_of=body.as_of, recorded_as_of=rec
    )


@router.get("/api/v1/wiki/templates")
async def list_templates(
    request: Request,
    side: WallSide | None = Query(default=None),
    principal: Principal = Depends(_principal),
) -> dict[str, Any]:
    svc = _wiki(request)
    ws = svc.resolve_side(principal, side)
    templates, errors = await svc.load_templates(principal, ws)
    return {
        "side": ws.value,
        "templates": [t.model_dump(mode="json") for t in templates],
        "builtin": BUILTIN_TEMPLATE.model_dump(mode="json"),
        "errors": errors,
    }


class TemplateBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    entity_types: list[str] = Field(min_length=1, max_length=50)
    sections: list[SectionSpec] = Field(min_length=1, max_length=30)
    note: str = ""
    reason: str = ""
    read_acl_groups: list[str] = Field(default_factory=lambda: ["firm-all"])
    write_acl_groups: list[str] = Field(default_factory=lambda: ["fund-stewards"])


@router.put("/api/v1/wiki/templates/{template_id}", response_model=None)
async def propose_template(
    template_id: str, body: TemplateBody, request: Request, principal: Principal = Depends(_principal)
) -> JSONResponse:
    """File a template change as a change set. Templates are public-side configuration."""
    versions = getattr(request.app.state.runtime.registry, "versions", None)
    if versions is None:
        raise ValidationError("Version control is not enabled for this runtime")
    slug = template_id.strip().lower()
    if not slug.replace("-", "").replace("_", "").isalnum():
        raise ValidationError("template_id may use letters, digits, '-' and '_' only")
    item = {
        "item_id": f"kf:page-template-{slug}",
        "knowledge_kind": PAGE_TEMPLATE_KIND,
        "display_name": body.name,
        "slug": slug,
        "source_path": f"knowledge/{PAGE_TEMPLATE_KIND}/{slug}.md",
        "body_markdown": render_template_body(body.entity_types, body.sections, note=body.note),
        "wall_side": WallSide.public_side.value,
        "read_acl_groups": body.read_acl_groups,
        "write_acl_groups": body.write_acl_groups,
    }
    cs = await versions.propose(
        title=f"Page template: {body.name}",
        reason=body.reason,
        items=[ChangeItemInput(record_type="knowledge_item", op="upsert", record_id=item["item_id"], payload=item)],
        principal=principal,
    )
    if cs.status == "merged":
        ctx = request.app.state.runtime
        ctx.bundle_cache.clear()
    return JSONResponse(status_code=201 if cs.status == "merged" else 202, content={"change_set": versions.view(cs, principal)})
