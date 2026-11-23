"""Server-Side Code Mode host RPC bridge and zero-egress Node 24 --permission executor (ADR-004)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from context_core.exceptions import (
    AuthenticationError,
    ContextLakeError,
    SurfacePolicyError,
    ValidationError,
)
from context_core.models import Principal

from context_mcp.primitives import (
    tool_filter,
    tool_flag_knowledge_gap,
    tool_impact,
    tool_lookup,
    tool_memory_upsert,
    tool_neighbours,
    tool_propose_update,
    tool_read,
    tool_resolve_context,
    tool_search,
)
from context_mcp.runtime import LakeRuntime, make_receipt
from context_mcp.security import (
    MAX_CODEMODE_OUTPUT_BYTES,
    sanitize_error_message,
    truncate_output_payload,
)

SANDBOX_RUNNER_PATH = Path(__file__).resolve().parent / "sandbox_runner.mjs"
DEFAULT_TIMEOUT_MS = 3000
MAX_TIMEOUT_MS = 10000
DEFAULT_MAX_RPC_CALLS = 25

SDK_METHODS: list[dict[str, Any]] = [
    {
        "name": "filter",
        "signature": (
            "lake.filter(opts?: { entity_types?: string[]; topic_slug?: string; "
            "facets?: { domain_nature?: 'financial'|'non_financial'; modality?: 'quantitative'|'qualitative'; "
            "epistemic_temporal?: 'actual'|'estimate'|'forecast'|'guidance'|'pro_forma'; "
            "actual_stage?: 'prelim'|'unaudited'|'audited'|'restated'; periodicity?: string; "
            "ingestion_channel?: string; coverage_statuses?: string[]; conflicts_only?: boolean }; "
            "scope_types?: string[]; epistemic_statuses?: string[]; as_of?: string; recorded_as_of?: string; limit?: number }): Promise<FilterResult>"
        ),
        "description": "Metadata and Plane 3/4 facet slice returning lightweight authorized entity/claim/topic summaries.",
        "example": 'const res = await lake.filter({ entity_types: ["issuer"], facets: { coverage_statuses: ["watchlist", "pipeline"] } }); return res.items;',
        "requires_memory": False,
    },
    {
        "name": "search",
        "signature": (
            "lake.search(queryOrOpts: string | { query: string; entity_types?: string[]; "
            "topic_slug?: string; facets?: object; as_of?: string; recorded_as_of?: string; limit?: number }, opts?: object): Promise<SearchResult>"
        ),
        "description": "Lexical + alias + facet search across caller-authorized entities, claims, topics, and knowledge items (zero existence leakage).",
        "example": 'const res = await lake.search("Alpha Credit", { limit: 10 }); return res.hits;',
        "requires_memory": False,
    },
    {
        "name": "read",
        "signature": (
            "lake.read(idOrOpts: string | { id_or_key: string; include_claims?: boolean; "
            "include_identifiers?: boolean; include_lineage?: boolean; include_topics?: boolean; "
            "as_of?: string; recorded_as_of?: string }, opts?: object): Promise<ReadResult>"
        ),
        "description": "Read a full authorized entity or knowledge card with bitemporal claims, redacted evidence stubs, identifiers, lineage, positions, coverages, and open conflicts.",
        "example": 'const card = await lake.read("veh:alpha-credit-offshore-lp", { as_of: "2025-12-31T00:00:00Z" }); return card;',
        "requires_memory": False,
    },
    {
        "name": "neighbours",
        "signature": (
            "lake.neighbours(idOrOpts: string | { id_or_key: string; depth?: 1|2|3; "
            "direction?: 'both'|'in'|'out'|'forward'|'reverse'; predicates?: string[]; "
            "as_of?: string; recorded_as_of?: string }, opts?: object): Promise<GraphPayload>"
        ),
        "description": "Bounded-hop (1-3) bidirectional or directional graph traversal omitting unauthorized nodes.",
        "example": 'const g = await lake.neighbours("issuer:apex-industrial", { depth: 2, direction: "both" }); return g.nodes;',
        "requires_memory": False,
    },
    {
        "name": "impact",
        "signature": (
            "lake.impact(nodeIdOrOpts: string | { node_id: string; max_depth?: number; "
            "as_of?: string; recorded_as_of?: string }, opts?: object): Promise<ImpactGraphPayload>"
        ),
        "description": "ACL-enforced reverse-BFS impact traversal from any authorized source, topic, issuer, vehicle, or skill through positions -> funds -> clients/opportunities -> skills/apps -> watchlist.",
        "example": 'const imp = await lake.impact("issuer:apex-industrial", { max_depth: 5 }); return imp.layers;',
        "requires_memory": False,
    },
    {
        "name": "lookup",
        "signature": (
            "lake.lookup(opts: { canonical_key: string; predicate: string; context_purpose?: string; "
            "as_of?: string; recorded_as_of?: string; snapshot_version?: number }): Promise<LookupResult>"
        ),
        "description": "Deterministic exact mapping lookup returning active approved_fact + conflict flag (never unverified observations).",
        "example": 'return await lake.lookup({ canonical_key: "veh:alpha-credit-offshore-lp", predicate: "has_admin_code", context_purpose: "legal_vehicle" });',
        "requires_memory": False,
    },
    {
        "name": "resolveContext",
        "signature": (
            "lake.resolveContext(opts: { entity_keys: string[]; skill_id?: string; project_id?: string; "
            "run_id?: string; include_unverified?: boolean; as_of?: string; recorded_as_of?: string; "
            "snapshot_version?: number }): Promise<ContextBundle>"
        ),
        "description": "Two-channel stratified context bundle separating authoritative facts from directives, open conflicts, memories, and immutable receipt.",
        "example": 'return await lake.resolveContext({ entity_keys: ["veh:alpha-credit-offshore-lp"], skill_id: "skill:cashflow-wire-reconciliation" });',
        "requires_memory": False,
    },
    {
        "name": "flagKnowledgeGap",
        "signature": (
            "lake.flagKnowledgeGap(opts: { subject_key_or_topic: string; kind: 'gap'|'stale'|'wrong'|'observation'; "
            "description: string; severity?: 'low'|'medium'|'high'; suggested_source?: string }): Promise<QuarantineResult>"
        ),
        "description": "Record a quarantined unverified_observation for a missing mapping, stale source, or erroneous value.",
        "example": 'return await lake.flagKnowledgeGap({ subject_key_or_topic: "veh:alpha-credit-offshore-lp", kind: "stale", description: "Admin code feed mismatch", severity: "high" });',
        "requires_memory": False,
    },
    {
        "name": "proposeUpdate",
        "signature": (
            "lake.proposeUpdate(opts: { subject_canonical_key: string; kind?: 'gap'|'stale'|'wrong'|'observation'; "
            "predicate: string; object_value: string; rationale: string; context_purpose?: string; "
            "evidence_uri?: string; valid_from?: string; valid_to?: string }): Promise<QuarantineResult>"
        ),
        "description": "Propose a quarantined claim update for steward review (always unverified_observation; never overwrites approved_fact).",
        "example": 'return await lake.proposeUpdate({ subject_canonical_key: "veh:alpha-credit-offshore-lp", kind: "observation", predicate: "custodian_note", object_value: "Verified Q1 wire instructions", rationale: "Ops email confirmation" });',
        "requires_memory": False,
    },
    {
        "name": "memoryUpsert",
        "signature": (
            "lake.memoryUpsert(opts: { key: string; content: string; scope_type?: 'user'|'user_x_skill'; "
            "skill_id?: string; tags?: string[]; linked_entity_ids?: string[]; linked_topic_ids?: string[] }): Promise<MemoryResult>"
        ),
        "description": "Create or update caller-scoped personal memory (automatically disabled on shared channels and service identities).",
        "example": 'return await lake.memoryUpsert({ scope_type: "user", key: "alpha-check", content: "Track Q1 audit sign-off" });',
        "requires_memory": True,
    },
]

# Canonical method map accepting both camelCase and snake_case aliases inside JS code
_METHOD_ALIASES: dict[str, str] = {
    "filter": "filter",
    "search": "search",
    "read": "read",
    "neighbours": "neighbours",
    "neighbors": "neighbours",
    "impact": "impact",
    "lookup": "lookup",
    "resolveContext": "resolveContext",
    "resolve_context": "resolveContext",
    "flagKnowledgeGap": "flagKnowledgeGap",
    "flag_knowledge_gap": "flagKnowledgeGap",
    "proposeUpdate": "proposeUpdate",
    "propose_update": "proposeUpdate",
    "memoryUpsert": "memoryUpsert",
    "memory_upsert": "memoryUpsert",
}


def search_sdk(
    query: str,
    *,
    principal: Principal,
    policy: Any,
) -> dict[str, Any]:
    """Search frozen sandbox `lake` SDK TypeScript declarations, respecting surface memory gating."""
    memory_allowed = bool(policy.is_memory_allowed(principal))
    available = [m for m in SDK_METHODS if (not m["requires_memory"]) or memory_allowed]

    q = (query or "").strip().lower()
    if not q or q in ("*", "all", "sdk", "lake"):
        matches = available
    else:
        tokens = [tok for tok in q.replace("_", " ").replace("-", " ").split() if tok]
        scored: list[tuple[int, dict[str, Any]]] = []
        for item in available:
            haystack = f"{item['name']} {item['signature']} {item['description']} {item['example']}".lower()
            score = sum(3 if tok in item["name"].lower() else 1 for tok in tokens if tok in haystack)
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["name"]))
        matches = [m for _, m in scored] if scored else available

    return {
        "query": query,
        "memory_enabled": memory_allowed,
        "methods": [
            {
                "name": m["name"],
                "signature": m["signature"],
                "description": m["description"],
                "example": m["example"],
            }
            for m in matches
        ],
        "sandbox_constraints": {
            "runtime": "node --permission --max-old-space-size=64",
            "fs_access": False,
            "network_access": False,
            "child_process": False,
            "worker_threads": False,
            "max_rpc_calls": DEFAULT_MAX_RPC_CALLS,
            "max_timeout_ms": MAX_TIMEOUT_MS,
            "max_output_bytes": MAX_CODEMODE_OUTPUT_BYTES,
        },
    }


def _unpack_call_args(args: list[Any], primary_key: str) -> dict[str, Any]:
    """Support both lake.fn({key: val}) and lake.fn(primaryVal, {extra: ...})."""
    if not args:
        return {}
    if len(args) == 1 and isinstance(args[0], dict):
        return dict(args[0])
    out: dict[str, Any] = {}
    if len(args) >= 2 and isinstance(args[1], dict):
        out.update(args[1])
    if not isinstance(args[0], dict):
        out[primary_key] = args[0]
    else:
        out.update(args[0])
    return out


async def _dispatch_host_rpc(
    method: str,
    args: list[Any],
    *,
    runtime: LakeRuntime,
    principal: Principal,
    touched_claims: set[str],
    touched_redacted: set[str],
    touched_conflicts: set[str],
    touched_memories: set[str],
) -> Any:
    """Strictly allowlisted, caller-bound host RPC dispatcher."""
    canonical = _METHOD_ALIASES.get(method)
    if canonical is None:
        raise ValidationError(
            f"RPC method '{method}' is not in the allowlisted lake SDK",
            code="forbidden_rpc_method",
            status_code=403,
        )

    if canonical == "memoryUpsert" and not runtime.policy.is_memory_allowed(principal):
        raise SurfacePolicyError(
            "Personal memory is disabled on shared channels or service identities",
            code="surface_policy_blocked",
            status_code=403,
        )

    if canonical == "filter":
        kw = dict(args[0]) if (args and isinstance(args[0], dict)) else {}
        res = await tool_filter(
            runtime,
            principal,
            entity_types=kw.get("entity_types"),
            topic_slug=kw.get("topic_slug"),
            facets=kw.get("facets"),
            scope_types=kw.get("scope_types"),
            epistemic_statuses=kw.get("epistemic_statuses"),
            as_of=kw.get("as_of"),
            recorded_as_of=kw.get("recorded_as_of"),
            limit=int(kw.get("limit", 50)),
            record_receipt=False,
        )
    elif canonical == "search":
        kw = _unpack_call_args(args, "query")
        res = await tool_search(
            runtime,
            principal,
            query=str(kw.get("query", "")),
            entity_types=kw.get("entity_types"),
            topic_slug=kw.get("topic_slug"),
            facets=kw.get("facets"),
            as_of=kw.get("as_of"),
            recorded_as_of=kw.get("recorded_as_of"),
            limit=int(kw.get("limit", 25)),
            record_receipt=False,
        )
    elif canonical == "read":
        kw = _unpack_call_args(args, "id_or_key")
        res = await tool_read(
            runtime,
            principal,
            id_or_key=str(kw.get("id_or_key") or kw.get("id") or kw.get("canonical_key") or ""),
            include_claims=bool(kw.get("include_claims", True)),
            include_identifiers=bool(kw.get("include_identifiers", True)),
            include_lineage=bool(kw.get("include_lineage", True)),
            include_topics=bool(kw.get("include_topics", True)),
            as_of=kw.get("as_of"),
            recorded_as_of=kw.get("recorded_as_of"),
            record_receipt=False,
        )
    elif canonical == "neighbours":
        kw = _unpack_call_args(args, "id_or_key")
        res = await tool_neighbours(
            runtime,
            principal,
            id_or_key=str(kw.get("id_or_key") or kw.get("id") or kw.get("node_id") or ""),
            depth=int(kw.get("depth", 1)),
            direction=str(kw.get("direction", "both")),
            predicates=kw.get("predicates"),
            as_of=kw.get("as_of"),
            recorded_as_of=kw.get("recorded_as_of"),
            record_receipt=False,
        )
    elif canonical == "impact":
        kw = _unpack_call_args(args, "node_id")
        res = await tool_impact(
            runtime,
            principal,
            node_id=str(kw.get("node_id") or kw.get("id_or_key") or kw.get("id") or ""),
            max_depth=int(kw.get("max_depth", 5)),
            as_of=kw.get("as_of"),
            recorded_as_of=kw.get("recorded_as_of"),
            record_receipt=False,
        )
    elif canonical == "lookup":
        if len(args) >= 2 and isinstance(args[0], str) and isinstance(args[1], str):
            kw = dict(args[2]) if len(args) >= 3 and isinstance(args[2], dict) else {}
            kw["canonical_key"] = args[0]
            kw["predicate"] = args[1]
        else:
            kw = dict(args[0]) if (args and isinstance(args[0], dict)) else {}
        res = await tool_lookup(
            runtime,
            principal,
            canonical_key=str(kw.get("canonical_key") or kw.get("key") or ""),
            predicate=str(kw.get("predicate") or ""),
            context_purpose=str(kw.get("context_purpose") or "general"),
            as_of=kw.get("as_of"),
            recorded_as_of=kw.get("recorded_as_of"),
            snapshot_version=kw.get("snapshot_version"),
            record_receipt=False,
        )
    elif canonical == "resolveContext":
        if len(args) >= 1 and isinstance(args[0], list):
            kw = dict(args[1]) if len(args) >= 2 and isinstance(args[1], dict) else {}
            kw["entity_keys"] = args[0]
        else:
            kw = dict(args[0]) if (args and isinstance(args[0], dict)) else {}
        res = await tool_resolve_context(
            runtime,
            principal,
            entity_keys=list(kw.get("entity_keys") or []),
            skill_id=kw.get("skill_id"),
            project_id=kw.get("project_id"),
            run_id=kw.get("run_id"),
            include_unverified=bool(kw.get("include_unverified", False)),
            as_of=kw.get("as_of"),
            recorded_as_of=kw.get("recorded_as_of"),
            snapshot_version=kw.get("snapshot_version"),
        )
    elif canonical == "flagKnowledgeGap":
        kw = dict(args[0]) if (args and isinstance(args[0], dict)) else {}
        res = await tool_flag_knowledge_gap(
            runtime,
            principal,
            subject_key_or_topic=str(kw.get("subject_key_or_topic") or ""),
            kind=str(kw.get("kind") or "gap"),
            description=str(kw.get("description") or ""),
            severity=str(kw.get("severity") or "medium"),
            suggested_source=kw.get("suggested_source"),
        )
    elif canonical == "proposeUpdate":
        kw = dict(args[0]) if (args and isinstance(args[0], dict)) else {}
        res = await tool_propose_update(
            runtime,
            principal,
            subject_canonical_key=str(kw.get("subject_canonical_key") or ""),
            predicate=str(kw.get("predicate") or ""),
            object_value=str(kw.get("object_value") or ""),
            rationale=str(kw.get("rationale") or ""),
            kind=str(kw.get("kind") or "observation"),
            context_purpose=str(kw.get("context_purpose") or "general"),
            evidence_uri=kw.get("evidence_uri"),
            valid_from=kw.get("valid_from"),
            valid_to=kw.get("valid_to"),
        )
    elif canonical == "memoryUpsert":
        kw = dict(args[0]) if (args and isinstance(args[0], dict)) else {}
        res = await tool_memory_upsert(
            runtime,
            principal,
            key=str(kw.get("key") or ""),
            content=str(kw.get("content") or ""),
            scope_type=str(kw.get("scope_type") or "user"),
            skill_id=kw.get("skill_id"),
            tags=kw.get("tags"),
            linked_entity_ids=kw.get("linked_entity_ids"),
            linked_topic_ids=kw.get("linked_topic_ids"),
        )
    else:  # pragma: no cover
        raise ValidationError(f"Unsupported RPC method: {canonical}")

    # Harvest receipt lineage from every sub-call into the outer Code Mode receipt.
    sub_receipt = res.get("receipt") if isinstance(res, dict) else None
    if isinstance(sub_receipt, dict):
        touched_claims.update(sub_receipt.get("claim_ids") or [])
        touched_redacted.update(sub_receipt.get("redacted_evidence_claim_ids") or [])
        touched_conflicts.update(sub_receipt.get("conflict_set_ids") or [])
        touched_memories.update(sub_receipt.get("memory_ids") or [])
    if isinstance(res, dict) and isinstance(res.get("claim"), dict) and res["claim"].get("claim_id"):
        touched_claims.add(str(res["claim"]["claim_id"]))
        if res["claim"].get("evidence_redacted"):
            touched_redacted.add(str(res["claim"]["claim_id"]))
    if isinstance(res, dict) and isinstance(res.get("memory"), dict) and res["memory"].get("memory_id"):
        touched_memories.add(str(res["memory"]["memory_id"]))
    return res


def _build_clean_node_env() -> dict[str, str]:
    """Strip ALL secrets/user env vars from Node isolate, keeping only minimal OS bootstrap vars."""
    env: dict[str, str] = {"NODE_NO_WARNINGS": "1"}
    if os.name == "nt":
        # Windows V8 / Win32 DLL initialization requires SystemRoot; ZERO secrets or user paths passed.
        env["SystemRoot"] = os.environ.get("SystemRoot", r"C:\Windows")
    return env


def _resolve_node_executable() -> str:
    node_exe = shutil.which("node")
    if node_exe:
        return node_exe
    for candidate in (r"C:\Program Files\nodejs\node.exe", "/usr/bin/node", "/usr/local/bin/node"):
        if Path(candidate).is_file():
            return candidate
    raise RuntimeError("Node.js v24 executable not found on PATH")


async def execute_code(
    code: str,
    *,
    runtime: LakeRuntime,
    principal: Principal,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    max_rpc_calls: int = DEFAULT_MAX_RPC_CALLS,
    max_output_bytes: int = MAX_CODEMODE_OUTPUT_BYTES,
) -> dict[str, Any]:
    """Execute untrusted JS inside a zero-egress Node 24 --permission isolate with caller-bound RPC."""
    if principal.revoked or await runtime.store.is_principal_revoked(principal.user_id):
        raise AuthenticationError("Principal has been revoked")

    eff_timeout_ms = max(100, min(int(timeout_ms), MAX_TIMEOUT_MS))
    memory_allowed = bool(runtime.policy.is_memory_allowed(principal))
    allowed_methods = [
        name
        for name, canonical in _METHOD_ALIASES.items()
        if canonical != "memoryUpsert" or memory_allowed
    ]

    node_exe = _resolve_node_executable()
    clean_env = _build_clean_node_env()

    touched_claims: set[str] = set()
    touched_redacted: set[str] = set()
    touched_conflicts: set[str] = set()
    touched_memories: set[str] = set()
    rpc_calls_made = 0
    rpc_trace: list[dict[str, Any]] = []

    t0 = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        node_exe,
        "--permission",
        "--max-old-space-size=64",
        str(SANDBOX_RUNNER_PATH),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=clean_env,
        limit=4 * 1024 * 1024,
    )

    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

    ok = False
    value: Any = None
    logs: list[str] = []
    error_info: dict[str, Any] | None = None
    timed_out = False

    async def _drive_protocol() -> None:
        nonlocal ok, value, logs, error_info, rpc_calls_made
        exec_frame = {
            "type": "exec",
            "code": code,
            "timeout_ms": eff_timeout_ms,
            "allowed_methods": allowed_methods,
            "memory_enabled": memory_allowed,
        }
        proc.stdin.write((json.dumps(exec_frame) + "\n").encode("utf-8"))
        await proc.stdin.drain()

        while True:
            try:
                raw_line = await proc.stdout.readline()
            except ValueError:
                ok = True
                value = {"_truncated": True, "preview": "[Output exceeded 4 MiB IPC buffer ceiling]"}
                logs.append("[truncated] output exceeded maximum IPC frame limit")
                break
            if not raw_line:
                if error_info is None and not ok:
                    stderr_bytes = await proc.stderr.read()
                    err_text = sanitize_error_message(stderr_bytes.decode("utf-8", errors="replace").strip())
                    error_info = {
                        "code": "sandbox_terminated",
                        "message": err_text or "Sandbox isolate terminated unexpectedly",
                    }
                break

            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue

            ftype = frame.get("type")
            if ftype == "rpc":
                rpc_id = frame.get("id")
                method = str(frame.get("method") or "")
                args = frame.get("args") if isinstance(frame.get("args"), list) else []
                rpc_calls_made += 1
                rpc_t0 = time.perf_counter()

                if rpc_calls_made > max_rpc_calls:
                    reply = {
                        "type": "rpc_result",
                        "id": rpc_id,
                        "ok": False,
                        "error": {
                            "code": "max_rpc_calls_exceeded",
                            "message": f"Exceeded maximum {max_rpc_calls} RPC calls per Code Mode execution",
                        },
                    }
                else:
                    try:
                        res = await _dispatch_host_rpc(
                            method,
                            args,
                            runtime=runtime,
                            principal=principal,
                            touched_claims=touched_claims,
                            touched_redacted=touched_redacted,
                            touched_conflicts=touched_conflicts,
                            touched_memories=touched_memories,
                        )
                        reply = {"type": "rpc_result", "id": rpc_id, "ok": True, "result": res}
                        rpc_trace.append(
                            {
                                "method": method,
                                "ok": True,
                                "duration_ms": round((time.perf_counter() - rpc_t0) * 1000, 2),
                            }
                        )
                    except ContextLakeError as exc:
                        reply = {
                            "type": "rpc_result",
                            "id": rpc_id,
                            "ok": False,
                            "error": {
                                "code": exc.code,
                                "message": sanitize_error_message(exc.message),
                            },
                        }
                        rpc_trace.append(
                            {
                                "method": method,
                                "ok": False,
                                "error_code": exc.code,
                                "duration_ms": round((time.perf_counter() - rpc_t0) * 1000, 2),
                            }
                        )
                    except Exception as exc:
                        reply = {
                            "type": "rpc_result",
                            "id": rpc_id,
                            "ok": False,
                            "error": {
                                "code": "rpc_error",
                                "message": sanitize_error_message(str(exc)),
                            },
                        }
                        rpc_trace.append(
                            {
                                "method": method,
                                "ok": False,
                                "error_code": "rpc_error",
                                "duration_ms": round((time.perf_counter() - rpc_t0) * 1000, 2),
                            }
                        )

                try:
                    proc.stdin.write((json.dumps(reply, default=str) + "\n").encode("utf-8"))
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    break

            elif ftype == "result":
                ok = bool(frame.get("ok"))
                value = frame.get("value")
                logs = [sanitize_error_message(str(x)) for x in (frame.get("logs") or [])]
                if not ok:
                    err = frame.get("error") or {}
                    error_info = {
                        "name": str(err.get("name", "Error")),
                        "code": err.get("code") or "sandbox_execution_error",
                        "message": sanitize_error_message(str(err.get("message", "Execution failed"))),
                    }
                break

    try:
        # Host timeout includes small startup cushion above the VM script timeout.
        host_timeout_s = (eff_timeout_ms / 1000.0) + 1.5
        await asyncio.wait_for(_drive_protocol(), timeout=host_timeout_s)
    except asyncio.TimeoutError:
        timed_out = True
        ok = False
        error_info = {
            "name": "TimeoutError",
            "code": "execution_timeout",
            "message": f"Code Mode execution exceeded {eff_timeout_ms} ms wall-clock limit",
        }
    finally:
        # Guarantee zero orphaned Node.js processes on Windows/Linux.
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            pass

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
    trunc_value, trunc_logs, trunc_meta = truncate_output_payload(
        value, logs, max_bytes=max_output_bytes
    )

    epochs = await runtime.store.get_epochs()
    receipt = make_receipt(
        principal=principal,
        data_epoch=epochs.data_epoch,
        policy_epoch=epochs.policy_epoch,
        tool_or_endpoint="mcp.codemode.execute_code",
        claim_ids=sorted(touched_claims),
        redacted_evidence_claim_ids=sorted(touched_redacted),
        conflict_set_ids=sorted(touched_conflicts),
        memory_ids=sorted(touched_memories),
    )
    await runtime.store.record_receipt(receipt)

    return {
        "ok": ok,
        "result": trunc_value,
        "logs": trunc_logs,
        "error": error_info,
        "timed_out": timed_out,
        "rpc_calls": rpc_calls_made,
        "rpc_trace": rpc_trace,
        "duration_ms": elapsed_ms,
        "truncated": trunc_meta["truncated"],
        "original_bytes": trunc_meta["original_bytes"],
        "max_bytes": trunc_meta["max_bytes"],
        "receipt_id": receipt.receipt_id,
        "data_epoch": epochs.data_epoch,
        "policy_epoch": epochs.policy_epoch,
        "receipt": receipt.model_dump(mode="json"),
    }
