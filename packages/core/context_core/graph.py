"""ACL-enforced Diagnostic Graph Projection & Reversible Impact BFS Service (ADR-002 & ADR-005)."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Sequence

from context_core.compression import ensure_utc, utc_now
from context_core.exceptions import NotFoundError
from context_core.models import (
    Claim,
    ConflictSet,
    Coverage,
    Entity,
    EntityLineageEvent,
    EntityType,
    GraphCluster,
    GraphEdge,
    GraphLens,
    GraphNode,
    GraphPayload,
    GraphStats,
    ImpactGraphPayload,
    ImpactLayers,
    Position,
    Principal,
    SearchFilter,
    SkillBinding,
    Topic,
    TopicAttachment,
)
from context_core.policy import PolicyEngine
from context_core.ports import StorePort
from context_core.receipts import build_receipt

GOLDEN_ANGLE_DEG = 137.507764

FUND_ENTITY_TYPES = frozenset(
    {
        EntityType.strategy.value,
        EntityType.fund_family.value,
        EntityType.reporting_group.value,
        EntityType.legal_vehicle.value,
        EntityType.share_class.value,
    }
)

MARKET_ENTITY_TYPES = frozenset(
    {
        EntityType.ultimate_parent.value,
        EntityType.pe_sponsor.value,
        EntityType.issuer.value,
        EntityType.instrument.value,
    }
)

CLIENT_ENTITY_TYPES = frozenset(
    {
        EntityType.legal_party.value,
        EntityType.client.value,
        EntityType.consultant.value,
        EntityType.commitment.value,
        EntityType.opportunity.value,
    }
)

SKILL_APP_ENTITY_TYPES = frozenset({EntityType.app.value, EntityType.skill.value})


def _normalize_direction(direction: str) -> str:
    d = (direction or "both").strip().lower()
    if d in ("in", "reverse", "upstream"):
        return "in"
    if d in ("out", "forward", "downstream"):
        return "out"
    return "both"


class GraphProjectionService:
    """Implements GraphProjectionPort with strict post-ACL counts, golden-angle orphans, and impact BFS."""

    def __init__(self, store: StorePort, policy: PolicyEngine | None = None) -> None:
        self.store = store
        self.policy = policy or PolicyEngine()

    async def _load_authorized_graph_state(
        self,
        *,
        principal: Principal,
        as_of: datetime | None,
        recorded_as_of: datetime | None,
    ) -> dict[str, Any]:
        eff_as_of = ensure_utc(as_of) or utc_now()
        eff_rec = ensure_utc(recorded_as_of) or utc_now()

        entities = await self.store.list_entities(principal=principal, limit=None, offset=0)
        entity_map: dict[str, Entity] = {e.entity_id: e for e in entities}
        key_to_id: dict[str, str] = {e.canonical_key: e.entity_id for e in entities}

        claims = await self.store.query_claims(
            principal=principal,
            as_of=eff_as_of,
            recorded_as_of=eff_rec,
            include_superseded=False,
        )
        conflicts = await self.store.list_conflicts(principal=principal, status="open")
        positions = await self.store.list_positions(principal=principal)
        coverages = await self.store.list_coverages(principal=principal)
        skill_bindings = await self.store.list_skill_bindings()
        topics = await self.store.list_topics(principal=principal, include_archived=False)
        attachments = await self.store.list_topic_attachments(principal=principal)

        topic_by_id: dict[str, Topic] = {t.topic_id: t for t in topics}
        topic_by_slug: dict[str, Topic] = {t.slug: t for t in topics}

        # Build entity -> topic slugs strictly from authorized attachments
        entity_topics: dict[str, list[str]] = defaultdict(list)
        claim_by_id: dict[str, Claim] = {c.claim_id: c for c in claims}
        for att in attachments:
            t = topic_by_id.get(att.topic_id)
            if t is None:
                continue
            target_eid: str | None = None
            if att.target_type == "claim":
                clm = claim_by_id.get(att.target_id)
                if clm and clm.subject_id in entity_map:
                    target_eid = clm.subject_id
            elif att.target_id in entity_map:
                target_eid = att.target_id
            if target_eid and t.slug not in entity_topics[target_eid]:
                entity_topics[target_eid].append(t.slug)

        # Open conflicts indexed by subject_id and claim_id (only if >=1 claim is visible in slice)
        subject_conflicts: dict[str, list[ConflictSet]] = defaultdict(list)
        conflict_claim_ids: set[str] = set()
        for cs in conflicts:
            vis_ids = [cid for cid in cs.claim_ids if cid in claim_by_id]
            if vis_ids and cs.subject_id in entity_map:
                subject_conflicts[cs.subject_id].append(cs)
                conflict_claim_ids.update(vis_ids)

        # Coverage status per issuer
        issuer_coverage: dict[str, Coverage] = {}
        for cov in coverages:
            if cov.issuer_entity_id in entity_map:
                issuer_coverage[cov.issuer_entity_id] = cov

        # Lineage events per authorized entity (deduplicated)
        lineage_by_id: dict[str, EntityLineageEvent] = {}
        bulk_lineage = getattr(self.store, "list_lineage_events_for", None)
        if bulk_lineage is not None:
            # One store call instead of one per entity. Rebuild the per-entity iteration order of the
            # original loop so edge order (and therefore the client's layout) is unchanged.
            events_by_eid: dict[str, list[EntityLineageEvent]] = {}
            for ev in await bulk_lineage(None, principal=principal):
                events_by_eid.setdefault(ev.source_entity_id, []).append(ev)
                if ev.target_entity_id != ev.source_entity_id:
                    events_by_eid.setdefault(ev.target_entity_id, []).append(ev)
            for eid in entity_map:
                for ev in events_by_eid.get(eid, ()):
                    if ev.source_entity_id in entity_map and ev.target_entity_id in entity_map:
                        lineage_by_id[ev.event_id] = ev
        else:
            for eid in entity_map:
                for ev in await self.store.list_lineage_events(eid, principal=principal):
                    if ev.source_entity_id in entity_map and ev.target_entity_id in entity_map:
                        lineage_by_id[ev.event_id] = ev

        # Build authorized edges
        edges: list[GraphEdge] = []
        edge_ids: set[str] = set()

        def add_edge(edge: GraphEdge) -> None:
            if edge.id in edge_ids:
                return
            if edge.source not in entity_map or edge.target not in entity_map:
                return
            if edge.source == edge.target:
                return
            edge_ids.add(edge.id)
            edges.append(edge)

        # 1. Entity-to-entity claims
        for c in claims:
            if c.object_entity_id and c.subject_id in entity_map and c.object_entity_id in entity_map:
                add_edge(
                    GraphEdge(
                        id=f"edge:{c.claim_id}",
                        source=c.subject_id,
                        target=c.object_entity_id,
                        predicate=c.predicate,
                        label=c.context_purpose if c.predicate == "maps_as" else c.predicate,
                        context_purpose=c.context_purpose,
                        channel=c.channel,
                        epistemic_status=c.epistemic_status,
                        direction_role="downstream",
                        has_conflict=c.claim_id in conflict_claim_ids,
                    )
                )

        # 2. Reified Positions (vehicle -> instrument and vehicle -> issuer)
        for pos in positions:
            if pos.vehicle_entity_id in entity_map and pos.instrument_entity_id in entity_map:
                add_edge(
                    GraphEdge(
                        id=f"edge:pos:{pos.position_id}:inst",
                        source=pos.vehicle_entity_id,
                        target=pos.instrument_entity_id,
                        predicate="holds_instrument",
                        label=pos.position_type,
                        context_purpose="position",
                        channel="fact",
                        epistemic_status="approved_fact",
                        direction_role="downstream",
                        has_conflict=False,
                    )
                )
            if pos.vehicle_entity_id in entity_map and pos.issuer_entity_id in entity_map:
                add_edge(
                    GraphEdge(
                        id=f"edge:pos:{pos.position_id}:issuer",
                        source=pos.vehicle_entity_id,
                        target=pos.issuer_entity_id,
                        predicate="holds_position_in",
                        label=pos.position_type,
                        context_purpose="position",
                        channel="fact",
                        epistemic_status="approved_fact",
                        direction_role="downstream",
                        has_conflict=False,
                    )
                )

        # 3. Reified Coverages (analyst -> issuer)
        for cov in coverages:
            if cov.analyst_entity_id in entity_map and cov.issuer_entity_id in entity_map:
                add_edge(
                    GraphEdge(
                        id=f"edge:cov:{cov.coverage_id}",
                        source=cov.analyst_entity_id,
                        target=cov.issuer_entity_id,
                        predicate=f"covers_{cov.coverage_status}",
                        label=cov.coverage_status,
                        context_purpose="coverage",
                        channel="fact",
                        epistemic_status="approved_fact",
                        direction_role="downstream",
                        has_conflict=False,
                    )
                )

        # 4. Reified Skill Bindings (skill -> target) — only when BOTH endpoints are authorized
        for sb in skill_bindings:
            if not sb.active:
                continue
            if sb.skill_entity_id in entity_map and sb.target_entity_id in entity_map:
                # Avoid duplicate if an explicit maps_as claim already connects same pair
                has_claim_edge = any(
                    e.source == sb.skill_entity_id and e.target == sb.target_entity_id and e.predicate == "maps_as"
                    for e in edges
                )
                if not has_claim_edge:
                    add_edge(
                        GraphEdge(
                            id=f"edge:bind:{sb.binding_id}",
                            source=sb.skill_entity_id,
                            target=sb.target_entity_id,
                            predicate="binds_to",
                            label=sb.mapping_purpose,
                            context_purpose=sb.mapping_purpose,
                            channel="fact",
                            epistemic_status="approved_fact",
                            direction_role="downstream",
                            has_conflict=sb.target_entity_id in subject_conflicts,
                        )
                    )

        # 5. Lineage events (source -> target)
        for ev in lineage_by_id.values():
            add_edge(
                GraphEdge(
                    id=f"edge:lin:{ev.event_id}",
                    source=ev.source_entity_id,
                    target=ev.target_entity_id,
                    predicate=ev.event_type,
                    label=ev.event_type.replace("_", " "),
                    context_purpose="lineage",
                    channel="fact",
                    epistemic_status="approved_fact",
                    direction_role="upstream",
                    has_conflict=False,
                )
            )

        return {
            "as_of": eff_as_of,
            "recorded_as_of": eff_rec,
            "entities": entity_map,
            "key_to_id": key_to_id,
            "claims": claims,
            "claim_by_id": claim_by_id,
            "conflicts": conflicts,
            "subject_conflicts": subject_conflicts,
            "positions": positions,
            "coverages": coverages,
            "issuer_coverage": issuer_coverage,
            "skill_bindings": skill_bindings,
            "topics": topics,
            "topic_by_id": topic_by_id,
            "topic_by_slug": topic_by_slug,
            "attachments": attachments,
            "entity_topics": entity_topics,
            "lineage": list(lineage_by_id.values()),
            "edges": edges,
        }

    def _cluster_for_entity(
        self,
        entity: Entity,
        lens: GraphLens,
        entity_topics: dict[str, list[str]],
        topic_by_slug: dict[str, Topic],
    ) -> tuple[str, str, str]:
        """Return (cluster_id, cluster_label, cluster_kind) deterministically for a lens."""
        if lens == "topic":
            t_slugs = entity_topics.get(entity.entity_id, [])
            if t_slugs:
                slug = t_slugs[0]
                t = topic_by_slug.get(slug)
                return f"cluster:topic:{slug}", (t.pref_label if t else slug), "topic"
            return (
                f"cluster:type:{entity.entity_type.value}",
                entity.entity_type.value.replace("_", " ").title(),
                "entity_type",
            )
        if lens in ("app", "skill", "app_skill"):
            if entity.entity_type in (EntityType.app, EntityType.skill):
                return "cluster:ops:apps-skills", "Apps & Skills", "app_skill"
            if entity.entity_type.value in FUND_ENTITY_TYPES:
                return "cluster:funds:hierarchy", "Fund Hierarchy", "fund_hierarchy"
            return f"cluster:type:{entity.entity_type.value}", entity.entity_type.value.replace("_", " ").title(), "entity_type"
        if lens == "fund_hierarchy":
            return f"cluster:fund:{entity.entity_type.value}", entity.entity_type.value.replace("_", " ").title(), "fund_hierarchy"
        if lens == "issuer":
            cov_status = entity.attributes.get("coverage_status")
            if cov_status:
                return f"cluster:coverage:{cov_status}", f"Coverage: {str(cov_status).title()}", "coverage"
            return f"cluster:market:{entity.entity_type.value}", entity.entity_type.value.replace("_", " ").title(), "issuer"
        if lens == "project":
            if entity.entity_type == EntityType.project:
                return f"cluster:project:{entity.canonical_key}", entity.display_name, "project"
            return f"cluster:type:{entity.entity_type.value}", entity.entity_type.value.replace("_", " ").title(), "project"
        if lens == "personal":
            return "cluster:personal:my-context", "My Context & Watchlist", "personal"
        # "entity" or fallback
        return f"cluster:type:{entity.entity_type.value}", entity.entity_type.value.replace("_", " ").title(), "entity_type"

    def _lens_filter_entity_ids(
        self,
        lens: GraphLens,
        entity_map: dict[str, Entity],
        edges: list[GraphEdge],
        *,
        personal_ids: set[str] | None = None,
    ) -> set[str]:
        if lens in ("topic", "entity"):
            return set(entity_map.keys())
        if lens == "fund_hierarchy":
            seeds = {eid for eid, e in entity_map.items() if e.entity_type.value in FUND_ENTITY_TYPES}
            # Include 1-hop connected share classes/skills/commitments plus deliberate orphans? Keep fund hierarchy crisp
            out = set(seeds)
            for edge in edges:
                if edge.source in seeds and entity_map[edge.target].entity_type.value in FUND_ENTITY_TYPES | SKILL_APP_ENTITY_TYPES:
                    out.add(edge.target)
                if edge.target in seeds and entity_map[edge.source].entity_type.value in FUND_ENTITY_TYPES | SKILL_APP_ENTITY_TYPES | CLIENT_ENTITY_TYPES:
                    out.add(edge.source)
            return out
        if lens in ("app", "skill", "app_skill"):
            seeds = {eid for eid, e in entity_map.items() if e.entity_type.value in SKILL_APP_ENTITY_TYPES}
            out = set(seeds)
            for edge in edges:
                if edge.source in seeds:
                    out.add(edge.target)
                if edge.target in seeds:
                    out.add(edge.source)
            return out
        if lens == "issuer":
            seeds = {
                eid
                for eid, e in entity_map.items()
                if e.entity_type.value in MARKET_ENTITY_TYPES | {EntityType.legal_party.value}
            }
            out = set(seeds)
            for edge in edges:
                if edge.source in seeds or edge.target in seeds:
                    out.add(edge.source)
                    out.add(edge.target)
            return out
        if lens == "project":
            seeds = {eid for eid, e in entity_map.items() if e.entity_type == EntityType.project}
            out = set(seeds)
            for edge in edges:
                if edge.source in seeds or edge.target in seeds:
                    out.add(edge.source)
                    out.add(edge.target)
            # Also include entities tied via project-scoped claims
            return out if out else set(entity_map.keys())
        if lens == "personal":
            if not personal_ids:
                return set()
            seeds = {eid for eid in personal_ids if eid in entity_map}
            out = set(seeds)
            for edge in edges:
                if edge.source in seeds or edge.target in seeds:
                    out.add(edge.source)
                    out.add(edge.target)
            return out
        return set(entity_map.keys())

    def _apply_search_filter(
        self,
        candidate_ids: set[str],
        state: dict[str, Any],
        filters: SearchFilter | None,
        search_query: str | None,
    ) -> set[str]:
        entities: dict[str, Entity] = state["entities"]
        claims: list[Claim] = state["claims"]
        entity_topics: dict[str, list[str]] = state["entity_topics"]
        subject_conflicts: dict[str, list[ConflictSet]] = state["subject_conflicts"]
        issuer_coverage: dict[str, Coverage] = state["issuer_coverage"]
        topic_by_slug: dict[str, Topic] = state["topic_by_slug"]

        keep = set(candidate_ids)
        if filters is not None:
            if filters.entity_types:
                allowed_types = set(filters.entity_types)
                keep = {eid for eid in keep if entities[eid].entity_type.value in allowed_types}
            if filters.topic_slug:
                slug = filters.topic_slug.strip().lower()
                # Include child topics if filtering by a parent topic (e.g., "financial" or "leverage-covenants")
                matching_slugs = {slug}
                root_topic = topic_by_slug.get(slug)
                if root_topic is not None:
                    q = deque([root_topic.topic_id])
                    while q:
                        cur = q.popleft()
                        for t in state["topics"]:
                            if t.broader_topic_id == cur or t.merged_into_topic_id == cur:
                                if t.slug not in matching_slugs:
                                    matching_slugs.add(t.slug)
                                    q.append(t.topic_id)
                keep = {eid for eid in keep if set(entity_topics.get(eid, [])) & matching_slugs}
            if filters.coverage_statuses:
                allowed_cov = set(filters.coverage_statuses)
                keep = {
                    eid
                    for eid in keep
                    if (
                        (eid in issuer_coverage and issuer_coverage[eid].coverage_status in allowed_cov)
                        or str(entities[eid].attributes.get("coverage_status") or "") in allowed_cov
                    )
                }
            if filters.conflicts_only:
                keep = {eid for eid in keep if eid in subject_conflicts}

            # Plane 3 claim facet filters
            has_facet_filter = any(
                [
                    filters.domain_nature,
                    filters.modality,
                    filters.epistemic_temporal,
                    filters.actual_stage,
                    filters.periodicity,
                    filters.ingestion_channel,
                    filters.scope_types,
                    filters.epistemic_statuses,
                ]
            )
            if has_facet_filter:
                matched_subjects: set[str] = set()
                for c in claims:
                    if filters.domain_nature and c.facets.domain_nature != filters.domain_nature:
                        continue
                    if filters.modality and c.facets.modality != filters.modality:
                        continue
                    if filters.epistemic_temporal and c.facets.epistemic_temporal != filters.epistemic_temporal:
                        continue
                    if filters.actual_stage and c.facets.actual_stage != filters.actual_stage:
                        continue
                    if filters.periodicity and c.facets.periodicity != filters.periodicity:
                        continue
                    if filters.ingestion_channel and c.facets.ingestion_channel != filters.ingestion_channel:
                        continue
                    if filters.scope_types and c.scope_type not in filters.scope_types:
                        continue
                    if filters.epistemic_statuses and c.epistemic_status not in filters.epistemic_statuses:
                        continue
                    matched_subjects.add(c.subject_id)
                    if c.object_entity_id:
                        matched_subjects.add(c.object_entity_id)
                keep &= matched_subjects

        if search_query and search_query.strip():
            ql = search_query.strip().lower()
            matched: set[str] = set()
            for eid in keep:
                e = entities[eid]
                haystack = " ".join(
                    [e.display_name, e.canonical_key, e.description, " ".join(e.aliases), " ".join(entity_topics.get(eid, []))]
                ).lower()
                if ql in haystack:
                    matched.add(eid)
            for c in claims:
                if c.subject_id in keep and (
                    ql in c.predicate.lower() or ql in c.object_value.lower()
                ):
                    matched.add(c.subject_id)
            keep &= matched

        return keep

    def _bfs_focus(
        self,
        focus_eid: str,
        allowed_ids: set[str],
        edges: list[GraphEdge],
        *,
        hops: int,
        direction: str,
    ) -> tuple[set[str], list[GraphEdge]]:
        norm_dir = _normalize_direction(direction)
        out_adj: dict[str, list[GraphEdge]] = defaultdict(list)
        in_adj: dict[str, list[GraphEdge]] = defaultdict(list)
        for edge in edges:
            if edge.source in allowed_ids and edge.target in allowed_ids:
                out_adj[edge.source].append(edge)
                in_adj[edge.target].append(edge)

        visited: set[str] = {focus_eid}
        collected_edges: dict[str, GraphEdge] = {}
        queue: deque[tuple[str, int]] = deque([(focus_eid, 0)])

        while queue:
            cur, dist = queue.popleft()
            if dist >= hops:
                continue
            if norm_dir in ("both", "out"):
                for edge in out_adj.get(cur, []):
                    tagged = edge.model_copy(update={"direction_role": "downstream"})
                    collected_edges[edge.id] = tagged
                    if edge.target not in visited:
                        visited.add(edge.target)
                        queue.append((edge.target, dist + 1))
            if norm_dir in ("both", "in"):
                for edge in in_adj.get(cur, []):
                    tagged = edge.model_copy(update={"direction_role": "upstream"})
                    collected_edges[edge.id] = tagged
                    if edge.source not in visited:
                        visited.add(edge.source)
                        queue.append((edge.source, dist + 1))

        return visited, list(collected_edges.values())

    async def project_graph(
        self,
        *,
        principal: Principal,
        lens: GraphLens = "topic",
        focus_id: str | None = None,
        hops: int = 2,
        direction: str = "both",
        search_query: str | None = None,
        filters: SearchFilter | None = None,
        as_of: datetime | None = None,
        recorded_as_of: datetime | None = None,
    ) -> GraphPayload:
        state = await self._load_authorized_graph_state(
            principal=principal, as_of=as_of, recorded_as_of=recorded_as_of
        )
        entities: dict[str, Entity] = state["entities"]
        key_to_id: dict[str, str] = state["key_to_id"]
        all_edges: list[GraphEdge] = state["edges"]
        claims: list[Claim] = state["claims"]
        subject_conflicts: dict[str, list[ConflictSet]] = state["subject_conflicts"]
        entity_topics: dict[str, list[str]] = state["entity_topics"]
        topic_by_slug: dict[str, Topic] = state["topic_by_slug"]
        issuer_coverage: dict[str, Coverage] = state["issuer_coverage"]

        personal_ids: set[str] = set()
        if lens == "personal" and self.policy.is_memory_allowed(principal):
            wl = await self.store.get_watchlist(principal=principal)
            personal_ids.update(wl.entity_ids)
            personal_ids.update(wl.source_ids)
            memories = await self.store.list_memories(principal=principal)
            for m in memories:
                personal_ids.update(m.linked_entity_ids)

        visible_ids = self._lens_filter_entity_ids(
            lens, entities, all_edges, personal_ids=personal_ids
        )
        visible_ids = self._apply_search_filter(visible_ids, state, filters, search_query)

        # Resolve optional focus_id (supports entity_id OR canonical_key)
        selected_edges: list[GraphEdge]
        if focus_id is not None:
            resolved_focus = focus_id if focus_id in entities else key_to_id.get(focus_id.lower())
            if resolved_focus is None:
                raise NotFoundError(f"Graph focus node not found: {focus_id}")
            visible_ids.add(resolved_focus)
            visible_ids, selected_edges = self._bfs_focus(
                resolved_focus,
                set(entities.keys()),
                all_edges,
                hops=max(0, min(hops, 4)),
                direction=direction,
            )
        else:
            selected_edges = [
                e for e in all_edges if e.source in visible_ids and e.target in visible_ids
            ]

        # Compute post-ACL degrees strictly over visible edges
        deg_in: dict[str, int] = defaultdict(int)
        deg_out: dict[str, int] = defaultdict(int)
        for edge in selected_edges:
            deg_out[edge.source] += 1
            deg_in[edge.target] += 1

        # Compute epistemic badge per entity strictly from caller-visible claims
        claims_by_subject: dict[str, list[Claim]] = defaultdict(list)
        for c in claims:
            if c.subject_id in visible_ids:
                claims_by_subject[c.subject_id].append(c)

        nodes: list[GraphNode] = []
        cluster_meta: dict[str, tuple[str, str]] = {}
        cluster_counts: dict[str, int] = defaultdict(int)

        sorted_eids = sorted(visible_ids)
        orphan_idx = 0
        for eid in sorted_eids:
            ent = entities[eid]
            cid, clabel, ckind = self._cluster_for_entity(ent, lens, entity_topics, topic_by_slug)
            cluster_meta[cid] = (clabel, ckind)
            cluster_counts[cid] += 1

            din = deg_in[eid]
            dout = deg_out[eid]
            is_orphan = (din + dout) == 0
            orphan_angle: float | None = None
            if is_orphan:
                orphan_angle = round((orphan_idx * GOLDEN_ANGLE_DEG) % 360.0, 4)
                orphan_idx += 1

            open_cs = subject_conflicts.get(eid, [])
            subj_claims = claims_by_subject.get(eid, [])
            if open_cs:
                badge = "disputed"
            elif any(c.epistemic_status == "unverified_observation" for c in subj_claims) and not any(
                c.epistemic_status == "approved_fact" for c in subj_claims
            ):
                badge = "unverified_observation"
            else:
                badge = "approved_fact"

            cov = issuer_coverage.get(eid)
            cov_status = cov.coverage_status if cov else ent.attributes.get("coverage_status")

            nodes.append(
                GraphNode(
                    id=eid,
                    canonical_key=ent.canonical_key,
                    label=ent.display_name,
                    entity_type=ent.entity_type.value,
                    cluster_id=cid,
                    wall_side=ent.wall_side,
                    degree_in=din,
                    degree_out=dout,
                    is_orphan=is_orphan,
                    orphan_angle_deg=orphan_angle,
                    has_conflict=bool(open_cs),
                    open_conflict_count=len(open_cs),
                    epistemic_badge=badge,
                    topics=sorted(entity_topics.get(eid, [])),
                    coverage_status=str(cov_status) if cov_status is not None else None,
                )
            )

        clusters = [
            GraphCluster(id=cid, label=cluster_meta[cid][0], kind=cluster_meta[cid][1], member_count=cnt)
            for cid, cnt in sorted(cluster_counts.items())
        ]

        epochs = await self.store.get_epochs()
        touched_claims = [c for c in claims if c.subject_id in visible_ids]
        touched_conflicts = [cs for eid in visible_ids for cs in subject_conflicts.get(eid, [])]
        receipt = build_receipt(
            principal=principal,
            epochs=epochs,
            as_of=state["as_of"],
            recorded_as_of=state["recorded_as_of"],
            claims=touched_claims,
            conflicts=touched_conflicts,
            tool_or_endpoint=f"graph:{lens}",
        )
        await self.store.record_receipt(receipt)

        return GraphPayload(
            lens=lens,
            nodes=nodes,
            edges=sorted(selected_edges, key=lambda e: e.id),
            clusters=clusters,
            stats=GraphStats(
                node_count=len(nodes),
                edge_count=len(selected_edges),
                conflict_count=sum(1 for n in nodes if n.has_conflict),
                orphan_count=sum(1 for n in nodes if n.is_orphan),
            ),
            data_epoch=epochs.data_epoch,
            policy_epoch=epochs.policy_epoch,
            receipt_id=receipt.receipt_id,
        )

    async def impact_traversal(
        self,
        node_id: str,
        *,
        principal: Principal,
        max_depth: int = 5,
        as_of: datetime | None = None,
        recorded_as_of: datetime | None = None,
    ) -> ImpactGraphPayload:
        """ACL- and wall_side-enforced reverse BFS across reified positions, funds, clients, skills & watchlist."""
        state = await self._load_authorized_graph_state(
            principal=principal, as_of=as_of, recorded_as_of=recorded_as_of
        )
        entities: dict[str, Entity] = state["entities"]
        key_to_id: dict[str, str] = state["key_to_id"]
        claims: list[Claim] = state["claims"]
        positions: list[Position] = state["positions"]
        skill_bindings: list[SkillBinding] = state["skill_bindings"]
        topic_by_id: dict[str, Topic] = state["topic_by_id"]
        topic_by_slug: dict[str, Topic] = state["topic_by_slug"]
        attachments: list[TopicAttachment] = state["attachments"]
        subject_conflicts: dict[str, list[ConflictSet]] = state["subject_conflicts"]
        entity_topics: dict[str, list[str]] = state["entity_topics"]

        # Resolve starting node: can be an authorized entity_id/canonical_key, a topic_id/slug, or a claim_id
        seed_eids: set[str] = set()
        root_display_id = node_id
        impact_edges: dict[str, GraphEdge] = {}
        layer_positions: set[str] = set()
        layer_funds: set[str] = set()
        layer_clients: set[str] = set()
        layer_skills_apps: set[str] = set()

        resolved_eid = node_id if node_id in entities else key_to_id.get(node_id.lower())
        if resolved_eid is not None:
            root_display_id = resolved_eid
            seed_eids.add(resolved_eid)
            root_ent = entities[resolved_eid]
            # If starting from a data_source, seed every authorized entity whose claims originate from it
            if root_ent.entity_type == EntityType.data_source:
                src_tokens = {
                    root_ent.canonical_key.removeprefix("src:").lower(),
                    *(a.lower() for a in root_ent.aliases),
                }
                for c in claims:
                    if c.provenance.source_system.lower() in src_tokens and c.subject_id in entities:
                        seed_eids.add(c.subject_id)
                        impact_edges[f"impact:src:{c.claim_id}"] = GraphEdge(
                            id=f"impact:src:{c.claim_id}",
                            source=resolved_eid,
                            target=c.subject_id,
                            predicate="feeds_claim",
                            label=c.predicate,
                            context_purpose=c.context_purpose,
                            channel=c.channel,
                            epistemic_status=c.epistemic_status,
                            direction_role="downstream",
                            has_conflict=c.subject_id in subject_conflicts,
                        )
        elif node_id in topic_by_id or node_id.lower() in topic_by_slug:
            topic = topic_by_id.get(node_id) or topic_by_slug[node_id.lower()]
            root_display_id = topic.topic_id
            for att in attachments:
                if att.topic_id != topic.topic_id:
                    continue
                if att.target_type == "claim":
                    clm = state["claim_by_id"].get(att.target_id)
                    if clm and clm.subject_id in entities:
                        seed_eids.add(clm.subject_id)
                elif att.target_id in entities:
                    seed_eids.add(att.target_id)
        elif node_id in state["claim_by_id"]:
            clm = state["claim_by_id"][node_id]
            root_display_id = clm.claim_id
            if clm.subject_id in entities:
                seed_eids.add(clm.subject_id)
            if clm.object_entity_id and clm.object_entity_id in entities:
                seed_eids.add(clm.object_entity_id)
        else:
            # Unauthorized or nonexistent -> strict 404 (zero existence leakage)
            raise NotFoundError(f"Impact node not found: {node_id}")

        # Build reverse-impact adjacency strictly across authorized records!
        # Reverse impact flows from upstream data/issuer/vehicle -> positions -> funds -> clients/opps -> skills/apps
        impact_adj: dict[str, list[tuple[str, GraphEdge]]] = defaultdict(list)

        def add_impact_link(src: str, dst: str, edge: GraphEdge) -> None:
            if src not in entities or dst not in entities or src == dst:
                return
            impact_adj[src].append((dst, edge))

        # Positions link issuer/instrument -> vehicle
        for pos in positions:
            if pos.vehicle_entity_id not in entities:
                continue
            if pos.issuer_entity_id in entities:
                add_impact_link(
                    pos.issuer_entity_id,
                    pos.vehicle_entity_id,
                    GraphEdge(
                        id=f"impact:pos:{pos.position_id}:issuer",
                        source=pos.issuer_entity_id,
                        target=pos.vehicle_entity_id,
                        predicate="held_by_vehicle",
                        label=f"position ({pos.position_type})",
                        context_purpose="position",
                        direction_role="downstream",
                    ),
                )
            if pos.instrument_entity_id in entities:
                add_impact_link(
                    pos.instrument_entity_id,
                    pos.vehicle_entity_id,
                    GraphEdge(
                        id=f"impact:pos:{pos.position_id}:inst",
                        source=pos.instrument_entity_id,
                        target=pos.vehicle_entity_id,
                        predicate="held_by_vehicle",
                        label=f"position ({pos.position_type})",
                        context_purpose="position",
                        direction_role="downstream",
                    ),
                )

        # Claims propagation (strictly authorized)
        for c in claims:
            if not c.object_entity_id or c.subject_id not in entities or c.object_entity_id not in entities:
                continue
            # Forward structural / issuer->instrument / legal_party->roles
            if c.predicate in (
                "issues_instrument",
                "sponsors_issuer",
                "acts_as_issuer",
                "acts_as_lp_client",
                "acts_as_counterparty",
                "has_share_class",
            ):
                add_impact_link(
                    c.subject_id,
                    c.object_entity_id,
                    GraphEdge(
                        id=f"impact:clm:{c.claim_id}:fwd",
                        source=c.subject_id,
                        target=c.object_entity_id,
                        predicate=c.predicate,
                        label=c.predicate,
                        context_purpose=c.context_purpose,
                        direction_role="downstream",
                    ),
                )
            # Reverse consumer propagation:
            # vehicle -> reporting_group -> fund_family -> strategy
            # vehicle -> commitment -> client / legal_party
            # vehicle -> opportunity -> client <- consultant
            # target -> skill -> app
            if c.predicate in (
                "includes_vehicle",
                "has_reporting_group",
                "has_fund_family",
                "committed_to_vehicle",
                "holds_commitment",
                "targets_vehicle",
                "pursues_opportunity",
                "advises_client",
                "maps_as",
                "hosts_skill",
                "acts_as_lp_client",
                "acts_as_counterparty",
                "acts_as_issuer",
            ):
                add_impact_link(
                    c.object_entity_id,
                    c.subject_id,
                    GraphEdge(
                        id=f"impact:clm:{c.claim_id}:rev",
                        source=c.object_entity_id,
                        target=c.subject_id,
                        predicate=f"impacts_{c.predicate}",
                        label=c.predicate,
                        context_purpose=c.context_purpose,
                        direction_role="downstream",
                    ),
                )

        # Also link sibling feeder vehicles inside a reporting group to catch LP impact when Master holds an issuer
        for c in claims:
            if c.predicate == "includes_vehicle" and c.subject_id in entities and c.object_entity_id in entities:
                add_impact_link(
                    c.subject_id,
                    c.object_entity_id,
                    GraphEdge(
                        id=f"impact:grp:{c.claim_id}",
                        source=c.subject_id,
                        target=c.object_entity_id,
                        predicate="includes_vehicle",
                        label="reporting_group_member",
                        context_purpose="consolidated_group",
                        direction_role="downstream",
                    ),
                )

        # Skill bindings link target -> skill
        for sb in skill_bindings:
            if sb.active and sb.target_entity_id in entities and sb.skill_entity_id in entities:
                add_impact_link(
                    sb.target_entity_id,
                    sb.skill_entity_id,
                    GraphEdge(
                        id=f"impact:bind:{sb.binding_id}",
                        source=sb.target_entity_id,
                        target=sb.skill_entity_id,
                        predicate="consumed_by_skill",
                        label=sb.mapping_purpose,
                        context_purpose=sb.mapping_purpose,
                        direction_role="downstream",
                    ),
                )

        # Lineage events (bidirectional impact across renames/LMEs/mergers)
        for ev in state["lineage"]:
            add_impact_link(
                ev.source_entity_id,
                ev.target_entity_id,
                GraphEdge(
                    id=f"impact:lin:{ev.event_id}:fwd",
                    source=ev.source_entity_id,
                    target=ev.target_entity_id,
                    predicate=ev.event_type,
                    label=ev.event_type,
                    context_purpose="lineage",
                    direction_role="downstream",
                ),
            )
            add_impact_link(
                ev.target_entity_id,
                ev.source_entity_id,
                GraphEdge(
                    id=f"impact:lin:{ev.event_id}:rev",
                    source=ev.target_entity_id,
                    target=ev.source_entity_id,
                    predicate=ev.event_type,
                    label=ev.event_type,
                    context_purpose="lineage",
                    direction_role="downstream",
                ),
            )

        # Bounded reverse BFS strictly within authorized entity graph
        visited: set[str] = set(seed_eids)
        queue: deque[tuple[str, int]] = deque((eid, 0) for eid in sorted(seed_eids))
        while queue:
            cur, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for nxt, edge in impact_adj.get(cur, []):
                # Double-check authorization before enqueueing (zero leakage)
                if nxt not in entities:
                    continue
                impact_edges[edge.id] = edge
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append((nxt, depth + 1))

        # Classify impacted entities into ADR-005 layers
        for pos in positions:
            if (
                pos.issuer_entity_id in visited
                or pos.instrument_entity_id in visited
                or pos.vehicle_entity_id in visited
            ):
                if pos.issuer_entity_id in seed_eids or pos.instrument_entity_id in seed_eids or pos.vehicle_entity_id in visited:
                    layer_positions.add(pos.position_id)

        for eid in visited:
            ent = entities[eid]
            if ent.entity_type.value in FUND_ENTITY_TYPES:
                layer_funds.add(eid)
            elif ent.entity_type.value in CLIENT_ENTITY_TYPES:
                layer_clients.add(eid)
            elif ent.entity_type.value in SKILL_APP_ENTITY_TYPES:
                layer_skills_apps.add(eid)

        # Personal watchlist hits ONLY when OBO + direct channel (ADR-002 / ADR-003)
        watchlist_hits: list[str] = []
        if self.policy.is_memory_allowed(principal):
            wl = await self.store.get_watchlist(principal=principal)
            watched_eids = set(wl.entity_ids) | set(wl.source_ids)
            watched_tids = set(wl.topic_ids)
            hit_set = visited & watched_eids
            if root_display_id in watched_tids:
                hit_set.add(root_display_id)
            for eid in visited:
                for slug in entity_topics.get(eid, []):
                    t = topic_by_slug.get(slug)
                    if t and t.topic_id in watched_tids:
                        hit_set.add(t.topic_id)
            watchlist_hits = sorted(hit_set)

        # Build nodes for visited subgraph
        deg_in: dict[str, int] = defaultdict(int)
        deg_out: dict[str, int] = defaultdict(int)
        final_edges = [
            e for e in impact_edges.values() if e.source in visited and e.target in visited
        ]
        for edge in final_edges:
            deg_out[edge.source] += 1
            deg_in[edge.target] += 1

        nodes: list[GraphNode] = []
        for eid in sorted(visited):
            ent = entities[eid]
            open_cs = subject_conflicts.get(eid, [])
            nodes.append(
                GraphNode(
                    id=eid,
                    canonical_key=ent.canonical_key,
                    label=ent.display_name,
                    entity_type=ent.entity_type.value,
                    cluster_id=f"cluster:impact:{ent.entity_type.value}",
                    wall_side=ent.wall_side,
                    degree_in=deg_in[eid],
                    degree_out=deg_out[eid],
                    is_orphan=(deg_in[eid] + deg_out[eid]) == 0,
                    has_conflict=bool(open_cs),
                    open_conflict_count=len(open_cs),
                    epistemic_badge="disputed" if open_cs else "approved_fact",
                    topics=sorted(entity_topics.get(eid, [])),
                )
            )

        epochs = await self.store.get_epochs()
        touched_claims = [c for c in claims if c.subject_id in visited]
        receipt = build_receipt(
            principal=principal,
            epochs=epochs,
            as_of=state["as_of"],
            recorded_as_of=state["recorded_as_of"],
            claims=touched_claims,
            tool_or_endpoint=f"impact:{root_display_id}",
        )
        await self.store.record_receipt(receipt)

        return ImpactGraphPayload(
            root_node_id=root_display_id,
            layers=ImpactLayers(
                positions=sorted(layer_positions),
                funds=sorted(layer_funds),
                clients_opportunities=sorted(layer_clients),
                skills_apps=sorted(layer_skills_apps),
                watchlist_hits=watchlist_hits,
            ),
            nodes=nodes,
            edges=sorted(final_edges, key=lambda e: e.id),
            total_affected=len(visited),
            receipt_id=receipt.receipt_id,
        )
