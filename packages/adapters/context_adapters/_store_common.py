"""Shared bitemporal write, conflict, staleness, and watchlist-alert invariants for StorePort adapters."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Iterable, Sequence

from context_core.bitemporal import claim_collision_key, claims_conflict, is_claim_visible
from context_core.compression import ensure_utc, lf_sha256_file, to_posix_path, utc_now
from context_core.exceptions import (
    AuthenticationError,
    NotFoundError,
    OptimisticConcurrencyError,
    ValidationError,
)
from context_core.models import (
    Claim,
    ClaimACL,
    ClaimCreateInput,
    ConflictSet,
    Entity,
    KnowledgeItem,
    Principal,
    Provenance,
    StalenessItem,
    StalenessReport,
    Topic,
    TopicAttachment,
    WallSide,
    WatchlistAlert,
    WatchlistRecord,
)
from context_core.policy import PolicyEngine, sanitize_uri_secrets, validate_no_ssrf, wall_allows


def prepare_incoming_claim(
    payload: ClaimCreateInput,
    *,
    target_entity: Entity,
    principal: Principal,
    repo_root: Path | None = None,
) -> Claim:
    """Validate and construct a canonical Claim from ClaimCreateInput."""
    if payload.is_mandatory:
        if payload.channel != "directive" or payload.scope_type not in ("firm", "project", "skill"):
            raise ValidationError(
                "is_mandatory=True is only valid on directive channel at firm, project, or skill scope"
            )

    now = utc_now()
    prov = (payload.provenance or Provenance()).model_copy(deep=True)
    if prov.evidence_uri is not None:
        prov.evidence_uri = validate_no_ssrf(prov.evidence_uri)
        if prov.evidence_uri and not prov.content_sha256 and repo_root is not None:
            candidate = repo_root / prov.evidence_uri
            if candidate.is_file():
                prov.content_sha256 = lf_sha256_file(candidate)

    acl = (payload.acl or ClaimACL()).model_copy(deep=True)
    # Never allow a claim on a private_side / above_the_wall entity to downgrade its wall_side to public_side
    if payload.wall_side is None and target_entity.wall_side != WallSide.public_side:
        acl.wall_side = target_entity.wall_side
        if payload.read_acl_groups is None:
            acl.read_acl_groups = list(target_entity.read_acl_groups)
    elif not wall_allows(acl.wall_side, target_entity.wall_side):
        raise ValidationError(
            f"Claim wall_side ({acl.wall_side.value}) cannot be less restrictive than target entity ({target_entity.wall_side.value})"
        )

    if payload.scope_type in ("user", "user_x_skill"):
        if payload.scope_type == "user" and not payload.scope_id:
            scope_id = principal.user_id
        else:
            scope_id = payload.scope_id
        # Ensure owner has read access to their own user-scoped directive/claim
        if not principal.groups.intersection(acl.read_acl_groups):
            acl.read_acl_groups = sorted(principal.groups)[:1] or ["firm-all"]
    else:
        scope_id = payload.scope_id

    claim_id = payload.claim_id or f"clm-{uuid.uuid4().hex[:12]}"
    return Claim(
        claim_id=claim_id,
        subject_id=target_entity.entity_id,
        predicate=payload.predicate,
        object_value=payload.object_value,
        object_entity_id=payload.object_entity_id,
        context_purpose=payload.context_purpose,
        scope_type=payload.scope_type,
        scope_id=scope_id,
        channel=payload.channel,
        epistemic_status=payload.epistemic_status,
        kind=payload.kind,
        is_mandatory=payload.is_mandatory,
        facets=payload.facets.model_copy(deep=True),
        provenance=prov,
        acl=acl,
        valid_from=ensure_utc(payload.valid_from) or now,
        valid_to=ensure_utc(payload.valid_to),
        recorded_at=ensure_utc(payload.recorded_at) or now,
        superseded_at=None,
        superseded_by=None,
        created_by=principal.user_id,
    )


def check_toctou_epoch(expected_data_epoch: int | None, current_data_epoch: int) -> None:
    """Enforce optimistic concurrency TOCTOU guard (ADR-003 §5 -> HTTP 409)."""
    if expected_data_epoch is not None and expected_data_epoch != current_data_epoch:
        raise OptimisticConcurrencyError(
            f"Stale data_epoch: expected {expected_data_epoch}, current {current_data_epoch}"
        )


def compute_watchlist_alerts(
    watchlist: WatchlistRecord,
    *,
    principal: Principal,
    policy: PolicyEngine,
    entities: dict[str, Entity],
    claims: Sequence[Claim],
    conflicts: Sequence[ConflictSet],
    attachments: Sequence[TopicAttachment],
) -> list[WatchlistAlert]:
    """Compute strictly ACL-filtered change/conflict alerts for a caller's personal watchlist."""
    if not policy.is_memory_allowed(principal):
        return []

    watched_eids: set[str] = {eid for eid in watchlist.entity_ids if eid in entities}
    watched_tids: set[str] = set(watchlist.topic_ids)
    watched_source_tokens: set[str] = set()
    for sid in watchlist.source_ids:
        src_ent = entities.get(sid)
        if src_ent is not None:
            watched_source_tokens.add(src_ent.canonical_key.removeprefix("src:").lower())
            for alias in src_ent.aliases:
                watched_source_tokens.add(alias.lower())

    # Expand watched topics strictly through authorized attachments
    topic_watched_eids: set[str] = set()
    topic_watched_cids: set[str] = set()
    for att in attachments:
        if att.topic_id not in watched_tids:
            continue
        if not policy.can_read_record(
            principal, wall_side=att.wall_side, read_acl_groups=att.read_acl_groups
        ):
            continue
        if att.target_type == "claim":
            topic_watched_cids.add(att.target_id)
        elif att.target_id in entities:
            topic_watched_eids.add(att.target_id)

    all_watched_eids = watched_eids | topic_watched_eids
    alerts: list[WatchlistAlert] = []
    seen_ids: set[str] = set()

    claim_by_id = {c.claim_id: c for c in claims}

    # 1. Open conflict alerts
    for cs in conflicts:
        if cs.status != "open":
            continue
        if cs.subject_id not in entities:
            continue
        vis_claims = [claim_by_id[cid] for cid in cs.claim_ids if cid in claim_by_id]
        if not vis_claims:
            continue
        source_match = any(
            c.provenance.source_system.lower() in watched_source_tokens for c in vis_claims
        )
        topic_claim_match = any(cid in topic_watched_cids for cid in cs.claim_ids)
        if cs.subject_id in all_watched_eids or source_match or topic_claim_match:
            ent = entities[cs.subject_id]
            aid = f"alert:conflict:{cs.conflict_set_id}"
            if aid not in seen_ids:
                seen_ids.add(aid)
                alerts.append(
                    WatchlistAlert(
                        alert_id=aid,
                        kind="open_conflict",
                        severity="high",
                        target_type="entity" if cs.subject_id in watched_eids else (
                            "data_source" if source_match else "topic"
                        ),
                        target_id=cs.subject_id,
                        entity_canonical_key=ent.canonical_key,
                        title=f"Open {len(vis_claims)}-way conflict on {ent.display_name} ({cs.predicate})",
                        summary=cs.summary or f"Competing values detected on {cs.predicate}",
                        conflict_set_id=cs.conflict_set_id,
                        recorded_at=cs.created_at,
                    )
                )

    # 2. Restated claim revisions & quarantined unverified observations touching watched items
    for c in claims:
        if c.superseded_at is not None or c.subject_id not in entities:
            continue
        ent = entities[c.subject_id]
        source_match = c.provenance.source_system.lower() in watched_source_tokens
        watched_match = (
            c.subject_id in all_watched_eids
            or c.claim_id in topic_watched_cids
            or source_match
        )
        if not watched_match:
            continue
        if c.facets.actual_stage == "restated":
            aid = f"alert:revision:{c.claim_id}"
            if aid not in seen_ids:
                seen_ids.add(aid)
                alerts.append(
                    WatchlistAlert(
                        alert_id=aid,
                        kind="claim_revision",
                        severity="medium",
                        target_type="entity",
                        target_id=c.subject_id,
                        entity_canonical_key=ent.canonical_key,
                        title=f"Restated {c.predicate} on {ent.display_name}",
                        summary=f"Restated value {c.object_value} ({c.facets.fiscal_period or 'current'}) from {c.provenance.source_system}.",
                        claim_id=c.claim_id,
                        recorded_at=c.recorded_at,
                    )
                )
        elif c.epistemic_status == "unverified_observation":
            aid = f"alert:obs:{c.claim_id}"
            if aid not in seen_ids:
                seen_ids.add(aid)
                alerts.append(
                    WatchlistAlert(
                        alert_id=aid,
                        kind="observation",
                        severity="low",
                        target_type="entity",
                        target_id=c.subject_id,
                        entity_canonical_key=ent.canonical_key,
                        title=f"Quarantined observation on {ent.display_name} ({c.predicate})",
                        summary=f"Unverified observation '{c.object_value}' pending steward review.",
                        claim_id=c.claim_id,
                        recorded_at=c.recorded_at,
                    )
                )

    sev_order = {"high": 0, "medium": 1, "low": 2}
    alerts.sort(key=lambda a: (sev_order.get(a.severity, 9), -a.recorded_at.timestamp(), a.alert_id))
    return alerts


def _resolve_evidence_file(ev_uri: str, search_roots: Sequence[Path]) -> Path | None:
    for root in search_roots:
        candidate = root / ev_uri
        if candidate.is_file():
            return candidate
    return None


def compute_sha_staleness(
    *,
    claims: Iterable[Claim],
    knowledge_items: Iterable[KnowledgeItem],
    base_dir: Path,
    extra_search_dirs: Sequence[Path] = (),
    wall_side: str | None = None,
) -> StalenessReport:
    """Zero-LLM deterministic SHA-256 staleness comparison across claims and knowledge items."""
    checked = 0
    stale_items: list[StalenessItem] = []
    roots = [base_dir, *extra_search_dirs]

    for c in claims:
        if c.superseded_at is not None or c.epistemic_status == "revoked":
            continue
        if wall_side is not None and c.acl.wall_side.value != wall_side:
            continue
        ev_uri = c.provenance.evidence_uri
        expected_sha = c.provenance.content_sha256
        if not ev_uri or not expected_sha or "://" in ev_uri:
            continue
        checked += 1
        target_path = _resolve_evidence_file(ev_uri, roots)
        if target_path is None:
            stale_items.append(
                StalenessItem(
                    claim_id=c.claim_id,
                    evidence_path=to_posix_path(ev_uri),
                    expected_sha256=expected_sha,
                    actual_sha256=None,
                    reason="missing_source",
                    wall_side=c.acl.wall_side,
                )
            )
            continue
        actual_sha = lf_sha256_file(target_path)
        if actual_sha != expected_sha:
            stale_items.append(
                StalenessItem(
                    claim_id=c.claim_id,
                    evidence_path=to_posix_path(ev_uri),
                    expected_sha256=expected_sha,
                    actual_sha256=actual_sha,
                    reason="hash_mismatch",
                    wall_side=c.acl.wall_side,
                )
            )

    for ki in knowledge_items:
        if wall_side is not None and ki.wall_side.value != wall_side:
            continue
        if not ki.source_path or not ki.content_sha256 or "://" in ki.source_path:
            continue
        checked += 1
        target_path = _resolve_evidence_file(ki.source_path, roots)
        if target_path is None:
            stale_items.append(
                StalenessItem(
                    item_id=ki.item_id,
                    evidence_path=to_posix_path(ki.source_path),
                    expected_sha256=ki.content_sha256,
                    actual_sha256=None,
                    reason="missing_source",
                    wall_side=ki.wall_side,
                )
            )
            continue
        actual_sha = lf_sha256_file(target_path)
        if actual_sha != ki.content_sha256:
            stale_items.append(
                StalenessItem(
                    item_id=ki.item_id,
                    evidence_path=to_posix_path(ki.source_path),
                    expected_sha256=ki.content_sha256,
                    actual_sha256=actual_sha,
                    reason="hash_mismatch",
                    wall_side=ki.wall_side,
                )
            )

    return StalenessReport(
        checked_count=checked,
        stale_items=stale_items,
        wall_side=WallSide(wall_side) if wall_side is not None else None,
        checked_at=utc_now(),
    )


def apply_topic_patch(
    topics: dict[str, Topic],
    topic_id: str,
    *,
    pref_label: str | None = None,
    alt_labels: list[str] | None = None,
    broader_topic_id: str | None = None,
    related_topic_ids: list[str] | None = None,
    merged_into_topic_id: str | None = None,
    status: str | None = None,
    topic_kind: str | None = None,
) -> tuple[Topic, list[Topic]]:
    """Apply dynamic SKOS topic update (rename, nest/reparent, merge, archive) and return modified topics."""
    if topic_id not in topics:
        raise NotFoundError(f"Topic not found: {topic_id}")
    cur = topics[topic_id].model_copy(deep=True)
    modified: dict[str, Topic] = {}

    if pref_label is not None:
        cur.pref_label = pref_label
    if alt_labels is not None:
        cur.alt_labels = list(alt_labels)
    if related_topic_ids is not None:
        cur.related_topic_ids = list(related_topic_ids)
    if topic_kind is not None:
        cur.topic_kind = topic_kind  # type: ignore[assignment]

    # Reparenting / nesting with cycle protection
    if broader_topic_id is not None and broader_topic_id != cur.broader_topic_id:
        if broader_topic_id == topic_id:
            raise ValidationError("A topic cannot be its own broader parent")
        if broader_topic_id not in topics:
            raise NotFoundError(f"Broader topic not found: {broader_topic_id}")
        # Walk upwards from broader_topic_id to ensure no cycle
        walk: str | None = broader_topic_id
        while walk is not None:
            if walk == topic_id:
                raise ValidationError("Cycle detected in SKOS topic hierarchy")
            walk = topics[walk].broader_topic_id if walk in topics else None

        # Detach from old parent
        if cur.broader_topic_id and cur.broader_topic_id in topics:
            old_parent = topics[cur.broader_topic_id].model_copy(deep=True)
            old_parent.narrower_topic_ids = [
                nid for nid in old_parent.narrower_topic_ids if nid != topic_id
            ]
            modified[old_parent.topic_id] = old_parent

        # Attach to new parent
        new_parent = (modified.get(broader_topic_id) or topics[broader_topic_id]).model_copy(deep=True)
        if topic_id not in new_parent.narrower_topic_ids:
            new_parent.narrower_topic_ids.append(topic_id)
        modified[new_parent.topic_id] = new_parent
        cur.broader_topic_id = broader_topic_id

    # Merging into another topic (preserving redirect aliases)
    if merged_into_topic_id is not None:
        if merged_into_topic_id == topic_id:
            raise ValidationError("A topic cannot merge into itself")
        if merged_into_topic_id not in topics:
            raise NotFoundError(f"Merge target topic not found: {merged_into_topic_id}")
        cur.merged_into_topic_id = merged_into_topic_id
        cur.status = "merged"
        cur.topic_kind = "merged_alias"
        target = (modified.get(merged_into_topic_id) or topics[merged_into_topic_id]).model_copy(deep=True)
        for alias in [cur.slug, cur.pref_label, *cur.alt_labels]:
            if alias and alias not in target.alt_labels and alias != target.pref_label:
                target.alt_labels.append(alias)
        modified[target.topic_id] = target

    if status is not None:
        cur.status = status  # type: ignore[assignment]

    modified[cur.topic_id] = cur
    return cur, list(modified.values())
