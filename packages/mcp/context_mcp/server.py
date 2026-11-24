"""Dual FastMCP 4.x Server Surface: Standard Primitives (/mcp) & Server-Side Code Mode (/mcp/codemode)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool, ToolResult
from mcp import types as mt

from context_core.exceptions import AuthenticationError, ContextLakeError, SurfacePolicyError
from context_core.models import Principal

from context_mcp.codemode import execute_code, search_sdk
from context_mcp.primitives import (
    read_memory_me_resource,
    read_skill_resource,
    tool_filter,
    tool_flag_knowledge_gap,
    tool_lookup,
    tool_memory_upsert,
    tool_neighbours,
    tool_propose_update,
    tool_read,
    tool_resolve_context,
    tool_search,
)
from context_mcp.runtime import LakeRuntime, current_principal_var
from context_mcp.wiki_tools import tool_wiki_ask, tool_wiki_read, tool_wiki_structure


def _make_tool_result(data: dict[str, Any], summary: str) -> ToolResult:
    """Return compact text summary alongside full structuredContent per ADR-004."""
    return ToolResult(
        content=[mt.TextContent(type="text", text=summary)],
        structured_content=data,
    )


class PrincipalPolicyMiddleware(Middleware):
    """Binds authenticated caller Principal per MCP request and enforces surface-aware tool gating."""

    def __init__(self, runtime: LakeRuntime, *, gate_memory_tool: bool = True) -> None:
        self._runtime = runtime
        self._gate_memory_tool = gate_memory_tool

    async def _resolve_for_request(self) -> Principal:
        headers = get_http_headers(include_all=True)
        return await self._runtime.resolve_principal(headers or None)

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        try:
            principal = await self._resolve_for_request()
        except AuthenticationError as exc:
            raise ToolError(f"unauthorized: {exc.message}") from exc

        token = current_principal_var.set(principal)
        try:
            tools = list(await call_next(context))
            if self._gate_memory_tool and not self._runtime.policy.is_memory_allowed(principal):
                tools = [t for t in tools if t.name != "memory_upsert"]
            return tools
        finally:
            current_principal_var.reset(token)

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            principal = await self._resolve_for_request()
        except AuthenticationError as exc:
            raise ToolError(f"unauthorized: {exc.message}") from exc

        tool_name = context.message.name
        if (
            self._gate_memory_tool
            and tool_name == "memory_upsert"
            and not self._runtime.policy.is_memory_allowed(principal)
        ):
            raise ToolError("surface_policy_blocked (403): memory_upsert disabled on shared/service surface")

        token = current_principal_var.set(principal)
        try:
            return await call_next(context)
        except SurfacePolicyError as exc:
            raise ToolError(f"surface_policy_blocked (403): {exc.message}") from exc
        except ContextLakeError as exc:
            raise ToolError(f"{exc.code} ({exc.status_code}): {exc.message}") from exc
        finally:
            current_principal_var.reset(token)

    async def on_read_resource(
        self,
        context: MiddlewareContext[mt.ReadResourceRequestParams],
        call_next: CallNext[mt.ReadResourceRequestParams, Any],
    ) -> Any:
        try:
            principal = await self._resolve_for_request()
        except AuthenticationError as exc:
            raise ToolError(f"unauthorized (401): {exc.message}") from exc

        token = current_principal_var.set(principal)
        try:
            return await call_next(context)
        finally:
            current_principal_var.reset(token)


def create_primitive_mcp_server(runtime: LakeRuntime) -> FastMCP:
    """Create Surface A FastMCP server (`/mcp`) with 12 compact typed primitive tools + resources."""
    mcp = FastMCP(
        name="enterprise-context-lake",
        instructions=(
            "Enterprise Context Lake MCP server: bitemporal institutional ontology, "
            "decoupled claim/evidence ACLs, N-way conflicts, and caller-scoped personal memory."
        ),
    )
    mcp.add_middleware(PrincipalPolicyMiddleware(runtime, gate_memory_tool=True))

    async def _principal() -> Principal:
        return await runtime.resolve_principal(get_http_headers(include_all=True) or None)

    @mcp.tool(
        name="filter",
        description="Metadata and orthogonal facet slice returning lightweight authorized entity, claim, and topic summaries.",
        meta={
            "input_examples": [
                {"entity_types": ["issuer"], "facets": {"coverage_statuses": ["watchlist", "pipeline"]}, "limit": 20},
                {"topic_slug": "cashflows-liquidity", "facets": {"domain_nature": "financial"}, "limit": 25},
            ]
        },
    )
    async def filter_tool(
        entity_types: list[str] | None = None,
        topic_slug: str | None = None,
        facets: dict[str, Any] | None = None,
        scope_types: list[str] | None = None,
        epistemic_statuses: list[str] | None = None,
        as_of: str | None = None,
        recorded_as_of: str | None = None,
        limit: int = 50,
    ) -> ToolResult:
        principal = await _principal()
        res = await tool_filter(
            runtime,
            principal,
            entity_types=entity_types,
            topic_slug=topic_slug,
            facets=facets,
            scope_types=scope_types,
            epistemic_statuses=epistemic_statuses,
            as_of=as_of,
            recorded_as_of=recorded_as_of,
            limit=limit,
        )
        summary = (
            f"filter returned {res['total']} authorized items "
            f"(receipt={res['receipt_id']} data_epoch={res['data_epoch']} policy_epoch={res['policy_epoch']})"
        )
        return _make_tool_result(res, summary)

    @mcp.tool(
        name="search",
        description="Lexical, alias, and orthogonal facet search across caller-authorized entities, claims, and knowledge items.",
        meta={
            "input_examples": [
                {"query": "Alpha Credit Offshore", "limit": 10},
                {"query": "LME", "topic_slug": "leverage-covenants", "limit": 15},
            ]
        },
    )
    async def search_tool(
        query: str,
        entity_types: list[str] | None = None,
        topic_slug: str | None = None,
        facets: dict[str, Any] | None = None,
        as_of: str | None = None,
        recorded_as_of: str | None = None,
        limit: int = 25,
    ) -> ToolResult:
        principal = await _principal()
        res = await tool_search(
            runtime,
            principal,
            query=query,
            entity_types=entity_types,
            topic_slug=topic_slug,
            facets=facets,
            as_of=as_of,
            recorded_as_of=recorded_as_of,
            limit=limit,
        )
        summary = (
            f"search '{query}' matched {res['total']} authorized hits "
            f"(receipt={res['receipt_id']} data_epoch={res['data_epoch']} policy_epoch={res['policy_epoch']})"
        )
        return _make_tool_result(res, summary)

    @mcp.tool(
        name="read",
        description="Read a full authorized entity or knowledge card with bitemporal claims, redacted evidence stubs, identifiers, lineage, and topics.",
        meta={
            "input_examples": [
                {"id_or_key": "veh:alpha-credit-offshore-lp", "include_claims": True},
                {"id_or_key": "issuer:apex-industrial", "as_of": "2025-12-31T00:00:00Z"},
            ]
        },
    )
    async def read_tool(
        id_or_key: str,
        include_claims: bool = True,
        include_identifiers: bool = True,
        include_lineage: bool = True,
        include_topics: bool = True,
        as_of: str | None = None,
        recorded_as_of: str | None = None,
    ) -> ToolResult:
        principal = await _principal()
        res = await tool_read(
            runtime,
            principal,
            id_or_key=id_or_key,
            include_claims=include_claims,
            include_identifiers=include_identifiers,
            include_lineage=include_lineage,
            include_topics=include_topics,
            as_of=as_of,
            recorded_as_of=recorded_as_of,
        )
        if not res.get("found"):
            return _make_tool_result(res, f"read '{id_or_key}': not_found")
        label = (
            res.get("entity", {}).get("display_name")
            or res.get("knowledge_item", {}).get("display_name")
            or id_or_key
        )
        summary = (
            f"read '{label}': {len(res.get('claims', []))} claims, "
            f"{len(res.get('conflicts', []))} open conflicts "
            f"(receipt={res['receipt_id']} data_epoch={res['data_epoch']})"
        )
        return _make_tool_result(res, summary)

    @mcp.tool(
        name="neighbours",
        description="Bounded-hop (1-3) bidirectional or directional graph traversal omitting unauthorized neighbors.",
        meta={
            "input_examples": [
                {"id_or_key": "issuer:apex-industrial", "depth": 2, "direction": "both"},
                {"id_or_key": "veh:alpha-credit-offshore-lp", "depth": 1, "direction": "in"},
            ]
        },
    )
    async def neighbours_tool(
        id_or_key: str,
        depth: int = 1,
        direction: Literal["both", "in", "out", "forward", "reverse"] = "both",
        predicates: list[str] | None = None,
        as_of: str | None = None,
        recorded_as_of: str | None = None,
    ) -> ToolResult:
        principal = await _principal()
        res = await tool_neighbours(
            runtime,
            principal,
            id_or_key=id_or_key,
            depth=depth,
            direction=direction,
            predicates=predicates,
            as_of=as_of,
            recorded_as_of=recorded_as_of,
        )
        if not res.get("found"):
            return _make_tool_result(res, f"neighbours '{id_or_key}': not_found")
        summary = (
            f"neighbours '{id_or_key}' (depth={res['depth']}, dir={res['direction']}): "
            f"{len(res.get('nodes', []))} nodes, {len(res.get('edges', []))} edges "
            f"(receipt={res['receipt_id']})"
        )
        return _make_tool_result(res, summary)

    @mcp.tool(
        name="lookup",
        description="Deterministic exact mapping lookup returning active approved_fact and open conflict status.",
        meta={
            "input_examples": [
                {
                    "canonical_key": "veh:alpha-credit-offshore-lp",
                    "predicate": "has_admin_code",
                    "context_purpose": "legal_vehicle",
                },
                {
                    "canonical_key": "skill:crm-fund-mapping",
                    "predicate": "maps_as",
                    "context_purpose": "fund_family",
                },
            ]
        },
    )
    async def lookup_tool(
        canonical_key: str,
        predicate: str,
        context_purpose: str = "general",
        as_of: str | None = None,
        recorded_as_of: str | None = None,
        snapshot_version: int | None = None,
    ) -> ToolResult:
        principal = await _principal()
        res = await tool_lookup(
            runtime,
            principal,
            canonical_key=canonical_key,
            predicate=predicate,
            context_purpose=context_purpose,
            as_of=as_of,
            recorded_as_of=recorded_as_of,
            snapshot_version=snapshot_version,
        )
        summary = (
            f"lookup {canonical_key}.{predicate} ({context_purpose}) -> "
            f"{res.get('value')} [conflict={res.get('has_conflict', False)}] "
            f"(receipt={res.get('receipt_id')})"
        )
        return _make_tool_result(res, summary)

    @mcp.tool(
        name="resolve_context",
        description="Assemble a two-channel stratified context bundle (facts, directives, conflicts, memories, receipt).",
        meta={
            "input_examples": [
                {
                    "entity_keys": ["veh:alpha-credit-offshore-lp"],
                    "skill_id": "skill:cashflow-wire-reconciliation",
                },
                {
                    "entity_keys": ["grp:alpha-credit-gips-composite"],
                    "as_of": "2025-12-31T00:00:00Z",
                    "recorded_as_of": "2026-01-16T00:00:00Z",
                },
            ]
        },
    )
    async def resolve_context_tool(
        entity_keys: list[str],
        skill_id: str | None = None,
        project_id: str | None = None,
        run_id: str | None = None,
        include_unverified: bool = False,
        as_of: str | None = None,
        recorded_as_of: str | None = None,
        snapshot_version: int | None = None,
    ) -> ToolResult:
        principal = await _principal()
        res = await tool_resolve_context(
            runtime,
            principal,
            entity_keys=entity_keys,
            skill_id=skill_id,
            project_id=project_id,
            run_id=run_id,
            include_unverified=include_unverified,
            as_of=as_of,
            recorded_as_of=recorded_as_of,
            snapshot_version=snapshot_version,
        )
        summary = (
            f"resolve_context: {len(res.get('facts', []))} facts, "
            f"{len(res.get('directives', []))} directives, "
            f"{len(res.get('conflicts', []))} conflicts, "
            f"{len(res.get('memories', []))} memories "
            f"(receipt={res['receipt_id']} data_epoch={res['data_epoch']})"
        )
        return _make_tool_result(res, summary)

    @mcp.tool(
        name="flag_knowledge_gap",
        description="Record a quarantined observation for a knowledge gap, stale source, or wrong claim.",
        meta={
            "input_examples": [
                {
                    "subject_key_or_topic": "veh:alpha-credit-offshore-lp",
                    "kind": "stale",
                    "description": "Custodian side letter addendum references ADM-904X effective 2026-01-01",
                    "severity": "high",
                }
            ]
        },
    )
    async def flag_knowledge_gap_tool(
        subject_key_or_topic: str,
        kind: Literal["gap", "stale", "wrong", "observation"],
        description: str,
        severity: Literal["low", "medium", "high"] = "medium",
        suggested_source: str | None = None,
    ) -> ToolResult:
        principal = await _principal()
        res = await tool_flag_knowledge_gap(
            runtime,
            principal,
            subject_key_or_topic=subject_key_or_topic,
            kind=kind,
            description=description,
            severity=severity,
            suggested_source=suggested_source,
        )
        summary = (
            f"flag_knowledge_gap ({kind}, severity={severity}) quarantined as "
            f"{res['claim']['claim_id']} (receipt={res['receipt_id']})"
        )
        return _make_tool_result(res, summary)

    @mcp.tool(
        name="propose_update",
        description="Propose a quarantined claim update for steward review (always unverified_observation; never overwrites approved_fact).",
        meta={
            "input_examples": [
                {
                    "subject_canonical_key": "veh:alpha-credit-offshore-lp",
                    "kind": "observation",
                    "predicate": "has_admin_code",
                    "object_value": "ADM-904X",
                    "context_purpose": "legal_vehicle",
                    "rationale": "Custodian feed addendum confirmation",
                }
            ]
        },
    )
    async def propose_update_tool(
        subject_canonical_key: str,
        predicate: str,
        object_value: str,
        rationale: str,
        kind: Literal["gap", "stale", "wrong", "observation"] = "observation",
        context_purpose: str = "general",
        evidence_uri: str | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
    ) -> ToolResult:
        principal = await _principal()
        res = await tool_propose_update(
            runtime,
            principal,
            subject_canonical_key=subject_canonical_key,
            predicate=predicate,
            object_value=object_value,
            rationale=rationale,
            kind=kind,
            context_purpose=context_purpose,
            evidence_uri=evidence_uri,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        summary = (
            f"propose_update ({kind}) quarantined as {res['claim']['claim_id']} "
            f"[conflict={res['conflict_detected']}] (receipt={res['receipt_id']})"
        )
        return _make_tool_result(res, summary)

    @mcp.tool(
        name="memory_upsert",
        description="Create or update caller-scoped personal memory (auto-disabled on shared channels or service identities).",
        meta={
            "input_examples": [
                {
                    "scope_type": "user",
                    "key": "alpha-credit-watch",
                    "content": "Review Q1 audit tie-out before LP quarterly call.",
                    "tags": ["alpha-credit", "audit"],
                }
            ]
        },
    )
    async def memory_upsert_tool(
        key: str,
        content: str,
        scope_type: Literal["user", "user_x_skill"] = "user",
        skill_id: str | None = None,
        tags: list[str] | None = None,
        linked_entity_ids: list[str] | None = None,
        linked_topic_ids: list[str] | None = None,
    ) -> ToolResult:
        principal = await _principal()
        res = await tool_memory_upsert(
            runtime,
            principal,
            key=key,
            content=content,
            scope_type=scope_type,
            skill_id=skill_id,
            tags=tags,
            linked_entity_ids=linked_entity_ids,
            linked_topic_ids=linked_topic_ids,
        )
        summary = f"memory_upsert saved {res['memory']['memory_id']} (receipt={res['receipt_id']})"
        return _make_tool_result(res, summary)

    # Entity pages (ADR-009). Named after DeepWiki's tools so existing harness prompts carry over.
    @mcp.tool(
        name="read_wiki_structure",
        description=(
            "List the entity pages you can read, grouped by entity type, with each type's section outline. "
            "Use it to browse before searching. Pages are cut by wall side; the default is your own side."
        ),
        meta={"input_examples": [{"entity_types": ["issuer"], "limit": 50}, {"topic_slug": "cashflows-liquidity"}]},
    )
    async def read_wiki_structure_tool(
        side: Literal["public_side", "private_side", "above_the_wall"] | None = None,
        entity_types: list[str] | None = None,
        topic_slug: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> ToolResult:
        principal = await _principal()
        res = await tool_wiki_structure(
            runtime, principal, side=side, entity_types=entity_types, topic_slug=topic_slug, limit=limit, offset=offset
        )
        n_pages = sum(len(g["pages"]) for g in res["groups"])
        summary = f"read_wiki_structure ({res['side']}): {n_pages} of {res['total']} pages in {len(res['groups'])} entity types"
        return _make_tool_result(res, summary)

    @mcp.tool(
        name="read_wiki_contents",
        description=(
            "Read one entity page as Markdown: overview, key facts, relationships, a Mermaid diagram, open "
            "conflicts, directives and recent changes. Every statement cites a numbered source claim."
        ),
        meta={"input_examples": [{"id_or_key": "veh:alpha-credit-offshore-lp"}, {"id_or_key": "issuer:apex-industrial", "channel": "production"}]},
    )
    async def read_wiki_contents_tool(
        id_or_key: str,
        side: Literal["public_side", "private_side", "above_the_wall"] | None = None,
        as_of: str | None = None,
        recorded_as_of: str | None = None,
        channel: Literal["dev", "staging", "production"] | None = None,
        release: int | None = None,
    ) -> ToolResult:
        principal = await _principal()
        res = await tool_wiki_read(
            runtime, principal, id_or_key=id_or_key, side=side, as_of=as_of,
            recorded_as_of=recorded_as_of, channel=channel, release=release,
        )
        if not res.get("found"):
            return _make_tool_result(res, f"read_wiki_contents '{id_or_key}': not_found")
        return ToolResult(content=[mt.TextContent(type="text", text=res["markdown"])], structured_content=res)

    @mcp.tool(
        name="ask_wiki_question",
        description=(
            "Ask a question and get an answer built only from entity pages you can read, with numbered "
            "citations to source claims. Optionally name the pages to use."
        ),
        meta={"input_examples": [{"question": "What is the admin code for Alpha Credit Offshore?"}]},
    )
    async def ask_wiki_question_tool(
        question: str,
        side: Literal["public_side", "private_side", "above_the_wall"] | None = None,
        entity_keys: list[str] | None = None,
        as_of: str | None = None,
        recorded_as_of: str | None = None,
        channel: Literal["dev", "staging", "production"] | None = None,
        release: int | None = None,
    ) -> ToolResult:
        principal = await _principal()
        res = await tool_wiki_ask(
            runtime, principal, question=question, side=side, entity_keys=entity_keys, as_of=as_of,
            recorded_as_of=recorded_as_of, channel=channel, release=release,
        )
        return ToolResult(content=[mt.TextContent(type="text", text=res["answer_markdown"])], structured_content=res)

    @mcp.resource(
        "context://memory/me",
        name="personal_memory",
        description="Caller's personal notes, watchlist, and live ACL-filtered change/conflict alerts (OBO direct only).",
        mime_type="application/json",
    )
    async def memory_me_resource() -> str:
        principal = await _principal()
        data = await read_memory_me_resource(runtime, principal)
        return json.dumps(data, indent=2)

    @mcp.resource(
        "context://skills/{skill_id}",
        name="skill_procedure",
        description="Authorized SKILL.md operational procedure including metadata.include_tools.",
        mime_type="application/json",
    )
    async def skill_resource(skill_id: str) -> str:
        principal = await _principal()
        data = await read_skill_resource(runtime, principal, skill_id)
        return json.dumps(data, indent=2)

    return mcp


def create_codemode_mcp_server(runtime: LakeRuntime) -> FastMCP:
    """Create Surface B FastMCP server (`/mcp/codemode`) exposing `search_sdk` + `execute_code`."""
    mcp = FastMCP(
        name="enterprise-context-lake-codemode",
        instructions=(
            "Enterprise Context Lake Server-Side Code Mode MCP: call search_sdk to discover "
            "the frozen `lake` bridge, then execute_code to run multi-step JS queries inside a "
            "zero-egress Node 24 --permission isolate bound strictly to caller PolicyPort."
        ),
    )
    mcp.add_middleware(PrincipalPolicyMiddleware(runtime, gate_memory_tool=False))

    async def _principal() -> Principal:
        return await runtime.resolve_principal(get_http_headers(include_all=True) or None)

    @mcp.tool(
        name="search_sdk",
        description="Search TypeScript declarations and usage examples for the frozen sandbox `lake` RPC bridge.",
        meta={
            "input_examples": [
                {"query": "impact neighbours"},
                {"query": "lookup resolveContext"},
            ]
        },
    )
    async def search_sdk_tool(query: str = "") -> ToolResult:
        principal = await _principal()
        res = search_sdk(query, principal=principal, policy=runtime.policy)
        names = ", ".join(m["name"] for m in res["methods"])
        summary = f"search_sdk returned {len(res['methods'])} methods: {names}"
        return _make_tool_result(res, summary)

    @mcp.tool(
        name="execute_code",
        description=(
            "Execute JavaScript inside a zero-egress Node 24 --permission isolate with caller-bound `lake` RPC "
            "(no filesystem, no network, no child_process, no credentials, <=64 KiB output cap)."
        ),
        meta={
            "input_examples": [
                {
                    "code": 'const hits = await lake.search("Alpha Credit"); const card = await lake.read(hits.hits[0].canonical_key); return { entity: card.entity.display_name, conflicts: card.conflicts.length };',
                    "timeout_ms": 3000,
                }
            ]
        },
    )
    async def execute_code_tool(code: str, timeout_ms: int = 3000) -> ToolResult:
        principal = await _principal()
        res = await execute_code(
            code,
            runtime=runtime,
            principal=principal,
            timeout_ms=timeout_ms,
        )
        status = "ok" if res["ok"] else f"error:{(res.get('error') or {}).get('code', 'failed')}"
        summary = (
            f"execute_code [{status}] rpc_calls={res['rpc_calls']} "
            f"duration_ms={res['duration_ms']} truncated={res['truncated']} "
            f"(receipt={res['receipt_id']} data_epoch={res['data_epoch']})"
        )
        return _make_tool_result(res, summary)

    return mcp


if __name__ == "__main__":
    from context_mcp.__main__ import main

    main()

