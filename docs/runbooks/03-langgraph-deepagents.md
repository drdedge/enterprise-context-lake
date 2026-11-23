# Runbook 03: LangGraph & DeepAgents Harness (`ContextLakeBackend`)

Wire Enterprise Context Lake into LangChain 1.5+, LangGraph 1.2+, and DeepAgents 0.7+ using the canonical `CompositeBackend` mount topology, `SKILL.md` `metadata.include_tools` tool activation, Gemini API (`langchain-google-genai`), and optional LangSmith tracing.

---

## 1. `CompositeBackend` Mount Topology

`context_agents.backends.create_context_composite_backend(...)` mounts three caller-bound `ContextLakeBackend(BackendProtocol)` instances alongside LangGraph's ephemeral `StateBackend`:

| Mount Prefix | Backend | Mode | Enforcement |
|---|---|---|---|
| `/knowledge/` | `ContextLakeBackend(namespace="knowledge")` | Read-Only | Caller-authorized entities, evidence-redacted claims, SKOS topics, domain knowledge, and data access guidance |
| `/skills/` | `ContextLakeBackend(namespace="skills")` | Read-Only | Authorized `SKILL.md` procedures with YAML `metadata.include_tools` |
| `/memories/` | `ContextLakeBackend(namespace="memories")` | Read-Write | Caller-isolated `/memories/{user_id}/` notes & watchlist; auto-disabled on `channel_mode="shared"` or `actor_mode="service"` |
| `/` | `StateBackend()` | Read-Write (Ephemeral) | Run-scoped scratchpad discarded at run completion |

```python
from context_agents import (
    ContextLakeBackend,
    create_context_composite_backend,
    load_skill_include_tools,
)

backend = create_context_composite_backend(store, policy, principal)

# Read-only firm knowledge & SKILL.md discovery
entries = await backend.als("/knowledge/entities/")
skill_doc = await backend.aread("/skills/crm-fund-mapping/SKILL.md")

# Attempting to mutate /knowledge/ or /skills/ fails closed
denied = await backend.awrite("/knowledge/entities/hack.md", "unauthorized")
assert denied.error and "permission_denied" in denied.error

# Caller-scoped personal memory (succeeds only in OBO + direct channel)
saved = await backend.awrite("/memories/alpha-watch.md", "Track Q1 GIPS audit sign-off")

# Dynamically scope tool activation from SKILL.md metadata.include_tools
allowed_tools = await load_skill_include_tools(store, principal, "crm-fund-mapping")
# -> ["lookup", "read", "resolve_context", "propose_update"]
```

---

## 2. Gemini API + Optional LangSmith + Deterministic Offline Mode

Configure `.env` (gitignored) for live Gemini reasoning and LangSmith tracing:

```env
GEMINI_API_KEY=<your-gemini-api-key>
GEMINI_MODEL=gemini-2.5-flash
LANGSMITH_API_KEY=<optional-langsmith-api-key>
LANGSMITH_TRACING=true
LANGSMITH_PROJECT=enterprise-context-lake
```

### Runtime LLM Mode Switching

Control LLM mode via `CONTEXT_LAKE_LLM_MODE` or live via `PUT /api/v1/config/adapters`:

- `auto` (default): uses `ChatGoogleGenerativeAI` when `GEMINI_API_KEY` or `GOOGLE_API_KEY` is present; seamlessly falls back to deterministic grounded synthesis when offline.
- `deterministic`: strictly offline, zero external API calls, deterministic entity/claim/conflict citations (`[entity:key]`, `[claim:id]`, `[conflict:id]`).
- `gemini`: forces Gemini synthesis when a key is configured (falling back cleanly if the call errors).

---

## 3. Viewport-Grounded Q&A & Receipt Tagging

Every call to `ViewportGroundedAgent.answer(...)` (exposed via `POST /api/v1/agent/chat` and `POST /api/v1/agent/query`):

1. Resolves only caller-authorized viewport entities (`visible_entity_ids`, `selected_entity_id`) and search hits.
2. Enforces split claim/evidence redaction (`Restricted — Request Access` with zero URI/excerpt leakage).
3. Persists an immutable `ContextAssemblyReceipt` and attaches `receipt_id`, `snapshot_version`, `as_of`, `recorded_as_of`, `data_epoch`, and `policy_epoch` as LangGraph/LangSmith run metadata.

---

## 4. Background Knowledge Factory Curator

Trigger the two-stage Curator via `POST /api/v1/curator/run` (steward RBAC required):

1. **Stage 1 (Zero-LLM SHA-256 Pre-Pass):** Compares LF-normalized / binary `content_sha256` on stored claims, knowledge items, and OKF `.sha256.json` sidecars against disk artifacts.
2. **Stage 2 (Wall-Isolated Draft Patches):** Groups stale items and quarantined `unverified_observation` / `flag_knowledge_gap` claims strictly by `wall_side` and emits reviewable unified-diff `CuratorDraftPatch` records (`status="pending_steward_review"`). Live `approved_fact` claims are never auto-mutated.
