# ADR-003: Security, MNPI Walls, Split Claim/Evidence ACLs, Identity & Receipts

- **Status:** Accepted (Wave 0 Authoritative Contract)
- **Scope:** `packages/core/context_core/policy.py`, `packages/adapters/context_adapters/policy.py`, `packages/api/context_api/auth.py`
- **Resolves:**
  - Contradiction (b): Vault-per-audience (`Shared context...md`) vs. row-level split claim/evidence ACLs (`AI Knowledge Hub Architecture Design.pdf`).
  - Contradiction (c): Restricted note title visibility (`Shared context...md` p. 213) vs. zero existence leakage (`context-architecture-brief.txt` ll. 98–99, 163–164).
  - PDF Starter Defect #1: Missing steward authorization check on `force_supersede` (`main.py`, p. 19).

---

## 1. Resolution of Contradictions (b) & (c): Zero Existence Leakage + Split Claim/Evidence Provenance Stubs

### Hard Invariant 1 — Zero Existence Leakage for Unauthorized Records
If a caller lacks authorization (`read_acl_groups` intersection empty OR `wall_side` barrier fails OR principal is revoked) for ANY `Entity`, `Claim`, `Position`, `Coverage`, `EntityLineageEvent`, `KnowledgeItem`, `TopicAttachment`, or `MemoryRecord`:
- The record MUST be filtered out **before** ranking, pagination, aggregation, counting, graph construction, `_index.md` generation, snapshot compilation, or LLM prompt assembly.
- **No leakage vector is permitted:**
  - Direct GET by ID/key returns `404 Not Found` (identical body and timing shape to a nonexistent ID — NEVER `403` revealing that the ID exists).
  - Search & `Ctrl+K` typeahead omit unauthorized titles, aliases, and snippets completely.
  - Graph nodes, edges, 1-hop/2-hop neighbor expansions, and backlinks omit unauthorized records and never draw stub nodes for them.
  - Pre-aggregated counts (Home stream degree counts, topic badge counts, facet facet-bucket counts, open conflict counts) are computed **strictly over the caller's authorized slice**. A Public Analyst and a Private Credit Deal Team member see different counts for the same topic if MNPI claims exist under that topic.
  - Compiled `.sqlite.zst` snapshots are physically partitioned by `(acl_tier, wall_side)` (resolving Contradiction (b) by uniting vault-per-audience artifact isolation with live row-level enforcement).

### Hard Invariant 2 — Decoupled Claim vs. Evidence Visibility (Redacted Provenance Stub)
Every `Claim` carries `acl: ClaimACL` (`{wall_side: WallSide, read_acl_groups: list[str], write_acl_groups: list[str], evidence_acl_groups: list[str]}`) and three top-level server-computed redaction fields: `evidence_redacted: bool = False`, `evidence_redaction_label: str | None = None`, and `evidence_request_group: str | None = None`.

ONLY when the caller **IS** authorized to read a `Claim` (`can_read_claim == True`), the server independently evaluates `can_read_evidence(principal, claim)` against `claim.acl.evidence_acl_groups`:
- If `evidence_acl_groups` is empty/None OR intersects `principal.groups` (and wall permits): return full `provenance.evidence_uri`, `provenance.excerpt`, `provenance.span_locator`, and top-level `claim.evidence_redacted = False`, `claim.evidence_redaction_label = None`, `claim.evidence_request_group = None`.
- If `evidence_acl_groups` fails: **strip server-side before JSON/MCP/HTML serialization**:
  - `claim.provenance.evidence_uri = None`
  - `claim.provenance.excerpt = None`
  - `claim.provenance.span_locator = None`
  - `claim.evidence_redacted = True`
  - `claim.evidence_redaction_label = "Restricted — Request Access"`
  - `claim.evidence_request_group = "<first required Entra group slug>"` (or policy-defined access-request channel; NEVER the raw filename, S3 path, counterparty name, or document snippet).
- Compiled shared snapshots (`v{N}.sqlite.zst`) for a given `acl_tier` strip `evidence_uri` and `excerpt` for any claim whose `evidence_acl_groups` is stricter than `acl_tier`, setting `evidence_redacted = 1` at compile time.

---

## 2. Information Barriers (`wall_side`)

Every principal and every governed record carries an explicit `WallSide`:
- `public_side`: Public-market research, trading, portfolio management, IR/RP, operations.
- `private_side`: MNPI / deal teams (e.g., Private Credit Deal Team, restructuring/LME working groups).
- `above_the_wall`: Compliance officers, senior executive/risk oversight, cross-wall stewards explicitly cleared for both sides.

**Access Matrix (`PolicyPort`):**

| Principal `wall_side` | `public_side` record | `private_side` record | `above_the_wall` record |
|---|---|---|---|
| `public_side` | Allow (if `read_acl_groups` matches) | **DENY (Zero Leakage)** | **DENY (Zero Leakage)** |
| `private_side` | Allow (if `read_acl_groups` matches) | Allow (if `read_acl_groups` matches) | **DENY (Zero Leakage)** |
| `above_the_wall` | Allow (if `read_acl_groups` matches) | Allow (if `read_acl_groups` matches) | Allow (if `read_acl_groups` matches) |

- **Background Curation Wall Isolation:** Any offline Curator or consolidation pass MUST partition inputs by `wall_side`. A `private_side` observation or deal memo can NEVER contribute text, clusters, aliases, or suggested edits into a `public_side` note or snapshot.

---

## 3. Authentication, Entra JWT/JWKS, `>200`-Group Overage & Revocation

**`Principal` model (`context_core.models`):**
```python
class Principal(BaseModel):
    user_id: str
    display_name: str
    email: str
    roles: frozenset[str]               # e.g. {"analyst", "fund_steward"}
    groups: frozenset[str]              # Resolved Entra group IDs/slugs
    wall_side: WallSide
    auth_mode: Literal["entra_jwt", "local_mock"]
    actor_mode: Literal["obo", "service"] = "obo"
    channel_mode: Literal["direct", "shared"] = "direct"
    revoked: bool = False
    groups_overage_resolved: bool = False
    token_jti: str | None = None
```

1. **Entra OIDC / JWT Verification (Production Mode):**
   - Validates `RS256` JWT locally against cached Entra JWKS (`PyJWKClient`, TTL 3600s, ~0.2 ms CPU, zero per-request Entra RTT), checking `iss`, `aud`, `exp`, `nbf`.
   - **Entra `>200`-Group Overage Handling:** When an Entra token exceeds 200 security groups, Entra omits the `groups` array and injects `_claim_names: {"groups": "src1"}` + `_claim_sources: {"src1": {"endpoint": "..."}}` (or `hasgroups: true`).
     - `auth.py` detects overage, calls the configured `GroupResolver` (Microsoft Graph `/users/{oid}/getMemberObjects` in prod; deterministic `MockDirectoryGroupResolver` in local/test mode), caches `(user_id, policy_epoch) -> frozenset[str]` for 60s, and sets `groups_overage_resolved = True`.
     - **Fail-Closed:** If overage is present and group resolution fails/times out, return `503 Service Unavailable` (or deny all restricted reads) — NEVER fall back to an empty/default broad group!
2. **Revocation Enforcement:**
   - Every authenticated request checks `principal.user_id` against the store's `revoked_principals` table (keyed by `policy_epoch`).
   - A revoked user (`revoked=True` or present in `revoked_principals`) receives `401 Unauthorized` on ALL live API, Graph, MCP, and Upload endpoints immediately (within 0 ms on the write worker and ≤ 250 ms across replicas via `policy_epoch` check), and receives zero cached private context.
3. **Deterministic Local Mock Auth Mode (`AUTH_MODE=mock`):**
   - Accepts `X-Mock-User: <user_id>` (mapped from seeded `fixtures/principals.json` including `>200`-group overage simulation via `X-Mock-Groups-Overage: true` and `revoked` persona) OR signed HS256/RS256 local test JWTs.
   - `AUTH_MODE=mock` is explicitly refused if `ENVIRONMENT=production`.

---

## 4. Identity Delegation (`actor_mode`: OBO vs. Service) & Surface Gating (`channel_mode`)

- **`actor_mode == "obo"` (On-Behalf-Of):** Browser workspace, desktop agents, and interactive MCP sessions act under the human caller's exact Entra groups, `wall_side`, personal memory namespace (`/memories/{user_id}/`), and personal watchlist.
- **`actor_mode == "service"` (Fixed Service Identity):** Headless server-side skill runtimes (CRM, Reporting, Cash-Flow) authenticate with a scoped service principal bound to specific `(acl_tier, wall_side)` snapshots (e.g., `firm-all:public_side`). A `service` principal has **zero personal memory or watchlist access**.
- **Surface-Aware Tool Gating (`channel_mode: "direct" | "shared"`):**
  - When `channel_mode == "shared"` (Teams channel, group chat, shared workspace session) OR `actor_mode == "service"`:
    - `memory_upsert`, personal `/memories/{user_id}/` reads, MCP resource `context://memory/me`, `/api/v1/memory/watchlist`, and `/api/v1/memory/notes/{id}/compare` are **automatically disabled** (`PolicyPort.is_memory_allowed` returns `False`), preventing one user's private notes or watchlist alerts from leaking into a shared channel!
    - **User-Scoped Claim Gating:** `PolicyPort.can_read_claim(principal, claim)` MUST ALSO deny any `Claim` with `scope_type in ("user", "user_x_skill")` (such as `clm-dir-user-pm-format-pref`) whenever `not PolicyPort.is_memory_allowed(principal)` (`channel_mode == "shared"` / `actor_mode == "service"` / `revoked`) OR when `claim.scope_id` does not belong to `principal.user_id` (`claim.scope_id != principal.user_id` for `scope_type == "user"`, or `not claim.scope_id.startswith(f"{principal.user_id}:")` for `scope_type == "user_x_skill"`).
    - MCP `tools/list` dynamically omits `memory_upsert` on `shared` channels, and server-side execution rejects calls with `403 Forbidden (surface_policy_blocked)`.
- **Personal Memory Watchlists, Alerts & Compare/Promote:**
  - In `actor_mode == "obo"` + `channel_mode == "direct"`, users maintain a personal watchlist (`entity_ids`, `topic_ids`, `source_ids`). Whenever an enterprise `ConflictSet` or claim revision touches a watched item (within the caller's ACL & `wall_side`), the watchlist endpoint surfaces deterministic change/conflict alerts (`WatchlistAlertDTO`).
  - Users can compare a personal note against firm claims (`POST /api/v1/memory/notes/{id}/compare`) or promote it (`POST /api/v1/memory/notes/{id}/promote`) into the quarantined `unverified_observation` steward review queue — never bypassing steward approval.
  - Revoked principals have strictly empty memory/watchlist access (`401 Unauthorized` on all endpoints).

---

## 5. Steward Write RBAC (Fixing PDF Defect #1), TOCTOU Concurrency & Anti-Poisoning Quarantine

### Fixing PDF Defect #1 (`force_supersede` Authorization) & TOCTOU Protection
- `PolicyPort.authorize_write(...)` MUST run BEFORE any database mutation in `POST /api/v1/claims`, `PATCH /api/v1/claims/{id}`, `POST /api/v1/conflicts/{id}/resolve`, and MCP `propose_update`:
  - Writing `epistemic_status == "approved_fact"`, setting `is_mandatory == True`, calling `force_supersede == True`, or resolving a `ConflictSet` requires `principal.groups` to intersect `target_entity.write_acl_groups` (e.g., `fund-stewards`, `compliance-officers`) AND pass `wall_side` clearance.
  - Otherwise, `authorize_write` raises `AuthorizationError` -> `403 Forbidden` (no silent supersession, no mutation).
- **TOCTOU Optimistic Concurrency Check on Approvals:**
  - All state-changing approval/resolution endpoints (`POST /api/v1/conflicts/{id}/resolve`, `PATCH /api/v1/claims/{id}`, `POST /api/v1/claims` with `force_supersede=true`) accept `expected_data_epoch: int | None` (or `If-Match` ETag header). If provided and the store's current `data_epoch` (or target claim's `recorded_at`/`superseded_at` state) has changed since the steward inspected the record, the server aborts the transaction and returns **`409 Conflict (stale_version)`**, preventing time-of-check-to-time-of-use race overwrites.
- **Query-String Secret Stripping:**
  - Any `evidence_uri`, redirect URL, log line, error detail, or receipt field is passed through `sanitize_uri_secrets(uri)` stripping sensitive query parameters (`token`, `key`, `apikey`, `sig`, `signature`, `secret`, `password`, `auth`, `X-Amz-*`, `sv`, `se`, `sp`) before storage or response serialization.
- **Non-Steward & Agent Write Path (Anti-Poisoning Quarantine):**
  - Regular users, manual file uploads (PDF/CSV/Markdown), and agent tools (`propose_update`, `flag_knowledge_gap`, background Curator) can ONLY write `epistemic_status = "unverified_observation"` (or `"candidate_sop"`) into the quarantined inbox (`scope_type` matching caller permission, `ingestion_channel` set accurately).
  - Quarantined items **NEVER** overwrite an active `approved_fact`, **NEVER** compile into deterministic skill lookup snapshots, and enter the steward review queue with full SHA-256 provenance until an authorized steward promotes them.

---

## 6. Immutable Context Assembly Receipts

Every read operation (`POST /api/v1/context/bundle`, `resolve_context`, MCP tool calls, Code Mode executions, Viewport Agent answers) emits and persists an immutable **`ContextAssemblyReceipt`**:

```python
class ContextAssemblyReceipt(BaseModel):
    receipt_id: str                  # UUIDv7 / UUID4
    timestamp: datetime              # UTC
    principal_id: str
    auth_mode: str
    actor_mode: Literal["obo", "service"]
    channel_mode: Literal["direct", "shared"]
    wall_side: WallSide
    snapshot_version: int | None     # Compiled snapshot version used (or None for live-only)
    data_epoch: int
    policy_epoch: int
    as_of: datetime                  # Business time
    recorded_as_of: datetime         # System time
    claim_ids: list[str]             # Authorized claims included in context
    redacted_evidence_claim_ids: list[str]  # Authorized claims whose evidence was redacted
    conflict_set_ids: list[str]      # Open conflict sets surfaced
    memory_ids: list[str]            # Personal memories included (always [] when channel_mode=="shared" or actor_mode=="service")
    tool_or_endpoint: str
    receipt_sha256: str              # Deterministic SHA-256 of canonical JSON payload
```

- External observability platforms (e.g., LangSmith, OpenTelemetry, or an external Arize Phoenix deployment) log `receipt_id`, `snapshot_version`, `as_of`, `recorded_as_of`, and `policy_epoch` as span attributes for bit-for-bit reproducible audit trails — without coupling Arize Phoenix into this repository.
