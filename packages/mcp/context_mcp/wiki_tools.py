"""Entity page tools for MCP (ADR-009), named after DeepWiki's three tools so harness prompts port over.

- ``read_wiki_structure``: the page list, grouped by entity type, with each type's section outline.
- ``read_wiki_contents``: one entity page as Markdown, with numbered citations to claims.
- ``ask_wiki_question``: a cited answer built from the relevant pages.

All three run as the caller, on the caller's side by default, through the same WikiService as REST.
"""

from __future__ import annotations

from typing import Any

from context_core.compression import ensure_utc
from context_core.exceptions import NotFoundError, ValidationError
from context_core.models import Principal
from context_core.wiki import WikiService, render_markdown

from context_mcp.runtime import LakeRuntime


def _wiki(runtime: LakeRuntime) -> WikiService:
    svc = runtime.extra.get("wiki")
    if svc is None:
        raise ValidationError("Entity pages are not enabled for this runtime")
    return svc


def _recorded_as_of(runtime: LakeRuntime, recorded_as_of: str | None, channel: str | None, release: int | None) -> Any:
    if not channel and release is None:
        return ensure_utc(recorded_as_of)
    get_versions = runtime.extra.get("versions")
    versions = get_versions() if callable(get_versions) else None
    if versions is None:
        raise ValidationError("Version control is not enabled for this runtime")
    return ensure_utc(versions.resolve_as_of(channel=channel, release=release)) or ensure_utc(recorded_as_of)


async def tool_wiki_structure(
    runtime: LakeRuntime,
    principal: Principal,
    *,
    side: str | None = None,
    entity_types: list[str] | None = None,
    topic_slug: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 1000))
    return await _wiki(runtime).structure(
        principal, side=side, entity_types=entity_types, topic_slug=topic_slug, limit=limit, offset=max(0, int(offset))
    )


async def tool_wiki_read(
    runtime: LakeRuntime,
    principal: Principal,
    *,
    id_or_key: str,
    side: str | None = None,
    as_of: str | None = None,
    recorded_as_of: str | None = None,
    channel: str | None = None,
    release: int | None = None,
) -> dict[str, Any]:
    rec = _recorded_as_of(runtime, recorded_as_of, channel, release)
    try:
        page = await _wiki(runtime).build_page(id_or_key, principal, side=side, as_of=as_of, recorded_as_of=rec)
    except NotFoundError:
        return {"found": False, "error": {"code": "not_found", "message": "Resource not found"}}
    return {
        "found": True,
        "markdown": render_markdown(page),
        "page": page.model_dump(mode="json"),
    }


async def tool_wiki_ask(
    runtime: LakeRuntime,
    principal: Principal,
    *,
    question: str,
    side: str | None = None,
    entity_keys: list[str] | None = None,
    as_of: str | None = None,
    recorded_as_of: str | None = None,
    channel: str | None = None,
    release: int | None = None,
) -> dict[str, Any]:
    rec = _recorded_as_of(runtime, recorded_as_of, channel, release)
    return await _wiki(runtime).ask(
        question, principal, side=side, entity_keys=(entity_keys or None), as_of=as_of, recorded_as_of=rec
    )
