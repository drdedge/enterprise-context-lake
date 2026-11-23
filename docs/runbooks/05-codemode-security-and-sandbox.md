# Runbook 05: Server-Side Code Mode Security & Node 24 Sandbox Architecture

Enterprise Context Lake exposes Server-Side Code Mode at `/mcp/codemode` (`search_sdk` + `execute_code`), allowing agents to compose multi-hop queries, bitemporal diffs, and impact aggregations inside a zero-egress Node.js 24 isolate bound strictly to the caller's `PolicyPort`.

---

## 1. Why Server-Side Code Mode (Not Client-Side Claude Code Mode)

Provider-hosted / client-side Programmatic Tool Calling fails institutional enterprise security requirements for four concrete reasons:

1. **Enterprise AI Gateway & Cloud Incompatibility:** Client-side container execution is unavailable or blocked across enterprise API gateways, AWS Bedrock, Google Vertex AI, Azure Foundry, Zero-Data-Retention (ZDR) endpoints, and regulated OpenTelemetry inspection pipelines.
2. **Static Argument Inspection & DLP Bypass:** Packing opaque code into external provider containers bypasses internal DLP classifiers and static argument policy gates.
3. **MNPI Wall & Entra `>200`-Group Overage Enforcement:** External containers cannot resolve internal Microsoft Graph group overage or enforce `public_side` / `private_side` / `above_the_wall` barriers before bytes leave the enterprise perimeter.
4. **Zero Credential Exposure:** Running the isolate inside our own backend guarantees that no Entra JWTs, database strings, S3 keys, or LLM API keys ever enter the untrusted JavaScript runtime.

---

## 2. Layered Sandbox Architecture

Every `execute_code(code, timeout_ms=3000)` call executes through four independent containment layers:

### Layer 1: OS-Level Node.js 24 Permission Model
Python spawns a fresh, ephemeral subprocess per invocation:

```text
node --permission --max-old-space-size=64 packages/mcp/context_mcp/sandbox_runner.mjs
```

- **ZERO** `--allow-fs-read`, **ZERO** `--allow-fs-write`, **ZERO** `--allow-child-process`, **ZERO** `--allow-worker`, **ZERO** `--allow-addons`.
- Any attempt to touch the host filesystem, spawn processes, or create worker threads throws `ERR_ACCESS_DENIED`.
- **Clean Environment:** Subprocess `env` strips ALL host secrets (`GEMINI_API_KEY`, `LANGSMITH_API_KEY`, tokens, user profile paths), passing only `NODE_NO_WARNINGS=1` (plus `SystemRoot` on Windows solely for Win32 DLL initialization).

### Layer 2: Hardened `node:vm` Execution Context
- Outer runner deletes `fetch`, `WebSocket`, `XMLHttpRequest`, `EventSource`, `Request`, `Response`, and `Headers` before bootstrapping.
- User code executes inside `vm.createContext(..., { codeGeneration: { strings: false, wasm: false } })`:
  - `eval(...)`, `new Function(...)`, and `{}.constructor.constructor("return process")()` throw `EvalError`.
  - Dynamic `import()` is rejected via `importModuleDynamically`.
  - `process`, `require`, `module`, `Buffer`, and timers are shadowed as non-configurable `undefined`.
  - All RPC arguments and results cross the VM boundary strictly via `JSON.stringify` / `JSON.parse` (preventing host-realm prototype/constructor leakage).

### Layer 3: Caller-Bound Anti-SSRF Host RPC Proxy (stdin/stdout NDJSON)
- The isolate communicates ONLY via newline-delimited JSON frames over `stdin`/`stdout` calling a frozen allowlist on `lake`:
  `filter`, `search`, `read`, `neighbours`, `impact`, `lookup`, `resolveContext`, `flagKnowledgeGap`, `proposeUpdate`, and conditionally `memoryUpsert` (omitted and rejected with `403 surface_policy_blocked` on `shared` channels or `service` identities).
- **No Arbitrary URL Fetching:** There is no `http` or `fetch` RPC primitive.
- **Anti-SSRF Validation:** Any URI argument (`evidence_uri`, `suggested_source`) rejects `file://`, `gopher://`, `localhost`, loopback (`127.0.0.0/8`, `::1`), cloud metadata (`169.254.169.254`), RFC1918 private IPs, Windows drive paths, and `..` traversal.
- **Query-String Secret Stripping:** Sensitive parameters (`token`, `key`, `apikey`, `sig`, `signature`, `secret`, `password`, `auth`, `X-Amz-*`, `sv`, `se`, `sp`) are stripped before storage, logging, or response serialization.

### Layer 4: Deterministic Resource & Output Caps
- **Wall-Clock Timeout:** Default `3000 ms`, hard cap `10000 ms`; enforced inside `vm.Script` (sync loops), inside Node `Promise.race` (async hangs), AND by Python `asyncio.wait_for` with guaranteed `proc.kill()` + `await proc.wait()` in `finally` (zero orphaned Windows processes).
- **RAM Cap:** `--max-old-space-size=64` (64 MiB V8 heap ceiling).
- **RPC Call Cap:** Maximum `25` host RPC calls per execution.
- **Output Truncation:** Combined return value + logs exceeding `65536` bytes (`64 KiB`) are deterministically UTF-8 truncated with `{ truncated: true, original_bytes, max_bytes: 65536 }`.
- **Single Combined Receipt:** Every claim ID, redacted claim ID, conflict ID, and memory ID touched across all RPC calls is merged into one persisted, SHA-256-sealed `ContextAssemblyReceipt`.
