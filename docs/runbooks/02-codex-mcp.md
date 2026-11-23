# Runbook 02: OpenAI Codex CLI MCP Integration

Configure OpenAI Codex CLI (`~/.codex/config.toml` or project `.codex/config.toml`) to connect to Enterprise Context Lake over Streamable HTTP or stdio.

---

## 1. Streamable HTTP Configuration (`.codex/config.toml`)

Start the local server (`uvicorn context_api.main:app --host 127.0.0.1 --port 8000`) and configure both MCP endpoints:

```toml
[mcp_servers.context_lake]
url = "http://127.0.0.1:8000/mcp"
http_headers = { "X-Mock-User" = "user:public-analyst", "X-Mock-Actor-Mode" = "obo", "X-Mock-Channel-Mode" = "direct" }

[mcp_servers.context_lake_codemode]
url = "http://127.0.0.1:8000/mcp/codemode"
http_headers = { "X-Mock-User" = "user:public-analyst", "X-Mock-Actor-Mode" = "obo", "X-Mock-Channel-Mode" = "direct" }
```

### Production Entra JWT Mode

Replace `http_headers` with an environment-sourced OBO bearer token:

```toml
[mcp_servers.context_lake]
url = "https://context-lake.internal.example/mcp"
bearer_token_env_var = "CONTEXT_LAKE_ENTRA_JWT"
```

When `ENVIRONMENT=production` or `AUTH_MODE=entra_jwt`, all `X-Mock-*` headers and `Bearer mock:*` tokens are rejected with `401 Unauthorized`.

---

## 2. Stdio Transport Configuration (`.codex/config.toml`)

Use stdio when running Codex offline without a persistent HTTP server:

```toml
[mcp_servers.context_lake_stdio]
command = "C:\\Users\\david\\Documents\\Python\\enterprise-context-lake\\.venv\\Scripts\\python.exe"
args = ["-m", "context_mcp.server", "--mode", "primitive", "--user", "user:fund-steward", "--channel-mode", "direct"]
env = { "CONTEXT_LAKE_LLM_MODE" = "deterministic" }

[mcp_servers.context_lake_codemode_stdio]
command = "C:\\Users\\david\\Documents\\Python\\enterprise-context-lake\\.venv\\Scripts\\python.exe"
args = ["-m", "context_mcp.server", "--mode", "codemode", "--user", "user:fund-steward", "--channel-mode", "direct"]
```

---

## 3. Verifying Tools, Resources & Receipts

1. Confirm primitive tools visible in direct OBO mode:
   `filter`, `search`, `read`, `neighbours`, `lookup`, `resolve_context`, `flag_knowledge_gap`, `propose_update`, `memory_upsert`.
2. Confirm resources available:
   - `context://memory/me` (personal notes, watchlist, live conflict alerts; empty in `shared` or `service` mode)
   - `context://skills/{skill_id}` (`crm-fund-mapping`, `gips-portfolio-reporting`, `cashflow-wire-reconciliation`)
3. Confirm every tool response includes `structuredContent` with `receipt_id`, `data_epoch`, and `policy_epoch`.
4. Confirm bitemporal historical replay by passing both `as_of` (business time) and `recorded_as_of` (system belief time) to `lookup`, `read`, or `resolve_context`.
