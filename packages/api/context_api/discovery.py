"""AX machine-readable discovery (/llms.txt, /agents) and Accept: text/markdown renderers."""

from __future__ import annotations

from typing import Any

from context_core.models import Claim, ContextBundle, Entity, KnowledgeItem


def render_llms_txt(skills: list[KnowledgeItem]) -> str:
    """Render concise machine-readable `/llms.txt` catalog (ADR-004, ADR-005)."""
    skill_lines = [
        f"- `{sk.slug}` (`context://skills/{sk.slug}`): {sk.metadata.get('description') or sk.display_name} "
        f"[include_tools: {', '.join(sk.metadata.get('include_tools', []))}]"
        for sk in skills
    ]
    lines = [
        "# Enterprise Context Lake",
        "",
        "> Bitemporal institutional knowledge substrate with fail-closed MNPI wall barriers, decoupled claim/evidence ACLs, N-way conflict resolution, dual FastMCP + Server-Side Code Mode, and caller-isolated personal memory.",
        "",
        "## Agent & MCP Endpoints",
        "- Standard Typed FastMCP (Streamable HTTP): `/mcp` (tools: `filter`, `search`, `read`, `neighbours`, `lookup`, `resolve_context`, `flag_knowledge_gap`, `propose_update`, `memory_upsert`; resources: `context://memory/me`, `context://skills/{skill_id}`)",
        "- Server-Side Code Mode FastMCP (Streamable HTTP): `/mcp/codemode` (tools: `search_sdk`, `execute_code` inside zero-egress Node 24 `--permission` isolate)",
        "- Agent Manifest: `GET /agents`",
        "",
        "## Core REST Endpoints (`/api/v1`)",
        "- `GET /healthz` / `GET /api/v1/health`: health, `data_epoch`, `policy_epoch`, active hexagonal adapters",
        "- `GET/PUT /api/v1/config/adapters`: runtime hot-swap across `store`, `retrieval`, and `llm_mode`",
        "- `POST /api/v1/context/bundle` & `GET /api/v1/context/resolve`: two-channel stratified context bundle (`facts`, `directives`, `conflicts`, `memories`, immutable `ContextAssemblyReceipt`)",
        "- `GET /api/v1/entities` & `GET /api/v1/entities/{id_or_key}`: canonical four-plane entities (supports `Accept: text/markdown`)",
        "- `GET /api/v1/search`: zero-leakage lexical + alias + facet search",
        "- `GET/POST/PATCH /api/v1/topics`: dynamic hierarchical SKOS topics (`financial` & `non_financial`, strictly post-ACL counts)",
        "- `GET /api/v1/graph` & `GET /api/v1/map/impact/{node_id}`: diagnostic graph projection & ACL-enforced reverse-BFS blast radius",
        "- `GET /api/v1/conflicts`, `GET /api/v1/conflicts/{id}/blast-radius`, `POST /api/v1/conflicts/{id}/resolve`: N-way conflict resolution (steward RBAC + TOCTOU `expected_data_epoch`)",
        "- `GET/POST/PUT/DELETE /api/v1/memory/notes`, `.../compare`, `.../promote`, `GET/PUT /api/v1/memory/watchlist`: caller-scoped personal context (auto-disabled on `channel_mode=shared` or `actor_mode=service`)",
        "- `POST /api/v1/uploads` & `GET /api/v1/inbox`: quarantined PDF/CSV/Markdown/JSON ingestion (`unverified_observation`, never auto-overwrites `approved_fact`)",
        "- `POST /api/v1/curator/run` & `GET /api/v1/curator/patches`: zero-LLM SHA-256 staleness pre-pass & wall-isolated reviewable draft diffs",
        "",
        "## Bitemporal Query Semantics",
        "- `as_of`: ISO-8601 UTC business/valid time (`valid_from <= as_of < valid_to`)",
        "- `recorded_as_of` (alias `recorded_at`): ISO-8601 UTC system/belief time (`recorded_at <= recorded_as_of < superseded_at`)",
        "",
        "## Registered SKILL.md Procedures",
        *(skill_lines or ["- None loaded"]),
        "",
    ]
    return "\n".join(lines)


def build_agents_manifest(skills: list[KnowledgeItem]) -> dict[str, Any]:
    """Build structured `GET /agents` discovery manifest (ADR-005)."""
    return {
        "name": "Enterprise Context Lake",
        "version": "0.1.0",
        "mcp_endpoint": "/mcp",
        "codemode_endpoint": "/mcp/codemode",
        "llms_txt": "/llms.txt",
        "primitive_tools": [
            "filter",
            "search",
            "read",
            "neighbours",
            "lookup",
            "resolve_context",
            "flag_knowledge_gap",
            "propose_update",
            "memory_upsert",
        ],
        "code_mode_tools": ["search_sdk", "execute_code"],
        "resources": [
            "context://memory/me",
            "context://skills/{skill_id}",
        ],
        "skills": [
            {
                "skill_id": sk.slug,
                "display_name": sk.display_name,
                "description": sk.metadata.get("description", ""),
                "include_tools": sk.metadata.get("include_tools", []),
                "resource_uri": f"context://skills/{sk.slug}",
            }
            for sk in skills
        ],
        "auth_modes": ["entra_jwt", "local_mock"],
        "actor_modes": ["obo", "service"],
        "channel_modes": ["direct", "shared"],
        "bitemporal_parameters": ["as_of", "recorded_as_of", "snapshot_version"],
    }


def render_entity_detail_markdown(detail: dict[str, Any]) -> str:
    """Render an authorized entity detail payload as clean Markdown for `Accept: text/markdown`."""
    ent = detail["entity"]
    lines = [
        f"# {ent['display_name']} (`{ent['canonical_key']}`)",
        "",
        f"- **Type:** `{ent['entity_type']}` | **Wall:** `{ent['wall_side']}` | **Status:** `{ent['status']}`",
        f"- **Receipt:** `{detail.get('receipt_id', '')}`",
    ]
    if ent.get("description"):
        lines.extend(["", ent["description"]])
    if detail.get("facts"):
        lines.extend(["", "## Facts"])
        for c in detail["facts"]:
            ev = (
                f" — *{c.get('evidence_redaction_label')}*"
                if c.get("evidence_redacted")
                else f" — `{c['provenance']['source_system']}`"
            )
            lines.append(
                f"- `{c['claim_id']}` **{c['predicate']}** ({c['context_purpose']}, {c['epistemic_status']}): `{c['object_value']}`{ev}"
            )
    if detail.get("directives"):
        lines.extend(["", "## Directives"])
        for c in detail["directives"]:
            lines.append(
                f"- `{c['claim_id']}` **{c['predicate']}** ({c['scope_type']}): {c['object_value']}"
            )
    if detail.get("conflicts"):
        lines.extend(["", "## Open Conflicts"])
        for cf in detail["conflicts"]:
            lines.append(
                f"- `{cf['conflict_set_id']}` `{cf['predicate']}` ({len(cf.get('claim_ids', []))} competing claims)"
            )
    return "\n".join(lines) + "\n"


def render_claim_markdown(claim: Claim) -> str:
    ev = (
        f"Restricted ({claim.evidence_redaction_label})"
        if claim.evidence_redacted
        else (claim.provenance.evidence_uri or claim.provenance.source_system)
    )
    return (
        f"# Claim `{claim.claim_id}`\n\n"
        f"- **Subject:** `{claim.subject_id}`\n"
        f"- **Predicate:** `{claim.predicate}` (`{claim.context_purpose}`)\n"
        f"- **Value:** `{claim.object_value}`\n"
        f"- **Status:** `{claim.epistemic_status}` (`{claim.channel}`)\n"
        f"- **Evidence:** {ev}\n"
    )


def render_bundle_markdown(bundle: ContextBundle) -> str:
    lines = [
        f"# Context Bundle (`{bundle.receipt.receipt_id}`)",
        "",
        f"- **Source:** `{bundle.source}` | **Epoch:** `d{bundle.receipt.data_epoch}/p{bundle.receipt.policy_epoch}`",
        f"- **Facts:** {len(bundle.facts)} | **Directives:** {len(bundle.directives)} | **Conflicts:** {len(bundle.conflicts)} | **Memories:** {len(bundle.memories)}",
        "",
    ]
    if bundle.facts:
        lines.append("## Facts")
        for c in bundle.facts:
            lines.append(f"- `{c.claim_id}` {c.predicate} = `{c.object_value}` ({c.epistemic_status})")
        lines.append("")
    if bundle.directives:
        lines.append("## Directives")
        for c in bundle.directives:
            lines.append(f"- `{c.claim_id}` {c.predicate} ({c.scope_type}): {c.object_value}")
        lines.append("")
    return "\n".join(lines)
