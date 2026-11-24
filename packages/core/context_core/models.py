"""Canonical four-plane institutional domain models and DTOs (ADR-001 through ADR-005)."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from context_core.compression import ensure_utc, to_posix_path, utc_now


def _clone_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return clone_model(value)
    if isinstance(value, list):
        return [_clone_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _clone_value(v) for k, v in value.items()}
    if isinstance(value, set):
        return {_clone_value(v) for v in value}
    return value  # str, int, float, bool, None, Enum, datetime, frozenset, tuple of immutables


def clone_model(model: Any) -> Any:
    """Deep copy of a domain model without ``copy.deepcopy``.

    Domain models hold only JSON-like data (scalars, enums, datetimes, lists, dicts, sets and nested
    models), so rebuilding the mutable containers is a true deep copy. It is about 8x faster than
    ``model_copy(deep=True)``, which matters on hot read paths that copy every claim per request.
    """
    mutable = {
        k: _clone_value(v)
        for k, v in model.__dict__.items()
        if isinstance(v, (BaseModel, list, dict, set))
    }
    if not mutable:
        return model.model_copy()
    copy = model.model_copy(update=mutable)
    # model_copy(update=...) marks the updated fields as set; keep the original's set (exclude_unset)
    object.__setattr__(copy, "__pydantic_fields_set__", set(model.__pydantic_fields_set__))
    return copy


# ---------------------------------------------------------------------------
# Enums & Literals
# ---------------------------------------------------------------------------


class EntityType(str, Enum):
    # Investment Product Hierarchy
    strategy = "strategy"
    fund_family = "fund_family"
    reporting_group = "reporting_group"
    legal_vehicle = "legal_vehicle"
    share_class = "share_class"
    # Market & Credit Hierarchy
    ultimate_parent = "ultimate_parent"
    pe_sponsor = "pe_sponsor"
    issuer = "issuer"
    instrument = "instrument"
    # Party, Client & Distribution Hierarchy
    legal_party = "legal_party"
    client = "client"
    consultant = "consultant"
    commitment = "commitment"
    opportunity = "opportunity"
    # Operational & Knowledge Entities
    app = "app"
    skill = "skill"
    user = "user"
    project = "project"
    data_source = "data_source"
    topic = "topic"
    document_artifact = "document_artifact"
    knowledge_item = "knowledge_item"


class WallSide(str, Enum):
    public_side = "public_side"
    private_side = "private_side"
    above_the_wall = "above_the_wall"


EntityStatus = Literal["active", "inactive", "merged", "archived"]

IdentifierScheme = Literal[
    "lei",
    "openfigi",
    "cusip",
    "isin",
    "sedol",
    "bloomberg_ticker",
    "admin_code",
    "crm_id",
    "oms_id",
    "gips_code",
    "cik",
    "internal_slug",
]

LineageEventType = Literal[
    "renamed_from",
    "merged_into",
    "spun_off_from",
    "acquired_by",
    "lme_restructured_to",
    "split_into",
]

PositionType = Literal["long", "short", "unfunded_commitment", "credit_facility"]
CoverageRole = Literal["primary_analyst", "secondary_analyst", "deal_lead", "pm_sponsor"]
CoverageStatus = Literal["held", "watchlist", "pipeline", "benchmark", "passed"]
ReadMode = Literal["compiled_snapshot", "live_bundle", "on_demand_search"]

ScopeType = Literal["run", "user", "user_x_skill", "skill", "project", "firm"]
ClaimChannel = Literal["fact", "directive"]
EpistemicStatus = Literal[
    "approved_fact",
    "disputed",
    "project_convention",
    "skill_convention",
    "personal_preference",
    "unverified_observation",
    "candidate_sop",
    "revoked",
]

DomainNature = Literal["financial", "non_financial"]
Modality = Literal["quantitative", "qualitative"]
EpistemicTemporal = Literal["actual", "estimate", "forecast", "guidance", "pro_forma"]
ActualStage = Literal["prelim", "unaudited", "audited", "restated"]
Periodicity = Literal[
    "point_in_time",
    "daily",
    "monthly",
    "quarterly",
    "ltm",
    "annual",
    "multi_year",
    "evergreen",
]
IngestionChannel = Literal["automated_feed", "manual_upload", "human_authored", "agent_extracted"]

TopicKind = Literal["curated_core", "emergent_candidate", "merged_alias"]
TopicStatus = Literal["active", "proposed", "merged", "archived"]
KnowledgeKind = Literal["domain_knowledge", "skill_procedure", "data_access_guidance", "page_template"]

GraphLens = Literal[
    "topic",
    "app",
    "skill",
    "project",
    "issuer",
    "fund_hierarchy",
    "entity",
    "app_skill",
    "personal",
]

AuthMode = Literal["entra_jwt", "local_mock"]
ActorMode = Literal["obo", "service"]
ChannelMode = Literal["direct", "shared"]

REDACTED_EVIDENCE_LABEL = "Restricted — Request Access"


def _utc_validator(value: datetime | str | None) -> datetime | None:
    return ensure_utc(value)


# ---------------------------------------------------------------------------
# Security & Identity Models (ADR-003)
# ---------------------------------------------------------------------------


class Principal(BaseModel):
    """Authenticated caller identity with Entra groups, wall side, actor/channel surface flags."""

    model_config = ConfigDict(frozen=False)

    user_id: str
    display_name: str
    email: str
    roles: frozenset[str] = Field(default_factory=frozenset)
    groups: frozenset[str] = Field(default_factory=frozenset)
    wall_side: WallSide = WallSide.public_side
    auth_mode: AuthMode = "local_mock"
    actor_mode: ActorMode = "obo"
    channel_mode: ChannelMode = "direct"
    revoked: bool = False
    groups_overage_resolved: bool = False
    token_jti: str | None = None

    @field_validator("roles", "groups", mode="before")
    @classmethod
    def _coerce_frozenset(cls, value: Any) -> frozenset[str]:
        if value is None:
            return frozenset()
        if isinstance(value, frozenset):
            return value
        return frozenset(str(v) for v in value)


class PolicyEpochState(BaseModel):
    """Monotonic epoch counters preventing multi-worker cache drift and enabling TOCTOU guards."""

    data_epoch: int = 1
    policy_epoch: int = 1
    updated_at: datetime = Field(default_factory=utc_now)
    last_reason: str | None = None
    last_actor: str | None = None

    @field_validator("updated_at", mode="before")
    @classmethod
    def _norm_dt(cls, value: Any) -> datetime:
        return ensure_utc(value) or utc_now()


# ---------------------------------------------------------------------------
# Plane 1: Canonical Entities, Crosswalks & Lineage (ADR-001)
# ---------------------------------------------------------------------------


class Entity(BaseModel):
    """Canonical Plane 1 entity (investment hierarchy, market/credit, party/client, operational)."""

    entity_id: str
    entity_type: EntityType
    canonical_key: str
    display_name: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    owner_group: str = "fund-stewards"
    wall_side: WallSide = WallSide.public_side
    read_acl_groups: list[str] = Field(default_factory=lambda: ["firm-all"])
    write_acl_groups: list[str] = Field(default_factory=lambda: ["fund-stewards"])
    attributes: dict[str, Any] = Field(default_factory=dict)
    status: EntityStatus = "active"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("canonical_key")
    @classmethod
    def _norm_key(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _norm_dt(cls, value: Any) -> datetime:
        return ensure_utc(value) or utc_now()


class EntityIdentifier(BaseModel):
    """Bitemporal external identifier crosswalk (LEI, OpenFIGI, CUSIP, ISIN, admin_code, etc.)."""

    id: str
    entity_id: str
    scheme: IdentifierScheme
    identifier_value: str
    authority: str = ""
    is_primary: bool = True
    valid_from: datetime = Field(default_factory=utc_now)
    valid_to: datetime | None = None
    recorded_at: datetime = Field(default_factory=utc_now)
    superseded_at: datetime | None = None

    @field_validator("valid_from", "recorded_at", mode="before")
    @classmethod
    def _norm_req_dt(cls, value: Any) -> datetime:
        return ensure_utc(value) or utc_now()

    @field_validator("valid_to", "superseded_at", mode="before")
    @classmethod
    def _norm_opt_dt(cls, value: Any) -> datetime | None:
        return ensure_utc(value)


class EntityLineageEvent(BaseModel):
    """Historical rename, merger, spin-off, acquisition, or LME restructuring lineage event."""

    event_id: str
    source_entity_id: str
    target_entity_id: str
    event_type: LineageEventType
    effective_date: datetime
    recorded_at: datetime = Field(default_factory=utc_now)
    summary: str = ""
    evidence_uri: str | None = None
    evidence_acl_groups: list[str] = Field(default_factory=list)
    wall_side: WallSide = WallSide.public_side
    read_acl_groups: list[str] = Field(default_factory=lambda: ["firm-all"])
    evidence_redacted: bool = False
    evidence_redaction_label: str | None = None

    @field_validator("effective_date", "recorded_at", mode="before")
    @classmethod
    def _norm_dt(cls, value: Any) -> datetime:
        return ensure_utc(value) or utc_now()

    @field_validator("evidence_uri")
    @classmethod
    def _norm_uri(cls, value: str | None) -> str | None:
        return to_posix_path(value) if value is not None else None


# ---------------------------------------------------------------------------
# Plane 2 & Plane 3: Reified Associations, Facets, Claims & N-Way Conflicts
# ---------------------------------------------------------------------------


class Position(BaseModel):
    """Reified fund-to-instrument/issuer position snapshot for impact traversal."""

    position_id: str
    vehicle_entity_id: str
    instrument_entity_id: str
    issuer_entity_id: str
    position_type: PositionType = "long"
    quantity: float | None = None
    market_value: float | None = None
    currency: str = "USD"
    weight_bps: float | None = None
    as_of_date: date
    recorded_at: datetime = Field(default_factory=utc_now)
    source_system: str = "oms-pms"
    wall_side: WallSide = WallSide.public_side
    read_acl_groups: list[str] = Field(default_factory=lambda: ["firm-all"])

    @field_validator("recorded_at", mode="before")
    @classmethod
    def _norm_dt(cls, value: Any) -> datetime:
        return ensure_utc(value) or utc_now()


class Coverage(BaseModel):
    """Reified analyst coverage assignment supporting held and unheld issuers."""

    coverage_id: str
    analyst_entity_id: str
    issuer_entity_id: str
    coverage_role: CoverageRole
    coverage_status: CoverageStatus
    valid_from: datetime = Field(default_factory=utc_now)
    valid_to: datetime | None = None
    wall_side: WallSide = WallSide.public_side
    read_acl_groups: list[str] = Field(default_factory=lambda: ["firm-all"])

    @field_validator("valid_from", mode="before")
    @classmethod
    def _norm_req_dt(cls, value: Any) -> datetime:
        return ensure_utc(value) or utc_now()

    @field_validator("valid_to", mode="before")
    @classmethod
    def _norm_opt_dt(cls, value: Any) -> datetime | None:
        return ensure_utc(value)


class SkillBinding(BaseModel):
    """Reified skill consumption binding enabling deterministic blast-radius analysis."""

    binding_id: str
    skill_entity_id: str
    target_entity_id: str
    mapping_purpose: str
    read_mode: ReadMode = "compiled_snapshot"
    required_predicates: list[str] = Field(default_factory=list)
    active: bool = True


class ClaimFacets(BaseModel):
    """Plane 3 orthogonal information facets."""

    domain_nature: DomainNature = "non_financial"
    modality: Modality = "qualitative"
    numeric_value: float | None = None
    unit: str | None = None
    currency: str | None = None
    accounting_basis: str | None = None
    epistemic_temporal: EpistemicTemporal = "actual"
    actual_stage: ActualStage | None = None
    periodicity: Periodicity = "evergreen"
    fiscal_period: str | None = None
    ingestion_channel: IngestionChannel = "human_authored"


class Provenance(BaseModel):
    """Provenance metadata linked to a claim (evidence fields stripped on redaction)."""

    source_system: str = "manual-entry"
    evidence_uri: str | None = None
    content_sha256: str | None = None
    span_locator: str | None = None
    learned_from_run_id: str | None = None
    excerpt: str | None = None

    @field_validator("evidence_uri")
    @classmethod
    def _norm_uri(cls, value: str | None) -> str | None:
        return to_posix_path(value) if value is not None else None


class ClaimACL(BaseModel):
    """Decoupled claim-read vs. evidence-read ACL envelope with information-barrier wall."""

    wall_side: WallSide = WallSide.public_side
    read_acl_groups: list[str] = Field(default_factory=lambda: ["firm-all"])
    write_acl_groups: list[str] = Field(default_factory=lambda: ["fund-stewards"])
    evidence_acl_groups: list[str] = Field(default_factory=list)


class Claim(BaseModel):
    """Atomic bitemporal institutional assertion with Plane 3 facets and decoupled ACLs."""

    claim_id: str
    subject_id: str
    predicate: str
    object_value: str
    object_entity_id: str | None = None
    context_purpose: str = "general"
    scope_type: ScopeType = "firm"
    scope_id: str = "firm"
    channel: ClaimChannel = "fact"
    epistemic_status: EpistemicStatus = "approved_fact"
    kind: str | None = None
    is_mandatory: bool = False
    facets: ClaimFacets = Field(default_factory=ClaimFacets)
    provenance: Provenance = Field(default_factory=Provenance)
    acl: ClaimACL = Field(default_factory=ClaimACL)
    valid_from: datetime = Field(default_factory=utc_now)
    valid_to: datetime | None = None
    recorded_at: datetime = Field(default_factory=utc_now)
    superseded_at: datetime | None = None
    superseded_by: str | None = None
    created_by: str = "system"
    # Server-computed per-caller redaction envelope (never stored raw as redacted)
    evidence_redacted: bool = False
    evidence_redaction_label: str | None = None
    evidence_request_group: str | None = None

    @field_validator("valid_from", "recorded_at", mode="before")
    @classmethod
    def _norm_req_dt(cls, value: Any) -> datetime:
        return ensure_utc(value) or utc_now()

    @field_validator("valid_to", "superseded_at", mode="before")
    @classmethod
    def _norm_opt_dt(cls, value: Any) -> datetime | None:
        return ensure_utc(value)


class ConflictSet(BaseModel):
    """N-way conflict grouping 2..N competing claims over overlapping business-time windows."""

    conflict_set_id: str
    subject_id: str
    predicate: str
    context_purpose: str = "general"
    scope_type: ScopeType = "firm"
    scope_id: str = "firm"
    status: Literal["open", "resolved"] = "open"
    claim_ids: list[str] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    winning_claim_id: str | None = None
    resolution_rationale: str | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    summary: str = ""

    @field_validator("created_at", mode="before")
    @classmethod
    def _norm_req_dt(cls, value: Any) -> datetime:
        return ensure_utc(value) or utc_now()

    @field_validator("resolved_at", mode="before")
    @classmethod
    def _norm_opt_dt(cls, value: Any) -> datetime | None:
        return ensure_utc(value)


class ClaimCreateInput(BaseModel):
    """Input payload for creating/proposing a claim (supports both nested and flat ADR-005 forms)."""

    claim_id: str | None = None
    subject_id: str | None = None
    subject_canonical_key: str | None = None
    predicate: str
    object_value: str
    object_entity_id: str | None = None
    context_purpose: str = "general"
    scope_type: ScopeType = "firm"
    scope_id: str = "firm"
    channel: ClaimChannel = "fact"
    epistemic_status: EpistemicStatus = "unverified_observation"
    kind: str | None = None
    is_mandatory: bool = False
    facets: ClaimFacets = Field(default_factory=ClaimFacets)
    provenance: Provenance | None = None
    acl: ClaimACL | None = None
    # Shorthand fields accepted by REST/MCP inputs (merged into provenance/acl)
    source_system: str | None = None
    evidence_uri: str | None = None
    content_sha256: str | None = None
    span_locator: str | None = None
    learned_from_run_id: str | None = None
    excerpt: str | None = None
    wall_side: WallSide | None = None
    read_acl_groups: list[str] | None = None
    write_acl_groups: list[str] | None = None
    evidence_acl_groups: list[str] | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    recorded_at: datetime | None = None
    force_supersede: bool = False
    expected_data_epoch: int | None = None

    @field_validator("valid_from", "valid_to", "recorded_at", mode="before")
    @classmethod
    def _norm_opt_dt(cls, value: Any) -> datetime | None:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _merge_shorthand(self) -> ClaimCreateInput:
        if not self.subject_id and not self.subject_canonical_key:
            raise ValueError("Either subject_id or subject_canonical_key is required")
        if self.provenance is None:
            self.provenance = Provenance(
                source_system=self.source_system or "manual-entry",
                evidence_uri=self.evidence_uri,
                content_sha256=self.content_sha256,
                span_locator=self.span_locator,
                learned_from_run_id=self.learned_from_run_id,
                excerpt=self.excerpt,
            )
        else:
            if self.source_system and self.provenance.source_system == "manual-entry":
                self.provenance.source_system = self.source_system
            if self.evidence_uri and not self.provenance.evidence_uri:
                self.provenance.evidence_uri = to_posix_path(self.evidence_uri)
            if self.content_sha256 and not self.provenance.content_sha256:
                self.provenance.content_sha256 = self.content_sha256
            if self.span_locator and not self.provenance.span_locator:
                self.provenance.span_locator = self.span_locator
            if self.excerpt and not self.provenance.excerpt:
                self.provenance.excerpt = self.excerpt
        if self.acl is None:
            self.acl = ClaimACL(
                wall_side=self.wall_side or WallSide.public_side,
                read_acl_groups=self.read_acl_groups if self.read_acl_groups is not None else ["firm-all"],
                write_acl_groups=self.write_acl_groups if self.write_acl_groups is not None else ["fund-stewards"],
                evidence_acl_groups=self.evidence_acl_groups if self.evidence_acl_groups is not None else [],
            )
        else:
            if self.wall_side is not None:
                self.acl.wall_side = self.wall_side
            if self.read_acl_groups is not None:
                self.acl.read_acl_groups = self.read_acl_groups
            if self.write_acl_groups is not None:
                self.acl.write_acl_groups = self.write_acl_groups
            if self.evidence_acl_groups is not None:
                self.acl.evidence_acl_groups = self.evidence_acl_groups
        return self


class ClaimUpdateInput(BaseModel):
    """Steward inline edit / bitemporal supersession payload."""

    object_value: str | None = None
    epistemic_status: EpistemicStatus | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    is_mandatory: bool | None = None
    facets: ClaimFacets | None = None
    rationale: str | None = None
    force_supersede: bool = True
    expected_data_epoch: int | None = None

    @field_validator("valid_from", "valid_to", mode="before")
    @classmethod
    def _norm_opt_dt(cls, value: Any) -> datetime | None:
        return ensure_utc(value)


class ClaimWriteResult(BaseModel):
    """Result returned by write_claim / update_claim."""

    claim: Claim
    conflict_detected: bool = False
    conflict_set_id: str | None = None
    superseded_claim_id: str | None = None
    data_epoch: int = 1
    snapshot_scheduled: bool = False


class ConflictResolutionInput(BaseModel):
    """Steward conflict resolution payload with TOCTOU epoch guard."""

    conflict_set_id: str
    winning_claim_id: str | None = None
    replacement_object_value: str | None = None
    valid_from: datetime | None = None
    close_prior_valid_to: datetime | None = None
    rationale: str
    expected_data_epoch: int | None = None

    @field_validator("valid_from", "close_prior_valid_to", mode="before")
    @classmethod
    def _norm_opt_dt(cls, value: Any) -> datetime | None:
        return ensure_utc(value)


# ---------------------------------------------------------------------------
# Plane 4: Dynamic Hierarchical SKOS Topics & Attachments
# ---------------------------------------------------------------------------


class Topic(BaseModel):
    """Dynamic CRUD-managed hierarchical SKOS topic (never a frozen enum)."""

    topic_id: str
    slug: str
    pref_label: str
    alt_labels: list[str] = Field(default_factory=list)
    broader_topic_id: str | None = None
    narrower_topic_ids: list[str] = Field(default_factory=list)
    related_topic_ids: list[str] = Field(default_factory=list)
    topic_kind: TopicKind = "emergent_candidate"
    merged_into_topic_id: str | None = None
    owner_group: str = "fund-stewards"
    status: TopicStatus = "active"

    @field_validator("slug")
    @classmethod
    def _norm_slug(cls, value: str) -> str:
        return value.strip().lower()


class TopicPatchInput(BaseModel):
    """Update/nest/merge/archive payload for dynamic SKOS topics."""

    pref_label: str | None = None
    alt_labels: list[str] | None = None
    broader_topic_id: str | None = None
    related_topic_ids: list[str] | None = None
    merged_into_topic_id: str | None = None
    status: TopicStatus | None = None
    topic_kind: TopicKind | None = None


class TopicAttachment(BaseModel):
    """Polymorphic topic attachment linking a SKOS topic to any entity, claim, or artifact."""

    attachment_id: str
    topic_id: str
    target_type: Literal["entity", "claim", "artifact", "issuer", "fund", "opportunity"]
    target_id: str
    attachment_source: Literal["curated", "inferred", "user"] = "curated"
    confidence: float = 1.0
    wall_side: WallSide = WallSide.public_side
    read_acl_groups: list[str] = Field(default_factory=lambda: ["firm-all"])


class TopicWithCounts(BaseModel):
    """Topic DTO enriched with strictly post-ACL authorized counts."""

    topic: Topic
    authorized_item_count: int = 0
    entity_count: int = 0
    claim_count: int = 0
    artifact_count: int = 0


# ---------------------------------------------------------------------------
# Knowledge Factory, Staleness & Curator Draft Patches
# ---------------------------------------------------------------------------


class KnowledgeItem(BaseModel):
    """Three-part Knowledge Factory item (domain_knowledge | skill_procedure | data_access_guidance)."""

    item_id: str
    knowledge_kind: KnowledgeKind
    display_name: str
    slug: str
    source_path: str
    body_markdown: str = ""
    wall_side: WallSide = WallSide.public_side
    read_acl_groups: list[str] = Field(default_factory=lambda: ["firm-all"])
    write_acl_groups: list[str] = Field(default_factory=lambda: ["fund-stewards"])
    topics: list[str] = Field(default_factory=list)
    related_entities: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    content_sha256: str | None = None
    valid_from: datetime = Field(default_factory=utc_now)
    recorded_at: datetime = Field(default_factory=utc_now)
    okf_version: str = "0.2"

    @field_validator("source_path")
    @classmethod
    def _norm_path(cls, value: str) -> str:
        return to_posix_path(value)

    @field_validator("valid_from", "recorded_at", mode="before")
    @classmethod
    def _norm_dt(cls, value: Any) -> datetime:
        return ensure_utc(value) or utc_now()


class StalenessItem(BaseModel):
    """Single deterministic zero-LLM SHA-256 staleness finding."""

    claim_id: str | None = None
    item_id: str | None = None
    evidence_path: str
    expected_sha256: str
    actual_sha256: str | None = None
    reason: Literal["hash_mismatch", "missing_source", "unverified_observation_cluster"] = "hash_mismatch"
    wall_side: WallSide = WallSide.public_side


class StalenessReport(BaseModel):
    """Result of deterministic zero-LLM SHA-256 staleness diff."""

    checked_count: int = 0
    stale_items: list[StalenessItem] = Field(default_factory=list)
    wall_side: WallSide | None = None
    checked_at: datetime = Field(default_factory=utc_now)

    @property
    def stale_count(self) -> int:
        return len(self.stale_items)


class CuratorDraftPatch(BaseModel):
    """Reviewable draft diff emitted by the background Curator (never auto-mutates approved_fact)."""

    patch_id: str
    wall_side: WallSide = WallSide.public_side
    read_acl_groups: list[str] = Field(default_factory=lambda: ["firm-all"])
    target_type: Literal["claim", "entity", "knowledge_item", "topic"] = "claim"
    target_id: str
    title: str
    rationale: str = ""
    unified_diff: str = ""
    proposed_changes: dict[str, Any] = Field(default_factory=dict)
    source_claim_ids: list[str] = Field(default_factory=list)
    stale_evidence_paths: list[str] = Field(default_factory=list)
    status: Literal["pending_steward_review", "approved", "rejected"] = "pending_steward_review"
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str = "agent:curator"
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None

    @field_validator("created_at", mode="before")
    @classmethod
    def _norm_req_dt(cls, value: Any) -> datetime:
        return ensure_utc(value) or utc_now()

    @field_validator("reviewed_at", mode="before")
    @classmethod
    def _norm_opt_dt(cls, value: Any) -> datetime | None:
        return ensure_utc(value)


# Alias for ergonomic imports
CuratorPatch = CuratorDraftPatch


# ---------------------------------------------------------------------------
# Personal Memory & Watchlist DTOs
# ---------------------------------------------------------------------------


class MemoryRecord(BaseModel):
    """Caller-isolated personal memory note (`user` | `user_x_skill` | `run`)."""

    memory_id: str
    user_id: str
    scope_type: Literal["user", "user_x_skill", "run"] = "user"
    skill_id: str | None = None
    key: str
    content: str
    tags: list[str] = Field(default_factory=list)
    linked_entity_ids: list[str] = Field(default_factory=list)
    linked_topic_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _norm_dt(cls, value: Any) -> datetime:
        return ensure_utc(value) or utc_now()


UserMemoryNote = MemoryRecord


class MemoryUpsertInput(BaseModel):
    """Create or update payload for caller personal memory."""

    memory_id: str | None = None
    scope_type: Literal["user", "user_x_skill", "run"] = "user"
    skill_id: str | None = None
    key: str
    content: str
    tags: list[str] = Field(default_factory=list)
    linked_entity_ids: list[str] = Field(default_factory=list)
    linked_topic_ids: list[str] = Field(default_factory=list)


class WatchlistAlert(BaseModel):
    """Change or conflict alert triggered by enterprise activity touching a watched item."""

    alert_id: str
    kind: Literal["open_conflict", "claim_revision", "observation"]
    severity: Literal["low", "medium", "high"] = "medium"
    target_type: Literal["entity", "topic", "data_source"]
    target_id: str
    entity_canonical_key: str | None = None
    title: str
    summary: str
    conflict_set_id: str | None = None
    claim_id: str | None = None
    recorded_at: datetime = Field(default_factory=utc_now)

    @field_validator("recorded_at", mode="before")
    @classmethod
    def _norm_dt(cls, value: Any) -> datetime:
        return ensure_utc(value) or utc_now()


class WatchlistRecord(BaseModel):
    """Caller's personal watchlist with dynamically computed ACL-filtered alerts."""

    user_id: str
    entity_ids: list[str] = Field(default_factory=list)
    topic_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    alerts: list[WatchlistAlert] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("updated_at", mode="before")
    @classmethod
    def _norm_dt(cls, value: Any) -> datetime:
        return ensure_utc(value) or utc_now()


UserWatchlist = WatchlistRecord


class WatchlistUpdateInput(BaseModel):
    """Update payload for caller's personal watchlist."""

    entity_ids: list[str] = Field(default_factory=list)
    topic_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Context Assembly Receipts, Search, Bundles, Snapshots & Graph DTOs
# ---------------------------------------------------------------------------


class ContextAssemblyReceipt(BaseModel):
    """Immutable audit receipt emitted on every context read (ADR-003 §6)."""

    receipt_id: str
    timestamp: datetime = Field(default_factory=utc_now)
    principal_id: str
    auth_mode: str = "local_mock"
    actor_mode: ActorMode = "obo"
    channel_mode: ChannelMode = "direct"
    wall_side: WallSide = WallSide.public_side
    snapshot_version: int | None = None
    data_epoch: int = 1
    policy_epoch: int = 1
    as_of: datetime = Field(default_factory=utc_now)
    recorded_as_of: datetime = Field(default_factory=utc_now)
    claim_ids: list[str] = Field(default_factory=list)
    redacted_evidence_claim_ids: list[str] = Field(default_factory=list)
    conflict_set_ids: list[str] = Field(default_factory=list)
    memory_ids: list[str] = Field(default_factory=list)
    tool_or_endpoint: str = "resolve_context"
    receipt_sha256: str = ""

    @field_validator("timestamp", "as_of", "recorded_as_of", mode="before")
    @classmethod
    def _norm_dt(cls, value: Any) -> datetime:
        return ensure_utc(value) or utc_now()

    @property
    def redacted_ids(self) -> list[str]:
        return self.redacted_evidence_claim_ids

    @property
    def recorded_at(self) -> datetime:
        return self.recorded_as_of


class SearchFilter(BaseModel):
    """Orthogonal facet, topic, scope, entity-type, and bitemporal filter DTO."""

    entity_types: list[str] | None = None
    topic_slug: str | None = None
    domain_nature: DomainNature | None = None
    modality: Modality | None = None
    epistemic_temporal: EpistemicTemporal | None = None
    actual_stage: ActualStage | None = None
    periodicity: Periodicity | None = None
    ingestion_channel: IngestionChannel | None = None
    scope_types: list[str] | None = None
    epistemic_statuses: list[str] | None = None
    coverage_statuses: list[str] | None = None
    conflicts_only: bool = False
    as_of: datetime | None = None
    recorded_as_of: datetime | None = None
    limit: int = 50
    offset: int = 0

    @field_validator("as_of", "recorded_as_of", mode="before")
    @classmethod
    def _norm_opt_dt(cls, value: Any) -> datetime | None:
        return ensure_utc(value)


class SearchHit(BaseModel):
    """ACL-filtered search/filter hit across entities, claims, knowledge items, topics, or memories."""

    hit_type: Literal["entity", "claim", "knowledge_item", "topic", "memory"] = "entity"
    id: str
    canonical_key: str
    display_name: str
    entity_type: str | None = None
    summary: str = ""
    snippet: str | None = None
    score: float = 1.0
    wall_side: WallSide = WallSide.public_side
    topics: list[str] = Field(default_factory=list)
    epistemic_status: str | None = None
    has_conflict: bool = False
    evidence_redacted: bool = False


class ContextBundleQuery(BaseModel):
    """Query DTO for two-channel stratified context bundle assembly."""

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
    snapshot_version: int | None = None
    tool_or_endpoint: str = "resolve_context"

    @field_validator("as_of", "recorded_as_of", mode="before")
    @classmethod
    def _norm_opt_dt(cls, value: Any) -> datetime | None:
        return ensure_utc(value)


class ContextBundle(BaseModel):
    """Resolved two-channel context bundle separating authoritative facts from directives."""

    source: Literal["live", "cache", "snapshot"] = "live"
    entities: list[Entity] = Field(default_factory=list)
    facts: list[Claim] = Field(default_factory=list)
    directives: list[Claim] = Field(default_factory=list)
    conflicts: list[ConflictSet] = Field(default_factory=list)
    memories: list[MemoryRecord] = Field(default_factory=list)
    receipt: ContextAssemblyReceipt


class GraphCluster(BaseModel):
    """Deterministic cluster grouping for diagnostic graph view (ADR-005)."""

    id: str
    label: str
    kind: str
    member_count: int = 0


class GraphNode(BaseModel):
    """Authorized diagnostic graph node with strictly post-ACL degrees and orphan metadata."""

    id: str
    canonical_key: str
    label: str
    entity_type: str
    cluster_id: str
    wall_side: WallSide = WallSide.public_side
    degree_in: int = 0
    degree_out: int = 0
    is_orphan: bool = False
    orphan_angle_deg: float | None = None
    has_conflict: bool = False
    open_conflict_count: int = 0
    epistemic_badge: str = "approved_fact"
    topics: list[str] = Field(default_factory=list)
    coverage_status: str | None = None


class GraphEdge(BaseModel):
    """Authorized directed diagnostic graph edge."""

    id: str
    source: str
    target: str
    predicate: str
    label: str
    context_purpose: str = "general"
    channel: str = "fact"
    epistemic_status: str = "approved_fact"
    direction_role: Literal["upstream", "downstream", "lateral"] = "downstream"
    has_conflict: bool = False


class GraphStats(BaseModel):
    """Strictly post-ACL summary statistics for a projected graph."""

    node_count: int = 0
    edge_count: int = 0
    conflict_count: int = 0
    orphan_count: int = 0


class GraphPayload(BaseModel):
    """Deterministic UI diagnostic graph payload (ADR-005)."""

    lens: GraphLens = "topic"
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    clusters: list[GraphCluster] = Field(default_factory=list)
    stats: GraphStats = Field(default_factory=GraphStats)
    data_epoch: int = 1
    policy_epoch: int = 1
    receipt_id: str | None = None


class ImpactLayers(BaseModel):
    """Layered reverse-BFS blast-radius breakdown (ADR-002 / ADR-005)."""

    positions: list[str] = Field(default_factory=list)
    funds: list[str] = Field(default_factory=list)
    clients_opportunities: list[str] = Field(default_factory=list)
    skills_apps: list[str] = Field(default_factory=list)
    watchlist_hits: list[str] = Field(default_factory=list)


class ImpactGraphPayload(BaseModel):
    """ACL-enforced reverse-BFS impact graph payload."""

    root_node_id: str
    layers: ImpactLayers = Field(default_factory=ImpactLayers)
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    total_affected: int = 0
    receipt_id: str | None = None


class SnapshotManifest(BaseModel):
    """Atomic snapshot manifest published per (acl_tier, wall_side) partition."""

    version_id: int
    acl_tier: str
    wall_side: WallSide
    policy_epoch: int
    data_epoch: int
    etag: str
    sha256: str
    compiled_at: datetime = Field(default_factory=utc_now)
    filename: str
    relative_path: str
    claim_count: int = 0
    entity_count: int = 0
    byte_size: int = 0

    @field_validator("compiled_at", mode="before")
    @classmethod
    def _norm_dt(cls, value: Any) -> datetime:
        return ensure_utc(value) or utc_now()

    @field_validator("relative_path")
    @classmethod
    def _norm_path(cls, value: str) -> str:
        return to_posix_path(value)


class UploadIngestResult(BaseModel):
    """Result DTO for quarantined manual upload ingestion (ADR-005)."""

    upload_id: str
    filename: str
    media_type: str
    content_sha256: str
    extracted_preview: str = ""
    quarantined_claim_ids: list[str] = Field(default_factory=list)
    epistemic_status: EpistemicStatus = "unverified_observation"
