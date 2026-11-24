"""Oracle 8: Wall-aware tracing — only public-side traces reach LangSmith, with content masked (ADR-006).

Verifies with in-memory exporters (100% offline):
  1. A public-side trace exports with receipt metadata mirrored as `langsmith.metadata.*`.
  2. Private-side, above-the-wall, unmarked and mixed traces never reach the LangSmith exporter,
     while the internal exporter still receives them.
  3. Prompt/response content attributes and span events are stripped unless explicitly allowed.
  4. End-to-end through the API and MCP: no MNPI, side-letter or personal-memory canary appears
     in anything exported to LangSmith, and private-side requests export nothing.
  5. LangSmith SDK (LangChain callback) tracing is disabled for non-public principals.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from conftest import build_principal_from_fixture, load_json_fixture, mock_headers
from context_core import tracing as ct
from context_core.models import PolicyEpochState
from context_core.receipts import build_receipt

LANGSMITH_EXPORTER = InMemorySpanExporter()
INTERNAL_EXPORTER = InMemorySpanExporter()


@pytest.fixture(scope="module", autouse=True)
def _tracing_provider() -> Any:
    provider = ct.configure_tracing(
        langsmith_processor_delegate=SimpleSpanProcessor(LANGSMITH_EXPORTER),
        internal_processor=SimpleSpanProcessor(INTERNAL_EXPORTER),
    )
    assert provider is not None
    assert trace.get_tracer_provider() is provider, "tracing must be configured before any other provider"
    yield provider


@pytest.fixture(autouse=True)
def _clear_exporters() -> None:
    LANGSMITH_EXPORTER.clear()
    INTERNAL_EXPORTER.clear()
    ct.TRACE_WALLS.clear()


def _tracer() -> Any:
    return trace.get_tracer("test")


def _exported_blob(exporter: InMemorySpanExporter) -> str:
    parts: list[str] = []
    for span in exporter.get_finished_spans():
        parts.append(span.name)
        parts.append(json.dumps(dict(span.attributes or {}), default=str))
        for ev in span.events:
            parts.append(ev.name)
            parts.append(json.dumps(dict(ev.attributes or {}), default=str))
    return "\n".join(parts)


def _receipt_for(user_id: str) -> Any:
    principal = build_principal_from_fixture(user_id)
    return build_receipt(
        principal=principal,
        epochs=PolicyEpochState(data_epoch=3, policy_epoch=2),
        claim_ids=["clm-1", "clm-2"],
        redacted_evidence_claim_ids=["clm-2"],
        snapshot_version=7,
        tool_or_endpoint="resolve_context",
    )


def test_public_trace_exports_with_receipt_metadata_and_masked_content() -> None:
    with _tracer().start_as_current_span("root") as root:
        receipt = _receipt_for("user:public-analyst")
        root.set_attribute("gen_ai.prompt.0.content", "Question about Cobalt")
        root.set_attribute("input.value", "secret prompt body")
        root.add_event("gen_ai.content.prompt", {"gen_ai.prompt": "event body"})
        with _tracer().start_as_current_span("child") as child:
            child.set_attribute("output.value", "model answer body")
            child.set_attribute("context.custom_count", 3)

    exported = {s.name: s for s in LANGSMITH_EXPORTER.get_finished_spans()}
    assert set(exported) == {"root", "child"}
    root_attrs = dict(exported["root"].attributes or {})
    assert root_attrs["langsmith.metadata.context_receipt_id"] == receipt.receipt_id
    assert root_attrs["context.receipt_sha256"] == receipt.receipt_sha256
    assert root_attrs["langsmith.metadata.context_snapshot_version"] == 7
    assert root_attrs["context.redacted_evidence_count"] == 1
    assert root_attrs[ct.WALL_SIDE_ATTR] == "public_side"
    blob = _exported_blob(LANGSMITH_EXPORTER)
    for body in ("Question about Cobalt", "secret prompt body", "model answer body", "event body"):
        assert body not in blob
    assert all(not s.events for s in LANGSMITH_EXPORTER.get_finished_spans())
    assert dict(exported["child"].attributes or {})["context.custom_count"] == 3
    # The internal collector keeps the unmasked spans.
    assert "secret prompt body" in _exported_blob(INTERNAL_EXPORTER)


@pytest.mark.parametrize("user_id", ["user:private-deal-team", "user:fund-steward", "user:overage-director"])
def test_non_public_traces_stay_internal(user_id: str) -> None:
    with _tracer().start_as_current_span("root"):
        _receipt_for(user_id)
    assert LANGSMITH_EXPORTER.get_finished_spans() == ()
    assert len(INTERNAL_EXPORTER.get_finished_spans()) == 1


def test_unmarked_trace_is_dropped_fail_closed() -> None:
    with _tracer().start_as_current_span("root"):
        with _tracer().start_as_current_span("child"):
            pass
    assert LANGSMITH_EXPORTER.get_finished_spans() == ()
    assert len(INTERNAL_EXPORTER.get_finished_spans()) == 2


def test_restricted_marking_is_sticky_across_a_trace() -> None:
    with _tracer().start_as_current_span("root"):
        _receipt_for("user:public-analyst")
        with _tracer().start_as_current_span("private-read"):
            _receipt_for("user:private-deal-team")
        _receipt_for("user:public-analyst")
    assert LANGSMITH_EXPORTER.get_finished_spans() == ()


def test_revoked_principal_never_exports() -> None:
    principal = build_principal_from_fixture("user:revoked-analyst")
    assert principal.revoked
    with ct.traced("revoked", principal=principal):
        pass
    assert LANGSMITH_EXPORTER.get_finished_spans() == ()


def test_span_attribute_alone_blocks_export() -> None:
    with _tracer().start_as_current_span("root"):
        ct.mark_trace_wall_side("public_side")
        with _tracer().start_as_current_span("child") as child:
            child.set_attribute(ct.WALL_SIDE_ATTR, "private_side")
    assert LANGSMITH_EXPORTER.get_finished_spans() == ()


def test_content_allowed_only_when_explicitly_enabled() -> None:
    sink = InMemorySpanExporter()
    registry = ct._TraceWallRegistry()
    filt = ct.WallSideExportFilter(SimpleSpanProcessor(sink), allow_content=True, registry=registry)
    provider = TracerProvider()
    provider.add_span_processor(filt)
    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("root") as span:
        registry.mark(span.get_span_context().trace_id, "public_side")
        span.set_attribute("input.value", "kept body")
    assert "kept body" in _exported_blob(sink)


def test_buffer_overflow_drops_traces_fail_closed() -> None:
    sink = InMemorySpanExporter()
    registry = ct._TraceWallRegistry()
    filt = ct.WallSideExportFilter(SimpleSpanProcessor(sink), max_spans_per_trace=2, registry=registry)
    provider = TracerProvider()
    provider.add_span_processor(filt)
    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("root") as root:
        registry.mark(root.get_span_context().trace_id, "public_side")
        for i in range(3):
            with tracer.start_as_current_span(f"c{i}"):
                pass
    assert sink.get_finished_spans() == ()
    assert filt.dropped_traces >= 1


def test_langsmith_sdk_scope_disables_callbacks_for_non_public() -> None:
    from langsmith.run_helpers import get_tracing_context

    private = build_principal_from_fixture("user:private-deal-team")
    public = build_principal_from_fixture("user:public-analyst")
    with ct.langsmith_sdk_tracing_scope(private):
        assert get_tracing_context()["enabled"] is False
    with ct.langsmith_sdk_tracing_scope(None):
        assert get_tracing_context()["enabled"] is False
    with ct.langsmith_sdk_tracing_scope(public):
        assert get_tracing_context()["enabled"] is not False


def _all_canaries() -> list[str]:
    doc = load_json_fixture("principals.json")
    tokens: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)
        elif isinstance(node, str) and node.startswith("CANARY_"):
            tokens.append(node)

    _walk(doc)
    assert len(tokens) >= 5
    return tokens


async def test_api_end_to_end_exports_only_public_side_and_no_canaries(api_client: httpx.AsyncClient) -> None:
    canaries = _all_canaries()
    public_users = ["user:public-analyst", "user:pm-credit", "user:product-lead"]
    for uid in public_users + ["user:private-deal-team", "user:fund-steward"]:
        h = mock_headers(uid)
        await api_client.get("/api/v1/search", params={"q": "Cobalt"}, headers=h)
        await api_client.get("/api/v1/entities", headers=h)
        await api_client.get("/api/v1/claims", headers=h)
        await api_client.get("/api/v1/memory/notes", headers=h)
        await api_client.post(
            "/api/v1/agent/chat",
            json={"message": "What do we know about Cobalt?", "viewport": {}},
            headers=h,
        )

    exported = LANGSMITH_EXPORTER.get_finished_spans()
    internal = INTERNAL_EXPORTER.get_finished_spans()
    assert exported, "public-side requests should reach LangSmith"
    assert len(internal) > len(exported)

    walls = {dict(s.attributes or {}).get(ct.WALL_SIDE_ATTR) for s in exported}
    assert walls <= {"public_side", None}
    assert any("langsmith.metadata.context_receipt_id" in (s.attributes or {}) for s in exported)

    blob = _exported_blob(LANGSMITH_EXPORTER)
    for token in canaries:
        assert token not in blob, f"CANARY LEAKED TO LANGSMITH: {token}"
    for private_marker in ("private_side", "above_the_wall", "user:private-deal-team", "user:fund-steward"):
        assert private_marker not in blob


async def test_curator_run_never_exports(api_client: httpx.AsyncClient) -> None:
    resp = await api_client.post("/api/v1/curator/run", json={}, headers=mock_headers("user:fund-steward"))
    assert resp.status_code < 500
    assert LANGSMITH_EXPORTER.get_finished_spans() == ()
    assert any(s.name == "curator.run" for s in INTERNAL_EXPORTER.get_finished_spans())
