# ADR-005: Minimalist Obsidian UX, Diagnostic Graph & Shared `/api/v1` Contract

- **Status:** Accepted (Wave 0 Authoritative Contract)
- **Scope:** `packages/api/context_api/` (Subagent 2) and `apps/web/` (Subagent 3)
- **Design Principle:** Deeper, Not Bigger — quiet dark obsidian discipline (Linear / Apple Notes restraint). Zero marketing copy, zero taglines, zero helper text, zero purple/neon borders, zero permanent multi-section control panels, zero Architecture/MCP marketing tabs.

---

## 1. Visual & Interaction Contract (`apps/web/`)

### Locked Dark Quiet Obsidian Palette & Typography
- **Surfaces:** Primary canvas `#0A0A0B`, elevated panels/drawers `#111113`, hover/active row `#18181B`, hairline dividers `border-white/[0.08]` (`rgba(255,255,255,0.08)`).
- **Ink & Controls:** Primary text `#F4F4F5`, secondary text `#A1A1AA`, muted metadata `#71717A`; crisp high-contrast primary buttons (`#F4F4F5` surface with `#0A0A0B` ink, hover `#E4E4E7`); zero purple, zero neon glows, zero decorative gradients.
- **Typography:** Inter / SF Pro / system sans for UI & prose; monospace with `font-variant-numeric: tabular-nums` for canonical keys, admin codes, dates, counts, NAV/IRR figures, and receipt/SHA prefixes.
- **Semantic Accents (restrained, strictly diagnostic):**
  - Upstream / incoming provenance edges: **Sky (`#38BDF8`)**
  - Downstream / outgoing consumers & impact edges: **Amber (`#F59E0B`)**
  - Open conflict edges & conflict badges: **Rose (`#F43F5E`)**
  - Approved fact dot/pill: **Emerald (`#34D399`)**
  - Unverified / quarantined observation pill: **Amber (`#F59E0B`)**
  - Redacted evidence stub: muted slate lock chip (`Restricted — Request Access`)

### Top Header (Single 48px Quiet Strip — Lean Navigation)
- **Left:** Quiet brand mark (`Context Lake`) + 4 lean views ONLY: **`Home` · `Table` · `Graph` · `My Context`**. (Do NOT add Architecture, Docs, or MCP marketing tabs.)
- **Center:** Instant search trigger pill (`Search…  Ctrl+K` on Windows/Linux; `Search…  ⌘K` on macOS — platform-detected).
- **Right:** Compact `as_of` / `recorded_as_of` Snapshot time-travel chip, `Upload` button (opens Upload Drawer), `Agent` toggle (opens Docked Viewport Agent), and compact Persona / `channel_mode` (`direct` | `shared`) switcher.

### The 4 Lean Views + Focused Drawers

1. **`Home` (Stream):**
   - Quiet scannable stream of caller-authorized entities, open N-way conflicts, and recent knowledge updates with **inline degree counts** (e.g., `6 edges · 3 skills · 1 conflict` in `tabular-nums`, computed strictly over caller's authorized slice).
   - Clicking any row opens the **Right-Hand Inspector Drawer**; clicking a Plane 4 topic chip filters immediately.

2. **`Table` (Dense Table + Split Inspector):**
   - High-density sortable table with Plane 3 orthogonal facet chips (`financial` / `non_financial`, `quantitative` / `qualitative`, `actual` / `estimate` / `forecast` / `guidance` / `pro_forma`, `ingestion_channel`), Plane 4 hierarchical Topic tree/filter, and `Conflicts` toggle.
   - **Right-Hand Inspector Drawer (shared across `Home`, `Table`, `Graph`):**
     - Identity: Display name, canonical key, entity type, `wall_side`, aliases, bitemporal identifiers (`LEI`, `OpenFIGI`, `Admin Code`, `CRM ID`, `GIPS Code`), and Entity Lineage timeline (`renamed_from`, `lme_restructured_to`, `merged_into`, `spun_off_from`).
     - **Two-Channel Claims:** Separate `Facts` and `Directives` sections.
     - **N-Way Conflict Resolution Card:** All N competing claims side-by-side (`source_system`, value, `valid_from`/`valid_to`, evidence link OR redacted stub `Restricted — Request Access` with zero URI/snippet leakage), **Reversible Impact / Blast Radius** panel (via `GET /api/v1/map/impact/{node_id}`), and Steward `Resolve & Approve` action (passing `expected_data_epoch` for TOCTOU safety; non-stewards see `Propose Correction` writing quarantined `unverified_observation`).
     - **Topics & Reverse Navigation:** Clickable SKOS topic chips + `Add Topic` / dynamic hierarchy picker.

3. **`Graph` (Diagnostic Graph View — Cytoscape.js):**
   - **Single Compact 40px Graph Control Bar** (zero permanent multi-section side panels!):
     - Lens pills: `Topic` | `App` | `Skill` | `Project` | `Issuer` | `Fund Hierarchy`
     - Inline graph search box + `Isolate` toggle (foreground matching nodes at `1.0`, dim non-matches to `0.12`; `Isolate` hides dimmed nodes)
     - **`Filter` popover** (closed by default: Plane 3 facets, conflicts-only, topic filter)
     - **`Display` / Tuning popover (`⚙`)** (closed by default: spacing scale, φ-cluster cohesion, `Reset Pins`, and runtime adapter selector via `GET/PUT /api/v1/config/adapters`)
   - **Deterministic Physics & Interaction Invariants:**
     - Deterministic seeded Barnes-Hut / compound cluster layout with **φ-asymmetric cluster springs (`φ ≈ 1.618`)** (intra-cluster spring `1.618×` inter-cluster).
     - **Golden-angle orphan ring (`137.507764°`)** placing zero-degree nodes deterministically around the outer perimeter.
     - **300ms-debounced φ-decay hover spotlight** (`hop 0 = 1.0`, `hop 1 = 0.618`, `hop 2 = 0.382`, `rest = 0.12`).
     - **Sticky 1-hop directional traversal & Impact Trace:** Selecting a node opens a compact floating focus pill (`← Upstream` sky, `Both`, `Downstream →` amber, `Impact BFS`, `Clear`).
     - **Drag-to-pin spatial memory** persisted in `localStorage` per `(lens, node_id)` with double-click unpin.
     - **Zoom Label LOD:** `< 0.55×` cluster headers & conflicts; `0.55–1.10×` entity labels; `> 1.10×` edge labels & epistemic badges.

4. **`My Context` (Personal Memory, Watchlist & Alerts):**
   - **Personal Notes:** Caller's `user` and `user_x_skill` notes with inline create/edit/delete, **`Compare vs Firm`** (`POST /api/v1/memory/notes/{id}/compare`), and **`Promote to Review`** (`POST /api/v1/memory/notes/{id}/promote`).
   - **Personal Watchlist & Alerts:** Manage watched entities, topics, and data sources (`GET/PUT /api/v1/memory/watchlist`) with live change/conflict alerts when open conflicts or claim revisions touch watched items.
   - When `channel_mode == "shared"` or `actor_mode == "service"`, renders a quiet single-line state (`Personal context disabled in shared channel`).

5. **Overlays:**
   - **Command Palette (`Ctrl+K` / `⌘K`):** Instant ACL-filtered search across entities, topics, skills, conflicts, and view jumps.
   - **Manual Upload Drawer:** Accepts `.pdf`, `.csv`, `.md`, `.txt`; computes SHA-256 server-side; writes strictly to quarantined `unverified_observation`.
   - **Docked Viewport Agent:** Collapsible right panel passing viewport context (`visible_entity_ids`, `selected_entity_id`, `active_lens`, `as_of`, `recorded_as_of`); clickable inline citations (`[entity:key]`, `[claim:id]`, `[conflict:id]`) + compact `receipt_id` & `v{snapshot_version}` footer.

---

## 2. Authoritative Shared REST, SSE & Discovery Contract (`packages/api` <-> `apps/web`)

### Common Headers & Error Semantics
- Headers: `Authorization: Bearer <jwt>` (prod), `X-Mock-User: <user_id>` (local mock auth), `X-Mock-Actor-Mode: obo | service` (default `obo`), `X-Mock-Channel-Mode: direct | shared` (default `direct`), `X-Mock-Groups-Overage: true | false`, `If-Match: "<data_epoch>"` (optional TOCTOU guard).
- Errors: `{ "error": { "code": "unauthorized|forbidden|not_found|conflict|validation_error|surface_policy_blocked", "message": "..." } }`.
  - Unauthorized read of restricted entity/claim/memory -> **`404`** (zero existence leakage).
  - Unauthorized write / steward action / shared-channel memory write -> **`403`**.
  - Revoked user or invalid JWT -> **`401`**.
  - TOCTOU stale epoch/version on approval -> **`409`**.

### Complete Endpoint Table

| Method & Path | Query / Body Parameters | Response DTO (JSON / Text) |
|---|---|---|
| `GET /healthz` | — | `{status: "ok", data_epoch, policy_epoch, active_adapters: {store, retrieval, llm_mode}, latest_snapshots: [...]}` |
| `GET /llms.txt` | — | `text/plain`: concise machine-readable catalog of Context Lake capabilities, `/api/v1` endpoints, `/mcp` + `/mcp/codemode` FastMCP endpoints, `SKILL.md` index, and bitemporal semantics |
| `GET /agents` | — | `application/json`: structured agent discovery manifest (`mcp_endpoint: "/mcp"`, `codemode_endpoint: "/mcp/codemode"`, `code_mode_tools`, `primitive_tools`, `resources: ["context://memory/me"]`, `skills: [...]`, `auth_modes`) |
| `GET /api/v1/me` | — | `{user_id, display_name, email, roles, groups, wall_side, actor_mode, channel_mode, auth_mode, groups_overage_resolved, memory_enabled}` |
| `GET /api/v1/personas` | *(local mock mode only)* | `{personas: [{user_id, display_name, role_summary, wall_side, revoked, groups_overage}]}` |
| `GET /api/v1/config/adapters` | — | `{store: "sqlite_bitemporal"\|"okf_markdown_vault", retrieval: "lexical_manifest"\|"facet_graph", llm_mode: "auto"\|"gemini"\|"deterministic", available: {...}}` |
| `PUT /api/v1/config/adapters` | `{store?, retrieval?, llm_mode?}` | `200`: updated active adapter config + `{synced: true, data_epoch}` |
| `GET /api/v1/stream` | `topic?`, `entity_type?`, `conflicts_only?`, `as_of?`, `recorded_as_of?`, `limit=50`, `offset=0` | `{items: StreamItemDTO[], counts: {entities, open_conflicts, observations}, data_epoch, policy_epoch}` |
| `GET /api/v1/entities` | `entity_type?`, `topic?`, `domain_nature?`, `modality?`, `epistemic_temporal?`, `ingestion_channel?`, `q?`, `as_of?`, `recorded_as_of?`, `limit=100`, `offset=0` | `{items: EntitySummaryDTO[], total: int, facet_counts: Record<string, Record<string, int>>, data_epoch, policy_epoch}` |
| `GET /api/v1/entities/{id_or_key}` | `as_of?`, `recorded_as_of?` | `{entity, identifiers, lineage, facts, directives, conflicts, positions, coverages, skill_bindings, topics, backlinks, receipt_id}` |
| `GET /api/v1/search` | `q`, `entity_type?`, `topic?`, `domain_nature?`, `modality?`, `epistemic_temporal?`, `as_of?`, `recorded_as_of?`, `limit=25` | `{hits: SearchHitDTO[], total: int, receipt_id}` |
| `GET /api/v1/topics` | `include_archived=false`, `as_of?`, `recorded_as_of?` | `{topics: TopicWithCountsDTO[], tree: TopicTreeNodeDTO[]}` (hierarchical under `financial` & `non_financial`; counts strictly ACL-filtered!) |
| `POST /api/v1/topics` | `{slug, pref_label, alt_labels?, broader_topic_id?, related_topic_ids?, topic_kind="emergent_candidate", owner_group?}` | `201`: `{topic: TopicDTO, data_epoch}` |
| `PATCH /api/v1/topics/{topic_id}` | `{pref_label?, alt_labels?, broader_topic_id?, merged_into_topic_id?, status?: "active"\|"proposed"\|"merged"\|"archived"}` | `200`: `{topic: TopicDTO, data_epoch}` (supports nesting, merging, archiving) |
| `GET /api/v1/topics/{slug}/items` | `as_of?`, `recorded_as_of?` | `{topic, entities, claims, knowledge_items}` |
| `GET /api/v1/graph` | `lens=topic\|app\|skill\|project\|issuer\|fund_hierarchy`, `focus_id?`, `hops=2`, `direction=both\|in\|out`, `q?`, `topic?`, `as_of?`, `recorded_as_of?` | `{lens, nodes: GraphNodeDTO[], edges: GraphEdgeDTO[], clusters: GraphClusterDTO[], stats: {node_count, edge_count, conflict_count, orphan_count}, data_epoch, policy_epoch, receipt_id}` |
| `GET /api/v1/map/impact/{node_id}` | `max_depth=5`, `as_of?`, `recorded_as_of?` | `{root_node_id, layers: {positions: [], funds: [], clients_opportunities: [], skills_apps: [], watchlist_hits: []}, nodes: GraphNodeDTO[], edges: GraphEdgeDTO[], total_affected: int, receipt_id}` (ACL-enforced reverse BFS, zero leakage) |
| `POST /api/v1/context/bundle` | `{entity_keys: string[], skill_id?, project_id?, run_id?, include_shared=true, include_unverified=false, as_of?, recorded_as_of?}` | `{source: "live"\|"cache", facts, directives, conflicts, memories, receipt}` |
| `POST /api/v1/claims` | `{subject_canonical_key, predicate, object_value, object_entity_id?, context_purpose="general", scope_type, scope_id, channel="fact", epistemic_status, kind?="observation", is_mandatory=false, facets?, source_system, evidence_uri?, evidence_acl_groups?, valid_from?, valid_to?, force_supersede=false, expected_data_epoch?}` | `201`: `{claim, conflict_detected, conflict_set_id, data_epoch}` (`403` if unauthorized steward write; `409` on TOCTOU epoch mismatch) |
| `PATCH /api/v1/claims/{claim_id}` | `{object_value?, epistemic_status?, valid_from?, valid_to?, rationale?, force_supersede=true, expected_data_epoch?}` *(steward inline edit / bitemporal supersession)* | `200`: `{claim, superseded_claim_id, snapshot_scheduled, data_epoch}` (`403` if non-steward; `404` if unauthorized/missing; `409` on TOCTOU mismatch) |
| `GET /api/v1/conflicts` | `status=open\|resolved\|all`, `subject_id?` | `{conflicts: ConflictSetDTO[]}` |
| `POST /api/v1/conflicts/{conflict_set_id}/resolve` | `{winning_claim_id?, replacement_object_value?, valid_from?, close_prior_valid_to?, rationale, expected_data_epoch?}` | `200`: `{conflict, approved_claim, affected_skills, snapshot_scheduled, data_epoch}` (`403` non-steward; `409` TOCTOU mismatch) |
| `GET /api/v1/memory/notes` | `skill_id?` | `{memory_enabled: bool, disabled_reason?, items: MemoryDTO[]}` |
| `POST /api/v1/memory/notes` | `{scope_type: "user"\|"user_x_skill", skill_id?, key, content, tags?, linked_entity_ids?, linked_topic_ids?}` | `201`: `{memory: MemoryDTO}` (`403` when `channel_mode=="shared"` or `actor_mode=="service"`) |
| `DELETE /api/v1/memory/notes/{memory_id}` | — | `200`: `{deleted: true}` |
| `POST /api/v1/memory/notes/{memory_id}/compare` | `{as_of?}` | `200`: `{note: MemoryDTO, matching_firm_claims: ClaimDTO[], divergence_summary: string, can_promote: bool, receipt_id}` |
| `POST /api/v1/memory/notes/{memory_id}/promote` | `{subject_canonical_key, predicate?, rationale?}` | `201`: `{quarantined_claim: ClaimDTO, epistemic_status: "unverified_observation"}` |
| `GET /api/v1/memory/watchlist` | — | `{memory_enabled: bool, entity_ids: string[], topic_ids: string[], source_ids: string[], alerts: WatchlistAlertDTO[]}` |
| `PUT /api/v1/memory/watchlist` | `{entity_ids: string[], topic_ids: string[], source_ids: string[]}` | `200`: updated watchlist + recomputed ACL-filtered `alerts` |
| `POST /api/v1/uploads` | `multipart/form-data`: `file`, `subject_canonical_key?`, `topic_slug?`, `wall_side="public_side"`, `evidence_acl_groups?`, `notes?` | `201`: `{upload_id, filename, media_type, content_sha256, extracted_preview, quarantined_claim_ids, epistemic_status: "unverified_observation"}` |
| `POST /api/v1/agent/chat` | `{message: string, viewport: {lens?, visible_entity_ids: string[], selected_entity_id?, active_filters?, as_of?, recorded_as_of?}}` | `{answer_markdown, citations: [{kind, id, label}], tool_calls: [{tool, duration_ms}], receipt}` |
| `GET /api/v1/snapshots/latest` | `acl_tier`, `wall_side` | `{manifest: SnapshotManifestDTO}` |
| `POST /api/v1/snapshots/compile` | `{acl_tier, wall_side}` *(steward only)* | `{manifest: SnapshotManifestDTO}` |
| `GET /api/v1/receipts/{receipt_id}` | — | `{receipt: ContextAssemblyReceiptDTO}` |
| `GET /api/v1/curator/patches` | `status=pending_steward_review\|all` | `{patches: CuratorDraftPatchDTO[]}` |
| `POST /api/v1/curator/run` | `{wall_side?}` *(steward only)* | `{staleness_report: StalenessReportDTO, created_patches: CuratorDraftPatchDTO[]}` |
| `GET /api/v1/events` | *(SSE `text/event-stream`)* | Emits non-sensitive `epoch_updated`, `claim_written`, `conflict_resolved`, `snapshot_published` events only |

### Exact `GraphNodeDTO`, `GraphEdgeDTO` & `GraphClusterDTO` Shapes
```json
{
  "cluster": { "id": "cluster:topic:cashflows-liquidity", "label": "Cashflows & Liquidity", "kind": "topic", "member_count": 6 },
  "node": {
    "id": "ent:veh-alpha-offshore",
    "canonical_key": "veh:alpha-credit-offshore-lp",
    "label": "Alpha Credit Offshore LP (Cayman)",
    "entity_type": "legal_vehicle",
    "cluster_id": "cluster:topic:cashflows-liquidity",
    "wall_side": "public_side",
    "degree_in": 4,
    "degree_out": 3,
    "is_orphan": false,
    "has_conflict": true,
    "open_conflict_count": 1,
    "epistemic_badge": "disputed",
    "topics": ["cashflows-liquidity", "operational-admin"]
  },
  "edge": {
    "id": "edge:clm-scope-cashflow-alpha-vehicle",
    "source": "ent:skill-cashflow-recon",
    "target": "ent:veh-alpha-offshore",
    "predicate": "maps_as",
    "label": "legal_vehicle",
    "context_purpose": "legal_vehicle",
    "channel": "fact",
    "epistemic_status": "approved_fact",
    "direction_role": "downstream",
    "has_conflict": false
  }
}
```
