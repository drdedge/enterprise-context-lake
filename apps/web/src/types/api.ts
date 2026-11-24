export type WallSide = "public_side" | "private_side" | "above_the_wall";
export type ActorMode = "obo" | "service";
export type ChannelMode = "direct" | "shared";
export type ViewKey = "home" | "table" | "graph" | "memory";

export type GraphLens =
  | "topic"
  | "app"
  | "skill"
  | "project"
  | "issuer"
  | "fund_hierarchy";

export type GraphDirection = "both" | "in" | "out";

export type DomainNature = "financial" | "non_financial";
export type Modality = "quantitative" | "qualitative";
export type EpistemicTemporal =
  | "actual"
  | "estimate"
  | "forecast"
  | "guidance"
  | "pro_forma";
export type IngestionChannel =
  | "automated_feed"
  | "manual_upload"
  | "human_authored"
  | "agent_extracted";

export interface AuthContextState {
  userId: string;
  actorMode: ActorMode;
  channelMode: ChannelMode;
  groupsOverage: boolean;
}

export interface TemporalSnapshot {
  id: string;
  label: string;
  shortLabel: string;
  asOf?: string;
  recordedAsOf?: string;
}

export interface PrincipalMeDTO {
  user_id: string;
  display_name: string;
  email: string;
  roles: string[];
  groups: string[];
  wall_side: WallSide;
  actor_mode: ActorMode;
  channel_mode: ChannelMode;
  auth_mode: string;
  groups_overage_resolved: boolean;
  memory_enabled: boolean;
}

export interface PersonaDTO {
  user_id: string;
  display_name: string;
  role_summary: string;
  wall_side: WallSide;
  revoked: boolean;
  groups_overage: boolean;
}

export interface AdapterConfigDTO {
  store: "sqlite_bitemporal" | "okf_markdown_vault";
  retrieval: "lexical_manifest" | "facet_graph";
  llm_mode: "auto" | "gemini" | "deterministic";
  available?: {
    store?: string[];
    retrieval?: string[];
    llm_mode?: string[];
  };
  synced?: boolean;
  data_epoch?: number;
}

export interface HealthDTO {
  status: string;
  data_epoch: number;
  policy_epoch: number;
  active_adapters: {
    store: string;
    retrieval: string;
    llm_mode: string;
  };
  latest_snapshots?: Array<{
    acl_tier?: string;
    wall_side?: string;
    version?: number;
    snapshot_version?: number;
    published_at?: string;
  }>;
}

export interface ClaimFacetsDTO {
  domain_nature: DomainNature;
  modality: Modality;
  numeric_value?: number | null;
  unit?: string | null;
  currency?: string | null;
  accounting_basis?: string | null;
  epistemic_temporal: EpistemicTemporal;
  actual_stage?: "prelim" | "unaudited" | "audited" | "restated" | null;
  periodicity?: string | null;
  fiscal_period?: string | null;
  ingestion_channel: IngestionChannel;
}

export interface ProvenanceDTO {
  source_system: string;
  evidence_uri?: string | null;
  content_sha256?: string | null;
  span_locator?: string | null;
  learned_from_run_id?: string | null;
  excerpt?: string | null;
}

export interface ClaimACLDTO {
  wall_side: WallSide;
  read_acl_groups: string[];
  write_acl_groups: string[];
  evidence_acl_groups: string[];
}

export interface ClaimDTO {
  claim_id: string;
  subject_id: string;
  subject_canonical_key?: string;
  subject_display_name?: string;
  predicate: string;
  object_value: string;
  object_entity_id?: string | null;
  context_purpose: string;
  scope_type: string;
  scope_id: string;
  channel: "fact" | "directive";
  epistemic_status: string;
  is_mandatory: boolean;
  facets?: ClaimFacetsDTO;
  provenance: ProvenanceDTO;
  acl?: ClaimACLDTO;
  valid_from: string;
  valid_to?: string | null;
  recorded_at: string;
  superseded_at?: string | null;
  superseded_by?: string | null;
  created_by?: string;
  evidence_redacted: boolean;
  evidence_redaction_label?: string | null;
  evidence_request_group?: string | null;
}

export interface EntityDTO {
  entity_id: string;
  entity_type: string;
  canonical_key: string;
  display_name: string;
  aliases: string[];
  description: string;
  owner_group: string;
  wall_side: WallSide;
  read_acl_groups?: string[];
  write_acl_groups?: string[];
  attributes?: Record<string, unknown>;
  status: string;
  created_at?: string;
  updated_at?: string;
}

export interface EntitySummaryDTO extends EntityDTO {
  edge_count?: number;
  degree_in?: number;
  degree_out?: number;
  skill_count?: number;
  open_conflict_count?: number;
  has_conflict?: boolean;
  epistemic_badge?: string;
  topics?: string[];
  coverage_status?: string | null;
  domain_nature?: DomainNature | null;
  modality?: Modality | null;
  epistemic_temporal?: EpistemicTemporal | null;
  ingestion_channel?: IngestionChannel | null;
}

export interface StreamItemDTO {
  entity_id: string;
  canonical_key: string;
  display_name: string;
  entity_type: string;
  description?: string;
  wall_side: WallSide;
  epistemic_badge?: string;
  has_conflict?: boolean;
  open_conflict_count?: number;
  edge_count?: number;
  skill_count?: number;
  topics?: string[];
  updated_at?: string;
  domain_nature?: DomainNature;
  coverage_status?: string | null;
  latest_claim_summary?: string | null;
}

export interface StreamResponseDTO {
  items: StreamItemDTO[];
  counts: {
    entities: number;
    open_conflicts: number;
    observations: number;
  };
  data_epoch: number;
  policy_epoch: number;
}

export interface EntitiesResponseDTO {
  items: EntitySummaryDTO[];
  total: number;
  facet_counts: Record<string, Record<string, number>>;
  data_epoch: number;
  policy_epoch: number;
}

export interface EntityIdentifierDTO {
  identifier_id?: string;
  entity_id?: string;
  scheme: string;
  identifier_value: string;
  authority?: string;
  is_primary?: boolean;
  valid_from?: string;
  valid_to?: string | null;
  recorded_at?: string;
  superseded_at?: string | null;
}

export interface EntityLineageEventDTO {
  event_id: string;
  source_entity_id: string;
  target_entity_id: string;
  event_type: string;
  effective_date: string;
  recorded_at?: string;
  summary: string;
  evidence_uri?: string | null;
}

export interface PositionDTO {
  position_id: string;
  vehicle_entity_id: string;
  instrument_entity_id: string;
  issuer_entity_id: string;
  position_type: string;
  quantity?: number | null;
  market_value?: number | null;
  currency?: string;
  weight_bps?: number | null;
  as_of_date?: string;
  source_system?: string;
}

export interface CoverageDTO {
  coverage_id: string;
  analyst_entity_id: string;
  issuer_entity_id: string;
  coverage_role: string;
  coverage_status: string;
  valid_from?: string;
  valid_to?: string | null;
}

export interface SkillBindingDTO {
  binding_id: string;
  skill_entity_id: string;
  skill_name?: string;
  skill_canonical_key?: string;
  target_entity_id: string;
  mapping_purpose: string;
  read_mode: string;
  required_predicates: string[];
  active: boolean;
}

export interface BacklinkDTO {
  entity_id: string;
  canonical_key: string;
  display_name: string;
  entity_type: string;
  predicate?: string;
  direction?: "in" | "out";
}

export interface ConflictSetDTO {
  conflict_set_id: string;
  subject_id: string;
  subject_canonical_key?: string;
  subject_display_name?: string;
  predicate: string;
  context_purpose: string;
  scope_type?: string;
  scope_id?: string;
  status: "open" | "resolved";
  claim_ids: string[];
  claims?: ClaimDTO[];
  winning_claim_id?: string | null;
  resolution_rationale?: string | null;
  affected_skills?: SkillBindingDTO[];
  created_at?: string;
  resolved_at?: string | null;
}

export interface TopicDTO {
  topic_id: string;
  slug: string;
  pref_label: string;
  alt_labels: string[];
  broader_topic_id?: string | null;
  narrower_topic_ids?: string[];
  related_topic_ids?: string[];
  topic_kind: string;
  merged_into_topic_id?: string | null;
  owner_group?: string;
  status: "active" | "proposed" | "merged" | "archived";
  item_count?: number;
  entity_count?: number;
  claim_count?: number;
  children?: TopicDTO[];
}

export interface TopicsResponseDTO {
  topics: TopicDTO[];
  tree: TopicDTO[];
}

export interface EntityDetailDTO {
  entity: EntityDTO;
  identifiers: EntityIdentifierDTO[];
  lineage: EntityLineageEventDTO[];
  facts: ClaimDTO[];
  directives: ClaimDTO[];
  conflicts: ConflictSetDTO[];
  positions: PositionDTO[];
  coverages: CoverageDTO[];
  skill_bindings: SkillBindingDTO[];
  topics: TopicDTO[];
  backlinks: BacklinkDTO[];
  receipt_id: string;
}

export interface SearchHitDTO {
  kind: string;
  id: string;
  canonical_key?: string;
  title: string;
  subtitle?: string;
  entity_type?: string;
  wall_side?: WallSide;
  has_conflict?: boolean;
  epistemic_status?: string;
  score?: number;
}

export interface SearchResponseDTO {
  hits: SearchHitDTO[];
  total: number;
  receipt_id: string;
}

export interface GraphClusterDTO {
  id: string;
  label: string;
  kind: string;
  member_count: number;
}

export interface GraphNodeDTO {
  id: string;
  canonical_key: string;
  label: string;
  entity_type: string;
  cluster_id?: string | null;
  wall_side: WallSide;
  degree_in: number;
  degree_out: number;
  is_orphan: boolean;
  has_conflict: boolean;
  open_conflict_count: number;
  epistemic_badge?: string;
  topics: string[];
  domain_nature?: DomainNature | null;
  modality?: Modality | null;
  epistemic_temporal?: EpistemicTemporal | null;
  ingestion_channel?: IngestionChannel | null;
  coverage_status?: string | null;
}

export interface GraphEdgeDTO {
  id: string;
  source: string;
  target: string;
  predicate: string;
  label: string;
  context_purpose?: string;
  channel?: "fact" | "directive";
  epistemic_status?: string;
  direction_role?: "upstream" | "downstream" | "neutral";
  has_conflict: boolean;
}

export interface GraphResponseDTO {
  lens: string;
  nodes: GraphNodeDTO[];
  edges: GraphEdgeDTO[];
  clusters: GraphClusterDTO[];
  stats: {
    node_count: number;
    edge_count: number;
    conflict_count: number;
    orphan_count: number;
  };
  data_epoch: number;
  policy_epoch: number;
  receipt_id: string;
}

export interface ImpactLayerItem {
  id?: string;
  entity_id?: string;
  canonical_key?: string;
  display_name?: string;
  label?: string;
  entity_type?: string;
  summary?: string;
  mapping_purpose?: string;
}

export interface ImpactResponseDTO {
  root_node_id: string;
  layers: {
    positions: ImpactLayerItem[];
    funds: ImpactLayerItem[];
    clients_opportunities: ImpactLayerItem[];
    skills_apps: ImpactLayerItem[];
    watchlist_hits: ImpactLayerItem[];
  };
  nodes: GraphNodeDTO[];
  edges: GraphEdgeDTO[];
  total_affected: number;
  receipt_id: string;
}

export interface MemoryDTO {
  memory_id: string;
  user_id?: string;
  scope_type: "user" | "user_x_skill";
  skill_id?: string | null;
  key: string;
  content: string;
  tags: string[];
  linked_entity_ids: string[];
  linked_topic_ids: string[];
  created_at?: string;
  updated_at?: string;
}

export interface MemoryNotesResponseDTO {
  memory_enabled: boolean;
  disabled_reason?: string | null;
  items: MemoryDTO[];
}

export interface CompareMemoryResponseDTO {
  note: MemoryDTO;
  matching_firm_claims: ClaimDTO[];
  divergence_summary: string;
  can_promote: boolean;
  receipt_id: string;
}

export interface PromoteMemoryResponseDTO {
  quarantined_claim: ClaimDTO;
  epistemic_status: string;
}

export interface WatchlistAlertDTO {
  alert_id: string;
  severity?: "conflict" | "revision" | "info" | string;
  kind?: string;
  title: string;
  summary?: string;
  target_id?: string;
  target_type?: string;
  entity_canonical_key?: string;
  canonical_key?: string;
  conflict_set_id?: string;
  recorded_at?: string;
  timestamp?: string;
}

export interface WatchlistResponseDTO {
  memory_enabled: boolean;
  disabled_reason?: string | null;
  entity_ids: string[];
  topic_ids: string[];
  source_ids: string[];
  alerts: WatchlistAlertDTO[];
}

export interface UploadResponseDTO {
  upload_id: string;
  filename: string;
  media_type: string;
  content_sha256: string;
  extracted_preview: string;
  quarantined_claim_ids: string[];
  epistemic_status: string;
}

export interface AgentCitationDTO {
  kind: "entity" | "claim" | "conflict" | "topic" | string;
  id: string;
  label: string;
}

export interface ContextAssemblyReceiptDTO {
  receipt_id: string;
  timestamp?: string;
  principal_id?: string;
  wall_side?: WallSide;
  snapshot_version?: number | null;
  data_epoch?: number;
  policy_epoch?: number;
  as_of?: string;
  recorded_as_of?: string;
  claim_ids?: string[];
  redacted_evidence_claim_ids?: string[];
  conflict_set_ids?: string[];
  memory_ids?: string[];
  receipt_sha256?: string;
}

export interface AgentChatResponseDTO {
  answer_markdown: string;
  citations: AgentCitationDTO[];
  tool_calls: Array<{ tool: string; duration_ms: number }>;
  receipt: ContextAssemblyReceiptDTO;
}

export interface CuratorPatchDTO {
  patch_id: string;
  status: string;
  target_id?: string;
  summary: string;
  diff?: string;
  created_at?: string;
}

export interface ApiErrorBody {
  error?: {
    code?: string;
    message?: string;
  };
  detail?: string;
}

// ---------------------------------------------------------------------------
// Version control (ADR-007)
// ---------------------------------------------------------------------------

export interface RedlineSegment {
  op: "equal" | "delete" | "insert";
  text: string;
}

export interface FieldChangeDTO {
  field: string;
  kind: "added" | "removed" | "changed";
  old: unknown;
  new: unknown;
  redline?: RedlineSegment[];
}

export interface ApprovalDTO {
  approver_id: string;
  approver_name: string;
  decision: "approve" | "reject";
  comment: string;
  at: string;
}

export interface HistoryVersionDTO {
  version_no: number;
  op: string;
  recorded_at: string;
  label: string;
  change_id: string;
  title: string;
  reason: string;
  author_id: string;
  author_name: string;
  source: string;
  agent_id: string | null;
  run_id: string | null;
  kind: string;
  approvals: ApprovalDTO[];
  reverted_by_change_id: string | null;
  reverts_change_id: string | null;
  releases: number[];
  channels: string[];
  is_head: boolean;
}

export interface HistoryProposalDTO {
  change_id: string;
  title: string;
  status: string;
  base_version: number | null;
  author_name: string;
  source: string;
  created_at: string;
  approvers: string[];
  approvals: ApprovalDTO[];
}

export interface HistoryDTO {
  record_type: string;
  record_id: string;
  label: string;
  head_version: number | null;
  versions: HistoryVersionDTO[];
  proposals: HistoryProposalDTO[];
  channels: Record<string, number | null>;
}

export interface DiffDTO {
  record_type: string;
  record_id: string;
  from_version: number | null;
  to_version: number;
  restricted: boolean;
  message: string | null;
  changes: FieldChangeDTO[];
  redline: RedlineSegment[];
  text_field: string | null;
}

export interface ChangeItemDTO {
  seq: number;
  record_type: string;
  op: string;
  record_id: string | null;
  label: string;
  before_version: number | null;
  applied_version: number | null;
  rule_id: string | null;
  changes?: FieldChangeDTO[];
  redline?: RedlineSegment[];
}

export interface ChangeSetDTO {
  change_id: string;
  title: string;
  reason: string;
  kind: string;
  source: string;
  author_id: string;
  author_name: string;
  agent_id: string | null;
  run_id: string | null;
  status: "proposed" | "merged" | "rejected" | "conflict" | "failed";
  approval: "auto" | "review";
  rule_ids: string[];
  approvers: string[];
  min_approvals: number;
  items: ChangeItemDTO[];
  approvals: ApprovalDTO[];
  created_at: string;
  merged_at: string | null;
  reverts_change_id: string | null;
  reverted_by_change_id: string | null;
  error: string | null;
  can_approve: boolean;
}

// ---------------------------------------------------------------------------
// Entity pages (ADR-009)
// ---------------------------------------------------------------------------

export type WallSideName = "public_side" | "private_side" | "above_the_wall";

export interface WikiCitationDTO {
  n: number;
  claim_id: string;
  predicate: string;
  source_system: string;
  evidence_uri: string | null;
  span_locator: string | null;
  evidence_restricted: boolean;
  epistemic_status: string;
  valid_from: string | null;
  recorded_at: string | null;
}

export interface WikiStatementDTO {
  text: string;
  cites: number[];
  status: string | null;
}

export interface WikiSectionDTO {
  id: string;
  title: string;
  kind: string;
  text: string | null;
  statements: WikiStatementDTO[];
  mermaid: string | null;
}

export interface WikiPageDTO {
  entity_id: string;
  canonical_key: string;
  title: string;
  entity_type: string;
  side: WallSideName;
  template_id: string;
  sections: WikiSectionDTO[];
  citations: WikiCitationDTO[];
  narrator: string;
  fingerprint: string;
  narration_cached: boolean;
  generated_at: string;
}
