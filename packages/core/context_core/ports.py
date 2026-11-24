"""Hexagonal port Protocol contracts for Enterprise Context Lake (ADR-002)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence, runtime_checkable

from context_core.models import (
    Claim,
    ClaimCreateInput,
    ClaimUpdateInput,
    ClaimWriteResult,
    ConflictResolutionInput,
    ConflictSet,
    ContextAssemblyReceipt,
    ContextBundle,
    ContextBundleQuery,
    Coverage,
    CuratorDraftPatch,
    Entity,
    EntityIdentifier,
    EntityLineageEvent,
    GraphLens,
    GraphPayload,
    ImpactGraphPayload,
    KnowledgeItem,
    MemoryRecord,
    MemoryUpsertInput,
    PolicyEpochState,
    Position,
    Principal,
    SearchFilter,
    SearchHit,
    SkillBinding,
    SnapshotManifest,
    StalenessReport,
    Topic,
    TopicAttachment,
    TopicPatchInput,
    WatchlistRecord,
    WatchlistUpdateInput,
)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Optional pluggable embedding hook for FacetGraphRetriever (offline default is None)."""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@runtime_checkable
class GroupResolver(Protocol):
    """Directory group resolver for Entra >200-group overage tokens."""

    def resolve_groups(self, user_id: str, *, policy_epoch: int = 1) -> frozenset[str]: ...


@runtime_checkable
class PolicyPort(Protocol):
    """Fail-closed RBAC, MNPI wall_side barrier, split claim/evidence redaction, and surface gating."""

    def can_read_entity(self, principal: Principal, entity: Entity) -> bool: ...
    def can_read_claim(self, principal: Principal, claim: Claim) -> bool: ...
    def can_read_evidence(self, principal: Principal, claim: Claim) -> bool: ...
    def authorize_write(
        self,
        principal: Principal,
        *,
        target_entity: Entity,
        scope_type: str,
        epistemic_status: str,
        force_supersede: bool,
    ) -> None: ...
    def sanitize_claim_for_principal(self, principal: Principal, claim: Claim) -> Claim | None: ...
    def allowed_snapshot_partitions(self, principal: Principal) -> list[tuple[str, str]]: ...
    def is_memory_allowed(self, principal: Principal) -> bool: ...


@runtime_checkable
class StorePort(Protocol):
    """Canonical persistence port implemented coequally by SQLite and OKF Markdown Vault adapters."""

    async def initialize(self) -> None: ...
    async def close(self) -> None: ...
    async def get_epochs(self) -> PolicyEpochState: ...
    async def bump_policy_epoch(self, reason: str, actor: str) -> PolicyEpochState: ...
    async def is_principal_revoked(self, user_id: str) -> bool: ...
    async def revoke_principal(self, user_id: str, *, reason: str, actor: str) -> PolicyEpochState: ...

    # Entities, Crosswalks, Lineage & Reified Associations
    async def upsert_entity(self, entity: Entity) -> Entity: ...
    async def get_entity(self, entity_id_or_key: str, *, principal: Principal) -> Entity | None: ...
    async def list_entities(
        self,
        *,
        principal: Principal,
        entity_types: Sequence[str] | None = None,
        topic_slug: str | None = None,
        limit: int | None = 200,
        offset: int = 0,
    ) -> list[Entity]: ...
    async def upsert_identifier(self, ident: EntityIdentifier) -> EntityIdentifier: ...
    async def list_identifiers(
        self,
        entity_id: str,
        *,
        as_of: datetime | None = None,
        recorded_as_of: datetime | None = None,
    ) -> list[EntityIdentifier]: ...
    async def add_lineage_event(self, event: EntityLineageEvent) -> EntityLineageEvent: ...
    async def list_lineage_events(self, entity_id: str, *, principal: Principal) -> list[EntityLineageEvent]: ...
    async def upsert_position(self, pos: Position) -> Position: ...
    async def list_positions(
        self,
        *,
        principal: Principal,
        vehicle_id: str | None = None,
        issuer_id: str | None = None,
    ) -> list[Position]: ...
    async def upsert_coverage(self, cov: Coverage) -> Coverage: ...
    async def list_coverages(
        self,
        *,
        principal: Principal,
        issuer_id: str | None = None,
        status: str | None = None,
    ) -> list[Coverage]: ...
    async def upsert_skill_binding(self, binding: SkillBinding) -> SkillBinding: ...
    async def list_skill_bindings(
        self,
        *,
        skill_entity_id: str | None = None,
        target_entity_id: str | None = None,
    ) -> list[SkillBinding]: ...

    # Bitemporal Claims & N-Way Conflicts
    async def write_claim(self, payload: ClaimCreateInput, *, principal: Principal) -> ClaimWriteResult: ...
    async def update_claim(
        self, claim_id: str, patch: ClaimUpdateInput, *, principal: Principal
    ) -> ClaimWriteResult: ...
    async def get_claim(self, claim_id: str, *, principal: Principal) -> Claim | None: ...
    async def query_claims(
        self,
        *,
        principal: Principal,
        subject_ids: Sequence[str] | None = None,
        object_entity_ids: Sequence[str] | None = None,
        predicates: Sequence[str] | None = None,
        context_purpose: str | None = None,
        scope_types: Sequence[str] | None = None,
        scope_id: str | None = None,
        epistemic_statuses: Sequence[str] | None = None,
        as_of: datetime | None = None,
        recorded_as_of: datetime | None = None,
        include_superseded: bool = False,
    ) -> list[Claim]: ...
    async def list_conflicts(
        self,
        *,
        principal: Principal,
        status: str = "open",
        subject_id: str | None = None,
    ) -> list[ConflictSet]: ...
    async def resolve_conflict(
        self, resolution: ConflictResolutionInput, *, principal: Principal
    ) -> ConflictSet: ...

    # Plane 4 Dynamic Hierarchical Topics, Knowledge Factory, Curator Patches & Personal Memory
    async def upsert_topic(self, topic: Topic) -> Topic: ...
    async def update_topic(self, topic_id: str, patch: TopicPatchInput, *, principal: Principal) -> Topic: ...
    async def list_topics(self, *, principal: Principal, include_archived: bool = False) -> list[Topic]: ...
    async def attach_topic(self, attachment: TopicAttachment) -> TopicAttachment: ...
    async def list_topic_attachments(
        self,
        *,
        principal: Principal,
        topic_id: str | None = None,
        target_id: str | None = None,
    ) -> list[TopicAttachment]: ...
    async def upsert_knowledge_item(self, item: KnowledgeItem) -> KnowledgeItem: ...
    async def list_knowledge_items(
        self, *, principal: Principal, kind: str | None = None
    ) -> list[KnowledgeItem]: ...
    async def list_curator_patches(
        self, *, principal: Principal, status: str = "pending_steward_review"
    ) -> list[CuratorDraftPatch]: ...
    async def save_curator_patch(self, patch: CuratorDraftPatch) -> CuratorDraftPatch: ...
    async def upsert_memory(self, record: MemoryUpsertInput, *, principal: Principal) -> MemoryRecord: ...
    async def get_memory(self, memory_id: str, *, principal: Principal) -> MemoryRecord | None: ...
    async def list_memories(
        self, *, principal: Principal, skill_id: str | None = None
    ) -> list[MemoryRecord]: ...
    async def delete_memory(self, memory_id: str, *, principal: Principal) -> bool: ...
    async def get_watchlist(self, *, principal: Principal) -> WatchlistRecord: ...
    async def put_watchlist(
        self, watchlist: WatchlistUpdateInput, *, principal: Principal
    ) -> WatchlistRecord: ...

    # Receipts & Audit
    async def record_receipt(self, receipt: ContextAssemblyReceipt) -> None: ...
    async def get_receipt(
        self, receipt_id: str, *, principal: Principal
    ) -> ContextAssemblyReceipt | None: ...


@runtime_checkable
class RetrievalPort(Protocol):
    """ACL-enforced search, facet filtering, neighborhood traversal, and bundle resolution port."""

    async def filter_items(self, flt: SearchFilter, *, principal: Principal) -> list[SearchHit]: ...
    async def search(
        self,
        query: str,
        *,
        flt: SearchFilter | None = None,
        principal: Principal,
        limit: int = 25,
    ) -> list[SearchHit]: ...
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
    ) -> GraphPayload: ...
    async def resolve_context(
        self, query: ContextBundleQuery, *, principal: Principal
    ) -> ContextBundle: ...


@runtime_checkable
class SnapshotPort(Protocol):
    """Async non-blocking ACL/wall-partitioned SQLite+Zstd snapshot publisher."""

    def schedule_compile(self, acl_tier: str, wall_side: str) -> None: ...
    async def compile_and_publish(self, acl_tier: str, wall_side: str) -> SnapshotManifest: ...
    async def get_latest_manifest(self, acl_tier: str, wall_side: str) -> SnapshotManifest | None: ...
    async def get_snapshot_bytes(self, acl_tier: str, wall_side: str, version_id: int) -> bytes: ...


@runtime_checkable
class GraphProjectionPort(Protocol):
    """Deterministic post-ACL diagnostic graph projection and reverse impact traversal."""

    async def project_graph(
        self,
        *,
        principal: Principal,
        lens: GraphLens = "topic",
        focus_id: str | None = None,
        hops: int = 2,
        search_query: str | None = None,
        filters: SearchFilter | None = None,
        as_of: datetime | None = None,
        recorded_as_of: datetime | None = None,
    ) -> GraphPayload: ...
    async def impact_traversal(
        self,
        node_id: str,
        *,
        principal: Principal,
        max_depth: int = 5,
        as_of: datetime | None = None,
        recorded_as_of: datetime | None = None,
    ) -> ImpactGraphPayload: ...


@runtime_checkable
class StalenessCheckable(Protocol):
    """Optional deterministic zero-LLM SHA-256 staleness verification capability."""

    async def diff_staleness(self, *, wall_side: str | None = None) -> StalenessReport: ...
