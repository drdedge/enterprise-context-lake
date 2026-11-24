"""Entity pages: wiki-style read views generated from claims (ADR-009).

The idea comes from DeepWiki (generated, navigable pages with every statement linked to its source,
a page list, a page reader and a question tool), rebuilt on the lake's own model:

- **Pages are views, not records.** A page is assembled on request from the claims the caller may
  read. Nobody edits a page; edits go to the claims behind it, through change sets (ADR-007).
- **Every statement cites a claim.** Each line on a page carries a numbered citation naming the
  claim, its source system and, when the caller may see evidence, the evidence location.
- **Pages are cut by wall side.** A page is built for one side: a public-side page uses only
  public-side records, and a private-side page uses public-side and private-side records, exactly
  like the snapshot partitions (ADR-003). The caller may only ask for sides they are allowed to see.
- **Templates decide what a page shows.** A page template per entity type (sections, filters,
  limits) is stored as a knowledge item of kind ``page_template``, so template edits are proposed,
  approved, diffed and reverted like any other context.
- **Narration stays inside the boundary.** The overview paragraph and answers may be written by a
  model, but private-side or above-the-wall material only ever goes to an internal model or to the
  deterministic writer (see ``model_boundary``). Model text must cite only the numbered statements
  it was given, or it is discarded.
- **Rebuilt when claims change.** Narration is cached by a fingerprint of everything that went into
  it (side, template, entity and each claim's id and timestamps), so a page is regenerated as soon
  as any claim behind it changes, and never served stale.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

import yaml
from cachetools import TTLCache
from pydantic import BaseModel, Field, ValidationError as PydanticValidationError

from context_core.compression import ensure_utc, format_utc, utc_now
from context_core.exceptions import AuthorizationError, NotFoundError, ValidationError
from context_core.model_boundary import ModelBoundary, model_allowed
from context_core.models import (
    Claim,
    ConflictSet,
    Entity,
    KnowledgeItem,
    Principal,
    WallSide,
)
from context_core.policy import wall_allows
from context_core.receipts import build_receipt
from context_core.vocabulary import VocabularyIndex

PAGE_TEMPLATE_KIND = "page_template"
WILDCARD_TYPE = "*"
SHARED_SCOPES = ("firm", "project", "skill")  # personal (user, user_x_skill) and run scopes never appear on pages

SectionKind = Literal["overview", "facts", "directives", "related", "diagram", "conflicts", "recent_changes"]


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


class SectionSpec(BaseModel):
    """One section of a page template."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    title: str = Field(min_length=1, max_length=120)
    kind: SectionKind
    predicates: list[str] | None = Field(default=None, description="Only these predicates (facts, directives, related, recent_changes)")
    exclude_predicates: list[str] = Field(default_factory=list)
    context_purposes: list[str] | None = None
    domain_nature: Literal["financial", "non_financial"] | None = None
    limit: int = Field(default=25, ge=1, le=200)


class PageTemplate(BaseModel):
    """Parsed page template."""

    template_id: str
    name: str
    entity_types: list[str] = Field(min_length=1)
    sections: list[SectionSpec] = Field(min_length=1)
    source_item_id: str | None = None
    wall_side: WallSide = WallSide.public_side
    version_hash: str = ""

    def outline(self) -> list[dict[str, str]]:
        return [{"id": s.id, "title": s.title, "kind": s.kind} for s in self.sections]


BUILTIN_TEMPLATE = PageTemplate(
    template_id="builtin-default",
    name="Built-in default page",
    entity_types=[WILDCARD_TYPE],
    sections=[
        SectionSpec(id="overview", title="Overview", kind="overview"),
        SectionSpec(id="key-facts", title="Key facts", kind="facts"),
        SectionSpec(id="relationships", title="Relationships", kind="related"),
        SectionSpec(id="diagram", title="Relationship diagram", kind="diagram"),
        SectionSpec(id="conflicts", title="Open conflicts", kind="conflicts"),
        SectionSpec(id="directives", title="Directives", kind="directives"),
        SectionSpec(id="recent-changes", title="Recent changes", kind="recent_changes", limit=10),
    ],
)

_YAML_BLOCK = re.compile(r"```ya?ml\s*\n(.*?)```", re.DOTALL)


def parse_template_body(body_markdown: str) -> dict[str, Any]:
    """Read the template spec from the first fenced YAML block of a template note."""
    m = _YAML_BLOCK.search(body_markdown or "")
    if not m:
        raise ValidationError("A page template needs a fenced ```yaml block with entity_types and sections")
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ValidationError(f"Page template YAML does not parse: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError("Page template YAML must be a mapping")
    return data


def template_from_item(item: KnowledgeItem) -> PageTemplate:
    """Parse a ``page_template`` knowledge item into a PageTemplate (raises ValidationError)."""
    data = parse_template_body(item.body_markdown)
    try:
        return PageTemplate(
            template_id=item.slug,
            name=item.display_name,
            entity_types=[str(t) for t in data.get("entity_types") or []],
            sections=data.get("sections") or [],
            source_item_id=item.item_id,
            wall_side=item.wall_side,
            version_hash=hashlib.sha256((item.body_markdown or "").encode("utf-8")).hexdigest()[:16],
        )
    except PydanticValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        raise ValidationError(f"Page template is invalid at {loc}: {first.get('msg')}") from exc


def render_template_body(entity_types: list[str], sections: list[SectionSpec], *, note: str = "") -> str:
    """Write a template spec back as a note body (used by the template proposal endpoint)."""
    spec = {
        "entity_types": entity_types,
        "sections": [s.model_dump(exclude_none=True, exclude_defaults=False) for s in sections],
    }
    head = note.strip() or "Page template. Edit through a change set; the YAML block below is what the lake reads."
    return f"{head}\n\n```yaml\n{yaml.safe_dump(spec, sort_keys=False, allow_unicode=True)}```\n"


TypeParentLookup = Callable[[str], str | None]
"""Returns the parent of an entity type in the taxonomy, or None at the top.

Kept as a plain function so the editable taxonomy tables can supply it without this module
depending on them. Template lookup walks from the entity's type up through its parents.
"""


def _no_parent(_entity_type: str) -> str | None:
    return None


def type_parent_from_index(index: VocabularyIndex) -> TypeParentLookup:
    """Parent lookup over the entity-type vocabulary; aliases and merged types resolve first."""

    def lookup(entity_type: str) -> str | None:
        term = index.get("entity_type", index.canonical("entity_type", entity_type))
        return term.parent_key if term is not None else None

    return lookup


def resolve_template(
    entity_type: str,
    templates: Sequence[PageTemplate],
    *,
    type_parent: TypeParentLookup = _no_parent,
) -> PageTemplate:
    """Pick the template for an entity type: exact type, then each parent type, then '*', then built-in."""
    by_type: dict[str, PageTemplate] = {}
    for t in sorted(templates, key=lambda x: x.template_id):
        for et in t.entity_types:
            by_type.setdefault(et, t)
    seen: set[str] = set()
    current: str | None = entity_type
    while current and current not in seen:
        seen.add(current)
        if current in by_type:
            return by_type[current]
        current = type_parent(current)
    return by_type.get(WILDCARD_TYPE, BUILTIN_TEMPLATE)


# ---------------------------------------------------------------------------
# Page model
# ---------------------------------------------------------------------------


class Citation(BaseModel):
    """Source of one statement. Evidence location is omitted when the caller may not see evidence."""

    n: int
    claim_id: str
    predicate: str
    source_system: str
    evidence_uri: str | None = None
    span_locator: str | None = None
    evidence_restricted: bool = False
    epistemic_status: str
    valid_from: str | None = None
    recorded_at: str | None = None


class Statement(BaseModel):
    text: str
    cites: list[int] = Field(default_factory=list)
    status: str | None = None


class PageSection(BaseModel):
    id: str
    title: str
    kind: SectionKind
    text: str | None = None
    statements: list[Statement] = Field(default_factory=list)
    mermaid: str | None = None


class EntityPage(BaseModel):
    entity_id: str
    canonical_key: str
    title: str
    entity_type: str
    side: WallSide
    as_of: str | None = None
    recorded_as_of: str | None = None
    template_id: str
    template_version: str
    sections: list[PageSection]
    citations: list[Citation]
    narrator: str
    fingerprint: str
    narration_cached: bool = False
    generated_at: str
    receipt_id: str | None = None


class NumberedStatement(BaseModel):
    n: int
    text: str
    section: str
    status: str | None = None


# ---------------------------------------------------------------------------
# Narrators
# ---------------------------------------------------------------------------


@runtime_checkable
class PageNarrator(Protocol):
    """Writes the overview paragraph and answers from numbered statements."""

    name: str
    boundary: ModelBoundary

    async def overview(self, *, entity: Entity, statements: list[NumberedStatement]) -> str | None: ...

    async def answer(self, *, question: str, statements: list[NumberedStatement]) -> str | None: ...


_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "to", "is", "are", "what", "who", "which", "for", "on", "in", "and", "or", "does", "do", "with", "has", "have", "how", "about", "tell", "me"}


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 1}


def rank_statements(question: str, statements: Sequence[NumberedStatement]) -> list[NumberedStatement]:
    q = _tokens(question)
    scored = [(len(q & _tokens(s.text)), -s.n, s) for s in statements]
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [s for score, _, s in scored if score > 0]


class DeterministicNarrator:
    """Offline writer: no model, no network. Always available and always within the boundary."""

    name = "deterministic"
    boundary: ModelBoundary = "internal"

    async def overview(self, *, entity: Entity, statements: list[NumberedStatement]) -> str | None:
        parts: list[str] = []
        if entity.description.strip():
            parts.append(entity.description.strip().rstrip(".") + ".")
        facts = [s for s in statements if s.section == "facts"]
        facts.sort(key=lambda s: (s.status != "approved_fact", s.n))  # settled facts first
        facts = facts[:3]
        if facts:
            parts.append(" ".join(f"{s.text.rstrip('.')} [{s.n}]." for s in facts))
        n_total = len(statements)
        if n_total:
            parts.append(f"This page draws on {n_total} cited statement{'s' if n_total != 1 else ''}.")
        else:
            parts.append("The lake holds no shared claims about this entity that you can see on this side.")
        return " ".join(parts)

    async def answer(self, *, question: str, statements: list[NumberedStatement]) -> str | None:
        ranked = rank_statements(question, statements)[:6]
        if not ranked:
            return None
        lines = ["The most relevant statements in the lake:"]
        lines += [f"- {s.text.rstrip('.')} [{s.n}]" for s in ranked]
        return "\n".join(lines)


_CITE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


def cited_numbers(text: str) -> list[int]:
    out: list[int] = []
    for m in _CITE.finditer(text):
        out.extend(int(x) for x in re.split(r"\s*,\s*", m.group(1)))
    return out


def citations_valid(text: str, allowed: set[int]) -> bool:
    """Model output must cite, cite only given statements, and cite in every paragraph or bullet."""
    if not text or not text.strip():
        return False
    nums = cited_numbers(text)
    if not nums or any(n not in allowed for n in nums):
        return False
    blocks = [b.strip() for b in re.split(r"\n\s*\n|\n(?=\s*[-*] )", text) if b.strip()]
    content_blocks = [b for b in blocks if not b.endswith(":")]
    return all(_CITE.search(b) for b in content_blocks)


class NarratorConfig:
    """Which narrator writes for each side. The private-side narrator must be internal."""

    def __init__(self, *, public: PageNarrator | None = None, private: PageNarrator | None = None) -> None:
        self.deterministic = DeterministicNarrator()
        self.public = public or self.deterministic
        self.private = private or self.deterministic
        if self.private.boundary != "internal":
            raise ValueError(
                f"Narrator '{self.private.name}' is external; private-side and above-the-wall pages "
                "may only use an internal model"
            )

    def for_side(self, side: WallSide) -> PageNarrator:
        return self.public if side == WallSide.public_side else self.private


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def predicate_label(predicate: str) -> str:
    words = predicate.replace("-", "_").split("_")
    words = [w for w in words if w]
    if words and words[0] in ("has", "is"):
        words = words[1:] or words
    text = " ".join(words)
    return text[:1].upper() + text[1:] if text else predicate


def _date(dt: datetime | None) -> str | None:
    return dt.date().isoformat() if dt else None


def _qualifiers(c: Claim) -> list[str]:
    q: list[str] = []
    f = c.facets
    if f.epistemic_temporal != "actual":
        q.append(f.epistemic_temporal.replace("_", " "))
    if f.fiscal_period:
        q.append(f.fiscal_period)
    if c.context_purpose and c.context_purpose != "general":
        q.append(f"for {c.context_purpose.replace('_', ' ')}")
    if c.epistemic_status != "approved_fact":
        q.append(c.epistemic_status.replace("_", " "))
    return q


def _mermaid_text(label: str) -> str:
    cleaned = re.sub(r"[\r\n]+", " ", label)
    return cleaned.replace('"', "#quot;").replace("<", "#lt;").replace(">", "#gt;")[:80]


def _mermaid_id(raw: str, ids: dict[str, str]) -> str:
    if raw not in ids:
        ids[raw] = f"n{len(ids)}"
    return ids[raw]


def build_mermaid(center: Entity, edges: Sequence[tuple[str, str, str]], names: dict[str, str]) -> str | None:
    """Relationship diagram. ``edges`` are (source_id, predicate, target_id) already filtered for the side."""
    if not edges:
        return None
    ids: dict[str, str] = {}
    lines = ["flowchart LR"]
    cid = _mermaid_id(center.entity_id, ids)
    lines.append(f'    {cid}["{_mermaid_text(center.display_name)}"]:::focus')
    declared = {center.entity_id}
    for src, pred, dst in edges:
        for eid in (src, dst):
            if eid not in declared:
                declared.add(eid)
                lines.append(f'    {_mermaid_id(eid, ids)}["{_mermaid_text(names.get(eid, eid))}"]')
    seen: set[tuple[str, str, str]] = set()
    for src, pred, dst in edges:
        if (src, pred, dst) in seen:
            continue
        seen.add((src, pred, dst))
        lines.append(f'    {_mermaid_id(src, ids)} -->|"{_mermaid_text(predicate_label(pred))}"| {_mermaid_id(dst, ids)}')
    lines.append("    classDef focus stroke-width:3px")
    return "\n".join(lines)


def render_markdown(page: EntityPage) -> str:
    """Render a page as Markdown, with numbered citations as footnotes at the end."""
    side_label = {"public_side": "Public side", "private_side": "Private side", "above_the_wall": "Above the wall"}[page.side.value]
    out = [f"# {page.title}", "", f"_{page.entity_type.replace('_', ' ')} · {side_label} view · generated {page.generated_at}_"]
    for s in page.sections:
        body: list[str] = []
        if s.kind == "overview" and s.text:
            body.append(s.text)
        elif s.kind == "diagram" and s.mermaid:
            body += ["```mermaid", s.mermaid, "```"]
        elif s.statements:
            for st in s.statements:
                cites = "".join(f"[{n}]" for n in st.cites)
                body.append(f"- {st.text} {cites}".rstrip())
        if body:
            out += ["", f"## {s.title}", "", *body]
    if page.citations:
        out += ["", "## Sources", ""]
        for c in page.citations:
            where = c.source_system
            if c.evidence_restricted:
                where += ", evidence restricted"
            elif c.evidence_uri:
                where += f", {c.evidence_uri}" + (f" ({c.span_locator})" if c.span_locator else "")
            out.append(f"{c.n}. `{c.claim_id}` {c.predicate}, {where}, recorded {c.recorded_at or 'unknown'}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def _wall(value: WallSide | str) -> str:
    return value.value if isinstance(value, WallSide) else str(value)


def _on_side(side: WallSide, record_wall: WallSide | str) -> bool:
    return wall_allows(side, record_wall)


class WikiService:
    """Builds entity pages, the page list and cited answers for one caller at a time."""

    def __init__(
        self,
        *,
        get_store: Callable[[], Any],
        get_retrieval: Callable[[], Any] | None = None,
        narrators: NarratorConfig | None = None,
        type_parent: TypeParentLookup | None = None,
        models_enabled: Callable[[], bool] | None = None,
        cache_size: int = 2048,
        cache_ttl_seconds: float = 24 * 3600.0,
    ) -> None:
        self._get_store = get_store
        self._get_retrieval = get_retrieval
        self.narrators = narrators or NarratorConfig()
        # An explicit lookup wins; otherwise the store's entity-type tree (ADR-008) is used per call.
        self._explicit_type_parent = type_parent
        self.type_parent: TypeParentLookup = type_parent or _no_parent
        # Lets the runtime switch every model off (deterministic mode) without rebuilding the service.
        self._models_enabled = models_enabled or (lambda: True)
        self._narration_cache: TTLCache[tuple[str, str, str], str] = TTLCache(maxsize=cache_size, ttl=cache_ttl_seconds)
        self.narrator_calls: dict[str, int] = {}

    @property
    def store(self) -> Any:
        return self._get_store()

    # -- side --------------------------------------------------------------

    @staticmethod
    def resolve_side(principal: Principal, side: str | WallSide | None) -> WallSide:
        """Default to the caller's own side; refuse a side the caller may not see."""
        if side is None or side == "":
            return principal.wall_side
        try:
            ws = WallSide(_wall(side))
        except ValueError as exc:
            raise ValidationError(f"Unknown side '{side}'; use public_side, private_side or above_the_wall") from exc
        if not wall_allows(principal.wall_side, ws):
            raise AuthorizationError("Information barrier: you cannot view pages for that side")
        return ws

    # -- templates ---------------------------------------------------------

    async def load_templates(self, principal: Principal, side: WallSide) -> tuple[list[PageTemplate], list[dict[str, str]]]:
        """Templates the caller may read on this side, plus any that fail to parse."""
        items = await self.store.list_knowledge_items(principal=principal, kind=PAGE_TEMPLATE_KIND)
        good: list[PageTemplate] = []
        bad: list[dict[str, str]] = []
        for item in items:
            if item.knowledge_kind != PAGE_TEMPLATE_KIND or not _on_side(side, item.wall_side):
                continue
            try:
                good.append(template_from_item(item))
            except ValidationError as exc:
                bad.append({"item_id": item.item_id, "error": exc.message})
        return good, bad

    async def type_parent_lookup(self) -> TypeParentLookup:
        """The parent-of-type lookup: the one passed in, else the store's entity-type vocabulary."""
        if self._explicit_type_parent is not None:
            return self._explicit_type_parent
        index_fn = getattr(self.store, "vocabulary_index", None)
        if index_fn is None:
            return _no_parent
        return type_parent_from_index(await index_fn())

    async def template_for(self, entity_type: str, principal: Principal, side: WallSide) -> PageTemplate:
        templates, _ = await self.load_templates(principal, side)
        return resolve_template(entity_type, templates, type_parent=await self.type_parent_lookup())

    # -- structure ---------------------------------------------------------

    async def structure(
        self,
        principal: Principal,
        *,
        side: str | WallSide | None = None,
        entity_types: Sequence[str] | None = None,
        topic_slug: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        """The page list (DeepWiki's 'wiki structure'): pages grouped by entity type, with outlines."""
        ws = self.resolve_side(principal, side)
        entities = await self.store.list_entities(
            principal=principal, entity_types=list(entity_types) if entity_types else None, topic_slug=topic_slug, limit=None
        )
        visible = [e for e in entities if _on_side(ws, e.wall_side) and e.status != "archived"]
        visible.sort(key=lambda e: (e.entity_type, e.display_name.lower()))
        total = len(visible)
        page_slice = visible[offset : offset + limit]
        templates, bad = await self.load_templates(principal, ws)
        type_parent = await self.type_parent_lookup()
        groups: dict[str, dict[str, Any]] = {}
        for e in page_slice:
            et = e.entity_type
            if et not in groups:
                tpl = resolve_template(et, templates, type_parent=type_parent)
                groups[et] = {
                    "entity_type": et,
                    "parent_type": type_parent(et),
                    "template_id": tpl.template_id,
                    "outline": tpl.outline(),
                    "pages": [],
                }
            groups[et]["pages"].append(
                {"entity_id": e.entity_id, "canonical_key": e.canonical_key, "title": e.display_name}
            )
        return {
            "side": ws.value,
            "total": total,
            "offset": offset,
            "limit": limit,
            "groups": list(groups.values()),
            "template_errors": bad,
        }

    # -- page --------------------------------------------------------------

    async def _entity_on_side(self, key: str, principal: Principal, side: WallSide) -> Entity:
        entity = await self.store.get_entity(key, principal=principal)
        if entity is None or not _on_side(side, entity.wall_side):
            raise NotFoundError("Resource not found")
        return entity

    def _keep(self, c: Claim, side: WallSide) -> bool:
        return (
            _on_side(side, c.acl.wall_side)
            and c.scope_type in SHARED_SCOPES
            and c.epistemic_status != "revoked"
        )

    async def build_page(
        self,
        key: str,
        principal: Principal,
        *,
        side: str | WallSide | None = None,
        as_of: datetime | str | None = None,
        recorded_as_of: datetime | str | None = None,
        record_receipt: bool = True,
    ) -> EntityPage:
        ws = self.resolve_side(principal, side)
        eff_as_of = ensure_utc(as_of)
        eff_rec = ensure_utc(recorded_as_of)
        store = self.store
        entity = await self._entity_on_side(key, principal, ws)
        template = await self.template_for(entity.entity_type, principal, ws)
        eid = entity.entity_id

        own = [
            c for c in await store.query_claims(principal=principal, subject_ids=[eid], as_of=eff_as_of, recorded_as_of=eff_rec)
            if self._keep(c, ws)
        ]
        inbound = [
            c for c in await store.query_claims(principal=principal, object_entity_ids=[eid], as_of=eff_as_of, recorded_as_of=eff_rec)
            if self._keep(c, ws) and c.subject_id != eid
        ]
        needs_history = any(s.kind == "recent_changes" for s in template.sections)
        history: list[Claim] = []
        if needs_history:
            history = [
                c for c in await store.query_claims(
                    principal=principal, subject_ids=[eid], as_of=None, recorded_as_of=eff_rec, include_superseded=True
                )
                if self._keep(c, ws)
            ]
        conflicts: list[ConflictSet] = []
        if any(s.kind == "conflicts" for s in template.sections):
            for cf in await store.list_conflicts(principal=principal, status="open", subject_id=eid):
                vis = [c for c in cf.claims if self._keep(c, ws)]
                # Show a conflict only when every claim in it is visible on this side; otherwise its
                # mere existence would hint at hidden material.
                if vis and len(vis) == len(cf.claim_ids) == len(cf.claims):
                    conflicts.append(cf)

        # Names of related entities, each checked for this side.
        names: dict[str, str] = {eid: entity.display_name}
        related_ok: dict[str, bool] = {eid: True}

        async def _related_visible(other_id: str) -> bool:
            if other_id in related_ok:
                return related_ok[other_id]
            other = await store.get_entity(other_id, principal=principal)
            ok = other is not None and _on_side(ws, other.wall_side)
            related_ok[other_id] = ok
            if ok and other is not None:
                names[other_id] = other.display_name
            return ok

        citations: list[Citation] = []
        cite_no: dict[str, int] = {}
        used_claims: dict[str, Claim] = {}

        def _cite(c: Claim) -> int:
            if c.claim_id not in cite_no:
                n = len(citations) + 1
                cite_no[c.claim_id] = n
                used_claims[c.claim_id] = c
                restricted = bool(c.evidence_redacted)
                citations.append(
                    Citation(
                        n=n,
                        claim_id=c.claim_id,
                        predicate=c.predicate,
                        source_system=c.provenance.source_system,
                        evidence_uri=None if restricted else c.provenance.evidence_uri,
                        span_locator=None if restricted else c.provenance.span_locator,
                        evidence_restricted=restricted,
                        epistemic_status=c.epistemic_status,
                        valid_from=_date(c.valid_from),
                        recorded_at=format_utc(c.recorded_at),
                    )
                )
            return cite_no[c.claim_id]

        def _match(spec: SectionSpec, c: Claim) -> bool:
            if spec.predicates is not None and c.predicate not in spec.predicates:
                return False
            if c.predicate in spec.exclude_predicates:
                return False
            if spec.context_purposes is not None and c.context_purpose not in spec.context_purposes:
                return False
            if spec.domain_nature is not None and c.facets.domain_nature != spec.domain_nature:
                return False
            return True

        def _fact_text(c: Claim) -> str:
            value = names.get(c.object_entity_id, c.object_value) if c.object_entity_id else c.object_value
            q = _qualifiers(c)
            return f"{predicate_label(c.predicate)}: {value}" + (f" ({', '.join(q)})" if q else "")

        sections: list[PageSection] = []
        numbered: list[NumberedStatement] = []
        diagram_edges: list[tuple[str, str, str]] = []
        relation_claims_out = [c for c in own if c.object_entity_id]
        relation_claims_in = list(inbound)
        for c in relation_claims_out:
            await _related_visible(c.object_entity_id or "")
        for c in relation_claims_in:
            await _related_visible(c.subject_id)

        overview_spec: SectionSpec | None = None
        for spec in template.sections:
            sec = PageSection(id=spec.id, title=spec.title, kind=spec.kind)
            if spec.kind == "overview":
                overview_spec = spec
            elif spec.kind in ("facts", "directives"):
                channel = "fact" if spec.kind == "facts" else "directive"
                rows = [
                    c for c in own
                    if c.channel == channel and _match(spec, c)
                    and (spec.kind == "directives" or not c.object_entity_id)
                ]
                rows.sort(key=lambda c: (c.predicate, -(c.valid_from.timestamp() if c.valid_from else 0)))
                for c in rows[: spec.limit]:
                    n = _cite(c)
                    sec.statements.append(Statement(text=_fact_text(c), cites=[n], status=c.epistemic_status))
                    numbered.append(NumberedStatement(n=n, text=_fact_text(c), section=spec.kind, status=c.epistemic_status))
            elif spec.kind in ("related", "diagram"):
                rows_out = [c for c in relation_claims_out if _match(spec, c) and related_ok.get(c.object_entity_id or "", False)]
                rows_in = [c for c in relation_claims_in if _match(spec, c) and related_ok.get(c.subject_id, False)]
                if spec.kind == "related":
                    for c in rows_out[: spec.limit]:
                        n = _cite(c)
                        text = f"{predicate_label(c.predicate)} {names[c.object_entity_id or '']}"
                        q = _qualifiers(c)
                        text += f" ({', '.join(q)})" if q else ""
                        sec.statements.append(Statement(text=text, cites=[n], status=c.epistemic_status))
                        numbered.append(NumberedStatement(n=n, text=text, section="related"))
                    for c in rows_in[: max(0, spec.limit - len(rows_out))]:
                        n = _cite(c)
                        text = f"{names[c.subject_id]} {predicate_label(c.predicate).lower()} {entity.display_name}"
                        sec.statements.append(Statement(text=text, cites=[n], status=c.epistemic_status))
                        numbered.append(NumberedStatement(n=n, text=text, section="related"))
                else:
                    for c in rows_out[: spec.limit]:
                        diagram_edges.append((eid, c.predicate, c.object_entity_id or ""))
                    for c in rows_in[: spec.limit]:
                        diagram_edges.append((c.subject_id, c.predicate, eid))
                    sec.mermaid = build_mermaid(entity, diagram_edges, names)
            elif spec.kind == "conflicts":
                for cf in conflicts[: spec.limit]:
                    ns = [_cite(c) for c in cf.claims]
                    values = "; ".join(f"{c.object_value} ({c.provenance.source_system})" for c in cf.claims)
                    text = f"{predicate_label(cf.predicate)} is disputed: {values}"
                    sec.statements.append(Statement(text=text, cites=ns, status="disputed"))
                    numbered.append(NumberedStatement(n=ns[0], text=text, section="conflicts"))
            elif spec.kind == "recent_changes":
                by_id = {c.claim_id: c for c in history}
                prev_of = {c.superseded_by: c for c in history if c.superseded_by}
                rows = [c for c in history if _match(spec, c)]
                rows.sort(key=lambda c: c.recorded_at, reverse=True)
                for c in rows[: spec.limit]:
                    if c.superseded_at is not None and c.superseded_by in by_id:
                        continue  # shown through its replacement
                    if c.object_entity_id and not await _related_visible(c.object_entity_id):
                        continue
                    n = _cite(c)
                    prev = prev_of.get(c.claim_id)
                    when = _date(c.recorded_at)
                    value = names.get(c.object_entity_id, c.object_value) if c.object_entity_id else c.object_value
                    if prev is not None:
                        was = names.get(prev.object_entity_id, prev.object_value) if prev.object_entity_id else prev.object_value
                        text = f"{when}: {predicate_label(c.predicate)} changed to {value} (was {was})"
                    else:
                        text = f"{when}: {predicate_label(c.predicate)} recorded as {value}"
                    sec.statements.append(Statement(text=text, cites=[n], status=c.epistemic_status))
            sections.append(sec)

        # Fingerprint of everything the page depends on; narration is cached against it.
        fp_input = {
            "side": ws.value,
            "template": [template.template_id, template.version_hash],
            "entity": [eid, entity.display_name, entity.description, format_utc(entity.updated_at)],
            "claims": sorted(
                [c.claim_id, format_utc(c.recorded_at), format_utc(c.superseded_at), c.object_value, bool(c.evidence_redacted)]
                for c in used_claims.values()
            ),
            "names": sorted(names.items()),
            "as_of": format_utc(eff_as_of),
            "recorded_as_of": format_utc(eff_rec),
        }
        fingerprint = hashlib.sha256(json.dumps(fp_input, sort_keys=True, default=str).encode("utf-8")).hexdigest()

        narrator_name = "none"
        cached = False
        if overview_spec is not None:
            inputs: list[Any] = [ws, entity, *used_claims.values()]
            text, narrator_name, cached = await self._narrate(
                ws, fingerprint, "overview", inputs, numbered,
                lambda n: n.overview(entity=entity, statements=numbered),
            )
            for sec in sections:
                if sec.kind == "overview":
                    sec.text = text

        receipt_id: str | None = None
        if record_receipt:
            epochs = await store.get_epochs()
            receipt = build_receipt(
                principal=principal,
                epochs=epochs,
                as_of=eff_as_of,
                recorded_as_of=eff_rec,
                claims=list(used_claims.values()),
                conflicts=conflicts,
                tool_or_endpoint="wiki.read_page",
            )
            await store.record_receipt(receipt)
            receipt_id = receipt.receipt_id

        return EntityPage(
            entity_id=eid,
            canonical_key=entity.canonical_key,
            title=entity.display_name,
            entity_type=entity.entity_type,
            side=ws,
            as_of=format_utc(eff_as_of),
            recorded_as_of=format_utc(eff_rec),
            template_id=template.template_id,
            template_version=template.version_hash or "builtin",
            sections=sections,
            citations=citations,
            narrator=narrator_name,
            fingerprint=fingerprint,
            narration_cached=cached,
            generated_at=format_utc(utc_now()) or "",
            receipt_id=receipt_id,
        )

    async def _narrate(
        self,
        side: WallSide,
        fingerprint: str,
        purpose: str,
        inputs: list[Any],
        numbered: list[NumberedStatement],
        call: Callable[[PageNarrator], Awaitable[str | None]],
    ) -> tuple[str, str, bool]:
        """Run the side's narrator if the boundary allows it, else fall back to the deterministic one."""
        allowed = {s.n for s in numbered}
        candidates: list[PageNarrator] = []
        chosen = self.narrators.for_side(side)
        if chosen is not self.narrators.deterministic and self._models_enabled():
            candidates.append(chosen)
        candidates.append(self.narrators.deterministic)
        for narrator in candidates:
            # Second check at call time, independent of configuration: external models only ever
            # receive inputs that are all public-side.
            if not model_allowed(narrator.boundary, inputs):
                continue
            key = (purpose, fingerprint, narrator.name)
            if key in self._narration_cache:
                return self._narration_cache[key], narrator.name, True
            self.narrator_calls[narrator.name] = self.narrator_calls.get(narrator.name, 0) + 1
            try:
                text = await call(narrator)
            except Exception:
                text = None
            if text is None:
                continue
            if narrator is not self.narrators.deterministic and not citations_valid(text, allowed):
                continue
            self._narration_cache[key] = text
            return text, narrator.name, False
        return "", "deterministic", False

    # -- ask ---------------------------------------------------------------

    async def ask(
        self,
        question: str,
        principal: Principal,
        *,
        side: str | WallSide | None = None,
        entity_keys: Sequence[str] | None = None,
        as_of: datetime | str | None = None,
        recorded_as_of: datetime | str | None = None,
        max_pages: int = 3,
    ) -> dict[str, Any]:
        """Answer from pages (DeepWiki's 'ask'): every line cites page statements."""
        if not question or not question.strip():
            raise ValidationError("Ask needs a question")
        ws = self.resolve_side(principal, side)
        keys: list[str] = list(entity_keys or [])
        if not keys and self._get_retrieval is not None:
            keys = await self._find_pages(question, principal, ws, max_pages)
        pages: list[EntityPage] = []
        for k in keys[:max_pages]:
            try:
                pages.append(await self.build_page(k, principal, side=ws, as_of=as_of, recorded_as_of=recorded_as_of, record_receipt=False))
            except NotFoundError:
                continue

        # Renumber statements across pages so citations are unique in the answer.
        numbered: list[NumberedStatement] = []
        sources: list[dict[str, Any]] = []
        claims_used: list[str] = []
        seen_claims: set[str] = set()
        for p in pages:
            by_n = {c.n: c for c in p.citations}
            for sec in p.sections:
                if sec.kind == "recent_changes":
                    continue  # repeats statements already on the page
                for st in sec.statements:
                    if not st.cites:
                        continue
                    key_ids = tuple(by_n[n].claim_id for n in st.cites)
                    if len(key_ids) == 1 and key_ids[0] in seen_claims:
                        continue
                    seen_claims.update(key_ids)
                    n = len(sources) + 1
                    first = by_n[st.cites[0]]
                    sources.append({"n": n, "page": p.canonical_key, "section": sec.title, **first.model_dump(exclude={"n"})})
                    claims_used.append(first.claim_id)
                    numbered.append(NumberedStatement(n=n, text=f"{p.title}: {st.text}", section=sec.kind))

        inputs: list[Any] = [ws]
        for p in pages:
            inputs.append(p.side)
        # Inputs for the boundary check are the walls of every claim behind the statements.
        claim_walls = await self._claim_walls(claims_used, principal)
        inputs += claim_walls
        fp = hashlib.sha256(
            json.dumps([ws.value, question.strip().lower(), [p.fingerprint for p in pages]], sort_keys=True).encode("utf-8")
        ).hexdigest()
        answer, narrator_name, cached = await self._narrate(
            ws, fp, "answer", inputs, numbered, lambda n: n.answer(question=question, statements=numbered)
        )
        if not answer:
            answer = "The lake has no statements on this side that answer that question."
        cited = set(cited_numbers(answer))
        epochs = await self.store.get_epochs()
        receipt = build_receipt(
            principal=principal,
            epochs=epochs,
            as_of=ensure_utc(as_of),
            recorded_as_of=ensure_utc(recorded_as_of),
            claim_ids=sorted({s["claim_id"] for s in sources if s["n"] in cited}),
            tool_or_endpoint="wiki.ask",
        )
        await self.store.record_receipt(receipt)
        return {
            "question": question,
            "side": ws.value,
            "answer_markdown": answer,
            "sources": [s for s in sources if s["n"] in cited],
            "pages": [p.canonical_key for p in pages],
            "narrator": narrator_name,
            "cached": cached,
            "receipt_id": receipt.receipt_id,
        }

    async def _find_pages(self, question: str, principal: Principal, side: WallSide, max_pages: int) -> list[str]:
        """Entities to read for a question: entity hits, and the subjects of matching claims."""
        assert self._get_retrieval is not None
        retrieval = self._get_retrieval()
        words = [t for t in _TOKEN.findall(question.lower()) if t not in _STOP and len(t) > 1]
        queries = [" ".join(words)] if words else []
        if len(words) > 3:
            queries.append(" ".join(words[-3:]))  # names tend to come last in a question
        keys: list[str] = []
        for q in queries:
            for h in await retrieval.search(q, principal=principal, limit=20):
                if not _on_side(side, h.wall_side):
                    continue
                eid: str | None = None
                if h.hit_type == "entity":
                    eid = h.id
                elif h.hit_type == "claim":
                    c = await self.store.get_claim(h.id, principal=principal)
                    eid = c.subject_id if c is not None and self._keep(c, side) else None
                if eid and eid not in keys:
                    keys.append(eid)
                if len(keys) >= max_pages:
                    return keys
        return keys

    async def _claim_walls(self, claim_ids: Sequence[str], principal: Principal) -> list[Any]:
        walls: list[Any] = []
        for cid in dict.fromkeys(claim_ids):
            c = await self.store.get_claim(cid, principal=principal)
            walls.append(c if c is not None else None)  # None fails the external check closed
        return walls
