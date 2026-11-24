# ADR-006: Wall-Aware OpenTelemetry Tracing & LangSmith Export

- **Status:** Accepted (2026-11-24)
- **Supersedes:** the Arize Phoenix observability recommendation in `docs/reference/`
- **Related:** ADR-003 (MNPI barriers, receipts), ADR-004 (agents, LangSmith)

## Context

The current build is a stopgap. The target platform for tracing, evals, prompts and agent hosting is LangSmith, and the lake's Markdown content should stay portable into LangSmith Context Hub.

Hosted LangSmith controls access per workspace only. That is too coarse for material-non-public information (MNPI). The decision is that private-side and above-the-wall context never goes into LangSmith. The lake keeps its fine-grained controls.

Before this ADR, only the Gemini call in the Viewport Agent was traced, through LangChain callbacks, and prompts carried claim text from any wall side.

## Decision

1. **OpenTelemetry is the tracing layer.** It is vendor-neutral, and LangSmith ingests OTLP natively (`<LANGSMITH_ENDPOINT>/otel/v1/traces`), as do Phoenix and most collectors.
2. **Every HTTP request gets one root span** (`RequestTracingMiddleware`, covering `/api/v1/*`, `/mcp` and `/mcp/codemode`). The Viewport Agent and Curator add their own spans.
3. **Traces are marked with a wall side** in two places: principal resolution (`AuthService.resolve_principal`) and receipt construction (`build_receipt`). Marking is sticky. Once any part of a trace touches non-public context, the whole trace is restricted.
4. **Two destinations, configured independently:**
   - `CONTEXT_TRACING_INTERNAL_OTLP_ENDPOINT`: every span, unfiltered. Firm-run infrastructure only.
   - `CONTEXT_TRACING_LANGSMITH=true` with `LANGSMITH_API_KEY` (and optional `LANGSMITH_PROJECT`): spans pass through `WallSideExportFilter` first.
5. **`WallSideExportFilter` fails closed.** It buffers each trace until its local root ends, then forwards it only if the trace was explicitly marked `public_side` and no span carries another `context.wall_side`. Unmarked traces, restricted traces, revoked principals and buffer overflows are all dropped.
6. **Content is masked by default.** Exported spans lose prompt, completion, tool-payload, URL-query and exception-message attributes (GenAI semconv, OpenInference, Traceloop and LangSmith keys), and all span events. `CONTEXT_TRACE_CONTENT=true` keeps content for public-side traces.
7. **The LangSmith SDK path is gated separately.** LangChain callbacks trace straight to LangSmith when `LANGSMITH_TRACING=true`, bypassing OpenTelemetry. `langsmith_sdk_tracing_scope(principal)` disables them for any non-public principal. `LANGSMITH_HIDE_INPUTS` and `LANGSMITH_HIDE_OUTPUTS` default to `true` unless content export is enabled.
8. **Receipt metadata, not content, links a trace to what was read.** That means receipt ID, SHA-256, snapshot version, epochs, as-of times, wall side and counts. Principal IDs and claim values are never attributes.

With nothing configured, no provider is installed and nothing leaves the process. Offline mode and tests are unchanged.

## Consequences

- Private-side runs are visible only on the internal collector. LangSmith evals and dashboards cover public-side traffic only.
- A new code path that reads context without resolving a principal or building a receipt produces an unmarked trace, which is dropped rather than leaked.
- Oracle 8 (`tests/test_tracing_wall_routing.py`) runs requests as public, private-side and above-the-wall principals. It checks that no fixture canary token and no private marker reaches the LangSmith exporter.

## Not in this ADR

Context Hub export and import, eval datasets in LangSmith format, prompt-hub loading, and hosting the agent as a LangGraph deployment. These follow once the version-control release design lands.
