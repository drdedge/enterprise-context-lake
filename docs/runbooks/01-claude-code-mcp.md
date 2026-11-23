# Runbook 01: Claude Code & Desktop MCP Integration

Connect Claude Code (or any Streamable HTTP / stdio MCP client) to Enterprise Context Lake's **Standard Primitives** (`/mcp`) and **Server-Side Code Mode** (`/mcp/codemode`) surfaces.

---

## 1. Local Server Startup

Start the unified FastAPI + FastMCP server locally (binds `127.0.0.1:8000`, mounts `/mcp` and `/mcp/codemode`):

```powershell
.\.venv\Scripts\python.exe -m uvicorn context_api.main:app --host 127.0.0.1 --port 8000
```

Verify discovery endpoints before connecting a client:

```powershell
curl.exe -s http://127.0.0.1:8000/healthz
curl.exe -s http://127.0.0.1:8000/agents
curl.exe -s http://127.0.0.1:8000/llms.txt
```

---

## 2. Project `.mcp.json` (Streamable HTTP — Recommended)

Place `.mcp.json` at the workspace root to register **both** MCP surfaces under the caller's identity headers (`Authorization: Bearer <entra_jwt>` in production; `X-Mock-User` in local dev):

```json
{
  "mcpServers": {
    "context-lake": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {
        "X-Mock-User": "user:public-analyst",
        "X-Mock-Actor-Mode": "obo",
        "X-Mock-Channel-Mode": "direct"
      }
    },
    "context-lake-codemode": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp/codemode",
      "headers": {
        "X-Mock-User": "user:public-analyst",
        "X-Mock-Actor-Mode": "obo",
        "X-Mock-Channel-Mode": "direct"
      }
    }
  }
}
```

### Surface-Aware Channel Gating

- Set `"X-Mock-Channel-Mode": "shared"` for group chats / Teams / shared agent sessions.
- The server automatically omits `memory_upsert` from `tools/list`, blocks direct calls with `403 surface_policy_blocked`, hides `user` and `user_x_skill` claims, and returns `{ "memory_enabled": false, "notes": [], "watchlist": null, "alerts": [] }` from `context://memory/me`.

---

## 3. Stdio Transport (Single-User Desktop Process)

For isolated stdio execution without running Uvicorn:

```json
{
  "mcpServers": {
    "context-lake-stdio": {
      "command": "C:\\Users\\david\\Documents\\Python\\enterprise-context-lake\\.venv\\Scripts\\python.exe",
      "args": ["-m", "context_mcp", "--mode", "primitive", "--user", "user:pm-credit"]
    }
  }
}
```

Pass `--mode codemode` to run the Server-Side Code Mode surface over stdio.

---

## 4. Recommended Claude Code Hooks

### Pre-Session Context Hydration (`SessionStart`)

Read `context://memory/me` (direct sessions only) and `GET /agents` so the agent discovers active `SKILL.md` procedures and `metadata.include_tools` without pasting static prompt boilerplate.

### Post-Edit Knowledge Gap Capture (`PostToolUse` / Manual Workflow)

When an agent encounters a stale mapping, unmapped alias, or contradictory feed value:

1. Call `flag_knowledge_gap(subject_key_or_topic, kind="gap"|"stale"|"wrong"|"observation", description, severity)`.
2. Or call `propose_update(...)` to queue a quarantined `unverified_observation`.
3. Never attempt to mutate live `approved_fact` claims from an analyst/agent session — stewards review quarantined entries via `GET /api/v1/inbox` or `POST /api/v1/curator/run`.

---

## 5. Choosing Between `/mcp` and `/mcp/codemode`

| Query Pattern | Preferred Surface | Why |
|---|---|---|
| Single entity card, exact `lookup`, or quick `Ctrl+K`-style search | `/mcp` (9 typed primitives) | Zero script generation overhead; strict Pydantic input examples |
| Multi-hop joins, bitemporal diffs (`as_of` / `recorded_as_of`), reverse impact aggregation across 5+ entities | `/mcp/codemode` (`search_sdk` + `execute_code`) | Collapses 4–10 tool round-trips into 1 server-side Node `--permission` execution with one combined `ContextAssemblyReceipt` |
