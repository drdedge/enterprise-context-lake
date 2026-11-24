"""Dual RetrievalPort adapters: LexicalManifestRetriever (rg/FTS5/regex) and FacetGraphRetriever (ADR-002)."""

from __future__ import annotations

import asyncio
import math
import re
import shutil
import subprocess
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from context_core.bitemporal import resolve_stratified_claims
from context_core.compression import ensure_utc, utc_now
from context_core.exceptions import AuthenticationError, NotFoundError
from context_core.graph import GraphProjectionService
from context_core.models import (
    Claim,
    ConflictSet,
    ContextBundle,
    ContextBundleQuery,
    Coverage,
    Entity,
    EntityIdentifier,
    GraphPayload,
    KnowledgeItem,
    MemoryRecord,
    Principal,
    SearchFilter,
    SearchHit,
    Topic,
    TopicAttachment,
)
from context_core.policy import PolicyEngine
from context_core.ports import EmbeddingProvider, StorePort
from context_core.receipts import build_receipt


def _claim_matches_filter(claim: Claim, flt: SearchFilter | None) -> bool:
    if flt is None:
        return True
    if flt.domain_nature and claim.facets.domain_nature != flt.domain_nature:
        return False
    if flt.modality and claim.facets.modality != flt.modality:
        return False
    if flt.epistemic_temporal and claim.facets.epistemic_temporal != flt.epistemic_temporal:
        return False
    if flt.actual_stage and claim.facets.actual_stage != flt.actual_stage:
        return False
    if flt.periodicity and claim.facets.periodicity != flt.periodicity:
        return False
    if flt.ingestion_channel and claim.facets.ingestion_channel != flt.ingestion_channel:
        return False
    if flt.scope_types and claim.scope_type not in flt.scope_types:
        return False
    if flt.epistemic_statuses and claim.epistemic_status not in flt.epistemic_statuses:
        return False
    return True


def _has_claim_facet_filters(flt: SearchFilter | None) -> bool:
    if flt is None:
        return False
    return any(
        [
            flt.domain_nature,
            flt.modality,
            flt.epistemic_temporal,
            flt.actual_stage,
            flt.periodicity,
            flt.ingestion_channel,
            flt.scope_types,
            flt.epistemic_statuses,
        ]
    )


def _cosine_sim(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class _BaseRetriever:
    """Shared ACL-enforced context bundle resolution, facet slicing, and neighborhood projection."""

    def __init__(self, store: StorePort, policy: PolicyEngine | None = None) -> None:
        self.store = store
        self.policy = policy or PolicyEngine()
        self._graph = GraphProjectionService(store, self.policy)

    async def _check_active_principal(self, principal: Principal) -> Principal:
        if principal.revoked or await self.store.is_principal_revoked(principal.user_id):
            raise AuthenticationError("Principal is revoked")
        return principal

    async def _identifiers_by_entity(
        self, entity_ids: Sequence[str], *, as_of: datetime, recorded_as_of: datetime
    ) -> dict[str, list[EntityIdentifier]]:
        """Fetch identifiers for many entities in one store call (falls back to one call per entity)."""
        bulk = getattr(self.store, "list_identifiers_for", None)
        if bulk is not None:
            return await bulk(list(entity_ids), as_of=as_of, recorded_as_of=recorded_as_of)
        return {
            eid: await self.store.list_identifiers(eid, as_of=as_of, recorded_as_of=recorded_as_of)
            for eid in entity_ids
        }

    async def _authorized_slice(
        self, *, principal: Principal, flt: SearchFilter | None
    ) -> dict[str, Any]:
        eff = await self._check_active_principal(principal)
        eff_as_of = ensure_utc(flt.as_of) if flt and flt.as_of else utc_now()
        eff_rec = ensure_utc(flt.recorded_as_of) if flt and flt.recorded_as_of else utc_now()

        entities = await self.store.list_entities(
            principal=eff,
            entity_types=flt.entity_types if flt else None,
            topic_slug=flt.topic_slug if flt else None,
            limit=None,
            offset=0,
        )
        entity_map = {e.entity_id: e for e in entities}

        claims = await self.store.query_claims(
            principal=eff,
            as_of=eff_as_of,
            recorded_as_of=eff_rec,
            include_superseded=False,
        )
        conflicts = await self.store.list_conflicts(principal=eff, status="open")
        coverages = await self.store.list_coverages(principal=eff)
        topics = await self.store.list_topics(principal=eff, include_archived=False)
        attachments = await self.store.list_topic_attachments(principal=eff)
        knowledge_items = await self.store.list_knowledge_items(principal=eff)

        memories: list[MemoryRecord] = []
        if self.policy.is_memory_allowed(eff):
            memories = await self.store.list_memories(principal=eff)

        topic_by_id = {t.topic_id: t for t in topics}
        claim_by_id = {c.claim_id: c for c in claims}
        entity_topics: dict[str, list[str]] = defaultdict(list)
        for att in attachments:
            t = topic_by_id.get(att.topic_id)
            if t is None:
                continue
            target_eid: str | None = None
            if att.target_type == "claim" and att.target_id in claim_by_id:
                target_eid = claim_by_id[att.target_id].subject_id
            elif att.target_id in entity_map:
                target_eid = att.target_id
            if target_eid and t.slug not in entity_topics[target_eid]:
                entity_topics[target_eid].append(t.slug)

        conflict_subjects = {
            cs.subject_id
            for cs in conflicts
            if any(cid in claim_by_id for cid in cs.claim_ids)
        }
        issuer_cov: dict[str, Coverage] = {c.issuer_entity_id: c for c in coverages}

        return {
            "principal": eff,
            "as_of": eff_as_of,
            "recorded_as_of": eff_rec,
            "entities": entities,
            "entity_map": entity_map,
            "claims": claims,
            "conflicts": conflicts,
            "conflict_subjects": conflict_subjects,
            "issuer_cov": issuer_cov,
            "topics": topics,
            "topic_by_id": topic_by_id,
            "attachments": attachments,
            "entity_topics": entity_topics,
            "knowledge_items": knowledge_items,
            "memories": memories,
        }

    def _filter_entities_by_facets(
        self, slice_data: dict[str, Any], flt: SearchFilter | None
    ) -> list[Entity]:
        entities: list[Entity] = slice_data["entities"]
        claims: list[Claim] = slice_data["claims"]
        conflict_subjects: set[str] = slice_data["conflict_subjects"]
        issuer_cov: dict[str, Coverage] = slice_data["issuer_cov"]

        out = list(entities)
        if flt is None:
            return out
        if flt.coverage_statuses:
            allowed_cov = set(flt.coverage_statuses)
            out = [
                e
                for e in out
                if (e.entity_id in issuer_cov and issuer_cov[e.entity_id].coverage_status in allowed_cov)
                or str(e.attributes.get("coverage_status") or "") in allowed_cov
            ]
        if flt.conflicts_only:
            out = [e for e in out if e.entity_id in conflict_subjects]
        if _has_claim_facet_filters(flt):
            matching_eids: set[str] = set()
            for c in claims:
                if _claim_matches_filter(c, flt):
                    matching_eids.add(c.subject_id)
                    if c.object_entity_id:
                        matching_eids.add(c.object_entity_id)
            out = [e for e in out if e.entity_id in matching_eids]
        return out

    async def filter_items(self, flt: SearchFilter, *, principal: Principal) -> list[SearchHit]:
        data = await self._authorized_slice(principal=principal, flt=flt)
        filtered_entities = self._filter_entities_by_facets(data, flt)
        entity_topics = data["entity_topics"]
        conflict_subjects = data["conflict_subjects"]

        hits: list[SearchHit] = []
        for ent in filtered_entities[flt.offset : flt.offset + flt.limit]:
            hits.append(
                SearchHit(
                    hit_type="entity",
                    id=ent.entity_id,
                    canonical_key=ent.canonical_key,
                    display_name=ent.display_name,
                    entity_type=ent.entity_type,
                    summary=ent.description,
                    score=1.0,
                    wall_side=ent.wall_side,
                    topics=sorted(entity_topics.get(ent.entity_id, [])),
                    epistemic_status="disputed" if ent.entity_id in conflict_subjects else "approved_fact",
                    has_conflict=ent.entity_id in conflict_subjects,
                )
            )
        return hits

    async def neighbours(
        self,
        entity_id_or_key: str,
        *,
        principal: Principal,
        depth: int = 1,
        direction: str = "both",
        predicates: Sequence[str] | None = None,
        as_of: datetime | None = None,
        recorded_as_of: datetime | None = None,
    ) -> GraphPayload:
        eff = await self._check_active_principal(principal)
        payload = await self._graph.project_graph(
            principal=eff,
            lens="topic",
            focus_id=entity_id_or_key,
            hops=max(1, min(depth, 3)),
            direction=direction,
            as_of=as_of,
            recorded_as_of=recorded_as_of,
        )
        if predicates:
            pred_set = set(predicates)
            kept_edges = [e for e in payload.edges if e.predicate in pred_set]
            focus_ent = await self.store.get_entity(entity_id_or_key, principal=eff)
            kept_nids = {e.source for e in kept_edges} | {e.target for e in kept_edges}
            if focus_ent is not None:
                kept_nids.add(focus_ent.entity_id)
            kept_nodes = [n for n in payload.nodes if n.id in kept_nids]
            payload = payload.model_copy(
                update={
                    "nodes": kept_nodes,
                    "edges": kept_edges,
                    "stats": payload.stats.model_copy(
                        update={"node_count": len(kept_nodes), "edge_count": len(kept_edges)}
                    ),
                }
            )
        return payload

    async def resolve_context(
        self, query: ContextBundleQuery, *, principal: Principal
    ) -> ContextBundle:
        eff = await self._check_active_principal(principal)
        eff_as_of = ensure_utc(query.as_of) or utc_now()
        eff_rec = ensure_utc(query.recorded_as_of) or utc_now()

        # Resolve requested entity_keys strictly through ACL-enforced store reads
        entities: list[Entity] = []
        subject_ids: list[str] = []
        for key in query.entity_keys:
            ent = await self.store.get_entity(key, principal=eff)
            if ent is not None:
                entities.append(ent)
                subject_ids.append(ent.entity_id)

        if query.skill_id:
            skill_ent = await self.store.get_entity(query.skill_id, principal=eff)
            if skill_ent is not None and skill_ent.entity_id not in subject_ids:
                entities.append(skill_ent)
                subject_ids.append(skill_ent.entity_id)

        raw_claims: list[Claim] = []
        if subject_ids:
            raw_claims = await self.store.query_claims(
                principal=eff,
                subject_ids=subject_ids,
                predicates=query.predicates,
                context_purpose=query.context_purpose,
                as_of=eff_as_of,
                recorded_as_of=eff_rec,
                include_superseded=False,
            )

        open_conflicts: list[ConflictSet] = []
        for sid in subject_ids:
            open_conflicts.extend(
                await self.store.list_conflicts(principal=eff, status="open", subject_id=sid)
            )

        facts, directives, active_conflicts = resolve_stratified_claims(
            raw_claims,
            principal=eff,
            skill_id=query.skill_id,
            project_id=query.project_id,
            run_id=query.run_id,
            include_shared=query.include_shared,
            include_unverified=query.include_unverified,
            open_conflicts=open_conflicts,
        )

        # Personal memories included ONLY when OBO + direct channel (never on shared/service)
        memories: list[MemoryRecord] = []
        if self.policy.is_memory_allowed(eff):
            all_mems = await self.store.list_memories(principal=eff, skill_id=query.skill_id)
            subj_set = set(subject_ids)
            memories = [
                m
                for m in all_mems
                if (not subj_set)
                or (set(m.linked_entity_ids) & subj_set)
                or (query.skill_id and m.skill_id == query.skill_id)
                or (not m.linked_entity_ids)
            ]

        epochs = await self.store.get_epochs()
        receipt = build_receipt(
            principal=eff,
            epochs=epochs,
            as_of=eff_as_of,
            recorded_as_of=eff_rec,
            claims=facts + directives,
            conflicts=active_conflicts,
            memories=memories,
            snapshot_version=query.snapshot_version,
            tool_or_endpoint=query.tool_or_endpoint,
        )
        await self.store.record_receipt(receipt)

        return ContextBundle(
            source="live",
            entities=entities,
            facts=facts,
            directives=directives,
            conflicts=active_conflicts,
            memories=memories,
            receipt=receipt,
        )


class LexicalManifestRetriever(_BaseRetriever):
    """Retrieval Adapter 1: In-RAM manifest/alias matching + optional `rg` + SQLite FTS5 / Python regex fallback."""

    def __init__(
        self,
        store: StorePort,
        policy: PolicyEngine | None = None,
        *,
        rg_search_root: Path | None = None,
    ) -> None:
        super().__init__(store, policy)
        self._rg_bin: str | None = shutil.which("rg")
        self._rg_search_root = rg_search_root

    @property
    def backend_mode(self) -> str:
        if self._rg_bin and self._rg_search_root and self._rg_search_root.is_dir():
            return "rg"
        if hasattr(self.store, "fts_search"):
            return "sqlite_fts5"
        return "python_regex"

    def _run_rg_sync(self, pattern: str) -> set[str]:
        if not self._rg_bin or not self._rg_search_root or not self._rg_search_root.is_dir():
            return set()
        try:
            proc = subprocess.run(
                [self._rg_bin, "--files-with-matches", "--ignore-case", "--fixed-strings", pattern, str(self._rg_search_root)],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            return {Path(line.strip()).stem.lower() for line in proc.stdout.splitlines() if line.strip()}
        except Exception:
            return set()

    async def search(
        self,
        query: str,
        *,
        flt: SearchFilter | None = None,
        principal: Principal,
        limit: int = 25,
    ) -> list[SearchHit]:
        data = await self._authorized_slice(principal=principal, flt=flt)
        q_raw = (query or "").strip()
        if not q_raw:
            return await self.filter_items(flt or SearchFilter(limit=limit), principal=principal)

        q_lower = q_raw.lower()
        # Preserve cohesive tokens (identifiers, canary tokens, canonical keys) — split only on whitespace/commas
        tokens = [t for t in re.split(r"[\s,]+", q_lower) if t]
        regex = re.compile(re.escape(q_raw), re.IGNORECASE)

        # Optional FTS5 boost (still strictly intersected against caller-authorized slice!)
        fts_boost: dict[tuple[str, str], float] = {}
        if hasattr(self.store, "fts_search"):
            for doc_type, doc_id, fts_score in await self.store.fts_search(q_raw, limit=150):  # type: ignore[attr-defined]
                fts_boost[(doc_type, doc_id)] = fts_score

        rg_stems: set[str] = set()
        if self._rg_bin and self._rg_search_root and self._rg_search_root.is_dir():
            rg_stems = await asyncio.to_thread(self._run_rg_sync, q_raw)

        filtered_entities = self._filter_entities_by_facets(data, flt)
        idents_by_eid = await self._identifiers_by_entity(
            [e.entity_id for e in filtered_entities], as_of=data["as_of"], recorded_as_of=data["recorded_as_of"]
        )
        allowed_eids = {e.entity_id for e in filtered_entities}
        entity_map: dict[str, Entity] = data["entity_map"]
        entity_topics: dict[str, list[str]] = data["entity_topics"]
        conflict_subjects: set[str] = data["conflict_subjects"]

        hits: list[SearchHit] = []

        # 1. Entities (manifest + exact canonical_key/alias + regex + FTS5)
        for ent in filtered_entities:
            score = 0.0
            snippet: str | None = None
            if q_lower == ent.canonical_key.lower() or q_lower == ent.display_name.lower():
                score += 10.0
            elif any(q_lower == a.lower() for a in ent.aliases):
                score += 9.0
            elif q_lower in ent.display_name.lower() or q_lower in ent.canonical_key.lower():
                score += 6.0
            elif any(q_lower in a.lower() for a in ent.aliases):
                score += 5.5
            elif regex.search(ent.description):
                score += 3.5
                snippet = ent.description[:180]
            elif len(tokens) > 1:
                hay = f"{ent.display_name} {ent.canonical_key} {' '.join(ent.aliases)} {ent.description} {' '.join(entity_topics.get(ent.entity_id, []))}".lower()
                tok_hits = sum(1 for tok in tokens if tok in hay)
                if tok_hits == len(tokens):
                    score += 4.0
                elif tok_hits >= max(2, math.ceil(0.6 * len(tokens))):
                    score += 2.0 * (tok_hits / len(tokens))

            # Identifier match (bitemporal)
            for ident in idents_by_eid.get(ent.entity_id, ()):
                if q_lower == ident.identifier_value.lower():
                    score += 9.5
                    snippet = f"{ident.scheme.upper()}: {ident.identifier_value}"
                elif q_lower in ident.identifier_value.lower():
                    score += 5.0
                    snippet = f"{ident.scheme.upper()}: {ident.identifier_value}"

            if ("entity", ent.entity_id) in fts_boost:
                score += 1.5 + fts_boost[("entity", ent.entity_id)]
            if ent.canonical_key.replace(":", "-") in rg_stems:
                score += 2.0

            if score > 0.0:
                hits.append(
                    SearchHit(
                        hit_type="entity",
                        id=ent.entity_id,
                        canonical_key=ent.canonical_key,
                        display_name=ent.display_name,
                        entity_type=ent.entity_type,
                        summary=ent.description,
                        snippet=snippet or ent.description[:180],
                        score=round(score, 4),
                        wall_side=ent.wall_side,
                        topics=sorted(entity_topics.get(ent.entity_id, [])),
                        epistemic_status="disputed" if ent.entity_id in conflict_subjects else "approved_fact",
                        has_conflict=ent.entity_id in conflict_subjects,
                    )
                )

        # 2. Authorized claims (NEVER search redacted excerpt/evidence_uri!)
        for clm in data["claims"]:
            if not _claim_matches_filter(clm, flt):
                continue
            if flt and (flt.entity_types or flt.topic_slug or flt.coverage_statuses) and clm.subject_id not in allowed_eids:
                continue
            # Note: clm is already caller-sanitized by StorePort (`provenance.excerpt` and `evidence_uri` are None if redacted!)
            searchable = f"{clm.predicate} {clm.object_value} {clm.context_purpose} {clm.provenance.source_system} {clm.provenance.excerpt or ''}"
            score = 0.0
            if regex.search(clm.object_value):
                score += 6.0
            elif regex.search(searchable):
                score += 4.0
            else:
                hay = searchable.lower()
                tok_hits = sum(1 for tok in tokens if tok in hay)
                if tokens and tok_hits == len(tokens):
                    score += 3.0
            if ("claim", clm.claim_id) in fts_boost:
                score += 1.0 + fts_boost[("claim", clm.claim_id)]

            if score > 0.0:
                subj = entity_map.get(clm.subject_id)
                hits.append(
                    SearchHit(
                        hit_type="claim",
                        id=clm.claim_id,
                        canonical_key=subj.canonical_key if subj else clm.subject_id,
                        display_name=f"{subj.display_name if subj else clm.subject_id} — {clm.predicate}",
                        entity_type=subj.entity_type if subj else None,
                        summary=clm.object_value,
                        snippet=clm.provenance.excerpt or clm.object_value[:180],
                        score=round(score, 4),
                        wall_side=clm.acl.wall_side,
                        topics=sorted(entity_topics.get(clm.subject_id, [])),
                        epistemic_status=clm.epistemic_status,
                        has_conflict=clm.epistemic_status == "disputed",
                        evidence_redacted=clm.evidence_redacted,
                    )
                )

        # 3. Authorized Knowledge Factory items (skip if entity_types/conflicts_only restricts strictly)
        if not (flt and (flt.entity_types or flt.conflicts_only or _has_claim_facet_filters(flt))):
            for ki in data["knowledge_items"]:
                if flt and flt.topic_slug and flt.topic_slug.lower() not in ki.topics:
                    continue
                searchable = f"{ki.display_name} {ki.slug} {ki.body_markdown} {' '.join(ki.topics)} {' '.join(ki.related_entities)}"
                score = 0.0
                if regex.search(ki.display_name) or regex.search(ki.slug):
                    score += 5.5
                elif regex.search(ki.body_markdown):
                    score += 3.5
                else:
                    hay = searchable.lower()
                    tok_hits = sum(1 for tok in tokens if tok in hay)
                    if tokens and tok_hits == len(tokens):
                        score += 2.5
                if score > 0.0:
                    hits.append(
                        SearchHit(
                            hit_type="knowledge_item",
                            id=ki.item_id,
                            canonical_key=ki.item_id,
                            display_name=ki.display_name,
                            entity_type="knowledge_item",
                            summary=ki.knowledge_kind,
                            snippet=ki.body_markdown[:180],
                            score=round(score, 4),
                            wall_side=ki.wall_side,
                            topics=list(ki.topics),
                        )
                    )

        # 4. Authorized Topics
        if not (flt and (flt.entity_types or flt.conflicts_only or _has_claim_facet_filters(flt))):
            for topic in data["topics"]:
                hay = f"{topic.pref_label} {topic.slug} {' '.join(topic.alt_labels)}".lower()
                if q_lower in hay or (tokens and all(t in hay for t in tokens)):
                    hits.append(
                        SearchHit(
                            hit_type="topic",
                            id=topic.topic_id,
                            canonical_key=topic.topic_id,
                            display_name=topic.pref_label,
                            entity_type="topic",
                            summary=f"SKOS topic ({topic.status})",
                            snippet=", ".join(topic.alt_labels) if topic.alt_labels else topic.slug,
                            score=4.5 if q_lower in topic.slug or q_lower in topic.pref_label.lower() else 2.5,
                            topics=[topic.slug],
                        )
                    )

        # 5. Caller's personal memories (ONLY when OBO + direct channel)
        if not (flt and (flt.entity_types or flt.conflicts_only or _has_claim_facet_filters(flt))):
            for mem in data["memories"]:
                hay = f"{mem.key} {mem.content} {' '.join(mem.tags)}".lower()
                if q_lower in hay or (tokens and all(t in hay for t in tokens)):
                    hits.append(
                        SearchHit(
                            hit_type="memory",
                            id=mem.memory_id,
                            canonical_key=f"memory:{mem.key}",
                            display_name=f"My Note: {mem.key}",
                            entity_type="memory",
                            summary=mem.scope_type,
                            snippet=mem.content[:180],
                            score=5.0,
                            topics=[],
                        )
                    )

        hits.sort(key=lambda h: (-h.score, h.hit_type, h.id))
        return hits[:limit]


class FacetGraphRetriever(_BaseRetriever):
    """Retrieval Adapter 2: Plane 3 orthogonal facet slicing + bounded-hop graph boost + optional embeddings."""

    def __init__(
        self,
        store: StorePort,
        policy: PolicyEngine | None = None,
        *,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        super().__init__(store, policy)
        self.embedding_provider = embedding_provider

    async def search(
        self,
        query: str,
        *,
        flt: SearchFilter | None = None,
        principal: Principal,
        limit: int = 25,
    ) -> list[SearchHit]:
        data = await self._authorized_slice(principal=principal, flt=flt)
        q_raw = (query or "").strip()
        if not q_raw:
            return await self.filter_items(flt or SearchFilter(limit=limit), principal=principal)

        q_lower = q_raw.lower()
        tokens = [t for t in re.split(r"[\s,]+", q_lower) if t]

        filtered_entities = self._filter_entities_by_facets(data, flt)
        idents_by_eid = await self._identifiers_by_entity(
            [e.entity_id for e in filtered_entities], as_of=data["as_of"], recorded_as_of=data["recorded_as_of"]
        )
        allowed_eids = {e.entity_id for e in filtered_entities}
        entity_map: dict[str, Entity] = data["entity_map"]
        entity_topics: dict[str, list[str]] = data["entity_topics"]
        conflict_subjects: set[str] = data["conflict_subjects"]
        claims: list[Claim] = data["claims"]

        # Build authorized 1-hop adjacency strictly within caller's slice
        adj: dict[str, set[str]] = defaultdict(set)
        for c in claims:
            if c.object_entity_id and c.subject_id in entity_map and c.object_entity_id in entity_map:
                adj[c.subject_id].add(c.object_entity_id)
                adj[c.object_entity_id].add(c.subject_id)

        # Direct lexical + facet seed scoring on authorized entities
        entity_scores: dict[str, float] = {}
        entity_snippets: dict[str, str] = {}
        for ent in filtered_entities:
            hay = " ".join(
                [
                    ent.display_name,
                    ent.canonical_key,
                    ent.description,
                    " ".join(ent.aliases),
                    " ".join(entity_topics.get(ent.entity_id, [])),
                ]
            ).lower()
            s = 0.0
            if q_lower == ent.canonical_key.lower() or q_lower == ent.display_name.lower():
                s += 10.0
            elif any(q_lower == a.lower() for a in ent.aliases):
                s += 9.0
            elif q_lower in hay:
                s += 6.0
            elif len(tokens) > 1:
                tok_hits = sum(1 for tok in tokens if tok in hay)
                if tok_hits == len(tokens):
                    s += 4.5
                elif tok_hits >= max(2, math.ceil(0.6 * len(tokens))):
                    s += 2.0 * (tok_hits / len(tokens))

            for ident in idents_by_eid.get(ent.entity_id, ()):
                if q_lower == ident.identifier_value.lower():
                    s += 9.5
                    entity_snippets[ent.entity_id] = f"{ident.scheme.upper()}: {ident.identifier_value}"
                elif q_lower in ident.identifier_value.lower():
                    s += 5.0
                    entity_snippets[ent.entity_id] = f"{ident.scheme.upper()}: {ident.identifier_value}"
            if s > 0.0:
                entity_scores[ent.entity_id] = s

        # Claim matches boost both the claim hit and its subject entity via graph propagation
        claim_hits: list[SearchHit] = []
        for clm in claims:
            if not _claim_matches_filter(clm, flt):
                continue
            if flt and (flt.entity_types or flt.topic_slug or flt.coverage_statuses) and clm.subject_id not in allowed_eids:
                continue
            searchable = f"{clm.predicate} {clm.object_value} {clm.context_purpose} {clm.provenance.source_system} {clm.provenance.excerpt or ''}".lower()
            c_score = 0.0
            if q_lower in clm.object_value.lower():
                c_score += 6.5
            elif q_lower in searchable:
                c_score += 4.0
            elif tokens and all(tok in searchable for tok in tokens):
                c_score += 3.0
            if c_score > 0.0:
                if clm.subject_id in allowed_eids:
                    entity_scores[clm.subject_id] = max(entity_scores.get(clm.subject_id, 0.0), c_score * 0.85)
                subj = entity_map.get(clm.subject_id)
                claim_hits.append(
                    SearchHit(
                        hit_type="claim",
                        id=clm.claim_id,
                        canonical_key=subj.canonical_key if subj else clm.subject_id,
                        display_name=f"{subj.display_name if subj else clm.subject_id} — {clm.predicate}",
                        entity_type=subj.entity_type if subj else None,
                        summary=clm.object_value,
                        snippet=clm.provenance.excerpt or clm.object_value[:180],
                        score=round(c_score, 4),
                        wall_side=clm.acl.wall_side,
                        topics=sorted(entity_topics.get(clm.subject_id, [])),
                        epistemic_status=clm.epistemic_status,
                        has_conflict=clm.epistemic_status == "disputed",
                        evidence_redacted=clm.evidence_redacted,
                    )
                )

        # Bounded 1-hop graph expansion boost strictly inside authorized entities
        seed_top = sorted(entity_scores.items(), key=lambda kv: -kv[1])[:5]
        for seed_eid, seed_score in seed_top:
            if seed_score < 4.5:
                continue
            for nbr in adj.get(seed_eid, ()):
                if nbr in allowed_eids and nbr not in entity_scores:
                    entity_scores[nbr] = round(seed_score * 0.25, 4)

        # Optional pluggable embedding re-ranking (offline default is None)
        if self.embedding_provider is not None and filtered_entities:
            docs = [f"{e.display_name} {e.description}" for e in filtered_entities]
            vecs = await self.embedding_provider.embed([q_raw, *docs])
            q_vec, doc_vecs = vecs[0], vecs[1:]
            for ent, d_vec in zip(filtered_entities, doc_vecs):
                sim = _cosine_sim(q_vec, d_vec)
                if sim > 0.25:
                    entity_scores[ent.entity_id] = entity_scores.get(ent.entity_id, 0.0) + sim * 4.0

        hits: list[SearchHit] = list(claim_hits)
        for eid, sc in entity_scores.items():
            ent = entity_map[eid]
            hits.append(
                SearchHit(
                    hit_type="entity",
                    id=ent.entity_id,
                    canonical_key=ent.canonical_key,
                    display_name=ent.display_name,
                    entity_type=ent.entity_type,
                    summary=ent.description,
                    snippet=entity_snippets.get(eid) or ent.description[:180],
                    score=round(sc, 4),
                    wall_side=ent.wall_side,
                    topics=sorted(entity_topics.get(ent.entity_id, [])),
                    epistemic_status="disputed" if ent.entity_id in conflict_subjects else "approved_fact",
                    has_conflict=ent.entity_id in conflict_subjects,
                )
            )

        # Knowledge items, Topics, and caller-scoped Memories
        if not (flt and (flt.entity_types or flt.conflicts_only or _has_claim_facet_filters(flt))):
            for ki in data["knowledge_items"]:
                if flt and flt.topic_slug and flt.topic_slug.lower() not in ki.topics:
                    continue
                hay = f"{ki.display_name} {ki.slug} {ki.body_markdown} {' '.join(ki.topics)}".lower()
                if q_lower in hay or (tokens and all(t in hay for t in tokens)):
                    hits.append(
                        SearchHit(
                            hit_type="knowledge_item",
                            id=ki.item_id,
                            canonical_key=ki.item_id,
                            display_name=ki.display_name,
                            entity_type="knowledge_item",
                            summary=ki.knowledge_kind,
                            snippet=ki.body_markdown[:180],
                            score=4.0,
                            wall_side=ki.wall_side,
                            topics=list(ki.topics),
                        )
                    )
            for topic in data["topics"]:
                hay = f"{topic.pref_label} {topic.slug} {' '.join(topic.alt_labels)}".lower()
                if q_lower in hay or (tokens and all(t in hay for t in tokens)):
                    hits.append(
                        SearchHit(
                            hit_type="topic",
                            id=topic.topic_id,
                            canonical_key=topic.topic_id,
                            display_name=topic.pref_label,
                            entity_type="topic",
                            summary=f"SKOS topic ({topic.status})",
                            snippet=", ".join(topic.alt_labels) if topic.alt_labels else topic.slug,
                            score=4.5,
                            topics=[topic.slug],
                        )
                    )
            for mem in data["memories"]:
                hay = f"{mem.key} {mem.content} {' '.join(mem.tags)}".lower()
                if q_lower in hay or (tokens and all(t in hay for t in tokens)):
                    hits.append(
                        SearchHit(
                            hit_type="memory",
                            id=mem.memory_id,
                            canonical_key=f"memory:{mem.key}",
                            display_name=f"My Note: {mem.key}",
                            entity_type="memory",
                            summary=mem.scope_type,
                            snippet=mem.content[:180],
                            score=5.0,
                            topics=[],
                        )
                    )

        hits.sort(key=lambda h: (-h.score, h.hit_type, h.id))
        return hits[:limit]
