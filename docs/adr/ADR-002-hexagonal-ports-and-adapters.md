# ADR-002: Hexagonal Ports & Adapters, Dual Stores, Retrieval & Safe Snapshot SDK

- **Status:** Accepted (Wave 0 Authoritative Contract)
- **Scope:** `packages/core/context_core/ports.py`, `packages/adapters/context_adapters/`, `packages/sdk/context_sdk/`
- **Resolves:**
  - Contradiction (a): Git/OKF Markdown vault (`Shared context...md`, `context-architecture-brief.txt`) vs. SQL Server bitemporal relational store (`AI Knowledge Hub Architecture Design.pdf`, `gemini-code-*.txt`).
  - PDF Starter Defect #3: Single-worker in-process `bundle_cache` drift (`main.py`, pp. 16, 19).
  - PDF Starter Defect #4: Blocking synchronous `boto3` S3/compression calls inside `async def` (`compiler.py`, pp. 14–16).
  - PDF Starter Defect #5: Unsafe `old_conn.close()` while concurrent readers may access SQLite connection (`context_sdk.py`, pp. 20–21).
  - PDF Starter Defect #6: Background `_poll_loop` silently clobbering `pinned_version` (`context_sdk.py`, p. 20).

---

## 1. Resolution of Contradiction (a): Hexagonal Architecture with Coequal Dual Adapters

Rather than forcing a false binary choice between Git-native Markdown (human review, diffs, OKF portability) and relational bitemporal SQL (ACID transactions, multi-writer conflict detection, row-level split ACLs, private per-user memory), `context_core` defines five strict `typing.Protocol` ports. All API, MCP, SDK, and Agent layers depend **exclusively** on these ports:

1. **`StorePort`** — Implemented by two real, conformance-tested adapters:
   - `SqliteBitemporalStore`: SQLAlchemy 2.x async (`sqlite+aiosqlite:///...`, portable to `mssql+aioodbc` / `postgresql+asyncpg` via standard ANSI SQL DDL). Serves as the live transactional system of record for ACID writes, N-way `ConflictSet` detection, bitemporal point-in-time queries, private `user`/`user_x_skill`/`run` memory, and epoch counters.
   - `OkfMarkdownVaultStore`: Open Knowledge Format (OKF v0.2) Git-native Markdown files with validated YAML frontmatter, typed frontmatter links + `[[slug]]` body links, generated `_index.md` per topic folder (capped ≤200 lines), `.sha256.json` claim-evidence sidecars, deterministic zero-LLM staleness diffing, strict POSIX forward-slash relative paths, and LF-normalized SHA-256 hashes (cross-OS Windows/Linux identical).
2. **`RetrievalPort`** — Implemented by two real adapters:
   - `LexicalManifestRetriever`: In-RAM metadata/alias manifest filter (<1 ms) + lexical body search using `rg` if present on PATH, with automatic deterministic fallback to SQLite FTS5 + Python `re` search on environments without `rg` (such as Windows sandboxes).
   - `FacetGraphRetriever`: Plane 3 orthogonal facet slicing (`domain_nature`, `modality`, `epistemic_temporal`, `periodicity`, `ingestion_channel`) + bounded-hop (1–3 hop) bidirectional traversal across Entities, Claims, Positions, Coverages, Lineage, and Plane 4 SKOS Topics + optional pluggable embedding hook (`EmbeddingProvider | None = None`).
3. **`SnapshotPort`** — Async non-blocking snapshot compiler & publisher producing ACL/wall-partitioned `v{N}.sqlite.zst` + atomic `manifest.json` artifacts (local filesystem store with Windows-safe `os.replace()` retry, plus S3-compatible storage backend hook).
4. **`PolicyPort`** — Fail-closed RBAC, `wall_side` barrier, split claim/evidence redaction, surface gating, and steward write authorization (contract detailed in ADR-003).
5. **`GraphProjectionPort`** — Projects authorized entities, claims, conflicts, skill bindings, and SKOS topics into deterministic UI graph payloads by lens (`topic | app | skill | project | issuer | fund_hierarchy`) with pre-filtered counts (contract detailed in ADR-005).

---

## 2. Exact Python `Protocol` Contracts (`packages/core/context_core/ports.py`)

Wave 1 Subagents 1, 2, and 4 MUST implement and consume these exact signatures and DTOs (Pydantic v2 `BaseModel`s in `context_core.models`):

```python
from datetime import datetime
from typing import Protocol, Sequence
from context_core.models import (
    Claim, ClaimCreateInput, ClaimUpdateInput, ClaimWriteResult, ConflictResolutionInput,
    ConflictSet, ContextAssemblyReceipt, ContextBundle, ContextBundleQuery,
    CuratorDraftPatch, Entity, EntityIdentifier, EntityLineageEvent, GraphLens,
    GraphPayload, ImpactGraphPayload, KnowledgeItem, MemoryRecord, MemoryUpsertInput,
    PolicyEpochState, Position, Principal, Coverage, SearchFilter, SearchHit,
    SkillBinding, SnapshotManifest, StalenessReport, Topic, TopicAttachment,
    TopicPatchInput, UploadIngestResult, WatchlistRecord, WatchlistUpdateInput,
)

class PolicyPort(Protocol):
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
    ) -> None: ...  # Raises PermissionError / AuthorizationError (HTTP 403)
    def sanitize_claim_for_principal(self, principal: Principal, claim: Claim) -> Claim | None: ...
    def allowed_snapshot_partitions(self, principal: Principal) -> list[tuple[str, str]]: ...
    def is_memory_allowed(self, principal: Principal) -> bool: ...  # False if channel_mode=="shared" or actor_mode=="service" or revoked

class StorePort(Protocol):
    async def initialize(self) -> None: ...
    async def close(self) -> None: ...
    async def get_epochs(self) -> PolicyEpochState: ...
    async def bump_policy_epoch(self, reason: str, actor: str) -> PolicyEpochState: ...
    async def is_principal_revoked(self, user_id: str) -> bool: ...
    async def revoke_principal(
        self, user_id: str, *, reason: str, actor: str
    ) -> PolicyEpochState: ...

    # Entities, Crosswalks, Lineage & Reified Associations
    async def upsert_entity(self, entity: Entity) -> Entity: ...
    async def get_entity(
        self, entity_id_or_key: str, *, principal: Principal
    ) -> Entity | None: ...
    async def list_entities(
        self,
        *,
        principal: Principal,
        entity_types: Sequence[str] | None = None,
        topic_slug: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Entity]: ...
    async def upsert_identifier(self, ident: EntityIdentifier) -> EntityIdentifier: ...
    async def list_identifiers(
        self, entity_id: str, *, as_of: datetime | None = None, recorded_as_of: datetime | None = None
    ) -> list[EntityIdentifier]: ...
    async def add_lineage_event(self, event: EntityLineageEvent) -> EntityLineageEvent: ...
    async def list_lineage_events(
        self, entity_id: str, *, principal: Principal
    ) -> list[EntityLineageEvent]: ...
    async def upsert_position(self, pos: Position) -> Position: ...
    async def list_positions(
        self, *, principal: Principal, vehicle_id: str | None = None, issuer_id: str | None = None
    ) -> list[Position]: ...
    async def upsert_coverage(self, cov: Coverage) -> Coverage: ...
    async def list_coverages(
        self, *, principal: Principal, issuer_id: str | None = None, status: str | None = None
    ) -> list[Coverage]: ...
    async def upsert_skill_binding(self, binding: SkillBinding) -> SkillBinding: ...
    async def list_skill_bindings(
        self, *, skill_entity_id: str | None = None, target_entity_id: str | None = None
    ) -> list[SkillBinding]: ...

    # Bitemporal Claims & N-Way Conflicts
    # Note: ClaimCreateInput, ClaimUpdateInput, and ConflictResolutionInput all accept
    # `expected_data_epoch: int | None = None` for TOCTOU optimistic concurrency (raises OptimisticConcurrencyError -> HTTP 409).
    async def write_claim(
        self, payload: ClaimCreateInput, *, principal: Principal
    ) -> ClaimWriteResult: ...
    async def update_claim(
        self, claim_id: str, patch: ClaimUpdateInput, *, principal: Principal
    ) -> ClaimWriteResult: ...
    async def get_claim(
        self, claim_id: str, *, principal: Principal
    ) -> Claim | None: ...
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
        self, *, principal: Principal, status: str = "open", subject_id: str | None = None
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
        self, *, principal: Principal, topic_id: str | None = None, target_id: str | None = None
    ) -> list[TopicAttachment]: ...
    async def upsert_knowledge_item(self, item: KnowledgeItem) -> KnowledgeItem: ...
    async def list_knowledge_items(
        self, *, principal: Principal, kind: str | None = None
    ) -> list[KnowledgeItem]: ...
    async def list_curator_patches(
        self, *, principal: Principal, status: str = "pending_steward_review"
    ) -> list[CuratorDraftPatch]: ...
    async def save_curator_patch(self, patch: CuratorDraftPatch) -> CuratorDraftPatch: ...
    async def upsert_memory(
        self, record: MemoryUpsertInput, *, principal: Principal
    ) -> MemoryRecord: ...
    async def get_memory(
        self, memory_id: str, *, principal: Principal
    ) -> MemoryRecord | None: ...
    async def list_memories(
        self, *, principal: Principal, skill_id: str | None = None
    ) -> list[MemoryRecord]: ...
    async def delete_memory(self, memory_id: str, *, principal: Principal) -> bool: ...
    async def get_watchlist(self, *, principal: Principal) -> WatchlistRecord: ...
    async def put_watchlist(self, watchlist: WatchlistUpdateInput, *, principal: Principal) -> WatchlistRecord: ...

    # Receipts & Audit
    async def record_receipt(self, receipt: ContextAssemblyReceipt) -> None: ...
    async def get_receipt(
        self, receipt_id: str, *, principal: Principal
    ) -> ContextAssemblyReceipt | None: ...

class RetrievalPort(Protocol):
    async def filter_items(
        self, flt: SearchFilter, *, principal: Principal
    ) -> list[SearchHit]: ...
    async def search(
        self, query: str, *, flt: SearchFilter | None = None, principal: Principal, limit: int = 25
    ) -> list[SearchHit]: ...
    async def neighbours(
        self,
        entity_id_or_key: str,
        *,
        principal: Principal,
        depth: int = 1,
        direction: str = "both",  # "in" | "out" | "both"
        predicates: Sequence[str] | None = None,
        as_of: datetime | None = None,
        recorded_as_of: datetime | None = None,
    ) -> GraphPayload: ...
    async def resolve_context(
        self, query: ContextBundleQuery, *, principal: Principal
    ) -> ContextBundle: ...

class SnapshotPort(Protocol):
    def schedule_compile(self, acl_tier: str, wall_side: str) -> None: ...
    async def compile_and_publish(self, acl_tier: str, wall_side: str) -> SnapshotManifest: ...
    async def get_latest_manifest(self, acl_tier: str, wall_side: str) -> SnapshotManifest | None: ...
    async def get_snapshot_bytes(self, acl_tier: str, wall_side: str, version_id: int) -> bytes: ...

class GraphProjectionPort(Protocol):
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
```

### Runtime Adapter Swappability (`AdapterRegistry`)
A lightweight `AdapterRegistry` in `context_api` holds the active implementations behind `StorePort`, `RetrievalPort`, and `LLMProvider` and exposes **`GET /api/v1/config/adapters`** and **`PUT /api/v1/config/adapters`**:
- `store`: `"sqlite_bitemporal"` | `"okf_markdown_vault"`
- `retrieval`: `"lexical_manifest"` | `"facet_graph"`
- `llm_mode`: `"auto"` | `"gemini"` | `"deterministic"`
Switching `store` synchronizes or switches seamlessly over the shared seeded dataset without restarting the FastAPI process, proving true hexagonal port isolation at runtime.

### Reversible Impact Traversal (`impact_traversal`)
Given any authorized starting `node_id` (Data Source, Issuer, Topic, Claim/Mapping, Legal Vehicle, or Skill), `GraphProjectionPort.impact_traversal(...)` performs an **ACL- and `wall_side`-enforced reverse BFS** across reified links in layered order:
`Source / Topic / Issuer / Mapping -> Positions -> Legal Vehicles / Funds -> Commitments & Opportunities (Clients) -> Skills & Apps -> Caller's Personal Watchlist` (personal watchlist included ONLY when `channel_mode == "direct"` and `actor_mode == "obo"`).
Unauthorized intermediate or leaf nodes are pruned BEFORE enqueueing into the BFS frontier, guaranteeing zero indirect existence leakage.

---

## 3. OKF v0.2 Markdown Vault Specification (`OkfMarkdownVaultStore`)

- **Path Normalization:** All vault-relative paths use POSIX forward slashes (`funds/veh-alpha-credit-offshore-lp.md`) regardless of Windows `os.sep`.
- **LF SHA-256 Determinism:** Before hashing any text artifact or OKF Markdown note, normalize `\r\n` -> `\n` (`raw.replace(b"\r\n", b"\n")`), matching `.gitattributes` `eol=lf`.
- **Frontmatter Schema (OKF v0.2):**
  ```yaml
  okf_version: "0.2"
  id: "veh:alpha-credit-offshore-lp"
  entity_type: legal_vehicle
  display_name: "Alpha Credit Offshore LP (Cayman)"
  aliases: ["Alpha Credit III Offshore", "ACOF LP"]
  wall_side: public_side
  read_acl_groups: ["firm-all"]
  write_acl_groups: ["fund-stewards"]
  topics: ["operational-admin", "cashflows-liquidity"]
  valid_from: "2024-07-01T00:00:00Z"
  recorded_at: "2024-07-01T00:00:00Z"
  content_sha256: "<hex>"
  ```
- **Generated `_index.md`:** Automatically generated per topic directory (sorted deterministically, ≤200 lines; splits into sub-topic directories if >200 notes). Never leaks unauthorized notes when materialized per `(acl_tier, wall_side)` view.
- **Claim-Evidence Sidecar (`*.evidence.sha256.json`):** Stores `{claim_id, evidence_path, expected_sha256, verified_at}` enabling `diff_staleness() -> StalenessReport` with **zero LLM calls** (pure SHA-256 comparison against source files).

---

## 4. Snapshot Compiler & `context_sdk` Concurrency / Safety Fixes

### Fixing PDF Defect #3 (Single-Worker Cache Drift)
- A `system_epochs` table holds monotonic integers `data_epoch` (incremented on any entity/claim/conflict/memory commit) and `policy_epoch` (incremented on any ACL/group/revocation change).
- Every server-side cache key embeds `(data_epoch, policy_epoch, principal.user_id, frozenset(principal.groups), principal.wall_side, ...)`.
- Before returning a cached bundle, the API executes a lightweight epoch check (`SELECT data_epoch, policy_epoch FROM system_epochs` or 250ms-TTL epoch probe; instant bypass on revocation header/SSE signal). Multi-worker Uvicorn processes NEVER drift across commits or ACL revocations.

### Fixing PDF Defect #4 (Blocking Sync S3 / Compression on Event Loop)
- `SnapshotCompiler.compile_and_publish(acl_tier, wall_side)` reads eligible claims asynchronously via SQLAlchemy, then offloads ALL CPU-bound SQLite build (`conn.serialize()`), Zstd compression (`compression.zstd.compress(..., level=3)`), SHA-256 hashing, and filesystem/S3 publishing to `asyncio.to_thread(...)`.
- Local atomic manifest publish writes to `manifest.json.tmp.<pid>.<tid>` and calls `os.replace()` wrapped in a short Windows retry loop (up to 5 retries with 15ms–80ms exponential backoff for transient `PermissionError` / sharing violations on NTFS).

### Fixing PDF Defect #5 (Unsafe `old_conn.close()`)
- `context_sdk` wraps every deserialized in-RAM SQLite connection in a reference-counted `SnapshotHandle`:
  ```python
  class SnapshotHandle:
      version_id: int
      etag: str
      policy_epoch: int
      conn: sqlite3.Connection  # opened with check_same_thread=False, query_only=ON
      _lock: threading.Lock     # protects refcount + retired flag ONLY (never held across SQL)
      _refcount: int
      _retired: bool
  ```
- Actually, because Python's `sqlite3.Connection` is safest when a single connection is not simultaneously executing statements across OS threads, each `SnapshotHandle` stores the immutable `raw_sqlite: bytes` buffer + a thread-local `sqlite3.Connection` cache (deserialized lazily per worker thread with `PRAGMA query_only = ON`) OR a small checkout pool of deserialized connections, governed by `acquire()` / `release()` refcounting!
- On swap: the active handle pointer swaps atomically under a 1-microsecond lock, marking the previous handle `_retired = True`. Any in-flight reader finishes cleanly on the old handle; `close()` fires deterministically only when `_retired and _refcount == 0`. Zero `ProgrammingError: Cannot operate on a closed database`, zero reader serialization bottleneck!

### Fixing PDF Defect #6 (Poller Clobbering `pinned_version`)
- `ContextSDK` enforces strict separation between live and pinned modes:
  - `ContextSDK(..., pinned_version: int | None = None)`: when `pinned_version is not None`, the background poller thread is **never started**, and `refresh_snapshot()` is a no-op unless explicitly forced on a new version.
  - `sdk.pinned(version_id: int) -> PinnedSnapshotView`: loads `v{version_id}.sqlite.zst` into an independent immutable `SnapshotHandle` with no poller attached, guaranteeing a live runtime and a concurrent Q4 2025 historical replay never interfere.

### Snapshot Partitioning & Schema Inside `v{N}.sqlite.zst`
- Snapshots are strictly partitioned by `(acl_tier, wall_side)` — e.g., `firm-all__public_side/v{N}.sqlite.zst` and `private-credit-deal-team__private_side/v{N}.sqlite.zst`.
- Every compiled snapshot includes `entities`, `entity_identifiers`, `claims` (retaining `recorded_at` AND `superseded_at` for bitemporal replay, NEVER including restricted `evidence_uri` or `excerpt` for unauthorized tiers, NEVER including private user memories or unauthorized wall sides), `skill_bindings`, and `snapshot_meta(version_id, acl_tier, wall_side, policy_epoch, compiled_at, sha256)`.

---

## 5. Verified Python 3.15 Environment & Wheel Compatibility Policy

Verified on `Python 3.15.1` (`sqlite3 3.53.4` with `serialize()` / `deserialize()`, stdlib `compression.zstd`) inside `.venv`. The entire core serving, API, SDK, MCP, and Google GenAI stack installs strictly from prebuilt `--only-binary=:all:` wheels (zero compilation). For the optional/agent LangChain stack (`langchain`, `deepagents`, `langgraph`, `langchain-google-genai`), every direct package ships pure-Python or `cp315` binary wheels except transitive `zstandard 0.25.0` (pulled by `langsmith 0.16.0`), which compiled cleanly via host VS Build Tools while our own snapshot/SDK layer continues using stdlib `compression.zstd` directly:

| Package | Verified Version | Note |
|---|---|---|
| `fastapi` / `starlette` / `uvicorn` | `0.165.0` / `1.10.0` / `0.54.0` | Pure Python wheels |
| `sqlalchemy` / `aiosqlite` / `greenlet` | `2.1.9` / `0.22.1` / `3.5.6` | Native `cp315-win_amd64` + pure Python |
| `pydantic` / `pydantic-core` | `2.14.4` / `2.50.5` | Native `cp315-win_amd64` |
| `pyjwt` / `cryptography` | `2.15.1` / `50.0.2` | `cp315-abi3` wheel |
| `pyyaml` / `pypdf` / `python-multipart` | `6.0.4` / `7.1.1` / `0.0.34` | `cp315` + pure Python |
| `httpx` / `sse-starlette` / `cachetools` | `0.28.1` / `3.7.1` / `8.0.2` | Pure Python |
| `mcp` / `fastmcp` | `2.3.0` / `4.2.4` | Pure Python + binary wheels |
| `langchain` / `deepagents` / `langgraph` | `1.5.2` / `0.7.41` / `1.2.24` | Verified installed & importable on Python 3.15.1 |
| `langchain-google-genai` / `google-genai` / `langsmith` | `4.5.2` / `3.6.1` / `0.16.0` | Verified installed (`langsmith` transitively pulled `zstandard 0.25.0`, which compiled cleanly via host VS Build Tools; our own snapshot/SDK layer STILL uses stdlib `compression.zstd` so zero `zstandard` dependency is required in core/SDK!) |
| `pytest` / `pytest-asyncio` | `9.2.0` / `1.5.0` | Pure Python |
| **Compression (`zstd`)** | **Python 3.15 stdlib `compression.zstd`** | **Mandatory for `context_adapters` & `context_sdk`.** Wrap behind `context_core.compression` falling back to `zstandard` only on Python <3.14. |
| **Lexical search (`rg`)** | **Optional binary with FTS5/regex fallback** | `rg` is not installed on Windows PATH; `LexicalManifestRetriever` checks `shutil.which("rg")` and falls back seamlessly to SQLite FTS5 + Python `re`. |
