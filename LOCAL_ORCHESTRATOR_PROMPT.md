# Enterprise Context Lake — Local Orchestrator & Subagents Prompt (Windows)

You are the Principal Architect & Orchestrator for `drdedge/enterprise-context-lake`, running **locally on Windows** in `C:\Users\david\Documents\Python\enterprise-context-lake`. Coordinate specialized subagents with strict directory ownership to design, build, run, and externally verify a **deep, modular Enterprise Context Lake + Personal Memory Server** for an institutional investment firm.

**Core Philosophy — Deeper, Not Bigger:**
- Every architectural decision must elevate **UX (human), DX (developer), and AX (agent)** simultaneously.
- Cut scope bloat ruthlessly. **Explicitly OUT OF SCOPE:** wiring in Arize Phoenix (only emit reproducible revision/`Context Assembly Receipt` metadata that external Phoenix/LangSmith traces can log), replicating `vault-search-benchmark.tar.gz`, building a personal blog/essay site, or adding marketing copy, taglines, helper text, or redundant chrome.
- **Crash-Proof Local Git Discipline:** Commit and `git push` at **every milestone gate** once that milestone's verification checks pass. Never accumulate hours of unpushed work. Keep caches/temp artifacts clean.

---

### Local Windows Environment Rules (Mandatory Across All Subagents)
- **Workspace root:** `C:\Users\david\Documents\Python\enterprise-context-lake` (Windows / PowerShell).
- **Zero Unix/Docker/Compiler Assumptions:** Do **not** require `bash`, `make`, WSL, Docker, or MSVC C++ build tools to develop, run, or verify locally.
  - Provide cross-platform Python entrypoints (`scripts/dev.py`, `scripts/verify.py`) with thin PowerShell wrappers (`scripts/dev.ps1`, `scripts/verify.ps1`).
  - Use `uv` if available on `PATH`, otherwise standard `python -m venv .venv` (`.venv\Scripts\python.exe`). Frontend uses `npm` in `apps/web`.
- **Paths & Deterministic Line Endings:** Always use `pathlib.Path` and normalize relative keys/slugs/URIs to POSIX forward slashes (`/`). Add `.gitattributes` enforcing LF (`eol=lf`) for source, Markdown/OKF, YAML, and JSON files so SHA-256 evidence hashes are deterministic across Windows and Linux. Quote paths containing spaces when running `git mv` in PowerShell.
- **Windows File-Lock Hygiene (`WinError 32`):**
  - `context_sdk` must load `.sqlite.zst` via in-memory `sqlite3.Connection.deserialize()` (zero temp files on disk) with ref-counted reader locks before closing superseded connections.
  - Every SQLite/SQLAlchemy fixture must call `await engine.dispose()` / `conn.close()` in teardown so temporary test files never fail cleanup on Windows.
  - Snapshot writes use immutable `v{N}.sqlite.zst` files + `os.replace()` with short retry on Windows sharing violations for `manifest.json`.
- **No Hard `ripgrep` Dependency:** `LexicalManifestRetriever` uses `rg` if present on `PATH` and falls back cleanly to SQLite FTS5 / Python regex when `rg` is absent.
- **Zero-Compiler Code Mode Sandbox:** Run Server-Side Code Mode inside a prebuilt-wheel or native runtime zero-egress isolate (e.g., `langchain-quickjs` / `wasmtime` QuickJS-Wasm if Windows wheels install cleanly, or Node 22+ `--permission` / Deno isolated subprocess communicating strictly over stdin/stdout JSON-RPC with the host RPC proxy). Never `eval()` untrusted code inside FastAPI and never require local C/Rust compilation.
- **Clean Process & Port Cleanup:** `scripts/dev.py` and Playwright `webServer` must cleanly terminate child Uvicorn/Vite process trees on exit so ports (`8000`, `5173`) are not left orphaned. Install Chromium only (`npx playwright install chromium`).
- **Keyboard Shortcuts & Offline Mode:** Support both `Ctrl+K` (Windows) and `Cmd+K` (macOS) with platform-aware keycaps. Load optional `.env` (`GEMINI_API_KEY`/`GOOGLE_API_KEY`, `LANGSMITH_API_KEY`) when present, and include a deterministic local mock LLM mode so all verification oracles run cleanly offline with zero API keys required.

---

### Existing Repo Context & Mandatory First Audit
The repo currently contains 5 reference files at the root (`Shared context and knowledge map architecture recommendation.md`, `context-architecture-brief.txt`, `AI Knowledge Hub Architecture Design.pdf`, `gemini-code-1795262782033.txt`, `vault-search-benchmark.tar.gz`). They contain **unresolved contradictions** (Git/OKF Markdown vs. SQL Server bitemporal claims; vault-per-audience vs. row-level split claim/evidence ACLs; restricted title visibility vs. zero leakage; narrowest-scope-wins vs. epistemic-tier-first precedence) and **defects in the PDF starter code** (missing steward auth on `force_supersede`, broken bitemporal `superseded_at IS NULL` on historical `as_of` reads, single-worker cache drift, blocking sync S3 calls, unsafe SQLite `old_conn.close()`, and poller clobbering `pinned_version`). Do **not** blindly merge them—resolve them explicitly in ADRs first.

---

### Execution Plan: Orchestrator + Subagent Waves

#### Wave 0 — Repo Organization, ADRs & Raw Institutional Fixture Pack (Orchestrator + 1 Architect Subagent)
1. Reorganize the repo cleanly:
   - Add `.gitattributes` (LF normalization for code/OKF/JSON/YAML) and `.gitignore` (`.venv/`, `node_modules/`, `__pycache__/`, `.pytest_cache/`, `data/local/`, `*.sqlite`, `*.zst`, `.env`).
   - Move the 5 existing reference uploads into `docs/reference/` using quoted `git mv` commands (preserving them untouched).
   - Establish the workspace layout: `packages/core`, `packages/adapters`, `packages/api`, `packages/mcp`, `packages/sdk`, `packages/agents`, `apps/web`, `fixtures/`, `scripts/`, `tests/`, `evals/`, `docs/adr/`, `docs/runbooks/`, `docs/screenshots/`.
2. Write 5 concise **ADRs** in `docs/adr/` locking down:
   - **ADR-001 (4-Plane Investment Ontology):**
     - *Plane 1 (Canonical Entities & Party/Role Separation, FIBO/GLEIF/OpenFIGI-aligned):* `Strategy → Fund Family → Reporting Group / GIPS Composite → Legal Vehicle → Share Class`; `Ultimate Parent / PE Sponsor → Issuer → Instrument` (**Issuers exist independently of Funds** — watchlist, pipeline, benchmark, passed); `Client / Consultant → Commitment (existing LP) | Opportunity (prospect)`; **dual-role party support** (same legal entity acting as Issuer + LP + Counterparty); operational entities (`App`, `Skill`, `User`, `Project`, `Data Source`); bitemporal `entity_identifiers` crosswalks + entity lineage events (`renamed_from`, `merged_into`, `spun_off_from`).
     - *Plane 2 (Reified Associations, Bitemporal Claims & N-Way Conflicts):* First-class `Position / Holding`, `Coverage`, `Skill Binding`, and **N-way `conflict_sets`** (supporting 3+ disagreeing sources without overwriting `approved_fact`). Separate `valid_from/valid_to` (business time) from `recorded_at/superseded_at` (system time). Explicit deterministic precedence separating authoritative facts from working preferences/conventions.
     - *Plane 3 (Orthogonal Information Facets):* `domain_nature` (`financial` | `non_financial`), `modality` (`quantitative` w/ unit/currency/basis | `qualitative`), `epistemic_temporal` (`actual[prelim/unaudited/audited/restated]` | `estimate` | `forecast` | `guidance` | `pro_forma`), `periodicity`/`fiscal_period`, `ingestion_channel` (`automated_feed` | `manual_upload` | `human_authored` | `agent_extracted`), and `provenance` (`evidence_uri`, `content_sha256`, `span_locator`).
     - *Plane 4 (Dynamic SKOS Topic Overlay):* Curated core themes + dynamic/emergent/mergeable topics attached polymorphically to claims, artifacts, issuers, funds, and opportunities—enabling clean bidirectional reverse navigation without folder/entity conflation.
     - *3-Part Knowledge Factory Taxonomy:* First-class support for **Domain Knowledge**, **`SKILL.md` Procedures**, and **Data Access Guidance** (how to query live systems of record rather than duplicating fast-moving data).
   - **ADR-002 (Hexagonal Ports & Modular Swappability):** Strict Python `Protocol` contracts for `StorePort`, `RetrievalPort`, `SnapshotPort`, `PolicyPort`, and `GraphProjectionPort` — proven by implementing **two real Store adapters** (OKF v0.2 Git/Markdown Vault *and* Bitemporal SQLite/SQLAlchemy) and **two real Retrieval adapters** (Lexical FTS5/ripgrep + RAM manifest *and* Structured Facet/Hybrid).
   - **ADR-003 (Security, MNPI Barriers, Split ACLs & Identity Matrix):** Decoupled claim vs. evidence ACLs (redacted provenance stubs for authorized claim readers, **zero existence leakage** for unauthorized records including pre-aggregation badge/facet counts); `wall_side` (`public_side` | `private_side` | `above_the_wall`) hard barriers across storage, retrieval, snapshots, and background curation; Entra JWT/JWKS local auth + **>200-group overage** handling (with local mock auth mode); **OBO vs. Fixed Service** identity + **surface-aware tool gating** (personal `user` memory auto-disabled in shared channels); anti-poisoning quarantine for manual uploads & observations; immutable **`Context Assembly Receipts`** (`receipt_id`, `snapshot_version`, `as_of`, `recorded_at`, `claim_ids`, `redacted_ids`, `policy_epoch`).
   - **ADR-004 (Dual MCP Surface, Server-Side Code Mode & DeepAgents Harness):** Compact typed primitive MCP tools (`structuredContent` + `input_examples` + `flag_knowledge_gap`) **plus** a **Server-Side Code Mode MCP endpoint** (`search_sdk`, `execute_code`) executing in a zero-egress sandbox with caller-bound host RPC enforcement (because client-side Claude Code Mode is blocked on enterprise gateways/Bedrock/Vertex/ZDR/OTel and bypasses static argument classifiers). DeepAgents `ContextLakeBackend(BackendProtocol)` + `CompositeBackend` + `SKILL.md` (`include_tools`) + Gemini API + LangSmith tracing + background Curator with zero-LLM SHA staleness pre-pass.
   - **ADR-005 (Minimalist Linear / Apple Notes UX & Diagnostic Graph Mechanics):** Three quiet surfaces (Home stream with inline degree counts + `Ctrl+K`/`Cmd+K` search + Table/Inspector + Diagnostic Graph View); zero taglines, zero helper text, zero permanent multi-section control panels; deterministic Barnes-Hut/compound layout, φ-asymmetric cluster springs, 300ms-debounced φ-decay hover spotlight, sticky 1-hop directional reverse/forward traversal, golden-angle orphan ring, drag-to-pin spatial memory, zoom label LOD, and foreground-selection search filtering.
3. Build `fixtures/` — a **realistic institutional fixture pack** (no 20-word random filler):
   - Multi-system extracts (Admin, CRM, OMS/PMS, GIPS Reporting) covering Fund families/vehicles/share classes, held *and* unheld watchlist/pipeline Issuers, PE-sponsored private borrowers, a dual-role Legal Entity (Issuer + LP + Counterparty), Clients/Consultants/Commitments/Opportunities, historical entity rename/LME event, and a **3-way admin-code conflict**.
   - 3 legacy embedded skill lookup tables (CRM, Reporting, Cash-Flow) + 3 frozen historical report baselines for bitemporal replay.
   - Messy manual-upload samples (restricted LP Side Letter, CSV mapping table, qualitative analyst forecast memo).
   - Principal/barrier fixtures (Product, PM, Public Analyst, Private Credit Deal Team, RP/IR, Fund Steward, Revoked User, >200-group Entra user) with unique **canary tokens** planted in MNPI, side-letter, and personal-memory records.
4. **Gate 0:** Verify schemas & fixtures load cleanly on Windows. **Commit & push.**

---

#### Wave 1 — Parallel Implementation (4 Subagents with Strict Directory Ownership)

Spawn 4 parallel subagents with zero overlapping file paths:

1. **Subagent 1 — Core Domain, Modular Adapters & Snapshot SDK (`packages/core`, `packages/adapters`, `packages/sdk`):**
   - Implement the 4-plane canonical domain model, bitemporal query engine (both `as_of` business time AND `recorded_at` system time), N-way `conflict_sets`, deterministic precedence, dynamic SKOS topics, `Context Assembly Receipts`, and Policy/Barrier engine (MNPI walls, split claim/evidence ACLs, Entra >200-group overage, surface-aware gating, steward write RBAC).
   - Implement **two Store adapters**: `SqliteBitemporalStore` (SQLAlchemy 2.x, portable to SQL Server/Postgres) and `OkfMarkdownVaultStore` (OKF v0.2 YAML frontmatter, generated `_index.md`, claim-evidence SHA sidecars, deterministic zero-LLM staleness diff, normalized forward-slash paths & LF hashes).
   - Implement **two Retrieval adapters**: `LexicalManifestRetriever` (in-memory manifest + alias matching + `rg` if available with clean FTS5/Python fallback) and `FacetGraphRetriever` (orthogonal facet slicing + bounded-hop bidirectional traversal + optional embeddings hook).
   - Implement the debounced **Zstd-compressed SQLite Snapshot Compiler** (async non-blocking, epoch/ETag fencing, ACL/wall partitioned, Windows-safe `os.replace`) and `context_sdk` (in-memory `deserialize()`, ref-counted atomic swap so `old_conn` never breaks in-flight reads, strict `pinned_version` isolation that polling never overwrites).

2. **Subagent 2 — API, Personal Memory, Manual Uploads, Dual MCP + Code Mode & DeepAgents (`packages/api`, `packages/mcp`, `packages/agents`, `docs/runbooks`):**
   - Build FastAPI service (`packages/api`) with clean REST + OpenAPI 3.1 + AX discovery endpoints (`/llms.txt`, `/agents`, `Accept: text/markdown` negotiation):
     - Context bundle & bitemporal resolution, entity/topic/facet bidirectional navigation, N-way conflict resolution + `skill_bindings` blast-radius preview, and graph projection DTOs.
     - **Personal Memory Server** (`scope: user | user_x_skill | run`): zero-friction CRUD accessible from any client (browser, CLI, Claude Code, Codex, LangGraph), strict caller isolation, surface-gating enforcement, and explicit `promote` flow (`user → project → firm`).
     - **Manual Upload Ingestion Pipeline:** upload PDF/CSV/Markdown/JSON, store immutable SHA-256 artifact, extract structured candidate claims/notes into quarantined `inbox`/`unverified_observation` with provenance span links and decoupled `evidence_acl` (never auto-overwriting `approved_fact`).
   - Build **Dual MCP Surface (`packages/mcp`)**:
     - Typed primitives (`filter`, `search`, `read`, `neighbours`, `lookup`, `resolve_context`, `flag_knowledge_gap`, `propose_update`, `memory_upsert`) with `structuredContent` and `input_examples`.
     - **Server-Side Code Mode MCP (`search_sdk`, `execute_code`)**: runs agent-written JS inside a Windows-compatible zero-egress isolate (prebuilt QuickJS-Wasm/wasmtime or Node 22+ `--permission`/Deno isolated subprocess) with strict CPU/RAM/output-token limits, zero credentials inside sandbox, and every SDK call proxied through caller-authorized host RPC.
   - Build **LangGraph / `deepagents` + Gemini + LangSmith Harness (`packages/agents`)**:
     - `ContextLakeBackend(BackendProtocol)` mounted in `CompositeBackend` (`/knowledge/` read-only, `/skills/` `SKILL.md` with `metadata.include_tools`, `/memories/` user-scoped read-write, `/` ephemeral `StateBackend`).
     - Viewport-grounded Q&A agent (returns clickable claim/evidence citations, respects redacted stubs, runs with Gemini API via `langchain-google-genai` + LangSmith tracing when `.env` keys are set, plus deterministic local mock mode for offline tests).
     - Background **Knowledge Factory Curator** running the zero-LLM staleness pre-pass + reading `flag_knowledge_gap` items to produce reviewable draft patches/PR diffs (never silently mutating live approved knowledge).
   - Write crisp operational runbooks in `docs/runbooks/`: connecting Claude Code, Codex, LangGraph/DeepAgents, and server-hosted skills via Raw MCP, Server-Side Code Mode MCP, and `context_sdk`.

3. **Subagent 3 — Minimalist Linear / Apple Notes Frontend (`apps/web`):**
   - Build React + TypeScript + Vite + Tailwind SPA served standalone in dev and mountable by FastAPI.
   - **Strict Aesthetic & UX Discipline:** Linear / Apple Notes restraint. **Zero** taglines, **zero** explanatory paragraphs, **zero** secondary micro-labels, **zero** permanently-open multi-section control panels. If a screen needs helper text to make sense, fix the layout.
   - **Surfaces:**
     - **Home:** Quiet, scannable context & personal-memory stream with crisp facet/topic filters, inline connection counts (`· 7`), quiet status/conflict dots, and persona/perspective switcher (Product / PM / Analyst / RP / Personal).
     - **Global `Ctrl+K` / `Cmd+K` Search:** Works keyboard-only across Home, Table, and Graph (entities, aliases, claims, topics, facets, personal memories).
     - **Dense Table / Inspector View:** Fast tabular inspection for PMs/analysts + split-pane right drawer showing entity/claim details, orthogonal facets, bitemporal history (`as_of` switcher), N-way conflict resolution with `skill_bindings` blast-radius preview, inline edit, and server-enforced redacted evidence stubs (`Restricted — Request Access`).
     - **Diagnostic Graph View:** Toggleable lenses (`Topic` | `Entity` | `App/Skill` | `Project` | `Personal`); deterministic settling Barnes-Hut/compound layout; φ-asymmetric cluster springs; 300ms-debounced φ-decay hover spotlight; right-click/action sticky 1-hop directional reverse/forward traversal; golden-angle orphan perimeter ring; drag-to-pin spatial memory; zoom-based label LOD; search defaults to foreground-selection dimming (with isolate toggle); all tuning tucked inside a single minimal popover.
     - **Personal Memory View & Manual Upload Drawer:** Quiet personal topic/memory manager (editable, linkable, promotable to project/firm) + clean drag-and-drop manual upload + quarantined triage queue.
     - **Docked Viewport Agent:** Grounded strictly in the current viewport's authorized subgraph/selection, returning clickable citations that focus nodes/claims.

4. **Subagent 4 — Independent Verification & Red-Team Suite (`tests/`, `evals/`, `scripts/`):**
   - Implement cross-platform local runners (`scripts/dev.py`, `scripts/dev.ps1`, `scripts/verify.py`, `scripts/verify.ps1`) with clean Windows process-tree teardown.
   - Implement all **7 automated external oracles** (zero manual eyeballing):
     1. `tests/test_bitemporal_replay.py` — 0-diff historical replay across CRM, Reporting, Cash-Flow skills + restatement + `Context Assembly Receipt` verification.
     2. `tests/test_security_redteam.py` — Adversarial canary-token & count-oracle leakage matrix across **all 9 surfaces** (REST, search/typeahead, graph neighbors, backlinks, facet/topic counts, compiled `.sqlite.zst` file inspection, Raw MCP, Code Mode sandbox, shared-channel personal-memory gating) + Entra >200-group overage + steward RBAC enforcement.
     3. `tests/test_adapter_conformance.py` — `pytest` + Hypothesis behavioral contract suite executed identically against **both** Store adapters (`SqliteBitemporalStore` & `OkfMarkdownVaultStore`) and **both** Retrieval adapters (`LexicalManifestRetriever` & `FacetGraphRetriever`).
     4. `tests/test_domain_workflows.py` — Unheld watchlist Issuer, dual-role legal party, bidirectional reverse navigation (`Topic + Facet → Issuer → Position/Fund → Client/Opportunity → Skill`), 3-way conflict lifecycle + blast radius, manual upload quarantine + SHA provenance, personal memory CRUD + promotion.
     5. `tests/test_knowledge_factory.py` — Zero-LLM SHA staleness detection + `flag_knowledge_gap` → background Curator reviewable draft loop.
     6. `evals/eval_mcp_vs_codemode.py` — 15+ multi-hop persona queries comparing Raw MCP vs. Server-Side Code Mode MCP on deterministic entity-set F1, facet accuracy, citation fidelity, token usage, round-trips, latency, and 0 sandbox escapes.
     7. `tests/e2e/test_ui_playwright.spec.ts` — Live local full-stack Playwright suite (Chromium) asserting: 0 `@axe-core` WCAG 2.2 AA violations, 0 console/network errors, 0 horizontal overflow (`1280×800`, `1440×900`), layout-budget/clutter checks (no permanent control panel, keyboard-only `Ctrl+K`, working graph spotlight/directional focus/orphan ring), network-wire check proving redacted evidence URIs never reach the browser, and saving clean screenshots of every key screen to `docs/screenshots/` (`home.png`, `search-cmdk.png`, `graph-funds.png`, `graph-reverse-topic.png`, `inspector-conflict-redacted.png`, `personal-memory.png`, `manual-upload-triage.png`).

**Gate 1:** Commit & push each subagent's milestone once its unit/contract tests pass.

---

#### Wave 2 — Live Local Integration, Visual Inspection & Final Verification (Orchestrator)
1. Boot the complete stack locally via `.\scripts\dev.ps1` (or `python scripts/dev.py`) — zero cloud credentials required; optional `.env` enables live Gemini API + LangSmith.
2. Run **all 7 verification oracles** (`.\scripts\verify.ps1`) end-to-end and visually inspect every generated screenshot in `docs/screenshots/` against the Linear / Apple Notes bar—fixing any visual clutter, helper text, cramped spacing, or failing assertion before finishing.
3. Update `README.md` with exact Windows quickstart commands, architecture diagram, adapter-swapping guide, MCP/Code Mode runbook links, embedded screenshots, and actual measured verification results (never fabricate or extrapolate numbers).
4. **Final Gate:** Clean temporary files, verify `git status` is clean, commit & push everything to GitHub, and present a concise summary linking the ADRs, oracle test results, Code Mode vs. Raw MCP metrics, and screenshots.
