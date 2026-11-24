"""Viewport-grounded Q&A agent built on LangGraph / DeepAgents + Gemini API + deterministic offline mode."""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from context_core.compression import ensure_utc
from context_core.models import (
    Claim,
    ConflictSet,
    ContextAssemblyReceipt,
    Entity,
    Principal,
)
from context_core.ports import GraphProjectionPort, PolicyPort, RetrievalPort, StorePort
from context_core.receipts import build_receipt
from context_core.tracing import langsmith_sdk_tracing_scope, traced

from context_agents.backends import create_context_composite_backend, load_skill_include_tools

# Load optional .env without overriding explicitly configured process env vars.
load_dotenv(override=False)


class ViewportContext(BaseModel):
    """Active UI viewport slice passed to the docked Viewport Agent (ADR-005)."""

    lens: str | None = None
    visible_entity_ids: list[str] = Field(default_factory=list)
    selected_entity_id: str | None = None
    active_filters: dict[str, Any] = Field(default_factory=dict)
    skill_id: str | None = None
    as_of: datetime | None = None
    recorded_as_of: datetime | None = None


class AgentCitation(BaseModel):
    """Clickable inline citation pointing to an authorized entity, claim, or conflict."""

    kind: Literal["entity", "claim", "conflict"]
    id: str
    label: str


class AgentToolCallMetric(BaseModel):
    """Lightweight tool execution metric displayed in the Viewport Agent footer."""

    tool: str
    duration_ms: float


class ViewportAgentResponse(BaseModel):
    """Structured response matching ADR-005 `POST /api/v1/agent/chat` & `/api/v1/agent/query`."""

    answer_markdown: str
    citations: list[AgentCitation] = Field(default_factory=list)
    tool_calls: list[AgentToolCallMetric] = Field(default_factory=list)
    receipt: ContextAssemblyReceipt
    llm_mode_used: Literal["gemini", "deterministic"] = "deterministic"


def _resolve_gemini_api_key() -> str | None:
    key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    return key or None


def _is_langsmith_enabled() -> bool:
    return bool(os.environ.get("LANGSMITH_API_KEY", "").strip()) and (
        os.environ.get("LANGSMITH_TRACING", "").lower() in ("true", "1", "yes")
        or os.environ.get("LANGCHAIN_TRACING_V2", "").lower() in ("true", "1", "yes")
    )


def _select_effective_llm_mode(requested_mode: str) -> Literal["gemini", "deterministic"]:
    mode = (requested_mode or "auto").strip().lower()
    if mode == "deterministic":
        return "deterministic"
    if _resolve_gemini_api_key():
        return "gemini"
    return "deterministic"


class ViewportGroundedAgent:
    """Grounded Q&A agent bound strictly to the caller's authorized viewport & PolicyPort.

    - Supports Gemini (`langchain-google-genai`) + optional LangSmith tracing when API keys exist.
    - Guarantees 100% deterministic offline operation with zero API keys required.
    - Never leaks unauthorized entities, MNPI canaries, personal memories (in shared/service mode),
      or redacted evidence URIs/excerpts.
    """

    def __init__(
        self,
        *,
        store: StorePort,
        retrieval: RetrievalPort,
        policy: PolicyPort,
        graph: GraphProjectionPort,
        llm_mode: str = "auto",
        snapshot_version: int | None = None,
    ) -> None:
        self._store = store
        self._retrieval = retrieval
        self._policy = policy
        self._graph = graph
        self._llm_mode = llm_mode
        self._snapshot_version = snapshot_version

    async def answer(
        self,
        *,
        message: str,
        viewport: ViewportContext,
        principal: Principal,
    ) -> ViewportAgentResponse:
        """Assemble authorized viewport ground truth and generate a citation-grounded response."""
        with traced(
            "viewport_agent.answer",
            principal=principal,
            attributes={"context.llm_mode_requested": self._llm_mode},
        ) as span:
            response = await self._answer_untraced(message=message, viewport=viewport, principal=principal)
            if span is not None:
                span.set_attribute("context.llm_mode_used", response.llm_mode_used)
                span.set_attribute("context.citation_count", len(response.citations))
            return response

    async def _answer_untraced(
        self,
        *,
        message: str,
        viewport: ViewportContext,
        principal: Principal,
    ) -> ViewportAgentResponse:
        tool_calls: list[AgentToolCallMetric] = []
        eff_as_of = ensure_utc(viewport.as_of)
        eff_rec = ensure_utc(viewport.recorded_as_of)

        # Step 1: Resolve authorized entities in viewport (selected + visible) with zero leakage
        t0 = time.perf_counter()
        candidate_ids: list[str] = []
        if viewport.selected_entity_id:
            candidate_ids.append(viewport.selected_entity_id)
        for eid in viewport.visible_entity_ids[:25]:
            if eid not in candidate_ids:
                candidate_ids.append(eid)

        entities: list[Entity] = []
        seen_eids: set[str] = set()
        for cid in candidate_ids:
            ent = await self._store.get_entity(cid, principal=principal)
            if ent is not None and ent.entity_id not in seen_eids:
                seen_eids.add(ent.entity_id)
                entities.append(ent)
        tool_calls.append(
            AgentToolCallMetric(tool="viewport_entities", duration_ms=round((time.perf_counter() - t0) * 1000, 2))
        )

        # Step 2: Authorized lexical search for the user's question
        t1 = time.perf_counter()
        search_hits = await self._retrieval.search(message, principal=principal, limit=10)
        for hit in search_hits:
            if hit.hit_type == "entity" and hit.id not in seen_eids:
                ent = await self._store.get_entity(hit.id, principal=principal)
                if ent is not None:
                    seen_eids.add(ent.entity_id)
                    entities.append(ent)
        tool_calls.append(
            AgentToolCallMetric(tool="retrieval.search", duration_ms=round((time.perf_counter() - t1) * 1000, 2))
        )

        # Step 3: Load caller-authorized claims (with server-side evidence redaction) & open conflicts
        t2 = time.perf_counter()
        entity_ids = [e.entity_id for e in entities[:12]]
        claims: list[Claim] = []
        if entity_ids:
            claims = await self._store.query_claims(
                principal=principal,
                subject_ids=entity_ids,
                as_of=eff_as_of,
                recorded_as_of=eff_rec,
            )
        conflicts: list[ConflictSet] = []
        all_open = await self._store.list_conflicts(principal=principal, status="open")
        for cf in all_open:
            if not entity_ids or cf.subject_id in seen_eids or "conflict" in message.lower():
                conflicts.append(cf)
        tool_calls.append(
            AgentToolCallMetric(tool="store.query_claims", duration_ms=round((time.perf_counter() - t2) * 1000, 2))
        )

        # Step 4: Optional reverse impact summary when question mentions impact/blast/downstream/conflict
        impact_summary: str | None = None
        q_low = message.lower()
        if entities and any(k in q_low for k in ("impact", "blast", "downstream", "affected", "skill", "conflict")):
            t3 = time.perf_counter()
            try:
                imp = await self._graph.impact_traversal(
                    entities[0].entity_id,
                    principal=principal,
                    max_depth=5,
                    as_of=eff_as_of,
                    recorded_as_of=eff_rec,
                )
                impact_summary = (
                    f"Reverse impact from `{entities[0].canonical_key}`: "
                    f"{len(imp.layers.positions)} positions, {len(imp.layers.funds)} funds, "
                    f"{len(imp.layers.clients_opportunities)} client/opportunity records, "
                    f"{len(imp.layers.skills_apps)} skills/apps"
                )
                if imp.layers.watchlist_hits:
                    impact_summary += f", {len(imp.layers.watchlist_hits)} personal watchlist hits"
            except Exception:
                impact_summary = None
            tool_calls.append(
                AgentToolCallMetric(tool="graph.impact_traversal", duration_ms=round((time.perf_counter() - t3) * 1000, 2))
            )

        # Step 5: Personal memory grounding ONLY if allowed on caller's surface
        memories = []
        if self._policy.is_memory_allowed(principal):
            t4 = time.perf_counter()
            all_mems = await self._store.list_memories(principal=principal)
            memories = [
                m
                for m in all_mems
                if any(eid in seen_eids for eid in m.linked_entity_ids)
                or any(tok in m.content.lower() or tok in m.key.lower() for tok in q_low.split() if len(tok) > 3)
            ][:5]
            tool_calls.append(
                AgentToolCallMetric(tool="memory.list", duration_ms=round((time.perf_counter() - t4) * 1000, 2))
            )

        # Step 6: Build immutable ContextAssemblyReceipt BEFORE generation
        epochs = await self._store.get_epochs()
        receipt = build_receipt(
            principal=principal,
            epochs=epochs,
            as_of=eff_as_of,
            recorded_as_of=eff_rec,
            claims=claims,
            conflicts=conflicts,
            memories=memories,
            snapshot_version=self._snapshot_version,
            tool_or_endpoint="agent.viewport_chat",
        )
        await self._store.record_receipt(receipt)

        # Step 7: Build verified citations strictly from authorized objects
        citations: list[AgentCitation] = []
        seen_cite: set[tuple[str, str]] = set()

        def _add_cite(kind: Literal["entity", "claim", "conflict"], cid: str, label: str) -> None:
            if (kind, cid) not in seen_cite:
                seen_cite.add((kind, cid))
                citations.append(AgentCitation(kind=kind, id=cid, label=label))

        for ent in entities[:6]:
            _add_cite("entity", ent.canonical_key, ent.display_name)
        for cf in conflicts[:4]:
            _add_cite("conflict", cf.conflict_set_id, f"Conflict: {cf.predicate} ({len(cf.claim_ids)} sources)")
        for clm in claims[:8]:
            _add_cite("claim", clm.claim_id, f"{clm.predicate}: {clm.object_value}")

        effective_mode = _select_effective_llm_mode(self._llm_mode)
        answer_md: str | None = None
        used_mode: Literal["gemini", "deterministic"] = "deterministic"

        if effective_mode == "gemini":
            t_llm = time.perf_counter()
            answer_md = await self._try_gemini_answer(
                message=message,
                entities=entities,
                claims=claims,
                conflicts=conflicts,
                impact_summary=impact_summary,
                receipt=receipt,
                principal=principal,
            )
            if answer_md:
                used_mode = "gemini"
                tool_calls.append(
                    AgentToolCallMetric(tool="llm.gemini", duration_ms=round((time.perf_counter() - t_llm) * 1000, 2))
                )

        if not answer_md:
            answer_md = self._build_deterministic_answer(
                message=message,
                entities=entities,
                claims=claims,
                conflicts=conflicts,
                impact_summary=impact_summary,
                memories=memories,
                principal=principal,
                receipt=receipt,
            )
            used_mode = "deterministic"

        return ViewportAgentResponse(
            answer_markdown=answer_md,
            citations=citations,
            tool_calls=tool_calls,
            receipt=receipt,
            llm_mode_used=used_mode,
        )

    async def _try_gemini_answer(
        self,
        *,
        message: str,
        entities: list[Entity],
        claims: list[Claim],
        conflicts: list[ConflictSet],
        impact_summary: str | None,
        receipt: ContextAssemblyReceipt,
        principal: Principal | None = None,
    ) -> str | None:
        api_key = _resolve_gemini_api_key()
        if not api_key:
            return None
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            from langchain_google_genai import ChatGoogleGenerativeAI

            model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
            llm = ChatGoogleGenerativeAI(model=model_name, google_api_key=api_key, temperature=0.0)

            context_lines = [
                "Authorized Viewport Context (cite ONLY using [entity:canonical_key], [claim:claim_id], [conflict:conflict_set_id]):"
            ]
            for ent in entities[:6]:
                context_lines.append(
                    f"- [entity:{ent.canonical_key}] {ent.display_name} ({ent.entity_type.value}): {ent.description}"
                )
            for clm in claims[:12]:
                ev_note = (
                    "evidence=Restricted — Request Access"
                    if clm.evidence_redacted
                    else f"source={clm.provenance.source_system}"
                )
                context_lines.append(
                    f"- [claim:{clm.claim_id}] {clm.predicate}={clm.object_value} "
                    f"({clm.epistemic_status}, purpose={clm.context_purpose}, {ev_note})"
                )
            for cf in conflicts[:4]:
                context_lines.append(
                    f"- [conflict:{cf.conflict_set_id}] open conflict on {cf.predicate} across {len(cf.claim_ids)} claims"
                )
            if impact_summary:
                context_lines.append(f"- Impact: {impact_summary}")

            config: dict[str, Any] = {
                "metadata": {
                    "receipt_id": receipt.receipt_id,
                    "snapshot_version": receipt.snapshot_version,
                    "as_of": receipt.as_of.isoformat(),
                    "recorded_as_of": receipt.recorded_as_of.isoformat(),
                    "data_epoch": receipt.data_epoch,
                    "policy_epoch": receipt.policy_epoch,
                },
                "tags": ["enterprise-context-lake", "viewport-agent"],
            }
            if _is_langsmith_enabled():
                config["run_name"] = f"viewport-agent-{receipt.receipt_id}"

            # Private-side and above-the-wall context never reaches LangSmith via SDK callbacks.
            with langsmith_sdk_tracing_scope(principal):
                resp = await llm.ainvoke(
                    [
                        SystemMessage(
                            content=(
                                "You are the Enterprise Context Lake viewport analyst. Answer strictly from the "
                                "provided caller-authorized context. Cite entities as [entity:<key>], claims as "
                                "[claim:<id>], and conflicts as [conflict:<id>]. Never fabricate evidence URIs "
                                "or bypass redacted evidence labels."
                            )
                        ),
                        HumanMessage(content="\n".join(context_lines) + f"\n\nQuestion: {message}"),
                    ],
                    config=config,
                )
            content = resp.content
            if isinstance(content, str) and content.strip():
                return content.strip()
            return None
        except Exception:
            return None

    def _build_deterministic_answer(
        self,
        *,
        message: str,
        entities: list[Entity],
        claims: list[Claim],
        conflicts: list[ConflictSet],
        impact_summary: str | None,
        memories: list[Any],
        principal: Principal,
        receipt: ContextAssemblyReceipt,
    ) -> str:
        if not entities and not claims and not conflicts:
            return (
                f"No authorized records in the current viewport matched `{message}` "
                f"under wall `{principal.wall_side.value}`.\n\n"
                f"_Receipt `{receipt.receipt_id}` · epoch `d{receipt.data_epoch}/p{receipt.policy_epoch}`_"
            )

        lines: list[str] = []
        if entities:
            primary = entities[0]
            lines.append(f"### Grounded Context: {primary.display_name} `[entity:{primary.canonical_key}]`")
            if primary.description:
                lines.append(primary.description)
            if len(entities) > 1:
                related = ", ".join(f"`[entity:{e.canonical_key}]`" for e in entities[1:5])
                lines.append(f"**Related in viewport:** {related}")
            lines.append("")

        facts = [c for c in claims if c.channel == "fact"]
        directives = [c for c in claims if c.channel == "directive"]

        if facts:
            lines.append("**Authoritative & Disputed Facts**")
            for clm in facts[:6]:
                if clm.evidence_redacted:
                    prov_note = f" · Evidence: *{clm.evidence_redaction_label or 'Restricted — Request Access'}*"
                else:
                    prov_note = f" · Source: `{clm.provenance.source_system}`"
                lines.append(
                    f"- `{clm.predicate}` = **`{clm.object_value}`** "
                    f"(`{clm.epistemic_status}`, scope `{clm.context_purpose}`) `[claim:{clm.claim_id}]`{prov_note}"
                )
            lines.append("")

        if conflicts:
            lines.append("**Open N-Way Conflicts**")
            for cf in conflicts[:3]:
                sources = sorted({c.provenance.source_system for c in cf.claims}) if cf.claims else []
                src_text = f" across {', '.join(sources)}" if sources else ""
                lines.append(
                    f"- `[conflict:{cf.conflict_set_id}]` `{cf.predicate}` ({len(cf.claim_ids)} claims{src_text}) — "
                    f"active `approved_fact` preserved pending steward resolution."
                )
            lines.append("")

        if directives:
            lines.append("**Applicable Directives**")
            for d in directives[:3]:
                mand = "mandatory" if d.is_mandatory else d.scope_type
                lines.append(f"- `{d.predicate}` ({mand}): {d.object_value} `[claim:{d.claim_id}]`")
            lines.append("")

        if impact_summary:
            lines.append(f"**Impact Analysis:** {impact_summary}")
            lines.append("")

        if memories and self._policy.is_memory_allowed(principal):
            lines.append(f"**Personal Notes (`{principal.user_id}`):** {len(memories)} matching note(s) in scope.")
            lines.append("")

        snap_part = f" · snapshot `v{receipt.snapshot_version}`" if receipt.snapshot_version else ""
        lines.append(
            f"_Receipt `{receipt.receipt_id}`{snap_part} · epoch `d{receipt.data_epoch}/p{receipt.policy_epoch}`_"
        )
        return "\n".join(lines).strip()


async def build_deep_lake_agent_bundle(
    *,
    store: StorePort,
    policy: PolicyPort,
    principal: Principal,
    skill_slug: str | None = None,
) -> dict[str, Any]:
    """Assemble DeepAgents CompositeBackend + SKILL.md `metadata.include_tools` activation metadata."""
    backend = create_context_composite_backend(store, policy, principal)
    include_tools: list[str] = []
    if skill_slug:
        include_tools = await load_skill_include_tools(store, principal, skill_slug)
    return {
        "backend": backend,
        "skill_slug": skill_slug,
        "include_tools": include_tools,
        "memory_enabled": policy.is_memory_allowed(principal),
        "mounts": ["/knowledge/", "/skills/", "/memories/", "/"],
    }
