# ADR-001: Four-Plane Institutional Ontology, Bitemporal Model & Stratified Precedence

- **Status:** Accepted (Wave 0 Authoritative Contract)
- **Scope:** `packages/core/context_core/` (domain models, bitemporal query semantics, precedence engine)
- **Resolves:**
  - Contradiction (d): Narrowest-scope-wins (`Shared context...md` §Scopes) vs. epistemic-tier-first (`AI Knowledge Hub Architecture Design.pdf` §2, `gemini-code-*.txt`).
  - PDF Starter Defect #2: Broken `superseded_at IS NULL` filter on historical `as_of` reads (`main.py`, p. 18).
  - PDF Schema Limitation: Binary `conflict_tickets(claim_a_id, claim_b_id)` cannot represent 3+ source disagreements without proliferating duplicate tickets.

---

## 1. Context & Reference Audit

Across the five reference documents now archived untouched in `docs/reference/`:
1. The Markdown recommendation proposes flat notes (`entity`, `fact`, `convention`, `preference`, `observation`, `runbook`, `topic`) with `valid_from`/`valid_to` in frontmatter and system history delegated to Git commits, ranking preferences by narrowest scope (`run > user_x_skill > user > skill > project > firm`).
2. The PDF architecture design proposes relational `entities` and bitemporal `claims`, ranking all context in one list (`approved_fact > project_convention > personal_preference > unverified_observation`), which accidentally lets a `project_convention` outrank a `user_x_skill` formatting preference while still failing to represent investment-firm entity hierarchies, unheld watchlist/pipeline issuers, dual-role parties, or 3+ competing data sources.
3. The PDF starter code (`main.py` p. 18) executes `WHERE c.superseded_at IS NULL AND c.valid_from <= :as_of ...`. Once a restated claim supersedes a prior value (`superseded_at = T2`), replaying a historical report as believed at `T1 < T2` returns the *restated* value instead of the value known at `T1` — breaking auditability and 0-diff historical report reproduction.

---

## 2. Decision: Four-Plane Institutional Ontology

All domain records in `context_core.models` belong to four cleanly separated planes. Every identifier is a stable ASCII slug or UUID string; timestamps are ISO-8601 UTC (`datetime` with `timezone.utc`).

### Plane 1: Canonical Entities & Party/Role Separation (FIBO / GLEIF / OpenFIGI-Aligned)

**`EntityType` enum (exact string values):**
- **Investment Product Hierarchy:** `strategy`, `fund_family`, `reporting_group`, `legal_vehicle`, `share_class`
  - Structural edges: `strategy -> has_fund_family -> fund_family -> has_reporting_group -> reporting_group -> includes_vehicle -> legal_vehicle -> has_share_class -> share_class` (and direct `fund_family -> includes_vehicle -> legal_vehicle` for Master / Onshore Feeder / Offshore Feeder structures).
- **Market & Credit Hierarchy (independent of holdings):** `ultimate_parent`, `pe_sponsor`, `issuer`, `instrument`
  - Issuers exist as first-class entities regardless of whether any Fund holds them (`coverage_status` tracked in Plane 2: `held | watchlist | pipeline | benchmark | passed`).
  - PE-sponsored private credit borrowers link `pe_sponsor -> sponsors_issuer -> issuer -> issues_instrument -> instrument` and `ultimate_parent -> controls -> issuer`.
- **Party, Client & Distribution Hierarchy:** `legal_party`, `client`, `consultant`, `commitment`, `opportunity`
  - **Party/Role Separation:** A single canonical `legal_party` (anchored by LEI) holds independent role links (`acts_as_issuer`, `acts_as_lp_client`, `acts_as_counterparty`, `acts_as_sponsor`) so one institution (e.g., a global sovereign wealth fund or bank holding company that issues bonds, invests as an LP, and serves as ISDA swap counterparty) is NEVER duplicated into conflicting entity records.
  - Existing LP relationships are `commitment` records (`client -> holds_commitment -> legal_vehicle`); prospective allocations are `opportunity` records (`client|consultant -> pursues_opportunity -> fund_family|legal_vehicle`).
- **Operational & Knowledge Entities:** `app`, `skill`, `user`, `project`, `data_source`, `topic`, `document_artifact`, `knowledge_item`

**Core `Entity` fields:**
```python
entity_id: str                 # Stable UUID or deterministic ID
entity_type: EntityType
canonical_key: str             # Unique lowercase slug, e.g. "veh:alpha-credit-offshore-lp"
display_name: str
aliases: list[str]             # Current & historical synonyms for lexical matching
description: str               # Concise one-line definition
owner_group: str               # Entra group slug
wall_side: WallSide            # "public_side" | "private_side" | "above_the_wall"
read_acl_groups: list[str]     # Required Entra groups (any-of match + wall_side check)
write_acl_groups: list[str]    # Steward groups authorized to approve/supersede
attributes: dict[str, Any]     # Type-specific structured metadata (jurisdiction, domicile, etc.)
status: Literal["active", "inactive", "merged", "archived"]
created_at: datetime
updated_at: datetime
```

**Bitemporal `EntityIdentifier` crosswalk (`entity_identifiers`):**
- Maps any `entity_id` to external schemes over business and system time:
  - `scheme`: `lei | openfigi | cusip | isin | sedol | bloomberg_ticker | admin_code | crm_id | oms_id | gips_code | cik | internal_slug`
  - `identifier_value: str`, `authority: str`, `is_primary: bool`, `valid_from: datetime`, `valid_to: datetime | None`, `recorded_at: datetime`, `superseded_at: datetime | None`.

**`EntityLineageEvent` (`entity_lineage_events`):**
- Captures renames, corporate actions, and Liability Management Exercises (LMEs) without losing historical identity:
  - `event_id: str`, `source_entity_id: str`, `target_entity_id: str`
  - `event_type`: `renamed_from | merged_into | spun_off_from | acquired_by | lme_restructured_to | split_into`
  - `effective_date: datetime`, `recorded_at: datetime`, `summary: str`, `evidence_uri: str | None`, `evidence_acl_groups: list[str]`, `wall_side: WallSide`, `read_acl_groups: list[str]`.

---

### Plane 2: Reified Associations, Bitemporal Claims & N-Way Conflicts

Associations that carry effective dates, amounts, roles, or provenance are first-class reified records rather than bare graph edges:

1. **`Position` (`positions`):** `position_id`, `vehicle_entity_id`, `instrument_entity_id`, `issuer_entity_id`, `position_type` (`long | short | unfunded_commitment | credit_facility`), `quantity: float | None`, `market_value: float | None`, `currency: str`, `weight_bps: float | None`, `as_of_date: date`, `recorded_at: datetime`, `source_system: str`, `wall_side: WallSide`, `read_acl_groups: list[str]`.
2. **`Coverage` (`coverages`):** `coverage_id`, `analyst_entity_id`, `issuer_entity_id`, `coverage_role` (`primary_analyst | secondary_analyst | deal_lead | pm_sponsor`), `coverage_status` (`held | watchlist | pipeline | benchmark | passed`), `valid_from: datetime`, `valid_to: datetime | None`, `wall_side: WallSide`, `read_acl_groups: list[str]`.
3. **`SkillBinding` (`skill_bindings`):** `binding_id`, `skill_entity_id`, `target_entity_id` (topic, entity type, or specific entity), `mapping_purpose: str` (e.g., `fund_family`, `consolidated_group`, `legal_vehicle`, `cashflow_wire`), `read_mode` (`compiled_snapshot | live_bundle | on_demand_search`), `required_predicates: list[str]`, `active: bool`.
4. **`Claim` (`claims`):** Atomic bitemporal assertion:
   - Identity & Triple: `claim_id`, `subject_id`, `predicate`, `object_value: str` (canonical string or JSON-encoded scalar/object), `object_entity_id: str | None` (populated when edge points to another `Entity`).
   - Applicability & Scope: `context_purpose: str` (default `"general"`; e.g., `"fund_family"`, `"consolidated_group"`, `"legal_vehicle"`), `scope_type` (`run | user | user_x_skill | skill | project | firm`), `scope_id: str`.
   - Epistemic & Channel:
     - `channel`: `fact | directive`
     - `epistemic_status`: `approved_fact | disputed | project_convention | skill_convention | personal_preference | unverified_observation | candidate_sop | revoked`
     - `is_mandatory: bool` (valid only on `directive` channel at `firm | project | skill` scopes; prevents user preference override).
   - Bitemporal Windows:
     - **Business time:** `valid_from: datetime`, `valid_to: datetime | None` (`None` = open-ended).
     - **System time:** `recorded_at: datetime`, `superseded_at: datetime | None` (`None` = current belief), `superseded_by: str | None`.
   - Facets, Provenance, ACL & Redaction Envelope:
     - `facets: ClaimFacets`, `provenance: Provenance`, `acl: ClaimACL` where `ClaimACL` defines `{wall_side: WallSide, read_acl_groups: list[str], write_acl_groups: list[str], evidence_acl_groups: list[str]}` (see ADR-003).
     - Top-level redaction fields on `Claim` (computed server-side per caller; never stored raw): `evidence_redacted: bool = False`, `evidence_redaction_label: str | None = None`, `evidence_request_group: str | None = None`. When `evidence_redacted == True`, `provenance.evidence_uri`, `provenance.excerpt`, and `provenance.span_locator` are zeroed to `None`.
5. **N-Way `ConflictSet` (`conflict_sets` + `conflict_set_members`):**
   - Replaces the PDF's binary `conflict_tickets(claim_a_id, claim_b_id)`.
   - Keyed by collision tuple `(subject_id, predicate, context_purpose, scope_type, scope_id)` across overlapping `[valid_from, valid_to)` windows.
   - Holds `claim_ids: list[str]` (2, 3, or N simultaneous disagreeing sources — e.g., Admin Feed A, Custodian Feed B, and GIPS Reporting Feed C asserting 3 distinct admin codes).
   - **Non-overwrite invariant:** When a new assertion collides with an active `approved_fact` (and caller is not an authorized steward executing explicit `resolve_conflict` / `force_supersede`), the active `approved_fact` remains UNTOUCHED (`superseded_at IS NULL`), the incoming claim is stored as `epistemic_status="disputed"` (or `"unverified_observation"` if unverified upload), and all colliding `claim_id`s are linked under a single open `ConflictSet`.
   - **No False Conflicts:** Two claims with different `context_purpose` (e.g., `crm_family` vs. `consolidated_group` vs. `legal_vehicle`) or non-overlapping `[valid_from, valid_to)` intervals NEVER trigger a conflict set.

#### True Bitemporal Query Semantics (Fixing PDF Defect #2)

Every historical or live read takes TWO orthogonal timestamps:
- `as_of: datetime` (business/valid time; defaults to `now_utc`)
- `recorded_as_of: datetime | None` (system/transaction time; defaults to `now_utc`)

The mandatory predicate across **both** SQL and OKF/snapshot adapters is:
```sql
    c.valid_from <= :as_of
AND (c.valid_to IS NULL OR c.valid_to > :as_of)
AND c.recorded_at <= :recorded_as_of
AND (c.superseded_at IS NULL OR c.superseded_at > :recorded_as_of)
```
When a restatement at `T2` supersedes a Q4 2025 claim recorded at `T1`, querying `(as_of=2025-12-31, recorded_as_of=T1+1s)` returns the exact pre-restatement figure (0-diff historical replay), while `(as_of=2025-12-31, recorded_as_of=T2+1s)` returns the restated figure.

---

### Plane 3: Orthogonal Information Facets

Every `Claim` carries explicit orthogonal facets (`ClaimFacets`) plus `Provenance`:

| Facet | Allowed Values | Extra Typed Fields |
|---|---|---|
| `domain_nature` | `financial` \| `non_financial` | — |
| `modality` | `quantitative` \| `qualitative` | Quantitative: `numeric_value: float \| None`, `unit: str \| None` (`bps`, `pct`, `usd`, `x`, `count`), `currency: str \| None` (ISO-4217), `accounting_basis: str \| None` (`gaap`, `ifrs`, `non_gaap`, `gips`, `nav_net`, `nav_gross`) |
| `epistemic_temporal` | `actual` \| `estimate` \| `forecast` \| `guidance` \| `pro_forma` | Actual sub-stage `actual_stage`: `prelim \| unaudited \| audited \| restated \| None` |
| `periodicity` | `point_in_time` \| `daily` \| `monthly` \| `quarterly` \| `ltm` \| `annual` \| `multi_year` \| `evergreen` | `fiscal_period: str \| None` (e.g., `"2025-Q4"`, `"FY2025"`, `"2026-03-31"`) |
| `ingestion_channel` | `automated_feed` \| `manual_upload` \| `human_authored` \| `agent_extracted` | — |

**`Provenance`:**
- `source_system: str` (e.g., `"admin-feed"`, `"custodian-feed"`, `"gips-reporting"`, `"manual-upload"`, `"crm-extract"`)
- `evidence_uri: str | None` (POSIX normalized relative/object path; stripped to `None` on wire if evidence ACL fails)
- `content_sha256: str | None` (SHA-256 of LF-normalized text or raw binary bytes)
- `span_locator: str | None` (e.g., `"page=2,section=4.2"`, `"row=14,col=admin_code"`, `"lines=18-26"`)
- `learned_from_run_id: str | None`
- `excerpt: str | None` (stripped to `None` on wire if evidence ACL fails)

---

### Plane 4: Dynamic SKOS Topic Overlay

Topics are NOT folders, NOT synonymous with entities, and **NEVER a frozen enum**. `Topic` implements a dynamic, CRUD-managed hierarchical SKOS taxonomy (`broader_topic_id`, `narrower_topic_ids`, `related_topic_ids`, `pref_label`, `alt_labels`) that is addable, nestable, mergeable (`merged_into_topic_id`), and archivable (`status: "active" | "proposed" | "merged" | "archived"`):
- **Two Top-Level Domain Roots (aligned with Plane 3 `domain_nature`):**
  - **`financial`** (`topic:financial`): `performance-returns`, `nav-valuation`, `cashflows-liquidity`, `leverage-covenants`, `fees-terms`.
  - **`non_financial`** (`topic:non-financial`): `legal-side-letters`, `esg-sustainability`, `governance-controls`, `operational-admin`, `client-commercial`, `investment-thesis`.
- **Emergent, Nested & Mergeable Child Topics:** Users and curation jobs can create nested subtopics (`POST /api/v1/topics`), reparent/nest, archive, or merge duplicate topics (`PATCH /api/v1/topics/{topic_id}` setting `merged_into_topic_id` while preserving redirect aliases — e.g., `lme-uptier-emergent` merged into `lme-restructuring` under `leverage-covenants`).
- **Polymorphic `TopicAttachment`:** Links any `topic_id` to any `(target_type, target_id)` across `entity`, `claim`, `artifact`, `issuer`, `fund`, `opportunity`, enabling O(1) bidirectional navigation (`topic -> attached records` and `record -> topics`) subject to caller ACL + `wall_side` filtering prior to counting.

---

## 3. Resolution of Contradiction (d): Two-Channel Stratified Precedence

`context_core.precedence.resolve_context_bundle(...)` NEVER mixes facts and working preferences in a single winner-take-all slot. Every resolved bundle returns **two labelled channels** plus an explicit `conflicts` list:

1. **`facts` channel (Epistemic-Tier-First):**
   - Rank 1: `approved_fact` (authoritative institutional truth; CANNOT be overridden by any convention, personal preference, or observation).
   - Rank 2: `disputed` (if an open `ConflictSet` overlaps the active `(subject_id, predicate, context_purpose, as_of)`, return the active `approved_fact` if any PLUS all competing claims in the `ConflictSet` with explicit `conflict_set_id` and sources — NEVER silently pick one).
   - Rank 3: `unverified_observation` / `candidate_sop` (returned ONLY when `include_unverified=True`, always flagged `unverified=True`, NEVER injected into deterministic lookup APIs like `get_mapping()`).
   - Tie-breaker within equal epistemic status: narrower `scope_type` (`skill > firm`), then more recent `valid_from`, then latest `recorded_at`, then deterministic `claim_id` sort.

2. **`directives` channel (Mandatory-Pinned, Then Narrowest-Scope-Wins):**
   - Rank 1 (Pinned): Directives with `is_mandatory=True` (`firm`, `project`, or `skill` compliance/reporting mandates). Cannot be overridden by non-mandatory personal preferences.
   - Rank 2 (Narrowest-Scope-Wins for non-mandatory formatting/workflow conventions & preferences):
     `run (1) > user_x_skill (2) > user (3) > skill (4) > project (5) > firm (6)`.
   - Every item delivered to an agent carries explicit `channel`, `epistemic_status`, `scope_type`, `is_mandatory`, and `provenance` so models never confuse a personal preference with institutional truth.

---

## 4. Three-Part Knowledge Factory Taxonomy

Every `knowledge_item` or authored note classifies into one of three distinct roles:
1. **`domain_knowledge`:** Governed facts, fund/entity definitions, accounting conventions, corporate-action memos, and LME interpretations. Stored as structured `Entity`/`Claim` records + OKF notes.
2. **`skill_procedure` (`SKILL.md`):** Actionable operational playbooks with YAML frontmatter including `name`, `description`, `scope`, `wall_side`, and `metadata.include_tools: list[str]` controlling which MCP/SDK tools activate when a skill loads (see ADR-004).
3. **`data_access_guidance`:** Authoritative pointers instructing agents *how* to query live systems of record (OMS, PMS, Admin API, CRM, SQL warehouse — endpoints, table names, identifier crosswalks, latency/freshness caveats) rather than duplicating rapidly changing positions, intraday prices, or cash balances into static knowledge notes.
