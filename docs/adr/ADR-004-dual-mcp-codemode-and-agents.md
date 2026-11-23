# ADR-004: Dual FastMCP Surface, Server-Side Code Mode & DeepAgents Harness

- **Status:** Accepted (Wave 0 Authoritative Contract)
- **Scope:** `packages/mcp/context_mcp/`, `packages/agents/context_agents/`
- **Verified Stack on Python 3.15.1:** `fastmcp 4.2.4`, `mcp 2.3.0`, `langchain 1.5.2`, `deepagents 0.7.41`, `langgraph 1.2.24`, `langchain-google-genai 4.5.2`, `langsmith 0.16.0`, `Node.js v24.22.1` (`node --permission`)

---

## 1. Dual FastMCP Surface & Discovery Endpoints (`packages/mcp/context_mcp/`)

Enterprise Context Lake uses **`FastMCP` (`fastmcp 4.2.4`)** to expose **two complementary MCP surfaces** over Streamable HTTP (**`/mcp`** for primitive tools + resources and **`/mcp/codemode`** for Server-Side Code Mode) as well as stdio transport (`python -m context_mcp.server`), plus machine-readable discovery endpoints **`GET /llms.txt`** and **`GET /agents`** (explicitly omitting `/feed.json` and any Arize Phoenix exporters). Every invocation binds strictly to the caller's authenticated `Principal` (`actor_mode: "obo" | "service"`, `channel_mode: "direct" | "shared"`, `wall_side`, `PolicyPort`, and `ContextAssemblyReceipt`).

### Surface A (`/mcp`): Compact Typed Primitive Tools (9 Tools) + Resource `context://memory/me`

Every FastMCP tool declares strict Pydantic input/output schemas, `input_examples` in tool metadata, and returns `structuredContent` alongside a concise text summary carrying `receipt_id`, `snapshot_version`, `data_epoch`, and `policy_epoch`.

| Tool | Purpose | Key Parameters | Write / Gate Rules |
|---|---|---|---|
| `filter` | Metadata & facet slice returning lightweight `{id, canonical_key, display_name, entity_type, summary}` | `entity_types?`, `topic_slug?`, `facets?`, `scope_types?`, `epistemic_statuses?`, `as_of?`, `recorded_as_of?`, `limit=50` | Read-only, ACL + wall pre-filtered |
| `search` | Lexical + alias + facet search across authorized entities, claims, and knowledge items | `query`, `entity_types?`, `topic_slug?`, `facets?`, `as_of?`, `recorded_as_of?`, `limit=25` | Read-only, zero existence leakage |
| `read` | Fetch full authorized entity/knowledge card, active claims (with evidence redaction), identifiers, lineage, topics | `id_or_key`, `include_claims=True`, `include_identifiers=True`, `include_lineage=True`, `include_topics=True`, `as_of?`, `recorded_as_of?` | Read-only; unauthorized returns `not_found` |
| `neighbours` | Bounded-hop (1–3) bidirectional/directional traversal (including reverse impact path) | `id_or_key`, `depth=1`, `direction="both"\|"in"\|"out"`, `predicates?`, `as_of?`, `recorded_as_of?` | Read-only; omits unauthorized neighbors |
| `lookup` | Deterministic exact mapping lookup (skill hot-path equivalent) | `canonical_key`, `predicate`, `context_purpose="general"`, `as_of?`, `recorded_as_of?`, `snapshot_version?` | Read-only; approved facts + conflict flag |
| `resolve_context` | Two-channel stratified context bundle (`facts`, `directives`, `conflicts`, `receipt`) | `entity_keys`, `skill_id?`, `project_id?`, `run_id?`, `include_unverified=False`, `as_of?`, `recorded_as_of?` | Read-only; skips personal memory when `channel_mode=="shared"` or `actor_mode=="service"` |
| `flag_knowledge_gap` | Record missing mapping, stale source, wrong value, or coverage blind spot | `subject_key_or_topic`, `kind: "gap"\|"stale"\|"wrong"\|"observation"`, `description`, `severity="low"\|"medium"\|"high"`, `suggested_source?` | Appends quarantined `unverified_observation` |
| `propose_update` | Propose a claim addition, staleness fix, or correction for steward review | `subject_canonical_key`, `kind: "gap"\|"stale"\|"wrong"\|"observation"`, `predicate`, `object_value`, `context_purpose="general"`, `rationale`, `evidence_uri?`, `valid_from?`, `valid_to?` | **Quarantined:** always `unverified_observation`; NEVER overwrites `approved_fact` |
| `memory_upsert` | Write/update caller's personal (`user` or `user_x_skill`) memory | `scope_type="user"\|"user_x_skill"`, `skill_id?`, `key`, `content`, `tags?` | **Surface-gated:** hidden from `tools/list` and returns `403` when `channel_mode=="shared"` or `actor_mode=="service"` |

**FastMCP Resource — `context://memory/me`:**
- Returns caller's personal notes, personal watchlist (`entity_ids`, `topic_ids`, `source_ids`), and active ACL-filtered watchlist change/conflict alerts when `actor_mode == "obo"` and `channel_mode == "direct"`.
- Returns `{memory_enabled: false, notes: [], watchlist: null, alerts: []}` when `channel_mode == "shared"` or `actor_mode == "service"`, and `401` if revoked.

---

### Surface B (`/mcp/codemode`): Server-Side Code Mode MCP (`search_sdk`, `execute_code`)

#### Why Server-Side Code Mode (Not Client-Side Claude Code Mode)
1. **Enterprise Gateway & Cloud Parity:** Client-side Programmatic Tool Calling / container Code Mode is blocked or unavailable across enterprise AI gateways, AWS Bedrock, Google Vertex AI, Azure Foundry, Zero-Data-Retention (ZDR) endpoints, and custom OpenTelemetry inspection pipelines.
2. **DLP & Static Argument Enforcement:** Client-side provider-hosted sandboxes bypass enterprise egress firewalls and static tool-argument inspection classifiers, and cannot safely enforce Entra OBO `>200`-group overage resolution or MNPI `wall_side` walls inside external containers.
3. **Composable Multi-Step Efficiency:** Server-side Code Mode delivers programmatic joins, bitemporal diffs, and impact aggregations in one round-trip while running 100% inside our perimeter under caller-bound `PolicyPort` enforcement.

#### Code Mode Tools & Security Hardening
1. **`search_sdk(query: str) -> dict`:**
   - Searches typed TypeScript/JS declarations and examples for the frozen sandbox `lake` bridge (`lake.filter`, `lake.search`, `lake.read`, `lake.neighbours`, `lake.impact`, `lake.lookup`, `lake.resolveContext`, `lake.flagKnowledgeGap`, `lake.proposeUpdate`, and conditionally `lake.memoryUpsert` only when `actor_mode=="obo"` and `channel_mode=="direct"`).
2. **`execute_code(code: str, timeout_ms: int = 3000) -> dict`:**
   - **OS-Level Sandbox (Verified on Windows Node.js v24.22.1):**
     - Spawns `node --permission --max-old-space-size=64` with ZERO `--allow-fs-read`, ZERO `--allow-fs-write`, ZERO `--allow-child-process`, ZERO `--allow-worker`, ZERO `--allow-addons` (verified to throw `ERR_ACCESS_DENIED` on `fs`, `child_process`, and `worker_threads`).
   - **Defense-in-Depth VM Context:** User code executes inside `vm.runInNewContext` exposing ONLY frozen `lake` RPC proxy, bounded `console.log`, and pure JS builtins — NO `require`, NO `import`, NO `process`, NO `fetch`, NO `http`/`https`/`net`/`dns`/`tls`.
   - **Zero Credentials in Isolate:** Subprocess env stripped to `{"NODE_NO_WARNINGS": "1"}` (never inheriting `GEMINI_API_KEY`, `LANGSMITH_API_KEY`, Entra secrets, or tokens).
   - **Anti-SSRF Caller-Bound Host RPC Bridge (stdin/stdout NDJSON):**
     - Host RPC dispatcher accepts strictly a frozen allowlist of internal lake primitives (never arbitrary URL fetching or raw HTTP proxying).
     - Any URI parameter (`evidence_uri`, `suggested_source`) is validated against SSRF patterns (rejecting loopback `127.0.0.0/8`, `::1`, `localhost`, link-local/cloud metadata `169.254.169.254`, `169.254.0.0/16`, `file://`, `gopher://`, RFC1918 private IPs) and passed through **query-string secret stripping** (`token`, `key`, `apikey`, `sig`, `signature`, `secret`, `password`, `auth`, `X-Amz-*`, `sv`, `se`, `sp`).
     - Every RPC executes strictly under the caller's `Principal` (`PolicyPort`, `wall_side`, `actor_mode`, `channel_mode`) and merges touched `claim_ids` & `redacted_evidence_claim_ids` into one `ContextAssemblyReceipt`.
   - **Hard Resource & Output Caps:**
     - `timeout_ms`: default `3000`, hard cap `10000` (clean `proc.kill()` on expiry).
     - `max_rpc_calls`: `25` per invocation.
     - **`>64 KiB` Output Truncation:** Serialized stdout/return payload exceeding `65536` bytes is deterministically UTF-8 truncated with `truncated: true`, `original_bytes: N`, `max_bytes: 65536`.
     - **TOCTOU Optimistic Concurrency:** State-changing steward approvals verify `expected_data_epoch` and return `409 Conflict` on mismatch.

---

## 2. DeepAgents & LangGraph Harness (`packages/agents/context_agents/`)

### `ContextLakeBackend(BackendProtocol)` & `CompositeBackend` Mount Topology

`packages/agents/context_agents/backends.py` directly subclasses `deepagents.backends.BackendProtocol` (`deepagents 0.7.41`: `ls`/`als`, `read`/`aread`, `write`/`awrite`, `edit`/`aedit`, `grep`/`agrep`, `glob`/`aglob`, `delete`/`adelete`, `download_files`/`adownload_files`, `upload_files`/`aupload_files`) and composes routes via `CompositeBackend(default=StateBackend(...), routes={...})`:

| Mount Prefix | Backend | Mode | Contents & Security Enforcement |
|---|---|---|---|
| `/knowledge/` | `ContextLakeBackend` | **Read-Only** (`write`/`edit`/`delete` return error / raise `PermissionError`) | Caller-authorized Entities, Claims (with redacted evidence stubs), Topics, and `data_access_guidance` projected as deterministic Markdown/YAML virtual files |
| `/skills/` | `ContextLakeBackend` | **Read-Only** | `SKILL.md` operational procedures (`crm-fund-mapping`, `gips-portfolio-reporting`, `cashflow-wire-reconciliation`). YAML frontmatter `metadata.include_tools: list[str]` dynamically scopes tool activation |
| `/memories/` | `ContextLakeBackend` | **Read-Write** (`/memories/{user_id}/`) | Caller's personal notes & watchlist. **Auto-disabled (empty + write blocked)** whenever `channel_mode == "shared"` or `actor_mode == "service"` |
| `/` | `StateBackend` | **Read-Write (Ephemeral)** | Run-scoped scratchpad managed by LangGraph state; discarded on completion unless explicitly submitted via `propose_update` |

### LLM Modes (`auto | gemini | deterministic`), LangChain / LangGraph & LangSmith

- **`gemini` / `auto` mode (when `GEMINI_API_KEY` set in `.env`):** Uses `langchain_google_genai.ChatGoogleGenerativeAI` (`langchain-google-genai 4.5.2`, default `gemini-2.5-flash` configurable via `GEMINI_MODEL`) wired into `deepagents.create_deep_agent` / `langgraph` workflow with `CompositeBackend` and caller-scoped tools.
- **`deterministic` mode (or `auto` fallback when `GEMINI_API_KEY` unset):** Uses a deterministic offline `BaseChatModel` / mock orchestrator that executes real `StorePort`/`RetrievalPort`/impact queries and returns grounded, citation-verified answers (`[entity:key]`, `[claim:id]`, `[conflict:id]`) with zero external API calls for offline tests, CI, and Playwright E2E verification.
- **Optional LangSmith Tracing:** Automatically active via `langsmith 0.16.0` when `LANGSMITH_API_KEY` + `LANGSMITH_TRACING=true` are present in `.env`, tagging every LangGraph/DeepAgents run with `receipt_id`, `snapshot_version`, `as_of`, `recorded_as_of`, `data_epoch`, and `policy_epoch`.
- **Python 3.15 Compatibility Note:** Verified in `.venv` (`fastmcp 4.2.4`, `langchain 1.5.2`, `deepagents 0.7.41`, `langgraph 1.2.24`, `langchain-google-genai 4.5.2`, `langsmith 0.16.0`). Note that `langsmith 0.16.0` transitively requires `zstandard>=0.23.0` (built cleanly from sdist via host VS Build Tools into `zstandard-0.25.0-cp315-cp315-win_amd64.whl`), while our own snapshot/SDK layer continues using Python 3.15 stdlib `compression.zstd` directly.

### Background Curator (`POST /api/v1/curator/run`)
- **Stage 1 (Zero-LLM SHA Staleness Pre-Pass):** Compares LF-normalized / binary `content_sha256` against referenced source files and OKF `.sha256.json` sidecars; flags stale claims, open conflicts, and recurring `unverified_observation` clusters **strictly partitioned by `wall_side`**.
- **Stage 2 (Reviewable Draft Patch Synthesis):** Produces unified diff `CuratorDraftPatch` items (`status="pending_steward_review"`) — NEVER auto-mutates `approved_fact` or crosses `wall_side` barriers.
