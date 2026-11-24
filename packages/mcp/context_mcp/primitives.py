"""Single-source-of-truth caller-bound primitives backing both Raw FastMCP and Code Mode RPC."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from context_core.compression import ensure_utc, format_utc
from context_core.exceptions import AuthenticationError, NotFoundError, SurfacePolicyError, ValidationError
from context_core.models import (
    Claim,
    ClaimCreateInput,
    ClaimFacets,
    ContextBundleQuery,
    MemoryUpsertInput,
    Principal,
    SearchFilter,
)

from context_mcp.runtime import LakeRuntime, make_receipt
from context_mcp.security import sanitize_uri_secrets, validate_safe_uri

VALID_GAP_KINDS = frozenset({"gap", "stale", "wrong", "observation"})


def _normalize_direction(direction: str) -> str:
    d = (direction or "both").strip().lower()
    if d == "forward":
        return "out"
    if d == "reverse":
        return "in"
    if d not in ("both", "in", "out"):
        return "both"
    return d


def _build_search_filter(
    *,
    entity_types: list[str] | None = None,
    topic_slug: str | None = None,
    facets: dict[str, Any] | None = None,
    scope_types: list[str] | None = None,
    epistemic_statuses: list[str] | None = None,
    as_of: datetime | str | None = None,
    recorded_as_of: datetime | str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> SearchFilter:
    f = facets or {}
    return SearchFilter(
        entity_types=entity_types,
        topic_slug=topic_slug,
        domain_nature=f.get("domain_nature"),
        modality=f.get("modality"),
        epistemic_temporal=f.get("epistemic_temporal"),
        actual_stage=f.get("actual_stage"),
        periodicity=f.get("periodicity"),
        ingestion_channel=f.get("ingestion_channel"),
        coverage_statuses=f.get("coverage_statuses"),
        conflicts_only=bool(f.get("conflicts_only", False)),
        scope_types=scope_types,
        epistemic_statuses=epistemic_statuses,
        as_of=ensure_utc(as_of),
        recorded_as_of=ensure_utc(recorded_as_of),
        limit=max(1, min(int(limit), 200)),
        offset=max(0, int(offset)),
    )


def _dump_claim(claim: Claim) -> dict[str, Any]:
    data = claim.model_dump(mode="json")
    if claim.provenance.evidence_uri:
        data["provenance"]["evidence_uri"] = sanitize_uri_secrets(claim.provenance.evidence_uri)
    return data


async def _get_latest_snapshot_version(runtime: LakeRuntime, principal: Principal) -> int | None:
    snap_port = runtime.snapshots
    if snap_port is None:
        return None
    partitions = runtime.policy.allowed_snapshot_partitions(principal)
    for acl_tier, wall_side in partitions:
        try:
            manifest = await snap_port.get_latest_manifest(acl_tier, wall_side)
            if manifest is not None:
                return manifest.version_id
        except Exception:
            continue
    return None


async def tool_filter(
    runtime: LakeRuntime,
    principal: Principal,
    *,
    entity_types: list[str] | None = None,
    topic_slug: str | None = None,
    facets: dict[str, Any] | None = None,
    scope_types: list[str] | None = None,
    epistemic_statuses: list[str] | None = None,
    as_of: datetime | str | None = None,
    recorded_as_of: datetime | str | None = None,
    limit: int = 50,
    record_receipt: bool = True,
) -> dict[str, Any]:
    """Metadata & facet slice returning lightweight authorized hits."""
    flt = _build_search_filter(
        entity_types=entity_types,
        topic_slug=topic_slug,
        facets=facets,
        scope_types=scope_types,
        epistemic_statuses=epistemic_statuses,
        as_of=as_of,
        recorded_as_of=recorded_as_of,
        limit=limit,
    )
    hits = await runtime.retrieval.filter_items(flt, principal=principal)
    epochs = await runtime.store.get_epochs()
    snap_ver = await _get_latest_snapshot_version(runtime, principal)
    claim_ids = [h.id for h in hits if h.hit_type == "claim"]
    redacted_ids = [h.id for h in hits if h.hit_type == "claim" and h.evidence_redacted]

    receipt = make_receipt(
        principal=principal,
        data_epoch=epochs.data_epoch,
        policy_epoch=epochs.policy_epoch,
        tool_or_endpoint="mcp.filter",
        as_of=flt.as_of,
        recorded_as_of=flt.recorded_as_of,
        snapshot_version=snap_ver,
        claim_ids=claim_ids,
        redacted_evidence_claim_ids=redacted_ids,
    )
    if record_receipt:
        await runtime.store.record_receipt(receipt)

    return {
        "items": [h.model_dump(mode="json") for h in hits],
        "total": len(hits),
        "receipt_id": receipt.receipt_id,
        "snapshot_version": snap_ver,
        "data_epoch": epochs.data_epoch,
        "policy_epoch": epochs.policy_epoch,
        "receipt": receipt.model_dump(mode="json"),
    }


async def tool_search(
    runtime: LakeRuntime,
    principal: Principal,
    *,
    query: str,
    entity_types: list[str] | None = None,
    topic_slug: str | None = None,
    facets: dict[str, Any] | None = None,
    as_of: datetime | str | None = None,
    recorded_as_of: datetime | str | None = None,
    limit: int = 25,
    record_receipt: bool = True,
) -> dict[str, Any]:
    """Lexical + alias + facet search across authorized entities, claims, and knowledge items."""
    flt = _build_search_filter(
        entity_types=entity_types,
        topic_slug=topic_slug,
        facets=facets,
        as_of=as_of,
        recorded_as_of=recorded_as_of,
        limit=limit,
    )
    hits = await runtime.retrieval.search(query, flt=flt, principal=principal, limit=flt.limit)
    epochs = await runtime.store.get_epochs()
    snap_ver = await _get_latest_snapshot_version(runtime, principal)
    claim_ids = [h.id for h in hits if h.hit_type == "claim"]
    redacted_ids = [h.id for h in hits if h.hit_type == "claim" and h.evidence_redacted]
    memory_ids = [h.id for h in hits if h.hit_type == "memory"]

    receipt = make_receipt(
        principal=principal,
        data_epoch=epochs.data_epoch,
        policy_epoch=epochs.policy_epoch,
        tool_or_endpoint="mcp.search",
        as_of=flt.as_of,
        recorded_as_of=flt.recorded_as_of,
        snapshot_version=snap_ver,
        claim_ids=claim_ids,
        redacted_evidence_claim_ids=redacted_ids,
        memory_ids=memory_ids,
    )
    if record_receipt:
        await runtime.store.record_receipt(receipt)

    return {
        "query": query,
        "hits": [h.model_dump(mode="json") for h in hits],
        "total": len(hits),
        "receipt_id": receipt.receipt_id,
        "snapshot_version": snap_ver,
        "data_epoch": epochs.data_epoch,
        "policy_epoch": epochs.policy_epoch,
        "receipt": receipt.model_dump(mode="json"),
    }


async def tool_read(
    runtime: LakeRuntime,
    principal: Principal,
    *,
    id_or_key: str,
    include_claims: bool = True,
    include_identifiers: bool = True,
    include_lineage: bool = True,
    include_topics: bool = True,
    as_of: datetime | str | None = None,
    recorded_as_of: datetime | str | None = None,
    record_receipt: bool = True,
) -> dict[str, Any]:
    """Fetch full authorized entity/knowledge card with redacted evidence stubs; 404-style not_found if denied."""
    eff_as_of = ensure_utc(as_of)
    eff_rec = ensure_utc(recorded_as_of)
    epochs = await runtime.store.get_epochs()
    snap_ver = await _get_latest_snapshot_version(runtime, principal)

    entity = await runtime.store.get_entity(id_or_key, principal=principal)
    if entity is None:
        # Check authorized knowledge items as fallback (zero existence leakage)
        kitems = await runtime.store.list_knowledge_items(principal=principal)
        matched_ki = next(
            (k for k in kitems if k.item_id == id_or_key or k.slug == id_or_key),
            None,
        )
        if matched_ki is None:
            return {
                "found": False,
                "error": {"code": "not_found", "message": "Resource not found"},
                "data_epoch": epochs.data_epoch,
                "policy_epoch": epochs.policy_epoch,
            }
        receipt = make_receipt(
            principal=principal,
            data_epoch=epochs.data_epoch,
            policy_epoch=epochs.policy_epoch,
            tool_or_endpoint="mcp.read",
            as_of=eff_as_of,
            recorded_as_of=eff_rec,
            snapshot_version=snap_ver,
        )
        if record_receipt:
            await runtime.store.record_receipt(receipt)
        return {
            "found": True,
            "kind": "knowledge_item",
            "knowledge_item": matched_ki.model_dump(mode="json"),
            "receipt_id": receipt.receipt_id,
            "snapshot_version": snap_ver,
            "data_epoch": epochs.data_epoch,
            "policy_epoch": epochs.policy_epoch,
            "receipt": receipt.model_dump(mode="json"),
        }

    claims: list[Claim] = []
    if include_claims:
        claims = await runtime.store.query_claims(
            principal=principal,
            subject_ids=[entity.entity_id],
            as_of=eff_as_of,
            recorded_as_of=eff_rec,
        )
    identifiers = (
        await runtime.store.list_identifiers(entity.entity_id, as_of=eff_as_of, recorded_as_of=eff_rec)
        if include_identifiers
        else []
    )
    lineage = (
        await runtime.store.list_lineage_events(entity.entity_id, principal=principal)
        if include_lineage
        else []
    )
    topics_list: list[dict[str, Any]] = []
    if include_topics:
        attachments = await runtime.store.list_topic_attachments(
            principal=principal, target_id=entity.entity_id
        )
        all_topics = {t.topic_id: t for t in await runtime.store.list_topics(principal=principal)}
        for att in attachments:
            t = all_topics.get(att.topic_id)
            if t is not None:
                topics_list.append(t.model_dump(mode="json"))

    conflicts = await runtime.store.list_conflicts(
        principal=principal, status="open", subject_id=entity.entity_id
    )
    positions = await runtime.store.list_positions(
        principal=principal,
        vehicle_id=entity.entity_id if entity.entity_type == "legal_vehicle" else None,
        issuer_id=entity.entity_id if entity.entity_type == "issuer" else None,
    )
    if entity.entity_type not in ("legal_vehicle", "issuer"):
        positions = []
    coverages = (
        await runtime.store.list_coverages(principal=principal, issuer_id=entity.entity_id)
        if entity.entity_type == "issuer"
        else []
    )

    claim_ids = [c.claim_id for c in claims]
    redacted_ids = [c.claim_id for c in claims if c.evidence_redacted]
    conflict_ids = [cf.conflict_set_id for cf in conflicts]

    receipt = make_receipt(
        principal=principal,
        data_epoch=epochs.data_epoch,
        policy_epoch=epochs.policy_epoch,
        tool_or_endpoint="mcp.read",
        as_of=eff_as_of,
        recorded_as_of=eff_rec,
        snapshot_version=snap_ver,
        claim_ids=claim_ids,
        redacted_evidence_claim_ids=redacted_ids,
        conflict_set_ids=conflict_ids,
    )
    if record_receipt:
        await runtime.store.record_receipt(receipt)

    return {
        "found": True,
        "kind": "entity",
        "entity": entity.model_dump(mode="json"),
        "claims": [_dump_claim(c) for c in claims],
        "facts": [_dump_claim(c) for c in claims if c.channel == "fact"],
        "directives": [_dump_claim(c) for c in claims if c.channel == "directive"],
        "identifiers": [i.model_dump(mode="json") for i in identifiers],
        "lineage": [ev.model_dump(mode="json") for ev in lineage],
        "topics": topics_list,
        "conflicts": [cf.model_dump(mode="json") for cf in conflicts],
        "positions": [p.model_dump(mode="json") for p in positions],
        "coverages": [cv.model_dump(mode="json") for cv in coverages],
        "receipt_id": receipt.receipt_id,
        "snapshot_version": snap_ver,
        "data_epoch": epochs.data_epoch,
        "policy_epoch": epochs.policy_epoch,
        "receipt": receipt.model_dump(mode="json"),
    }


async def tool_neighbours(
    runtime: LakeRuntime,
    principal: Principal,
    *,
    id_or_key: str,
    depth: int = 1,
    direction: str = "both",
    predicates: list[str] | None = None,
    as_of: datetime | str | None = None,
    recorded_as_of: datetime | str | None = None,
    record_receipt: bool = True,
) -> dict[str, Any]:
    """Bounded-hop (1-3) bidirectional/directional traversal omitting unauthorized neighbors."""
    eff_depth = max(1, min(int(depth), 3))
    eff_dir = _normalize_direction(direction)
    eff_as_of = ensure_utc(as_of)
    eff_rec = ensure_utc(recorded_as_of)

    # Zero-leakage root existence check
    entity = await runtime.store.get_entity(id_or_key, principal=principal)
    epochs = await runtime.store.get_epochs()
    if entity is None:
        return {
            "found": False,
            "error": {"code": "not_found", "message": "Resource not found"},
            "nodes": [],
            "edges": [],
            "data_epoch": epochs.data_epoch,
            "policy_epoch": epochs.policy_epoch,
        }

    graph = await runtime.retrieval.neighbours(
        entity.entity_id,
        principal=principal,
        depth=eff_depth,
        direction=eff_dir,
        predicates=predicates,
        as_of=eff_as_of,
        recorded_as_of=eff_rec,
    )
    snap_ver = await _get_latest_snapshot_version(runtime, principal)
    receipt = make_receipt(
        principal=principal,
        data_epoch=epochs.data_epoch,
        policy_epoch=epochs.policy_epoch,
        tool_or_endpoint="mcp.neighbours",
        as_of=eff_as_of,
        recorded_as_of=eff_rec,
        snapshot_version=snap_ver,
    )
    if record_receipt:
        await runtime.store.record_receipt(receipt)

    payload = graph.model_dump(mode="json")
    payload.update(
        {
            "found": True,
            "root_id": entity.entity_id,
            "depth": eff_depth,
            "direction": eff_dir,
            "receipt_id": receipt.receipt_id,
            "snapshot_version": snap_ver,
            "data_epoch": epochs.data_epoch,
            "policy_epoch": epochs.policy_epoch,
            "receipt": receipt.model_dump(mode="json"),
        }
    )
    return payload


async def tool_impact(
    runtime: LakeRuntime,
    principal: Principal,
    *,
    node_id: str,
    max_depth: int = 5,
    as_of: datetime | str | None = None,
    recorded_as_of: datetime | str | None = None,
    record_receipt: bool = True,
) -> dict[str, Any]:
    """ACL-enforced reverse-BFS impact traversal from any authorized node."""
    eff_as_of = ensure_utc(as_of)
    eff_rec = ensure_utc(recorded_as_of)
    epochs = await runtime.store.get_epochs()

    try:
        impact = await runtime.graph.impact_traversal(
            node_id,
            principal=principal,
            max_depth=max(1, min(int(max_depth), 6)),
            as_of=eff_as_of,
            recorded_as_of=eff_rec,
        )
    except NotFoundError:
        return {
            "found": False,
            "error": {"code": "not_found", "message": "Resource not found"},
            "data_epoch": epochs.data_epoch,
            "policy_epoch": epochs.policy_epoch,
        }

    snap_ver = await _get_latest_snapshot_version(runtime, principal)
    receipt = make_receipt(
        principal=principal,
        data_epoch=epochs.data_epoch,
        policy_epoch=epochs.policy_epoch,
        tool_or_endpoint="mcp.impact",
        as_of=eff_as_of,
        recorded_as_of=eff_rec,
        snapshot_version=snap_ver,
    )
    if record_receipt:
        await runtime.store.record_receipt(receipt)

    data = impact.model_dump(mode="json")
    data["found"] = True
    data["receipt_id"] = receipt.receipt_id
    data["snapshot_version"] = snap_ver
    data["data_epoch"] = epochs.data_epoch
    data["policy_epoch"] = epochs.policy_epoch
    data["receipt"] = receipt.model_dump(mode="json")
    return data


async def tool_lookup(
    runtime: LakeRuntime,
    principal: Principal,
    *,
    canonical_key: str,
    predicate: str,
    context_purpose: str = "general",
    as_of: datetime | str | None = None,
    recorded_as_of: datetime | str | None = None,
    snapshot_version: int | None = None,
    record_receipt: bool = True,
) -> dict[str, Any]:
    """Deterministic exact mapping lookup (approved facts + conflict flag; excludes unverified observations)."""
    eff_as_of = ensure_utc(as_of)
    eff_rec = ensure_utc(recorded_as_of)
    epochs = await runtime.store.get_epochs()
    snap_ver = snapshot_version or await _get_latest_snapshot_version(runtime, principal)

    entity = await runtime.store.get_entity(canonical_key, principal=principal)
    if entity is None:
        return {
            "found": False,
            "error": {"code": "not_found", "message": "Resource not found"},
            "data_epoch": epochs.data_epoch,
            "policy_epoch": epochs.policy_epoch,
        }

    claims = await runtime.store.query_claims(
        principal=principal,
        subject_ids=[entity.entity_id],
        predicates=[predicate],
        context_purpose=context_purpose if context_purpose != "general" else None,
        as_of=eff_as_of,
        recorded_as_of=eff_rec,
    )
    if context_purpose != "general":
        exact = [c for c in claims if c.context_purpose == context_purpose]
        if exact:
            claims = exact
    else:
        exact_general = [c for c in claims if c.context_purpose == "general"]
        if exact_general:
            claims = exact_general

    approved = [c for c in claims if c.epistemic_status == "approved_fact"]
    disputed = [c for c in claims if c.epistemic_status == "disputed"]
    # Never inject unverified_observation into deterministic lookup
    approved.sort(key=lambda c: (c.valid_from, c.recorded_at, c.claim_id), reverse=True)
    primary = approved[0] if approved else None

    open_conflicts = [
        cf
        for cf in await runtime.store.list_conflicts(
            principal=principal, status="open", subject_id=entity.entity_id
        )
        if cf.predicate == predicate
        and (context_purpose == "general" or cf.context_purpose == context_purpose)
    ]

    surfaced_claims = ([primary] if primary else []) + disputed
    receipt = make_receipt(
        principal=principal,
        data_epoch=epochs.data_epoch,
        policy_epoch=epochs.policy_epoch,
        tool_or_endpoint="mcp.lookup",
        as_of=eff_as_of,
        recorded_as_of=eff_rec,
        snapshot_version=snap_ver,
        claim_ids=[c.claim_id for c in surfaced_claims],
        redacted_evidence_claim_ids=[c.claim_id for c in surfaced_claims if c.evidence_redacted],
        conflict_set_ids=[cf.conflict_set_id for cf in open_conflicts],
    )
    if record_receipt:
        await runtime.store.record_receipt(receipt)

    return {
        "found": primary is not None or bool(disputed),
        "canonical_key": entity.canonical_key,
        "entity_id": entity.entity_id,
        "predicate": predicate,
        "context_purpose": primary.context_purpose if primary else context_purpose,
        "value": primary.object_value if primary else None,
        "object_entity_id": primary.object_entity_id if primary else None,
        "claim": _dump_claim(primary) if primary else None,
        "has_conflict": bool(open_conflicts or disputed),
        "conflict_set_ids": [cf.conflict_set_id for cf in open_conflicts],
        "disputed_claims": [_dump_claim(c) for c in disputed],
        "receipt_id": receipt.receipt_id,
        "snapshot_version": snap_ver,
        "data_epoch": epochs.data_epoch,
        "policy_epoch": epochs.policy_epoch,
        "receipt": receipt.model_dump(mode="json"),
    }


async def tool_resolve_context(
    runtime: LakeRuntime,
    principal: Principal,
    *,
    entity_keys: list[str],
    skill_id: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
    include_unverified: bool = False,
    as_of: datetime | str | None = None,
    recorded_as_of: datetime | str | None = None,
    snapshot_version: int | None = None,
) -> dict[str, Any]:
    """Two-channel stratified context bundle (facts, directives, conflicts, memories, receipt)."""
    query = ContextBundleQuery(
        entity_keys=entity_keys,
        skill_id=skill_id,
        project_id=project_id,
        run_id=run_id,
        include_unverified=include_unverified,
        as_of=ensure_utc(as_of),
        recorded_as_of=ensure_utc(recorded_as_of),
        snapshot_version=snapshot_version,
        tool_or_endpoint="mcp.resolve_context",
    )
    bundle = await runtime.retrieval.resolve_context(query, principal=principal)
    data = bundle.model_dump(mode="json")
    data["receipt_id"] = bundle.receipt.receipt_id
    data["snapshot_version"] = bundle.receipt.snapshot_version
    data["data_epoch"] = bundle.receipt.data_epoch
    data["policy_epoch"] = bundle.receipt.policy_epoch
    return data


async def tool_flag_knowledge_gap(
    runtime: LakeRuntime,
    principal: Principal,
    *,
    subject_key_or_topic: str,
    kind: str = "gap",
    description: str,
    severity: str = "medium",
    suggested_source: str | None = None,
) -> dict[str, Any]:
    """Record a quarantined unverified_observation for a missing mapping, stale source, or wrong value."""
    norm_kind = (kind or "gap").strip().lower()
    if norm_kind not in VALID_GAP_KINDS:
        raise ValidationError(f"Invalid kind '{kind}'; expected one of {sorted(VALID_GAP_KINDS)}")

    safe_source = validate_safe_uri(suggested_source, field_name="suggested_source")

    # Resolve authorized subject entity or fallback to an authorized operational entity
    target = await runtime.store.get_entity(subject_key_or_topic, principal=principal)
    if target is None:
        entities = await runtime.store.list_entities(principal=principal, limit=50)
        target = next((e for e in entities if e.canonical_key == "proj:q1-2026-audit"), None)
        if target is None and entities:
            target = entities[0]
    if target is None:
        raise NotFoundError("No authorized subject entity available for knowledge gap report")

    payload = ClaimCreateInput(
        subject_id=target.entity_id,
        subject_canonical_key=target.canonical_key,
        predicate=f"knowledge_{norm_kind}",
        object_value=f"[{severity.upper()}] {subject_key_or_topic}: {description}",
        context_purpose="knowledge_factory",
        scope_type="firm",
        scope_id="firm",
        channel="fact",
        epistemic_status="unverified_observation",
        kind=norm_kind,
        is_mandatory=False,
        facets=ClaimFacets(
            domain_nature="non_financial",
            modality="qualitative",
            epistemic_temporal="actual",
            periodicity="point_in_time",
            ingestion_channel="agent_extracted",
        ),
        source_system="agent:flag_knowledge_gap",
        evidence_uri=safe_source,
        excerpt=description[:400],
        wall_side=target.wall_side,
        read_acl_groups=list(target.read_acl_groups),
        write_acl_groups=list(target.write_acl_groups),
        force_supersede=False,
    )
    res = await runtime.store.write_claim(payload, principal=principal)
    epochs = await runtime.store.get_epochs()
    receipt = make_receipt(
        principal=principal,
        data_epoch=res.data_epoch,
        policy_epoch=epochs.policy_epoch,
        tool_or_endpoint="mcp.flag_knowledge_gap",
        claim_ids=[res.claim.claim_id],
    )
    await runtime.store.record_receipt(receipt)
    return {
        "quarantined": True,
        "kind": norm_kind,
        "severity": severity,
        "claim": _dump_claim(res.claim),
        "conflict_detected": res.conflict_detected,
        "conflict_set_id": res.conflict_set_id,
        "data_epoch": res.data_epoch,
        "policy_epoch": epochs.policy_epoch,
        "receipt_id": receipt.receipt_id,
    }


async def tool_propose_update(
    runtime: LakeRuntime,
    principal: Principal,
    *,
    subject_canonical_key: str,
    predicate: str,
    object_value: str,
    rationale: str,
    kind: str = "observation",
    context_purpose: str = "general",
    evidence_uri: str | None = None,
    valid_from: datetime | str | None = None,
    valid_to: datetime | str | None = None,
) -> dict[str, Any]:
    """Quarantined update proposal: ALWAYS writes unverified_observation and NEVER overwrites approved_fact."""
    norm_kind = (kind or "observation").strip().lower()
    if norm_kind not in VALID_GAP_KINDS:
        raise ValidationError(f"Invalid kind '{kind}'; expected one of {sorted(VALID_GAP_KINDS)}")

    safe_uri = validate_safe_uri(evidence_uri, field_name="evidence_uri")
    target = await runtime.store.get_entity(subject_canonical_key, principal=principal)
    if target is None:
        raise NotFoundError("Resource not found")

    payload = ClaimCreateInput(
        subject_id=target.entity_id,
        subject_canonical_key=target.canonical_key,
        predicate=predicate,
        object_value=object_value,
        context_purpose=context_purpose or "general",
        scope_type="firm",
        scope_id="firm",
        channel="fact",
        epistemic_status="unverified_observation",
        kind=norm_kind,
        is_mandatory=False,
        facets=ClaimFacets(
            domain_nature="non_financial",
            modality="qualitative",
            epistemic_temporal="actual",
            periodicity="evergreen",
            ingestion_channel="agent_extracted",
        ),
        source_system="agent:propose_update",
        evidence_uri=safe_uri,
        excerpt=rationale[:400],
        wall_side=target.wall_side,
        read_acl_groups=list(target.read_acl_groups),
        write_acl_groups=list(target.write_acl_groups),
        valid_from=ensure_utc(valid_from),
        valid_to=ensure_utc(valid_to),
        force_supersede=False,
    )
    res = await runtime.store.write_claim(payload, principal=principal)
    epochs = await runtime.store.get_epochs()
    receipt = make_receipt(
        principal=principal,
        data_epoch=res.data_epoch,
        policy_epoch=epochs.policy_epoch,
        tool_or_endpoint="mcp.propose_update",
        claim_ids=[res.claim.claim_id],
        conflict_set_ids=[res.conflict_set_id] if res.conflict_set_id else [],
    )
    await runtime.store.record_receipt(receipt)
    return {
        "quarantined": True,
        "epistemic_status": "unverified_observation",
        "kind": norm_kind,
        "claim": _dump_claim(res.claim),
        "conflict_detected": res.conflict_detected,
        "conflict_set_id": res.conflict_set_id,
        "data_epoch": res.data_epoch,
        "policy_epoch": epochs.policy_epoch,
        "receipt_id": receipt.receipt_id,
    }


async def tool_memory_upsert(
    runtime: LakeRuntime,
    principal: Principal,
    *,
    key: str,
    content: str,
    scope_type: str = "user",
    skill_id: str | None = None,
    tags: list[str] | None = None,
    linked_entity_ids: list[str] | None = None,
    linked_topic_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Write or update caller's personal memory; strictly blocked on shared/service surfaces."""
    if not runtime.policy.is_memory_allowed(principal):
        raise SurfacePolicyError(
            "Personal memory is disabled on shared channels or service identities",
            code="surface_policy_blocked",
            status_code=403,
        )
    if scope_type not in ("user", "user_x_skill", "run"):
        raise ValidationError(f"Unsupported personal memory scope_type: {scope_type}")

    record = await runtime.store.upsert_memory(
        MemoryUpsertInput(
            scope_type=scope_type,  # type: ignore[arg-type]
            skill_id=skill_id,
            key=key,
            content=content,
            tags=list(tags or []),
            linked_entity_ids=list(linked_entity_ids or []),
            linked_topic_ids=list(linked_topic_ids or []),
        ),
        principal=principal,
    )
    epochs = await runtime.store.get_epochs()
    receipt = make_receipt(
        principal=principal,
        data_epoch=epochs.data_epoch,
        policy_epoch=epochs.policy_epoch,
        tool_or_endpoint="mcp.memory_upsert",
        memory_ids=[record.memory_id],
    )
    await runtime.store.record_receipt(receipt)
    return {
        "memory": record.model_dump(mode="json"),
        "data_epoch": epochs.data_epoch,
        "policy_epoch": epochs.policy_epoch,
        "receipt_id": receipt.receipt_id,
    }


async def read_memory_me_resource(runtime: LakeRuntime, principal: Principal) -> dict[str, Any]:
    """Read FastMCP resource `context://memory/me` respecting OBO + direct channel gating."""
    if principal.revoked or await runtime.store.is_principal_revoked(principal.user_id):
        raise AuthenticationError("Principal has been revoked")

    if not runtime.policy.is_memory_allowed(principal):
        reason = (
            "Personal context disabled in shared channel"
            if principal.channel_mode == "shared"
            else "Personal context disabled for service identity"
        )
        return {
            "memory_enabled": False,
            "disabled_reason": reason,
            "notes": [],
            "watchlist": None,
            "alerts": [],
        }

    notes = await runtime.store.list_memories(principal=principal)
    watchlist = await runtime.store.get_watchlist(principal=principal)
    return {
        "memory_enabled": True,
        "disabled_reason": None,
        "notes": [n.model_dump(mode="json") for n in notes],
        "watchlist": watchlist.model_dump(mode="json"),
        "alerts": [a.model_dump(mode="json") for a in watchlist.alerts],
    }


async def read_skill_resource(
    runtime: LakeRuntime, principal: Principal, skill_id: str
) -> dict[str, Any]:
    """Read FastMCP resource `context://skills/{skill_id}` with zero existence leakage."""
    if principal.revoked or await runtime.store.is_principal_revoked(principal.user_id):
        raise AuthenticationError("Principal has been revoked")

    items = await runtime.store.list_knowledge_items(principal=principal, kind="skill_procedure")
    norm = skill_id.strip().lower().removeprefix("skill:")
    for item in items:
        if (
            item.slug.lower() == norm
            or item.item_id.lower() == skill_id.lower()
            or str(item.metadata.get("name", "")).lower() == norm
        ):
            return {
                "found": True,
                "skill_id": item.slug,
                "display_name": item.display_name,
                "include_tools": item.metadata.get("include_tools", []),
                "metadata": item.metadata,
                "body_markdown": item.body_markdown,
                "content_sha256": item.content_sha256,
                "source_path": item.source_path,
                "recorded_at": format_utc(item.recorded_at),
            }
    return {"found": False, "error": {"code": "not_found", "message": "Skill not found"}}
