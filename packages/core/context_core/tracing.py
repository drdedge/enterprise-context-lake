"""Wall-aware OpenTelemetry tracing with LangSmith export (ADR-006).

Every trace is buffered until its local root span ends, then routed:

- **Internal collector** (``CONTEXT_TRACING_INTERNAL_OTLP_ENDPOINT``): receives every
  span, unfiltered. Point this only at infrastructure the firm runs itself.
- **LangSmith** (``CONTEXT_TRACING_LANGSMITH=true``): receives a trace only when it was
  explicitly marked ``public_side`` and nothing in it was marked otherwise. Unmarked,
  ``private_side``, ``above_the_wall`` and revoked-principal traces are dropped
  (fail-closed). Exported spans have prompt/response content attributes and span events
  stripped unless ``CONTEXT_TRACE_CONTENT=true``.

Traces are marked from two places: principal resolution (``mark_trace_wall_side``) and
``ContextAssemblyReceipt`` construction (``record_receipt``). Both are no-ops when
OpenTelemetry is not installed or no span is recording, so offline runs are unaffected.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from context_core.models import ContextAssemblyReceipt, Principal

try:  # OpenTelemetry is optional at import time; everything degrades to no-ops without it.
    from opentelemetry import trace as _otel_trace

    _OTEL_API = True
except ImportError:  # pragma: no cover - exercised only in minimal installs
    _otel_trace = None  # type: ignore[assignment]
    _OTEL_API = False

try:
    from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor

    _OTEL_SDK = True
except ImportError:  # pragma: no cover
    ReadableSpan = object  # type: ignore[assignment,misc]
    SpanProcessor = object  # type: ignore[assignment,misc]
    _OTEL_SDK = False


TRACER_NAME = "enterprise-context-lake"
EXPORTABLE_WALL_SIDE = "public_side"
WALL_SIDE_ATTR = "context.wall_side"
_RESTRICTED = "restricted"

# Attribute keys (or prefixes) that can carry prompt, completion or tool payload text.
# Covers OpenTelemetry GenAI semconv, OpenInference, OpenLLMetry/Traceloop and LangSmith-native keys.
CONTENT_ATTRIBUTE_PREFIXES: tuple[str, ...] = (
    "gen_ai.prompt",
    "gen_ai.completion",
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.system_instructions",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
    "input.value",
    "output.value",
    "llm.input_messages",
    "llm.output_messages",
    "llm.prompts",
    "tool.parameters",
    "traceloop.entity.input",
    "traceloop.entity.output",
    "langsmith.inputs",
    "langsmith.outputs",
    "context.content",
    "http.url",
    "url.query",
    "url.full",
    "exception.message",
    "exception.stacktrace",
)

_MAX_TRACKED_TRACES = 10_000


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _wall_value(wall_side: Any) -> str:
    return str(getattr(wall_side, "value", wall_side) or "")


def is_exportable_wall_side(wall_side: Any, *, revoked: bool = False) -> bool:
    """Only non-revoked ``public_side`` context may leave the firm in a trace."""
    return not revoked and _wall_value(wall_side) == EXPORTABLE_WALL_SIDE


def is_principal_exportable(principal: Principal | None) -> bool:
    if principal is None:
        return False
    return is_exportable_wall_side(principal.wall_side, revoked=bool(principal.revoked))


# ---------------------------------------------------------------------------
# Trace wall-side registry (sticky: once restricted, a trace never becomes exportable)
# ---------------------------------------------------------------------------


class _TraceWallRegistry:
    def __init__(self, max_traces: int = _MAX_TRACKED_TRACES) -> None:
        self._lock = threading.Lock()
        self._walls: OrderedDict[int, str] = OrderedDict()
        self._max = max_traces

    def mark(self, trace_id: int, wall_side: str) -> None:
        value = wall_side if wall_side == EXPORTABLE_WALL_SIDE else _RESTRICTED
        with self._lock:
            current = self._walls.get(trace_id)
            if current == _RESTRICTED:
                return
            self._walls[trace_id] = value
            self._walls.move_to_end(trace_id)
            while len(self._walls) > self._max:
                self._walls.popitem(last=False)

    def get(self, trace_id: int) -> str | None:
        with self._lock:
            return self._walls.get(trace_id)

    def pop(self, trace_id: int) -> str | None:
        with self._lock:
            return self._walls.pop(trace_id, None)

    def clear(self) -> None:
        with self._lock:
            self._walls.clear()


TRACE_WALLS = _TraceWallRegistry()


def _current_recording_span() -> Any | None:
    if not _OTEL_API:
        return None
    span = _otel_trace.get_current_span()
    if span is None or not span.is_recording():
        return None
    return span


def mark_trace_wall_side(wall_side: Any, *, revoked: bool = False) -> None:
    """Mark the current trace with the wall side of the context it touches."""
    span = _current_recording_span()
    if span is None:
        return
    value = _wall_value(wall_side) if not revoked else _RESTRICTED
    TRACE_WALLS.mark(span.get_span_context().trace_id, value)
    span.set_attribute(WALL_SIDE_ATTR, value or _RESTRICTED)


def mark_trace_principal(principal: Principal) -> None:
    mark_trace_wall_side(principal.wall_side, revoked=bool(principal.revoked))


def receipt_span_attributes(receipt: ContextAssemblyReceipt) -> dict[str, Any]:
    """Receipt fields as ``context.*`` attributes and mirrored ``langsmith.metadata.*`` metadata.

    Carries identifiers and counts only, never claim values, evidence or principal ids.
    """
    base: dict[str, Any] = {
        "receipt_id": receipt.receipt_id,
        "receipt_sha256": receipt.receipt_sha256,
        "data_epoch": receipt.data_epoch,
        "policy_epoch": receipt.policy_epoch,
        "as_of": receipt.as_of.isoformat(),
        "recorded_as_of": receipt.recorded_as_of.isoformat(),
        "wall_side": _wall_value(receipt.wall_side),
        "actor_mode": receipt.actor_mode,
        "channel_mode": receipt.channel_mode,
        "tool_or_endpoint": receipt.tool_or_endpoint,
        "claim_count": len(receipt.claim_ids),
        "redacted_evidence_count": len(receipt.redacted_evidence_claim_ids),
        "conflict_set_count": len(receipt.conflict_set_ids),
        "memory_count": len(receipt.memory_ids),
    }
    if receipt.snapshot_version is not None:
        base["snapshot_version"] = receipt.snapshot_version
    attrs: dict[str, Any] = {}
    for key, value in base.items():
        attrs[f"context.{key}"] = value
        attrs[f"langsmith.metadata.context_{key}"] = value
    return attrs


def record_receipt(receipt: ContextAssemblyReceipt) -> None:
    """Attach receipt metadata to the current span and mark the trace's wall side."""
    span = _current_recording_span()
    if span is None:
        return
    span.set_attributes(receipt_span_attributes(receipt))
    mark_trace_wall_side(receipt.wall_side)


@contextmanager
def traced(
    name: str,
    *,
    principal: Principal | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Any]:
    """Start a span (no-op without OpenTelemetry) and mark its trace with the principal's wall side."""
    if not _OTEL_API:
        yield None
        return
    tracer = _otel_trace.get_tracer(TRACER_NAME)
    with tracer.start_as_current_span(name, attributes=dict(attributes or {})) as span:
        if principal is not None:
            mark_trace_principal(principal)
        yield span


# ---------------------------------------------------------------------------
# Export filter
# ---------------------------------------------------------------------------


def _is_content_key(key: str) -> bool:
    return any(key.startswith(prefix) for prefix in CONTENT_ATTRIBUTE_PREFIXES)


def scrub_span(span: Any) -> Any:
    """Copy a finished span without content-bearing attributes or events."""
    attrs = {k: v for k, v in (span.attributes or {}).items() if not _is_content_key(k)}
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=attrs,
        events=(),
        links=span.links,
        kind=span.kind,
        status=span.status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class WallSideExportFilter(SpanProcessor):  # type: ignore[misc]
    """Buffers each trace until its local root ends, then forwards it only if it is public-side.

    Fail-closed: a trace that was never marked, was marked restricted anywhere, carries a
    non-public ``context.wall_side`` attribute on any span, or overflowed the buffer is dropped.
    """

    def __init__(
        self,
        delegate: Any,
        *,
        allow_content: bool = False,
        max_buffered_traces: int = 1_000,
        max_spans_per_trace: int = 5_000,
        registry: _TraceWallRegistry | None = None,
    ) -> None:
        self._delegate = delegate
        self._allow_content = allow_content
        self._max_traces = max_buffered_traces
        self._max_spans = max_spans_per_trace
        self._registry = registry or TRACE_WALLS
        self._lock = threading.Lock()
        self._buffers: OrderedDict[int, list[Any]] = OrderedDict()
        self._overflowed: set[int] = set()
        self.dropped_traces = 0
        self.exported_traces = 0

    def on_start(self, span: Any, parent_context: Any = None) -> None:  # noqa: D401 - SDK hook
        return None

    def on_end(self, span: Any) -> None:
        trace_id = span.context.trace_id
        is_local_root = span.parent is None or bool(getattr(span.parent, "is_remote", False))
        with self._lock:
            if trace_id in self._overflowed:
                if is_local_root:
                    self._overflowed.discard(trace_id)
                    self._registry.pop(trace_id)
                return
            buf = self._buffers.get(trace_id)
            if buf is None:
                buf = []
                self._buffers[trace_id] = buf
                while len(self._buffers) > self._max_traces:
                    old_id, _ = self._buffers.popitem(last=False)
                    self._overflowed.add(old_id)
                    self.dropped_traces += 1
            if len(buf) >= self._max_spans:
                self._buffers.pop(trace_id, None)
                self._overflowed.add(trace_id)
                self.dropped_traces += 1
                return
            buf.append(span)
            if not is_local_root:
                return
            spans = self._buffers.pop(trace_id, [])
        self._flush(trace_id, spans)

    def _flush(self, trace_id: int, spans: Sequence[Any]) -> None:
        wall = self._registry.pop(trace_id)
        exportable = wall == EXPORTABLE_WALL_SIDE and all(
            (s.attributes or {}).get(WALL_SIDE_ATTR, EXPORTABLE_WALL_SIDE) == EXPORTABLE_WALL_SIDE for s in spans
        )
        if not exportable:
            self.dropped_traces += 1
            return
        self.exported_traces += 1
        for s in spans:
            self._delegate.on_end(s if self._allow_content else scrub_span(s))

    def shutdown(self) -> None:
        with self._lock:
            self._buffers.clear()
            self._overflowed.clear()
        self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._delegate.force_flush(timeout_millis)


# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------

_CONFIGURED_PROVIDER: Any | None = None
_CONFIG_LOCK = threading.Lock()


def langsmith_otlp_settings() -> tuple[str, dict[str, str]]:
    """LangSmith OTLP traces endpoint and headers derived from standard LangSmith env vars."""
    base = (os.environ.get("LANGSMITH_ENDPOINT") or "https://api.smith.langchain.com").rstrip("/")
    endpoint = os.environ.get("CONTEXT_TRACING_LANGSMITH_OTLP_ENDPOINT") or f"{base}/otel/v1/traces"
    headers = {"x-api-key": os.environ.get("LANGSMITH_API_KEY", "").strip()}
    project = os.environ.get("LANGSMITH_PROJECT", "").strip()
    if project:
        headers["Langsmith-Project"] = project
    return endpoint, headers


def configure_tracing(
    *,
    service_name: str = TRACER_NAME,
    langsmith_processor_delegate: Any | None = None,
    internal_processor: Any | None = None,
) -> Any | None:
    """Install a TracerProvider wired to the internal collector and/or the filtered LangSmith export.

    Returns the provider, or ``None`` when no destination is configured (the default: zero egress).
    ``langsmith_processor_delegate`` / ``internal_processor`` let tests inject in-memory processors.
    """
    global _CONFIGURED_PROVIDER
    if not (_OTEL_API and _OTEL_SDK):
        return None
    with _CONFIG_LOCK:
        if _CONFIGURED_PROVIDER is not None:
            return _CONFIGURED_PROVIDER

        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        internal_endpoint = os.environ.get("CONTEXT_TRACING_INTERNAL_OTLP_ENDPOINT", "").strip()
        langsmith_on = _env_flag("CONTEXT_TRACING_LANGSMITH") and bool(os.environ.get("LANGSMITH_API_KEY", "").strip())

        if internal_processor is None and internal_endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            internal_processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=internal_endpoint))
        if langsmith_processor_delegate is None and langsmith_on:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            endpoint, headers = langsmith_otlp_settings()
            langsmith_processor_delegate = BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, headers=headers))

        if internal_processor is None and langsmith_processor_delegate is None:
            return None

        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        if internal_processor is not None:
            provider.add_span_processor(internal_processor)
        if langsmith_processor_delegate is not None:
            provider.add_span_processor(
                WallSideExportFilter(
                    langsmith_processor_delegate,
                    allow_content=_env_flag("CONTEXT_TRACE_CONTENT"),
                )
            )
        _otel_trace.set_tracer_provider(provider)
        _CONFIGURED_PROVIDER = provider
        return provider


def configure_langsmith_sdk_defaults() -> None:
    """Hide inputs/outputs on LangSmith SDK (callback) traces unless content export is explicitly enabled."""
    if _env_flag("CONTEXT_TRACE_CONTENT"):
        return
    os.environ.setdefault("LANGSMITH_HIDE_INPUTS", "true")
    os.environ.setdefault("LANGSMITH_HIDE_OUTPUTS", "true")


@contextmanager
def langsmith_sdk_tracing_scope(principal: Principal | None) -> Iterator[None]:
    """Disable LangSmith SDK/callback tracing for anything that is not public-side context.

    LangChain model calls trace straight to LangSmith when ``LANGSMITH_TRACING=true``, bypassing
    OpenTelemetry, so they need their own gate.
    """
    if is_principal_exportable(principal):
        yield
        return
    try:
        from langsmith import tracing_context
    except ImportError:  # pragma: no cover
        yield
        return
    with tracing_context(enabled=False):
        yield
