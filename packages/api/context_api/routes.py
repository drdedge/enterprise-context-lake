"""Complete /api/v1 REST, SSE, and AX Discovery routes matching ADR-005 & ADR-003."""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from context_agents.curator import BackgroundCurator
from context_agents.viewport_agent import ViewportContext, ViewportGroundedAgent
from context_core.compression import canonical_json, ensure_utc
from context_core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    NotFoundError,
    SurfacePolicyError,
    ValidationError,
)
from context_core.models import (
    ClaimCreateInput,
    ClaimFacets,
    ClaimUpdateInput,
    ConflictResolutionInput,
    ContextBundleQuery,
    GraphLens,
    MemoryUpsertInput,
    Principal,
    SearchFilter,
    Topic,
    TopicPatchInput,
    WatchlistUpdateInput,
)
from context_core.policy import sanitize_uri_secrets, validate_no_ssrf
from context_core.receipts import build_receipt
from context_mcp.security import validate_safe_uri

from context_api.auth import parse_if_match_epoch
from context_api.discovery import (
    build_agents_manifest,
    render_bundle_markdown,
    render_claim_markdown,
    render_entity_detail_markdown,
    render_llms_txt,
)

router = APIRouter()

_SLUG_CLEAN_RE = re.compile(r"[^a-z0-9-]+")


def _wants_markdown(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    return "text/markdown" in accept and "application/json" not in accept


def _get_ctx(request: Request) -> Any:
    return request.app.state.runtime


async def _current_principal(request: Request) -> Principal:
    ctx = _get_ctx(request)
    return await ctx.auth.resolve_principal(request.headers, store=ctx.registry.store)


def _is_steward(principal: Principal) -> bool:
    if principal.revoked:
        return False
    return bool(
        principal.groups.intersection({"fund-stewards", "compliance-oversight"})
        or principal.roles.intersection({"fund_steward", "compliance_reviewer", "managing_director"})
    )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return ensure_utc(value)


def _effective_recorded(recorded_as_of: str | None, recorded_at: str | None) -> datetime | None:
    return _parse_dt(recorded_as_of or recorded_at)


def _normalize_lens(lens: str) -> GraphLens:
    low = (lens or "topic").strip().lower()
    valid = {
        "topic",
        "app",
        "skill",
        "project",
        "issuer",
        "fund_hierarchy",
        "entity",
        "app_skill",
        "personal",
    }
    if low not in valid:
        return "topic"
    return low  # type: ignore[return-value]


def _normalize_direction(direction: str) -> str:
    d = (direction or "both").strip().lower()
    if d == "forward":
        return "out"
    if d == "reverse":
        return "in"
    if d not in ("both", "in", "out"):
        return "both"
    return d


# ---------------------------------------------------------------------------
# Request Body DTOs
# ---------------------------------------------------------------------------


class AdapterSwapBody(BaseModel):
    store: Literal["sqlite_bitemporal", "okf_markdown_vault"] | None = None
    retrieval: Literal["lexical_manifest", "facet_graph"] | None = None
    llm_mode: Literal["auto", "gemini", "deterministic"] | None = None


class TopicCreateBody(BaseModel):
    slug: str
    pref_label: str
    alt_labels: list[str] = Field(default_factory=list)
    broader_topic_id: str | None = None
    related_topic_ids: list[str] = Field(default_factory=list)
    topic_kind: Literal["curated_core", "emergent_candidate", "merged_alias"] = "emergent_candidate"
    owner_group: str = "fund-stewards"


class ContextBundleBody(BaseModel):
    entity_keys: list[str] = Field(default_factory=list)
    skill_id: str | None = None
    project_id: str | None = None
    run_id: str | None = None
    context_purpose: str | None = None
    predicates: list[str] | None = None
    include_shared: bool = True
    include_unverified: bool = False
    as_of: datetime | None = None
    recorded_as_of: datetime | None = None
    recorded_at: datetime | None = None
    snapshot_version: int | None = None


class ConflictResolveBody(BaseModel):
    winning_claim_id: str | None = None
    replacement_object_value: str | None = None
    valid_from: datetime | None = None
    close_prior_valid_to: datetime | None = None
    rationale: str = "Steward resolution"
    expected_data_epoch: int | None = None


class MemoryCompareBody(BaseModel):
    as_of: datetime | None = None


class MemoryPromoteBody(BaseModel):
    subject_canonical_key: str | None = None
    predicate: str = "promoted_observation"
    rationale: str | None = None
    target_scope: Literal["firm", "project"] = "firm"
    project_id: str | None = None


class MemoryUpdateBody(BaseModel):
    scope_type: Literal["user", "user_x_skill", "run"] | None = None
    skill_id: str | None = None
    key: str | None = None
    content: str | None = None
    tags: list[str] | None = None
    linked_entity_ids: list[str] | None = None
    linked_topic_ids: list[str] | None = None


class AgentChatBody(BaseModel):
    message: str | None = None
    query: str | None = None
    viewport: ViewportContext = Field(default_factory=ViewportContext)


class SnapshotCompileBody(BaseModel):
    acl_tier: str = "firm-all"
    wall_side: Literal["public_side", "private_side", "above_the_wall"] = "public_side"


class CuratorRunBody(BaseModel):
    wall_side: Literal["public_side", "private_side", "above_the_wall"] | None = None


class InboxActionBody(BaseModel):
    rationale: str | None = None
    expected_data_epoch: int | None = None


# ---------------------------------------------------------------------------
# Health & AX Discovery
# ---------------------------------------------------------------------------


async def _health_payload(request: Request) -> dict[str, Any]:
    ctx = _get_ctx(request)
    epochs = await ctx.registry.store.get_epochs()
    latest_snapshots: list[dict[str, Any]] = []
    if ctx.registry.snapshots is not None:
        for tier, ws in (("firm-all", "public_side"), ("private-credit-deal-team", "private_side")):
            try:
                manifest = await ctx.registry.snapshots.get_latest_manifest(tier, ws)
                if manifest is not None:
                    latest_snapshots.append(manifest.model_dump(mode="json"))
            except Exception:
                continue
    return {
        "status": "ok",
        "data_epoch": epochs.data_epoch,
        "policy_epoch": epochs.policy_epoch,
        "active_adapters": {
            "store": ctx.registry.active_store_name,
            "retrieval": ctx.registry.active_retrieval_name,
            "llm_mode": ctx.registry.llm_mode,
        },
        "latest_snapshots": latest_snapshots,
    }


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    return await _health_payload(request)


@router.get("/api/v1/health")
async def api_health(request: Request) -> dict[str, Any]:
    return await _health_payload(request)


async def _public_skills_catalog(request: Request) -> list[Any]:
    ctx = _get_ctx(request)
    pub_principal = Principal(
        user_id="system:discovery",
        display_name="Public Discovery",
        email="discovery@meridian.local",
        roles=frozenset({"reader"}),
        groups=frozenset({"firm-all"}),
        actor_mode="service",
        channel_mode="shared",
    )
    return await ctx.registry.store.list_knowledge_items(
        principal=pub_principal, kind="skill_procedure"
    )


@router.get("/llms.txt", response_class=PlainTextResponse)
async def llms_txt(request: Request) -> PlainTextResponse:
    skills = await _public_skills_catalog(request)
    return PlainTextResponse(content=render_llms_txt(skills), media_type="text/plain; charset=utf-8")


@router.get("/agents")
async def agents_manifest(request: Request) -> dict[str, Any]:
    skills = await _public_skills_catalog(request)
    return build_agents_manifest(skills)


# ---------------------------------------------------------------------------
# Identity, Personas & Runtime Adapter Config
# ---------------------------------------------------------------------------


@router.get("/api/v1/me")
async def get_me(
    request: Request, principal: Principal = Depends(_current_principal)
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    return {
        "user_id": principal.user_id,
        "display_name": principal.display_name,
        "email": principal.email,
        "roles": sorted(principal.roles),
        "groups": sorted(principal.groups),
        "wall_side": principal.wall_side.value,
        "actor_mode": principal.actor_mode,
        "channel_mode": principal.channel_mode,
        "auth_mode": principal.auth_mode,
        "groups_overage_resolved": principal.groups_overage_resolved,
        "memory_enabled": ctx.registry.policy.is_memory_allowed(principal),
    }


@router.get("/api/v1/personas")
async def list_personas(request: Request) -> dict[str, Any]:
    ctx = _get_ctx(request)
    if os.environ.get("ENVIRONMENT", "").lower() == "production" or ctx.auth.auth_mode == "entra_jwt":
        raise AuthorizationError("Local mock personas endpoint is disabled in production / Entra JWT mode")
    return {"personas": ctx.auth.directory.list_personas()}


@router.get("/api/v1/config/adapters")
async def get_adapters_config(
    request: Request, principal: Principal = Depends(_current_principal)
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    return ctx.registry.describe()


@router.put("/api/v1/config/adapters")
async def put_adapters_config(
    body: AdapterSwapBody,
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    result = await ctx.registry.swap(
        store=body.store,
        retrieval=body.retrieval,
        llm_mode=body.llm_mode,
    )
    ctx.bundle_cache.clear()
    ctx.events.publish(
        "epoch_updated",
        {
            "reason": "adapter_swap",
            "store": result["store"],
            "retrieval": result["retrieval"],
            "llm_mode": result["llm_mode"],
            "data_epoch": result["data_epoch"],
            "policy_epoch": result["policy_epoch"],
        },
    )
    return result


# ---------------------------------------------------------------------------
# Home Stream, Entities & Global Search (Strictly Post-ACL)
# ---------------------------------------------------------------------------


@router.get("/api/v1/stream")
async def get_stream(
    request: Request,
    topic: str | None = None,
    entity_type: str | None = None,
    conflicts_only: bool = False,
    as_of: str | None = None,
    recorded_as_of: str | None = None,
    recorded_at: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    store = ctx.registry.store
    eff_as_of = _parse_dt(as_of)
    eff_rec = _effective_recorded(recorded_as_of, recorded_at)

    entities = await store.list_entities(
        principal=principal,
        entity_types=[entity_type] if entity_type else None,
        topic_slug=topic,
        limit=None,
        offset=0,
    )
    claims = await store.query_claims(
        principal=principal,
        as_of=eff_as_of,
        recorded_as_of=eff_rec,
    )
    open_conflicts = await store.list_conflicts(principal=principal, status="open")
    attachments = await store.list_topic_attachments(principal=principal)
    topics_by_id = {t.topic_id: t.slug for t in await store.list_topics(principal=principal)}
    bindings = await store.list_skill_bindings()
    positions = await store.list_positions(principal=principal)
    coverages = await store.list_coverages(principal=principal)

    auth_eids = {e.entity_id for e in entities}
    edge_counts: dict[str, int] = {e.entity_id: 0 for e in entities}
    for c in claims:
        if c.subject_id in auth_eids:
            edge_counts[c.subject_id] += 1
        if c.object_entity_id and c.object_entity_id in auth_eids:
            edge_counts[c.object_entity_id] += 1
    for p in positions:
        for eid in (p.vehicle_entity_id, p.instrument_entity_id, p.issuer_entity_id):
            if eid in auth_eids:
                edge_counts[eid] += 1
    for cov in coverages:
        if cov.issuer_entity_id in auth_eids:
            edge_counts[cov.issuer_entity_id] += 1

    skill_counts: dict[str, int] = {e.entity_id: 0 for e in entities}
    for sb in bindings:
        if sb.active and sb.skill_entity_id in auth_eids and sb.target_entity_id in auth_eids:
            skill_counts[sb.target_entity_id] += 1
            edge_counts[sb.target_entity_id] += 1
            edge_counts[sb.skill_entity_id] += 1

    conflict_counts: dict[str, int] = {e.entity_id: 0 for e in entities}
    for cf in open_conflicts:
        if cf.subject_id in auth_eids:
            conflict_counts[cf.subject_id] += 1

    topics_for_ent: dict[str, list[str]] = {}
    for att in attachments:
        slug = topics_by_id.get(att.topic_id)
        if slug and att.target_id in auth_eids:
            topics_for_ent.setdefault(att.target_id, []).append(slug)

    obs_by_ent: dict[str, int] = {e.entity_id: 0 for e in entities}
    domain_by_ent: dict[str, str] = {}
    cov_by_ent: dict[str, str] = {cov.issuer_entity_id: cov.coverage_status for cov in coverages}
    total_obs = 0
    for c in claims:
        if c.subject_id in auth_eids:
            if c.facets.domain_nature == "financial" or c.subject_id not in domain_by_ent:
                domain_by_ent[c.subject_id] = c.facets.domain_nature
        if c.epistemic_status in ("unverified_observation", "candidate_sop"):
            total_obs += 1
            if c.subject_id in auth_eids:
                obs_by_ent[c.subject_id] += 1

    stream_items: list[dict[str, Any]] = []
    for e in entities:
        oc = conflict_counts.get(e.entity_id, 0)
        if conflicts_only and oc == 0:
            continue
        status_badge = (
            "disputed"
            if oc > 0
            else ("unverified_observation" if obs_by_ent.get(e.entity_id, 0) > 0 else "approved_fact")
        )
        stream_items.append(
            {
                "id": e.entity_id,
                "entity_id": e.entity_id,
                "canonical_key": e.canonical_key,
                "display_name": e.display_name,
                "entity_type": e.entity_type.value,
                "description": e.description,
                "wall_side": e.wall_side.value,
                "edge_count": edge_counts.get(e.entity_id, 0),
                "skill_count": skill_counts.get(e.entity_id, 0),
                "open_conflict_count": oc,
                "has_conflict": oc > 0,
                "observation_count": obs_by_ent.get(e.entity_id, 0),
                "epistemic_status": status_badge,
                "epistemic_badge": status_badge,
                "domain_nature": domain_by_ent.get(e.entity_id, "non_financial"),
                "coverage_status": cov_by_ent.get(e.entity_id),
                "topics": sorted(set(topics_for_ent.get(e.entity_id, []))),
                "updated_at": e.updated_at.isoformat().replace("+00:00", "Z"),
            }
        )

    # Sort conflicts first, then higher degree, then canonical_key for calm scannability
    stream_items.sort(
        key=lambda item: (-item["open_conflict_count"], -item["edge_count"], item["canonical_key"])
    )
    paged = stream_items[offset : offset + limit]
    epochs = await store.get_epochs()
    return {
        "items": paged,
        "counts": {
            "entities": len(stream_items),
            "open_conflicts": len(open_conflicts),
            "observations": total_obs,
        },
        "data_epoch": epochs.data_epoch,
        "policy_epoch": epochs.policy_epoch,
    }


@router.get("/api/v1/entities")
async def list_entities(
    request: Request,
    entity_type: str | None = None,
    topic: str | None = None,
    domain_nature: Literal["financial", "non_financial"] | None = None,
    modality: Literal["quantitative", "qualitative"] | None = None,
    epistemic_temporal: Literal["actual", "estimate", "forecast", "guidance", "pro_forma"] | None = None,
    actual_stage: Literal["prelim", "unaudited", "audited", "restated"] | None = None,
    ingestion_channel: Literal["automated_feed", "manual_upload", "human_authored", "agent_extracted"] | None = None,
    coverage_status: str | None = None,
    conflicts_only: bool = False,
    q: str | None = None,
    as_of: str | None = None,
    recorded_as_of: str | None = None,
    recorded_at: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    store = ctx.registry.store
    eff_as_of = _parse_dt(as_of)
    eff_rec = _effective_recorded(recorded_as_of, recorded_at)

    entities = await store.list_entities(
        principal=principal,
        entity_types=[entity_type] if entity_type else None,
        topic_slug=topic,
        limit=None,
        offset=0,
    )
    claims = await store.query_claims(
        principal=principal,
        as_of=eff_as_of,
        recorded_as_of=eff_rec,
    )
    open_conflicts = await store.list_conflicts(principal=principal, status="open")
    coverages = await store.list_coverages(principal=principal)
    attachments = await store.list_topic_attachments(principal=principal)
    topics_by_id = {t.topic_id: t.slug for t in await store.list_topics(principal=principal)}

    claims_by_subj: dict[str, list[Any]] = {}
    for c in claims:
        claims_by_subj.setdefault(c.subject_id, []).append(c)

    conflicts_by_subj: dict[str, int] = {}
    for cf in open_conflicts:
        conflicts_by_subj[cf.subject_id] = conflicts_by_subj.get(cf.subject_id, 0) + 1

    cov_by_issuer: dict[str, str] = {}
    for cov in coverages:
        cov_by_issuer[cov.issuer_entity_id] = cov.coverage_status

    topics_by_ent: dict[str, list[str]] = {}
    for att in attachments:
        slug = topics_by_id.get(att.topic_id)
        if slug:
            topics_by_ent.setdefault(att.target_id, []).append(slug)

    # Compute strictly post-ACL facet counts over authorized claims/entities BEFORE facet narrowing
    facet_counts: dict[str, dict[str, int]] = {
        "domain_nature": {},
        "modality": {},
        "epistemic_temporal": {},
        "ingestion_channel": {},
        "entity_type": {},
        "coverage_status": {},
    }
    auth_eids = {e.entity_id for e in entities}
    for e in entities:
        et = e.entity_type.value
        facet_counts["entity_type"][et] = facet_counts["entity_type"].get(et, 0) + 1
        cs = cov_by_issuer.get(e.entity_id)
        if cs:
            facet_counts["coverage_status"][cs] = facet_counts["coverage_status"].get(cs, 0) + 1
    for c in claims:
        if c.subject_id not in auth_eids:
            continue
        facet_counts["domain_nature"][c.facets.domain_nature] = (
            facet_counts["domain_nature"].get(c.facets.domain_nature, 0) + 1
        )
        facet_counts["modality"][c.facets.modality] = (
            facet_counts["modality"].get(c.facets.modality, 0) + 1
        )
        facet_counts["epistemic_temporal"][c.facets.epistemic_temporal] = (
            facet_counts["epistemic_temporal"].get(c.facets.epistemic_temporal, 0) + 1
        )
        facet_counts["ingestion_channel"][c.facets.ingestion_channel] = (
            facet_counts["ingestion_channel"].get(c.facets.ingestion_channel, 0) + 1
        )

    def _claim_matches_facets(c: Any) -> bool:
        if domain_nature and c.facets.domain_nature != domain_nature:
            return False
        if modality and c.facets.modality != modality:
            return False
        if epistemic_temporal and c.facets.epistemic_temporal != epistemic_temporal:
            return False
        if actual_stage and c.facets.actual_stage != actual_stage:
            return False
        if ingestion_channel and c.facets.ingestion_channel != ingestion_channel:
            return False
        return True

    has_claim_facet = any(
        x is not None for x in (domain_nature, modality, epistemic_temporal, actual_stage, ingestion_channel)
    )
    q_low = (q or "").strip().lower()

    filtered: list[dict[str, Any]] = []
    for e in entities:
        ent_claims = claims_by_subj.get(e.entity_id, [])
        if has_claim_facet and not any(_claim_matches_facets(c) for c in ent_claims):
            continue
        if coverage_status and cov_by_issuer.get(e.entity_id) != coverage_status:
            continue
        oc = conflicts_by_subj.get(e.entity_id, 0)
        if conflicts_only and oc == 0:
            continue
        if q_low:
            hay = " ".join([e.display_name, e.canonical_key, e.description, *e.aliases]).lower()
            if q_low not in hay and not any(q_low in c.object_value.lower() or q_low in c.predicate.lower() for c in ent_claims):
                continue

        dom_nat = (
            "financial"
            if any(c.facets.domain_nature == "financial" for c in ent_claims)
            else ("non_financial" if ent_claims else None)
        )
        mod = ent_claims[0].facets.modality if ent_claims else None
        ep_temp = ent_claims[0].facets.epistemic_temporal if ent_claims else None
        ing_ch = ent_claims[0].facets.ingestion_channel if ent_claims else None
        filtered.append(
            {
                "entity_id": e.entity_id,
                "canonical_key": e.canonical_key,
                "display_name": e.display_name,
                "entity_type": e.entity_type.value,
                "description": e.description,
                "aliases": e.aliases,
                "wall_side": e.wall_side.value,
                "status": e.status,
                "owner_group": e.owner_group,
                "topics": sorted(set(topics_by_ent.get(e.entity_id, []))),
                "claim_count": len(ent_claims),
                "edge_count": len(ent_claims),
                "open_conflict_count": oc,
                "has_conflict": oc > 0,
                "epistemic_badge": "disputed" if oc > 0 else "approved_fact",
                "coverage_status": cov_by_issuer.get(e.entity_id),
                "domain_nature": dom_nat,
                "modality": mod,
                "epistemic_temporal": ep_temp,
                "ingestion_channel": ing_ch,
                "updated_at": e.updated_at.isoformat().replace("+00:00", "Z"),
            }
        )

    total = len(filtered)
    paged = filtered[offset : offset + limit]
    epochs = await store.get_epochs()
    return {
        "items": paged,
        "total": total,
        "facet_counts": facet_counts,
        "data_epoch": epochs.data_epoch,
        "policy_epoch": epochs.policy_epoch,
    }


@router.get("/api/v1/entities/{id_or_key:path}")
async def get_entity_detail(
    id_or_key: str,
    request: Request,
    as_of: str | None = None,
    recorded_as_of: str | None = None,
    recorded_at: str | None = None,
    principal: Principal = Depends(_current_principal),
) -> Any:
    ctx = _get_ctx(request)
    store = ctx.registry.store
    eff_as_of = _parse_dt(as_of)
    eff_rec = _effective_recorded(recorded_as_of, recorded_at)

    entity = await store.get_entity(id_or_key, principal=principal)
    if entity is None:
        raise NotFoundError("Resource not found")

    claims = await store.query_claims(
        principal=principal,
        subject_ids=[entity.entity_id],
        as_of=eff_as_of,
        recorded_as_of=eff_rec,
    )
    incoming_claims = await store.query_claims(
        principal=principal,
        object_entity_ids=[entity.entity_id],
        as_of=eff_as_of,
        recorded_as_of=eff_rec,
    )
    identifiers = await store.list_identifiers(
        entity.entity_id, as_of=eff_as_of, recorded_as_of=eff_rec
    )
    lineage = await store.list_lineage_events(entity.entity_id, principal=principal)
    conflicts = await store.list_conflicts(
        principal=principal, status="open", subject_id=entity.entity_id
    )
    positions = await store.list_positions(
        principal=principal,
        vehicle_id=entity.entity_id if entity.entity_type.value == "legal_vehicle" else None,
        issuer_id=entity.entity_id if entity.entity_type.value == "issuer" else None,
    )
    if entity.entity_type.value not in ("legal_vehicle", "issuer"):
        positions = []
    coverages = (
        await store.list_coverages(principal=principal, issuer_id=entity.entity_id)
        if entity.entity_type.value == "issuer"
        else []
    )
    all_bindings = await store.list_skill_bindings()
    skill_bindings_dtos: list[dict[str, Any]] = []
    for sb in all_bindings:
        if sb.target_entity_id == entity.entity_id or sb.skill_entity_id == entity.entity_id:
            sk_ent = await store.get_entity(sb.skill_entity_id, principal=principal)
            item = sb.model_dump(mode="json")
            if sk_ent is not None:
                item["skill_name"] = sk_ent.display_name
                item["skill_canonical_key"] = sk_ent.canonical_key
            skill_bindings_dtos.append(item)
    attachments = await store.list_topic_attachments(principal=principal, target_id=entity.entity_id)
    all_topics = {t.topic_id: t for t in await store.list_topics(principal=principal)}
    topics = [all_topics[a.topic_id].model_dump(mode="json") for a in attachments if a.topic_id in all_topics]

    backlinks: list[dict[str, Any]] = []
    for inc in incoming_claims:
        subj = await store.get_entity(inc.subject_id, principal=principal)
        if subj is None:
            continue
        backlinks.append(
            {
                "claim_id": inc.claim_id,
                "entity_id": subj.entity_id,
                "canonical_key": subj.canonical_key,
                "display_name": subj.display_name,
                "entity_type": subj.entity_type.value,
                "source_entity_id": subj.entity_id,
                "source_canonical_key": subj.canonical_key,
                "source_display_name": subj.display_name,
                "source_entity_type": subj.entity_type.value,
                "predicate": inc.predicate,
                "direction": "in",
                "context_purpose": inc.context_purpose,
                "epistemic_status": inc.epistemic_status,
            }
        )

    epochs = await store.get_epochs()
    receipt = build_receipt(
        principal=principal,
        epochs=epochs,
        as_of=eff_as_of,
        recorded_as_of=eff_rec,
        claims=claims,
        conflicts=conflicts,
        tool_or_endpoint="api.get_entity",
    )
    await store.record_receipt(receipt)

    detail = {
        "entity": entity.model_dump(mode="json"),
        "identifiers": [i.model_dump(mode="json") for i in identifiers],
        "lineage": [ev.model_dump(mode="json") for ev in lineage],
        "facts": [c.model_dump(mode="json") for c in claims if c.channel == "fact"],
        "directives": [c.model_dump(mode="json") for c in claims if c.channel == "directive"],
        "conflicts": [cf.model_dump(mode="json") for cf in conflicts],
        "positions": [p.model_dump(mode="json") for p in positions],
        "coverages": [cv.model_dump(mode="json") for cv in coverages],
        "skill_bindings": skill_bindings_dtos,
        "topics": topics,
        "backlinks": backlinks,
        "receipt_id": receipt.receipt_id,
    }
    if _wants_markdown(request):
        return PlainTextResponse(
            content=render_entity_detail_markdown(detail),
            media_type="text/markdown; charset=utf-8",
        )
    return detail


@router.get("/api/v1/search")
async def search_lake(
    request: Request,
    q: str = "",
    entity_type: str | None = None,
    topic: str | None = None,
    domain_nature: Literal["financial", "non_financial"] | None = None,
    modality: Literal["quantitative", "qualitative"] | None = None,
    epistemic_temporal: Literal["actual", "estimate", "forecast", "guidance", "pro_forma"] | None = None,
    ingestion_channel: Literal["automated_feed", "manual_upload", "human_authored", "agent_extracted"] | None = None,
    as_of: str | None = None,
    recorded_as_of: str | None = None,
    recorded_at: str | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    eff_as_of = _parse_dt(as_of)
    eff_rec = _effective_recorded(recorded_as_of, recorded_at)
    flt = SearchFilter(
        entity_types=[entity_type] if entity_type else None,
        topic_slug=topic,
        domain_nature=domain_nature,
        modality=modality,
        epistemic_temporal=epistemic_temporal,
        ingestion_channel=ingestion_channel,
        as_of=eff_as_of,
        recorded_as_of=eff_rec,
        limit=limit,
    )
    hits = await ctx.registry.retrieval.search(q, flt=flt, principal=principal, limit=limit)
    epochs = await ctx.registry.store.get_epochs()
    receipt = build_receipt(
        principal=principal,
        epochs=epochs,
        as_of=eff_as_of,
        recorded_as_of=eff_rec,
        claim_ids=[h.id for h in hits if h.hit_type == "claim"],
        redacted_evidence_claim_ids=[h.id for h in hits if h.hit_type == "claim" and h.evidence_redacted],
        memory_ids=[h.id for h in hits if h.hit_type == "memory"],
        tool_or_endpoint="api.search",
    )
    await ctx.registry.store.record_receipt(receipt)
    hits_out: list[dict[str, Any]] = []
    for h in hits:
        dumped = h.model_dump(mode="json")
        dumped["kind"] = h.hit_type
        dumped["title"] = h.display_name
        dumped["subtitle"] = h.summary or h.snippet or ""
        hits_out.append(dumped)
    return {
        "hits": hits_out,
        "total": len(hits_out),
        "receipt_id": receipt.receipt_id,
    }


# ---------------------------------------------------------------------------
# Dynamic Hierarchical SKOS Topics CRUD (Strictly Post-ACL Counts)
# ---------------------------------------------------------------------------


@router.get("/api/v1/topics")
async def list_topics(
    request: Request,
    include_archived: bool = False,
    as_of: str | None = None,
    recorded_as_of: str | None = None,
    recorded_at: str | None = None,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    store = ctx.registry.store
    eff_as_of = _parse_dt(as_of)
    eff_rec = _effective_recorded(recorded_as_of, recorded_at)

    topics = await store.list_topics(principal=principal, include_archived=include_archived)
    attachments = await store.list_topic_attachments(principal=principal)
    auth_eids = {e.entity_id for e in await store.list_entities(principal=principal, limit=None)}
    auth_cids = {
        c.claim_id
        for c in await store.query_claims(
            principal=principal, as_of=eff_as_of, recorded_as_of=eff_rec
        )
    }
    auth_kids = {k.item_id for k in await store.list_knowledge_items(principal=principal)}

    counts_map: dict[str, dict[str, int]] = {
        t.topic_id: {"entity_count": 0, "claim_count": 0, "artifact_count": 0, "authorized_item_count": 0}
        for t in topics
    }
    for att in attachments:
        if att.topic_id not in counts_map:
            continue
        if att.target_type == "claim":
            if att.target_id not in auth_cids:
                continue
            counts_map[att.topic_id]["claim_count"] += 1
            counts_map[att.topic_id]["authorized_item_count"] += 1
        elif att.target_type == "artifact":
            if att.target_id not in auth_kids:
                continue
            counts_map[att.topic_id]["artifact_count"] += 1
            counts_map[att.topic_id]["authorized_item_count"] += 1
        else:
            if att.target_id not in auth_eids:
                continue
            counts_map[att.topic_id]["entity_count"] += 1
            counts_map[att.topic_id]["authorized_item_count"] += 1

    topic_dtos: list[dict[str, Any]] = []
    dto_by_id: dict[str, dict[str, Any]] = {}
    for t in topics:
        c = counts_map[t.topic_id]
        item = {
            **t.model_dump(mode="json"),
            "topic": t.model_dump(mode="json"),
            "item_count": c["authorized_item_count"],
            **c,
        }
        topic_dtos.append(item)
        dto_by_id[t.topic_id] = item

    # Build hierarchical tree rooted at top-level topics (broader_topic_id is None)
    children_map: dict[str, list[str]] = {}
    roots: list[str] = []
    for t in topics:
        if t.broader_topic_id and t.broader_topic_id in dto_by_id:
            children_map.setdefault(t.broader_topic_id, []).append(t.topic_id)
        else:
            roots.append(t.topic_id)

    def _build_node(tid: str, seen: set[str]) -> dict[str, Any]:
        seen.add(tid)
        base = dto_by_id[tid]
        kids = [
            _build_node(cid, seen)
            for cid in children_map.get(tid, [])
            if cid not in seen
        ]
        subtree_total = base["authorized_item_count"] + sum(k["subtree_item_count"] for k in kids)
        return {
            **base,
            "subtree_item_count": subtree_total,
            "children": kids,
        }

    tree = [_build_node(rid, set()) for rid in roots]
    return {"topics": topic_dtos, "tree": tree}


@router.post("/api/v1/topics", status_code=201)
async def create_topic(
    body: TopicCreateBody,
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    store = ctx.registry.store
    norm_slug = _SLUG_CLEAN_RE.sub("-", body.slug.strip().lower()).strip("-")
    if not norm_slug:
        raise ValidationError("Topic slug must contain alphanumeric characters")

    topic = Topic(
        topic_id=f"topic:{norm_slug}",
        slug=norm_slug,
        pref_label=body.pref_label.strip(),
        alt_labels=body.alt_labels,
        broader_topic_id=body.broader_topic_id,
        related_topic_ids=body.related_topic_ids,
        topic_kind=body.topic_kind,
        owner_group=body.owner_group,
        status="active",
    )
    saved = await store.upsert_topic(topic)

    # Link into parent topic's narrower_topic_ids if broader_topic_id exists
    if body.broader_topic_id:
        all_topics = await store.list_topics(principal=principal, include_archived=True)
        parent = next((t for t in all_topics if t.topic_id == body.broader_topic_id), None)
        if parent is not None and saved.topic_id not in parent.narrower_topic_ids:
            parent.narrower_topic_ids = [*parent.narrower_topic_ids, saved.topic_id]
            await store.upsert_topic(parent)

    epochs = await store.get_epochs()
    ctx.bundle_cache.clear()
    ctx.events.publish("epoch_updated", {"reason": "topic_created", "data_epoch": epochs.data_epoch})
    return {"topic": saved.model_dump(mode="json"), "data_epoch": epochs.data_epoch}


@router.patch("/api/v1/topics/{topic_id:path}")
async def patch_topic(
    topic_id: str,
    body: TopicPatchInput,
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    store = ctx.registry.store
    updated = await store.update_topic(topic_id, body, principal=principal)
    epochs = await store.get_epochs()
    ctx.bundle_cache.clear()
    ctx.events.publish("epoch_updated", {"reason": "topic_updated", "data_epoch": epochs.data_epoch})
    return {"topic": updated.model_dump(mode="json"), "data_epoch": epochs.data_epoch}


@router.get("/api/v1/topics/{slug}/items")
async def get_topic_items(
    slug: str,
    request: Request,
    as_of: str | None = None,
    recorded_as_of: str | None = None,
    recorded_at: str | None = None,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    store = ctx.registry.store
    eff_as_of = _parse_dt(as_of)
    eff_rec = _effective_recorded(recorded_as_of, recorded_at)

    topics = await store.list_topics(principal=principal, include_archived=True)
    topic = next((t for t in topics if t.slug == slug or t.topic_id == slug), None)
    if topic is None:
        raise NotFoundError("Topic not found")

    attachments = await store.list_topic_attachments(principal=principal, topic_id=topic.topic_id)
    entities_out: list[dict[str, Any]] = []
    claims_out: list[dict[str, Any]] = []
    items_out: list[dict[str, Any]] = []

    all_claims = {
        c.claim_id: c
        for c in await store.query_claims(principal=principal, as_of=eff_as_of, recorded_as_of=eff_rec)
    }
    all_kitems = {k.item_id: k for k in await store.list_knowledge_items(principal=principal)}
    seen_e: set[str] = set()
    seen_c: set[str] = set()
    seen_k: set[str] = set()

    for att in attachments:
        if att.target_type == "claim":
            clm = all_claims.get(att.target_id)
            if clm is not None and clm.claim_id not in seen_c:
                seen_c.add(clm.claim_id)
                claims_out.append(clm.model_dump(mode="json"))
        elif att.target_type == "artifact":
            ki = all_kitems.get(att.target_id)
            if ki is not None and ki.item_id not in seen_k:
                seen_k.add(ki.item_id)
                items_out.append(ki.model_dump(mode="json"))
        else:
            ent = await store.get_entity(att.target_id, principal=principal)
            if ent is not None and ent.entity_id not in seen_e:
                seen_e.add(ent.entity_id)
                entities_out.append(ent.model_dump(mode="json"))

    return {
        "topic": topic.model_dump(mode="json"),
        "entities": entities_out,
        "claims": claims_out,
        "knowledge_items": items_out,
    }


# ---------------------------------------------------------------------------
# Diagnostic Graph & Reversible Impact BFS
# ---------------------------------------------------------------------------


async def _graph_handler(
    request: Request,
    lens: str,
    focus_id: str | None,
    hops: int,
    direction: str,
    q: str | None,
    topic: str | None,
    domain_nature: Literal["financial", "non_financial"] | None,
    modality: Literal["quantitative", "qualitative"] | None,
    epistemic_temporal: Literal["actual", "estimate", "forecast", "guidance", "pro_forma"] | None,
    ingestion_channel: Literal["automated_feed", "manual_upload", "human_authored", "agent_extracted"] | None,
    coverage_status: str | None,
    conflicts_only: bool,
    as_of: str | None,
    recorded_as_of: str | None,
    recorded_at: str | None,
    principal: Principal,
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    eff_as_of = _parse_dt(as_of)
    eff_rec = _effective_recorded(recorded_as_of, recorded_at)
    norm_lens = _normalize_lens(lens)
    norm_dir = _normalize_direction(direction)

    # Zero-leakage guard when caller passes an unauthorized focus_id
    resolved_focus: str | None = focus_id
    if focus_id:
        ent = await ctx.registry.store.get_entity(focus_id, principal=principal)
        if ent is None:
            raise NotFoundError("Resource not found")
        resolved_focus = ent.entity_id

    flt = SearchFilter(
        topic_slug=topic,
        domain_nature=domain_nature,
        modality=modality,
        epistemic_temporal=epistemic_temporal,
        ingestion_channel=ingestion_channel,
        coverage_statuses=[coverage_status] if coverage_status else None,
        conflicts_only=conflicts_only,
        as_of=eff_as_of,
        recorded_as_of=eff_rec,
    )
    payload = await ctx.registry.graph.project_graph(
        principal=principal,
        lens=norm_lens,
        focus_id=resolved_focus,
        hops=max(1, min(int(hops), 3)),
        search_query=q,
        filters=flt,
        as_of=eff_as_of,
        recorded_as_of=eff_rec,
    )

    # Apply directional focus filter when caller requests upstream (in/reverse) or downstream (out/forward)
    if resolved_focus and norm_dir in ("in", "out"):
        if norm_dir == "in":
            kept_edges = [e for e in payload.edges if e.target == resolved_focus or e.direction_role == "upstream"]
        else:
            kept_edges = [e for e in payload.edges if e.source == resolved_focus or e.direction_role == "downstream"]
        kept_node_ids = {resolved_focus}
        for e in kept_edges:
            kept_node_ids.add(e.source)
            kept_node_ids.add(e.target)
        payload.edges = kept_edges
        payload.nodes = [n for n in payload.nodes if n.id in kept_node_ids]
        payload.stats.node_count = len(payload.nodes)
        payload.stats.edge_count = len(payload.edges)
        payload.stats.orphan_count = sum(1 for n in payload.nodes if n.is_orphan)

    epochs = await ctx.registry.store.get_epochs()
    receipt = build_receipt(
        principal=principal,
        epochs=epochs,
        as_of=eff_as_of,
        recorded_as_of=eff_rec,
        tool_or_endpoint="api.graph",
    )
    await ctx.registry.store.record_receipt(receipt)
    data = payload.model_dump(mode="json")
    data["receipt_id"] = receipt.receipt_id
    return data


@router.get("/api/v1/graph")
async def get_graph(
    request: Request,
    lens: str = "topic",
    focus_id: str | None = None,
    hops: int = Query(default=2, ge=1, le=3),
    direction: str = "both",
    q: str | None = None,
    topic: str | None = None,
    domain_nature: Literal["financial", "non_financial"] | None = None,
    modality: Literal["quantitative", "qualitative"] | None = None,
    epistemic_temporal: Literal["actual", "estimate", "forecast", "guidance", "pro_forma"] | None = None,
    ingestion_channel: Literal["automated_feed", "manual_upload", "human_authored", "agent_extracted"] | None = None,
    coverage_status: str | None = None,
    conflicts_only: bool = False,
    as_of: str | None = None,
    recorded_as_of: str | None = None,
    recorded_at: str | None = None,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    return await _graph_handler(
        request,
        lens,
        focus_id,
        hops,
        direction,
        q,
        topic,
        domain_nature,
        modality,
        epistemic_temporal,
        ingestion_channel,
        coverage_status,
        conflicts_only,
        as_of,
        recorded_as_of,
        recorded_at,
        principal,
    )


@router.get("/api/v1/map/graph")
async def get_map_graph_alias(
    request: Request,
    lens: str = "topic",
    focus_id: str | None = None,
    hops: int = Query(default=2, ge=1, le=3),
    direction: str = "both",
    q: str | None = None,
    topic: str | None = None,
    domain_nature: Literal["financial", "non_financial"] | None = None,
    modality: Literal["quantitative", "qualitative"] | None = None,
    epistemic_temporal: Literal["actual", "estimate", "forecast", "guidance", "pro_forma"] | None = None,
    ingestion_channel: Literal["automated_feed", "manual_upload", "human_authored", "agent_extracted"] | None = None,
    coverage_status: str | None = None,
    conflicts_only: bool = False,
    as_of: str | None = None,
    recorded_as_of: str | None = None,
    recorded_at: str | None = None,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    return await _graph_handler(
        request,
        lens,
        focus_id,
        hops,
        direction,
        q,
        topic,
        domain_nature,
        modality,
        epistemic_temporal,
        ingestion_channel,
        coverage_status,
        conflicts_only,
        as_of,
        recorded_as_of,
        recorded_at,
        principal,
    )


@router.get("/api/v1/map/impact/{node_id:path}")
async def get_impact(
    node_id: str,
    request: Request,
    max_depth: int = Query(default=5, ge=1, le=8),
    as_of: str | None = None,
    recorded_as_of: str | None = None,
    recorded_at: str | None = None,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    eff_as_of = _parse_dt(as_of)
    eff_rec = _effective_recorded(recorded_as_of, recorded_at)
    impact = await ctx.registry.graph.impact_traversal(
        node_id,
        principal=principal,
        max_depth=max_depth,
        as_of=eff_as_of,
        recorded_as_of=eff_rec,
    )
    epochs = await ctx.registry.store.get_epochs()
    receipt = build_receipt(
        principal=principal,
        epochs=epochs,
        as_of=eff_as_of,
        recorded_as_of=eff_rec,
        tool_or_endpoint="api.map.impact",
    )
    await ctx.registry.store.record_receipt(receipt)
    data = impact.model_dump(mode="json")
    data["receipt_id"] = receipt.receipt_id
    return data


# ---------------------------------------------------------------------------
# Context Bundle & Bitemporal Resolution (Epoch-Fenced Cache)
# ---------------------------------------------------------------------------


async def _resolve_bundle_impl(
    request: Request, query: ContextBundleQuery, principal: Principal
) -> Any:
    ctx = _get_ctx(request)
    epochs = await ctx.registry.store.get_epochs()
    cache_key = (
        epochs.data_epoch,
        epochs.policy_epoch,
        ctx.registry.active_store_name,
        ctx.registry.active_retrieval_name,
        principal.user_id,
        principal.groups,
        principal.wall_side.value,
        principal.actor_mode,
        principal.channel_mode,
        canonical_json(query.model_dump(mode="json")),
    )
    cached = ctx.bundle_cache.get(cache_key)
    if cached is not None:
        bundle = cached.model_copy(update={"source": "cache"})
    else:
        bundle = await ctx.registry.retrieval.resolve_context(query, principal=principal)
        ctx.bundle_cache[cache_key] = bundle

    if _wants_markdown(request):
        return PlainTextResponse(
            content=render_bundle_markdown(bundle),
            media_type="text/markdown; charset=utf-8",
        )
    return bundle.model_dump(mode="json")


@router.post("/api/v1/context/bundle")
async def post_context_bundle(
    body: ContextBundleBody,
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> Any:
    query = ContextBundleQuery(
        entity_keys=body.entity_keys,
        skill_id=body.skill_id,
        project_id=body.project_id,
        run_id=body.run_id,
        context_purpose=body.context_purpose,
        predicates=body.predicates,
        include_shared=body.include_shared,
        include_unverified=body.include_unverified,
        as_of=ensure_utc(body.as_of),
        recorded_as_of=ensure_utc(body.recorded_as_of or body.recorded_at),
        snapshot_version=body.snapshot_version,
        tool_or_endpoint="api.context.bundle",
    )
    return await _resolve_bundle_impl(request, query, principal)


@router.get("/api/v1/context/resolve")
async def get_context_resolve(
    request: Request,
    entity_keys: str = "",
    skill_id: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
    context_purpose: str | None = None,
    include_unverified: bool = False,
    as_of: str | None = None,
    recorded_as_of: str | None = None,
    recorded_at: str | None = None,
    snapshot_version: int | None = None,
    principal: Principal = Depends(_current_principal),
) -> Any:
    keys = [k.strip() for k in entity_keys.split(",") if k.strip()]
    query = ContextBundleQuery(
        entity_keys=keys,
        skill_id=skill_id,
        project_id=project_id,
        run_id=run_id,
        context_purpose=context_purpose,
        include_unverified=include_unverified,
        as_of=_parse_dt(as_of),
        recorded_as_of=_effective_recorded(recorded_as_of, recorded_at),
        snapshot_version=snapshot_version,
        tool_or_endpoint="api.context.resolve",
    )
    return await _resolve_bundle_impl(request, query, principal)


# ---------------------------------------------------------------------------
# Claims & Quarantined Inbox Triage
# ---------------------------------------------------------------------------


@router.get("/api/v1/claims")
async def list_claims(
    request: Request,
    subject_id: str | None = None,
    predicate: str | None = None,
    context_purpose: str | None = None,
    epistemic_status: str | None = None,
    scope_type: str | None = None,
    as_of: str | None = None,
    recorded_as_of: str | None = None,
    recorded_at: str | None = None,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    store = ctx.registry.store
    subj_ids: list[str] | None = None
    if subject_id:
        ent = await store.get_entity(subject_id, principal=principal)
        if ent is None:
            return {"claims": [], "total": 0}
        subj_ids = [ent.entity_id]

    claims = await store.query_claims(
        principal=principal,
        subject_ids=subj_ids,
        predicates=[predicate] if predicate else None,
        context_purpose=context_purpose,
        scope_types=[scope_type] if scope_type else None,
        epistemic_statuses=[epistemic_status] if epistemic_status else None,
        as_of=_parse_dt(as_of),
        recorded_as_of=_effective_recorded(recorded_as_of, recorded_at),
    )
    return {"claims": [c.model_dump(mode="json") for c in claims], "total": len(claims)}


@router.get("/api/v1/claims/{claim_id}")
async def get_claim(
    claim_id: str,
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> Any:
    ctx = _get_ctx(request)
    claim = await ctx.registry.store.get_claim(claim_id, principal=principal)
    if claim is None:
        raise NotFoundError("Resource not found")
    if _wants_markdown(request):
        return PlainTextResponse(
            content=render_claim_markdown(claim),
            media_type="text/markdown; charset=utf-8",
        )
    return {"claim": claim.model_dump(mode="json")}


@router.post("/api/v1/claims", status_code=201)
async def create_claim(
    payload: ClaimCreateInput,
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    # Apply anti-SSRF & query-string secret stripping on evidence_uri
    if payload.provenance and payload.provenance.evidence_uri:
        cleaned = validate_safe_uri(payload.provenance.evidence_uri, field_name="evidence_uri")
        payload.provenance.evidence_uri = validate_no_ssrf(cleaned)
    if payload.expected_data_epoch is None:
        if_match = parse_if_match_epoch(request.headers)
        if if_match is not None:
            payload.expected_data_epoch = if_match

    res = await ctx.registry.store.write_claim(payload, principal=principal)
    ctx.bundle_cache.clear()
    if res.claim.epistemic_status == "approved_fact" and ctx.registry.snapshots is not None:
        for tier, ws in ctx.registry.policy.allowed_snapshot_partitions(principal):
            ctx.registry.snapshots.schedule_compile(tier, ws)

    ctx.events.publish(
        "claim_written",
        {
            "claim_id": res.claim.claim_id,
            "conflict_detected": res.conflict_detected,
            "data_epoch": res.data_epoch,
        },
    )
    return {
        "claim": res.claim.model_dump(mode="json"),
        "conflict_detected": res.conflict_detected,
        "conflict_set_id": res.conflict_set_id,
        "data_epoch": res.data_epoch,
    }


@router.patch("/api/v1/claims/{claim_id}")
async def patch_claim(
    claim_id: str,
    patch: ClaimUpdateInput,
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    if patch.expected_data_epoch is None:
        if_match = parse_if_match_epoch(request.headers)
        if if_match is not None:
            patch.expected_data_epoch = if_match

    res = await ctx.registry.store.update_claim(claim_id, patch, principal=principal)
    ctx.bundle_cache.clear()
    snap_scheduled = False
    if ctx.registry.snapshots is not None:
        for tier, ws in ctx.registry.policy.allowed_snapshot_partitions(principal):
            ctx.registry.snapshots.schedule_compile(tier, ws)
            snap_scheduled = True

    ctx.events.publish(
        "claim_written",
        {
            "claim_id": res.claim.claim_id,
            "superseded_claim_id": res.superseded_claim_id,
            "data_epoch": res.data_epoch,
        },
    )
    return {
        "claim": res.claim.model_dump(mode="json"),
        "superseded_claim_id": res.superseded_claim_id,
        "snapshot_scheduled": snap_scheduled,
        "data_epoch": res.data_epoch,
    }


@router.get("/api/v1/inbox")
async def get_inbox(
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    store = ctx.registry.store
    items = await store.query_claims(
        principal=principal,
        epistemic_statuses=["unverified_observation", "candidate_sop"],
    )
    enriched: list[dict[str, Any]] = []
    for c in items:
        dumped = c.model_dump(mode="json")
        subj = await store.get_entity(c.subject_id, principal=principal)
        if subj is not None:
            dumped["subject_canonical_key"] = subj.canonical_key
            dumped["subject_display_name"] = subj.display_name
        enriched.append(dumped)
    return {
        "items": enriched,
        "total": len(enriched),
    }


@router.post("/api/v1/inbox/{claim_id}/approve")
async def approve_inbox_claim(
    claim_id: str,
    request: Request,
    body: InboxActionBody | None = None,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    expected = (body.expected_data_epoch if body else None) or parse_if_match_epoch(request.headers)
    res = await ctx.registry.store.update_claim(
        claim_id,
        ClaimUpdateInput(
            epistemic_status="approved_fact",
            rationale=(body.rationale if body else None) or "Steward inbox approval",
            force_supersede=True,
            expected_data_epoch=expected,
        ),
        principal=principal,
    )
    ctx.bundle_cache.clear()
    ctx.events.publish("claim_written", {"claim_id": res.claim.claim_id, "data_epoch": res.data_epoch})
    return {
        "approved": True,
        "claim": res.claim.model_dump(mode="json"),
        "superseded_claim_id": res.superseded_claim_id,
        "data_epoch": res.data_epoch,
    }


@router.post("/api/v1/inbox/{claim_id}/reject")
async def reject_inbox_claim(
    claim_id: str,
    request: Request,
    body: InboxActionBody | None = None,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    expected = (body.expected_data_epoch if body else None) or parse_if_match_epoch(request.headers)
    res = await ctx.registry.store.update_claim(
        claim_id,
        ClaimUpdateInput(
            epistemic_status="revoked",
            rationale=(body.rationale if body else None) or "Steward inbox rejection",
            force_supersede=True,
            expected_data_epoch=expected,
        ),
        principal=principal,
    )
    ctx.bundle_cache.clear()
    ctx.events.publish("claim_written", {"claim_id": res.claim.claim_id, "data_epoch": res.data_epoch})
    return {
        "rejected": True,
        "claim": res.claim.model_dump(mode="json"),
        "superseded_claim_id": res.superseded_claim_id,
        "data_epoch": res.data_epoch,
    }


# ---------------------------------------------------------------------------
# N-Way Conflicts & Blast-Radius Preview
# ---------------------------------------------------------------------------


@router.get("/api/v1/conflicts")
async def list_conflicts(
    request: Request,
    status: Literal["open", "resolved", "all"] = "open",
    subject_id: str | None = None,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    store = ctx.registry.store
    resolved_subj = subject_id
    if subject_id:
        ent = await store.get_entity(subject_id, principal=principal)
        if ent is None:
            return {"conflicts": []}
        resolved_subj = ent.entity_id
    conflicts = await store.list_conflicts(principal=principal, status=status, subject_id=resolved_subj)
    return {"conflicts": [cf.model_dump(mode="json") for cf in conflicts]}


@router.get("/api/v1/conflicts/{conflict_set_id}/blast-radius")
async def conflict_blast_radius(
    conflict_set_id: str,
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    store = ctx.registry.store
    all_conflicts = await store.list_conflicts(principal=principal, status="all")
    conflict = next((c for c in all_conflicts if c.conflict_set_id == conflict_set_id), None)
    if conflict is None:
        raise NotFoundError("Conflict set not found")

    impact = await ctx.registry.graph.impact_traversal(
        conflict.subject_id, principal=principal, max_depth=5
    )
    bindings = await store.list_skill_bindings(target_entity_id=conflict.subject_id)
    affected_skills: list[dict[str, Any]] = []
    for sb in bindings:
        sk = await store.get_entity(sb.skill_entity_id, principal=principal)
        if sk is not None:
            affected_skills.append(
                {
                    "skill_entity_id": sk.entity_id,
                    "canonical_key": sk.canonical_key,
                    "display_name": sk.display_name,
                    "mapping_purpose": sb.mapping_purpose,
                    "read_mode": sb.read_mode,
                    "required_predicates": sb.required_predicates,
                }
            )
    imp_dumped = impact.model_dump(mode="json")
    return {
        **imp_dumped,
        "conflict_set_id": conflict.conflict_set_id,
        "subject_id": conflict.subject_id,
        "predicate": conflict.predicate,
        "affected_skills": affected_skills,
        "impact": imp_dumped,
    }


@router.post("/api/v1/conflicts/{conflict_set_id}/resolve")
async def resolve_conflict(
    conflict_set_id: str,
    body: ConflictResolveBody,
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    store = ctx.registry.store
    expected = body.expected_data_epoch
    if expected is None:
        expected = parse_if_match_epoch(request.headers)

    resolved = await store.resolve_conflict(
        ConflictResolutionInput(
            conflict_set_id=conflict_set_id,
            winning_claim_id=body.winning_claim_id,
            replacement_object_value=body.replacement_object_value,
            valid_from=ensure_utc(body.valid_from),
            close_prior_valid_to=ensure_utc(body.close_prior_valid_to),
            rationale=body.rationale,
            expected_data_epoch=expected,
        ),
        principal=principal,
    )
    ctx.bundle_cache.clear()
    epochs = await store.get_epochs()

    approved_claim = None
    if resolved.winning_claim_id:
        approved_claim = await store.get_claim(resolved.winning_claim_id, principal=principal)

    bindings = await store.list_skill_bindings(target_entity_id=resolved.subject_id)
    affected_skills = [sb.skill_entity_id for sb in bindings if sb.active]

    snap_scheduled = False
    if ctx.registry.snapshots is not None:
        for tier, ws in ctx.registry.policy.allowed_snapshot_partitions(principal):
            ctx.registry.snapshots.schedule_compile(tier, ws)
            snap_scheduled = True

    ctx.events.publish(
        "conflict_resolved",
        {
            "conflict_set_id": resolved.conflict_set_id,
            "winning_claim_id": resolved.winning_claim_id,
            "data_epoch": epochs.data_epoch,
        },
    )
    return {
        "conflict": resolved.model_dump(mode="json"),
        "approved_claim": approved_claim.model_dump(mode="json") if approved_claim else None,
        "affected_skills": affected_skills,
        "snapshot_scheduled": snap_scheduled,
        "data_epoch": epochs.data_epoch,
    }


# ---------------------------------------------------------------------------
# Personal Memory Server ("My Context") & Watchlists (Surface-Gated)
# ---------------------------------------------------------------------------


def _memory_disabled_reason(principal: Principal) -> str:
    if principal.channel_mode == "shared":
        return "Personal context disabled in shared channel"
    if principal.actor_mode == "service":
        return "Personal context disabled for service identity"
    return "Personal context unavailable"


@router.get("/api/v1/memory/me")
async def get_memory_me(
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    if not ctx.registry.policy.is_memory_allowed(principal):
        return {
            "memory_enabled": False,
            "disabled_reason": _memory_disabled_reason(principal),
            "notes": [],
            "items": [],
            "watchlist": None,
            "alerts": [],
        }
    notes = await ctx.registry.store.list_memories(principal=principal)
    wl = await ctx.registry.store.get_watchlist(principal=principal)
    dumped = [n.model_dump(mode="json") for n in notes]
    return {
        "memory_enabled": True,
        "disabled_reason": None,
        "notes": dumped,
        "items": dumped,
        "watchlist": wl.model_dump(mode="json"),
        "alerts": [a.model_dump(mode="json") for a in wl.alerts],
    }


@router.get("/api/v1/memory/notes")
async def list_memory_notes(
    request: Request,
    skill_id: str | None = None,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    if not ctx.registry.policy.is_memory_allowed(principal):
        return {
            "memory_enabled": False,
            "disabled_reason": _memory_disabled_reason(principal),
            "items": [],
        }
    notes = await ctx.registry.store.list_memories(principal=principal, skill_id=skill_id)
    return {
        "memory_enabled": True,
        "disabled_reason": None,
        "items": [n.model_dump(mode="json") for n in notes],
    }


@router.post("/api/v1/memory/notes", status_code=201)
async def create_memory_note(
    body: MemoryUpsertInput,
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    if not ctx.registry.policy.is_memory_allowed(principal):
        raise SurfacePolicyError(_memory_disabled_reason(principal))
    saved = await ctx.registry.store.upsert_memory(body, principal=principal)
    ctx.bundle_cache.clear()
    return {"memory": saved.model_dump(mode="json")}


@router.put("/api/v1/memory/notes/{memory_id}")
async def update_memory_note(
    memory_id: str,
    body: MemoryUpdateBody,
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    if not ctx.registry.policy.is_memory_allowed(principal):
        raise SurfacePolicyError(_memory_disabled_reason(principal))
    existing = await ctx.registry.store.get_memory(memory_id, principal=principal)
    if existing is None:
        raise NotFoundError("Memory note not found")
    payload = MemoryUpsertInput(
        memory_id=memory_id,
        scope_type=body.scope_type or existing.scope_type,
        skill_id=body.skill_id if body.skill_id is not None else existing.skill_id,
        key=body.key if body.key is not None else existing.key,
        content=body.content if body.content is not None else existing.content,
        tags=body.tags if body.tags is not None else existing.tags,
        linked_entity_ids=(
            body.linked_entity_ids if body.linked_entity_ids is not None else existing.linked_entity_ids
        ),
        linked_topic_ids=(
            body.linked_topic_ids if body.linked_topic_ids is not None else existing.linked_topic_ids
        ),
    )
    saved = await ctx.registry.store.upsert_memory(payload, principal=principal)
    ctx.bundle_cache.clear()
    return {"memory": saved.model_dump(mode="json")}


@router.delete("/api/v1/memory/notes/{memory_id}")
async def delete_memory_note(
    memory_id: str,
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    if not ctx.registry.policy.is_memory_allowed(principal):
        raise SurfacePolicyError(_memory_disabled_reason(principal))
    deleted = await ctx.registry.store.delete_memory(memory_id, principal=principal)
    if not deleted:
        raise NotFoundError("Memory note not found")
    ctx.bundle_cache.clear()
    return {"deleted": True}


@router.post("/api/v1/memory/notes/{memory_id}/compare")
async def compare_memory_note(
    memory_id: str,
    request: Request,
    body: MemoryCompareBody | None = None,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    if not ctx.registry.policy.is_memory_allowed(principal):
        raise SurfacePolicyError(_memory_disabled_reason(principal))
    store = ctx.registry.store
    note = await store.get_memory(memory_id, principal=principal)
    if note is None:
        raise NotFoundError("Memory note not found")

    eff_as_of = ensure_utc(body.as_of) if body and body.as_of else None
    matching_claims: list[Any] = []
    if note.linked_entity_ids:
        matching_claims = await store.query_claims(
            principal=principal,
            subject_ids=note.linked_entity_ids,
            as_of=eff_as_of,
        )
    if not matching_claims:
        hits = await ctx.registry.retrieval.search(note.key.replace("-", " "), principal=principal, limit=5)
        eids = [h.id for h in hits if h.hit_type == "entity"]
        if eids:
            matching_claims = await store.query_claims(
                principal=principal, subject_ids=eids, as_of=eff_as_of
            )

    firm_claims = [c for c in matching_claims if c.scope_type in ("firm", "project", "skill")][:10]
    disputed_count = sum(1 for c in firm_claims if c.epistemic_status == "disputed")
    if not firm_claims:
        summary = "No directly matching firm claims found for linked entities; safe to promote as a new quarantined observation."
    elif disputed_count > 0:
        summary = (
            f"Found {len(firm_claims)} matching firm claim(s) including {disputed_count} disputed claim(s). "
            "Promoting this note will enter the quarantined steward review queue without overwriting approved facts."
        )
    else:
        summary = (
            f"Found {len(firm_claims)} authoritative firm claim(s) for comparison. "
            "Personal note remains isolated until promoted for steward review."
        )

    epochs = await store.get_epochs()
    receipt = build_receipt(
        principal=principal,
        epochs=epochs,
        as_of=eff_as_of,
        claims=firm_claims,
        memories=[note],
        tool_or_endpoint="api.memory.compare",
    )
    await store.record_receipt(receipt)
    return {
        "note": note.model_dump(mode="json"),
        "matching_firm_claims": [c.model_dump(mode="json") for c in firm_claims],
        "divergence_summary": summary,
        "can_promote": True,
        "receipt_id": receipt.receipt_id,
    }


@router.post("/api/v1/memory/notes/{memory_id}/promote", status_code=201)
async def promote_memory_note(
    memory_id: str,
    body: MemoryPromoteBody,
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    if not ctx.registry.policy.is_memory_allowed(principal):
        raise SurfacePolicyError(_memory_disabled_reason(principal))
    store = ctx.registry.store
    note = await store.get_memory(memory_id, principal=principal)
    if note is None:
        raise NotFoundError("Memory note not found")

    target_key = body.subject_canonical_key or (note.linked_entity_ids[0] if note.linked_entity_ids else None)
    target = await store.get_entity(target_key, principal=principal) if target_key else None
    if target is None:
        ents = await store.list_entities(principal=principal, limit=25)
        target = next((e for e in ents if e.canonical_key == "proj:q1-2026-audit"), None) or (ents[0] if ents else None)
    if target is None:
        raise NotFoundError("Target entity not found")

    scope_type = "project" if body.target_scope == "project" else "firm"
    scope_id = body.project_id or "proj:q1-2026-audit" if scope_type == "project" else "firm"

    # Always quarantine promoted notes as unverified_observation (NEVER bypass steward approval!)
    res = await store.write_claim(
        ClaimCreateInput(
            subject_id=target.entity_id,
            subject_canonical_key=target.canonical_key,
            predicate=body.predicate or "promoted_observation",
            object_value=note.content,
            context_purpose="general",
            scope_type=scope_type,  # type: ignore[arg-type]
            scope_id=scope_id,
            channel="fact",
            epistemic_status="unverified_observation",
            kind="observation",
            is_mandatory=False,
            facets=ClaimFacets(
                domain_nature="non_financial",
                modality="qualitative",
                epistemic_temporal="actual",
                periodicity="point_in_time",
                ingestion_channel="human_authored",
            ),
            source_system=f"personal-memory:{principal.user_id}",
            excerpt=(body.rationale or f"Promoted from personal note '{note.key}'")[:300],
            wall_side=target.wall_side,
            read_acl_groups=list(target.read_acl_groups),
            write_acl_groups=list(target.write_acl_groups),
            force_supersede=False,
        ),
        principal=principal,
    )
    ctx.bundle_cache.clear()
    ctx.events.publish("claim_written", {"claim_id": res.claim.claim_id, "data_epoch": res.data_epoch})
    return {
        "quarantined_claim": res.claim.model_dump(mode="json"),
        "epistemic_status": "unverified_observation",
        "data_epoch": res.data_epoch,
    }


@router.get("/api/v1/memory/watchlist")
async def get_watchlist(
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    if not ctx.registry.policy.is_memory_allowed(principal):
        return {
            "memory_enabled": False,
            "disabled_reason": _memory_disabled_reason(principal),
            "entity_ids": [],
            "topic_ids": [],
            "source_ids": [],
            "alerts": [],
        }
    wl = await ctx.registry.store.get_watchlist(principal=principal)
    return {
        "memory_enabled": True,
        "entity_ids": wl.entity_ids,
        "topic_ids": wl.topic_ids,
        "source_ids": wl.source_ids,
        "alerts": [a.model_dump(mode="json") for a in wl.alerts],
    }


@router.put("/api/v1/memory/watchlist")
async def put_watchlist(
    body: WatchlistUpdateInput,
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    if not ctx.registry.policy.is_memory_allowed(principal):
        raise SurfacePolicyError(_memory_disabled_reason(principal))
    wl = await ctx.registry.store.put_watchlist(body, principal=principal)
    return {
        "memory_enabled": True,
        "entity_ids": wl.entity_ids,
        "topic_ids": wl.topic_ids,
        "source_ids": wl.source_ids,
        "alerts": [a.model_dump(mode="json") for a in wl.alerts],
    }


# ---------------------------------------------------------------------------
# Manual Uploads & Sources
# ---------------------------------------------------------------------------


async def _upload_handler(
    request: Request,
    file: UploadFile,
    subject_canonical_key: str | None,
    topic_slug: str | None,
    wall_side: str,
    evidence_acl_groups: str | None,
    notes: str | None,
    principal: Principal,
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    raw = await file.read()
    ev_groups = (
        [g.strip() for g in evidence_acl_groups.split(",") if g.strip()]
        if evidence_acl_groups
        else []
    )
    res = await ctx.uploads.ingest_upload(
        filename=file.filename or "upload.txt",
        content_type=file.content_type,
        raw_bytes=raw,
        principal=principal,
        store=ctx.registry.store,
        policy=ctx.registry.policy,
        subject_canonical_key=subject_canonical_key,
        topic_slug=topic_slug,
        wall_side=wall_side,
        evidence_acl_groups=ev_groups,
        notes=notes,
    )
    ctx.bundle_cache.clear()
    epochs = await ctx.registry.store.get_epochs()
    ctx.events.publish(
        "claim_written",
        {"upload_id": res.upload_id, "quarantined_count": len(res.quarantined_claim_ids), "data_epoch": epochs.data_epoch},
    )
    return res.model_dump(mode="json")


@router.post("/api/v1/uploads", status_code=201)
async def post_uploads(
    request: Request,
    file: UploadFile = File(...),
    subject_canonical_key: str | None = Form(default=None),
    topic_slug: str | None = Form(default=None),
    wall_side: str = Form(default="public_side"),
    evidence_acl_groups: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    return await _upload_handler(
        request, file, subject_canonical_key, topic_slug, wall_side, evidence_acl_groups, notes, principal
    )


@router.post("/api/v1/sources/upload", status_code=201)
async def post_sources_upload_alias(
    request: Request,
    file: UploadFile = File(...),
    subject_canonical_key: str | None = Form(default=None),
    topic_slug: str | None = Form(default=None),
    wall_side: str = Form(default="public_side"),
    evidence_acl_groups: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    return await _upload_handler(
        request, file, subject_canonical_key, topic_slug, wall_side, evidence_acl_groups, notes, principal
    )


@router.get("/api/v1/sources")
async def list_sources(
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    items = ctx.uploads.list_sources(principal=principal)
    return {"sources": items, "total": len(items)}


@router.get("/api/v1/sources/{source_id}")
async def get_source(
    source_id: str,
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    item = ctx.uploads.get_source(source_id, principal=principal)
    if item is None:
        raise NotFoundError("Source not found")
    return {"source": item}


# ---------------------------------------------------------------------------
# Docked Viewport Agent
# ---------------------------------------------------------------------------


async def _agent_chat_impl(
    body: AgentChatBody, request: Request, principal: Principal
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    msg = (body.message or body.query or "").strip()
    if not msg:
        raise ValidationError("Agent message/query cannot be empty")

    snap_ver: int | None = None
    if ctx.registry.snapshots is not None:
        for tier, ws in ctx.registry.policy.allowed_snapshot_partitions(principal):
            try:
                manifest = await ctx.registry.snapshots.get_latest_manifest(tier, ws)
                if manifest is not None:
                    snap_ver = manifest.version_id
                    break
            except Exception:
                continue

    agent = ViewportGroundedAgent(
        store=ctx.registry.store,
        retrieval=ctx.registry.retrieval,
        policy=ctx.registry.policy,
        graph=ctx.registry.graph,
        llm_mode=ctx.registry.llm_mode,
        snapshot_version=snap_ver,
    )
    resp = await agent.answer(message=msg, viewport=body.viewport, principal=principal)
    return resp.model_dump(mode="json")


@router.post("/api/v1/agent/chat")
async def post_agent_chat(
    body: AgentChatBody,
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    return await _agent_chat_impl(body, request, principal)


@router.post("/api/v1/agent/query")
async def post_agent_query_alias(
    body: AgentChatBody,
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    return await _agent_chat_impl(body, request, principal)


# ---------------------------------------------------------------------------
# Snapshots, Receipts, Curator & SSE Events
# ---------------------------------------------------------------------------


@router.get("/api/v1/snapshots")
async def list_snapshots(
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    if ctx.registry.snapshots is None:
        return {"snapshots": []}
    manifests: list[dict[str, Any]] = []
    for tier, ws in ctx.registry.policy.allowed_snapshot_partitions(principal):
        manifest = await ctx.registry.snapshots.get_latest_manifest(tier, ws)
        if manifest is not None:
            manifests.append(manifest.model_dump(mode="json"))
    return {"snapshots": manifests}


@router.get("/api/v1/snapshots/latest")
async def get_latest_snapshot(
    request: Request,
    acl_tier: str = "firm-all",
    wall_side: str = "public_side",
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    if not ctx.registry.policy.can_access_snapshot_partition(principal, acl_tier, wall_side):
        raise NotFoundError("Snapshot partition not found")
    if ctx.registry.snapshots is None:
        raise NotFoundError("Snapshot compiler not configured")
    manifest = await ctx.registry.snapshots.get_latest_manifest(acl_tier, wall_side)
    if manifest is None:
        manifest = await ctx.registry.snapshots.compile_and_publish(acl_tier, wall_side)
    return {"manifest": manifest.model_dump(mode="json")}


@router.post("/api/v1/snapshots/compile")
async def compile_snapshot(
    body: SnapshotCompileBody,
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    if not _is_steward(principal):
        raise AuthorizationError("Steward authorization required to compile snapshots")
    if not ctx.registry.policy.can_access_snapshot_partition(principal, body.acl_tier, body.wall_side):
        raise AuthorizationError("Caller lacks clearance for requested snapshot partition")
    if ctx.registry.snapshots is None:
        raise ValidationError("Snapshot compiler not configured")
    manifest = await ctx.registry.snapshots.compile_and_publish(body.acl_tier, body.wall_side)
    ctx.events.publish(
        "snapshot_published",
        {
            "acl_tier": manifest.acl_tier,
            "wall_side": manifest.wall_side.value,
            "version_id": manifest.version_id,
            "etag": manifest.etag,
        },
    )
    return {"manifest": manifest.model_dump(mode="json")}


@router.get("/api/v1/snapshots/{version}/download")
async def download_snapshot(
    version: int,
    request: Request,
    acl_tier: str = "firm-all",
    wall_side: str = "public_side",
    principal: Principal = Depends(_current_principal),
) -> Response:
    ctx = _get_ctx(request)
    if not ctx.registry.policy.can_access_snapshot_partition(principal, acl_tier, wall_side):
        raise NotFoundError("Snapshot partition not found")
    if ctx.registry.snapshots is None:
        raise NotFoundError("Snapshot compiler not configured")
    data = await ctx.registry.snapshots.get_snapshot_bytes(acl_tier, wall_side, version)
    return Response(
        content=data,
        media_type="application/zstd",
        headers={"Content-Disposition": f'attachment; filename="v{version}.sqlite.zst"'},
    )


@router.get("/api/v1/receipts/{receipt_id}")
async def get_receipt(
    receipt_id: str,
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    receipt = await ctx.registry.store.get_receipt(receipt_id, principal=principal)
    if receipt is None:
        raise NotFoundError("Receipt not found")
    return {"receipt": receipt.model_dump(mode="json")}


@router.get("/api/v1/curator/patches")
async def list_curator_patches(
    request: Request,
    status: str = "pending_steward_review",
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    patches = await ctx.registry.store.list_curator_patches(principal=principal, status=status)
    return {"patches": [p.model_dump(mode="json") for p in patches]}


@router.post("/api/v1/curator/run")
async def run_curator(
    request: Request,
    body: CuratorRunBody | None = None,
    principal: Principal = Depends(_current_principal),
) -> dict[str, Any]:
    ctx = _get_ctx(request)
    curator = BackgroundCurator(
        store=ctx.registry.store,
        policy=ctx.registry.policy,
        repo_root=ctx.repo_root,
    )
    staleness, created_patches = await curator.run(
        principal=principal,
        wall_side=body.wall_side if body else None,
    )
    return {
        "staleness_report": staleness.model_dump(mode="json"),
        "created_patches": [p.model_dump(mode="json") for p in created_patches],
    }


@router.get("/api/v1/events")
async def sse_events(
    request: Request,
    principal: Principal = Depends(_current_principal),
) -> EventSourceResponse:
    ctx = _get_ctx(request)
    return EventSourceResponse(ctx.events.subscribe())
