"""Deterministic fixture seeder loading fixtures/ cleanly into ANY StorePort implementation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from context_core.compression import lf_sha256_file, to_posix_path
from context_core.models import (
    Claim,
    ConflictSet,
    Coverage,
    Entity,
    EntityIdentifier,
    EntityLineageEvent,
    KnowledgeItem,
    MemoryRecord,
    Position,
    SkillBinding,
    Topic,
    TopicAttachment,
    WallSide,
    WatchlistRecord,
)
from context_core.ports import StorePort


def _parse_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}, text
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        return {}, text
    fm = yaml.safe_load(parts[1]) or {}
    return (fm if isinstance(fm, dict) else {}), parts[2].strip()


def load_fixture_bundle(repo_root: Path | None = None) -> dict[str, Any]:
    """Parse all canonical Wave 0 fixtures into validated Pydantic domain objects."""
    root = repo_root or Path(__file__).resolve().parents[3]
    fdir = root / "fixtures"

    principals_doc = json.loads((fdir / "principals.json").read_text(encoding="utf-8"))
    admin = json.loads((fdir / "extracts/admin_extract.json").read_text(encoding="utf-8"))
    crm = json.loads((fdir / "extracts/crm_extract.json").read_text(encoding="utf-8"))
    oms = json.loads((fdir / "extracts/oms_pms_extract.json").read_text(encoding="utf-8"))
    gips = json.loads((fdir / "extracts/gips_reporting_extract.json").read_text(encoding="utf-8"))
    ops = json.loads((fdir / "extracts/operations_and_ontology_seed.json").read_text(encoding="utf-8"))

    entities = [
        Entity.model_validate(raw)
        for doc in (admin, crm, oms, ops)
        for raw in doc.get("entities", [])
    ]
    identifiers = [
        EntityIdentifier.model_validate(raw)
        for doc in (admin, crm, oms)
        for raw in doc.get("entity_identifiers", [])
    ]
    lineage_events = [
        EntityLineageEvent.model_validate(raw)
        for doc in (admin, oms)
        for raw in doc.get("lineage_events", [])
    ]
    positions = [Position.model_validate(raw) for raw in oms.get("positions", [])]
    coverages = [Coverage.model_validate(raw) for raw in oms.get("coverages", [])]
    skill_bindings = [SkillBinding.model_validate(raw) for raw in ops.get("skill_bindings", [])]
    topics = [Topic.model_validate(raw) for raw in ops.get("topics", [])]
    topic_attachments = [
        TopicAttachment.model_validate(raw) for raw in ops.get("topic_attachments", [])
    ]

    raw_claims = (
        admin.get("structural_claims", [])
        + crm.get("claims", [])
        + oms.get("claims", [])
        + gips.get("claims", [])
        + ops.get("claims", [])
    )
    claims: list[Claim] = []
    for raw in raw_claims:
        claim = Claim.model_validate(raw)
        ev_uri = claim.provenance.evidence_uri
        if ev_uri and ev_uri.startswith("fixtures/") and not claim.provenance.content_sha256:
            ev_path = root / ev_uri
            if ev_path.is_file():
                claim.provenance.content_sha256 = lf_sha256_file(ev_path)
        claims.append(claim)

    conflict_sets = [ConflictSet.model_validate(raw) for raw in gips.get("conflict_sets", [])]
    memories = [MemoryRecord.model_validate(raw) for raw in ops.get("personal_memories", [])]
    watchlists = [WatchlistRecord.model_validate(raw) for raw in ops.get("personal_watchlists", [])]

    # Knowledge Factory items (domain_knowledge, data_access_guidance, skills/*/SKILL.md)
    knowledge_items: list[KnowledgeItem] = []
    kf_dir = fdir / "knowledge_factory"
    for md_path in sorted(kf_dir.rglob("*.md")):
        fm, body = _parse_frontmatter(md_path)
        rel_path = to_posix_path(md_path.relative_to(root))
        sha = lf_sha256_file(md_path)
        if md_path.name == "SKILL.md":
            skill_name = str(fm.get("name") or md_path.parent.name)
            knowledge_items.append(
                KnowledgeItem(
                    item_id=f"kf:skill-{skill_name}",
                    knowledge_kind="skill_procedure",
                    display_name=f"SKILL: {skill_name}",
                    slug=skill_name,
                    source_path=rel_path,
                    body_markdown=body,
                    wall_side=WallSide(fm.get("wall_side", "public_side")),
                    read_acl_groups=list(fm.get("read_acl_groups", ["firm-all"])),
                    write_acl_groups=list(fm.get("write_acl_groups", ["fund-stewards"])),
                    topics=list(fm.get("topics", [])),
                    related_entities=list(fm.get("related_entities", [])),
                    metadata={
                        "name": skill_name,
                        "description": fm.get("description", ""),
                        "scope": fm.get("scope", "skill"),
                        "version": fm.get("version", "1.0.0"),
                        **(fm.get("metadata") or {}),
                    },
                    content_sha256=sha,
                    okf_version=str(fm.get("okf_version", "0.2")),
                )
            )
        else:
            item_id = str(fm.get("id") or f"kf:{md_path.stem}")
            knowledge_items.append(
                KnowledgeItem(
                    item_id=item_id,
                    knowledge_kind=fm.get("knowledge_kind", "domain_knowledge"),
                    display_name=str(fm.get("display_name") or md_path.stem),
                    slug=md_path.stem,
                    source_path=rel_path,
                    body_markdown=body,
                    wall_side=WallSide(fm.get("wall_side", "public_side")),
                    read_acl_groups=list(fm.get("read_acl_groups", ["firm-all"])),
                    write_acl_groups=list(fm.get("write_acl_groups", ["fund-stewards"])),
                    topics=list(fm.get("topics", [])),
                    related_entities=list(fm.get("related_entities", [])),
                    metadata={k: v for k, v in fm.items() if k not in {"id", "okf_version"}},
                    content_sha256=sha,
                    valid_from=fm.get("valid_from"),
                    recorded_at=fm.get("recorded_at"),
                    okf_version=str(fm.get("okf_version", "0.2")),
                )
            )

    revoked_principals = [
        (p["user_id"], p.get("revocation_reason", "Revoked in fixture seed"))
        for p in principals_doc.get("principals", [])
        if p.get("revoked") is True
    ]

    return {
        "entities": entities,
        "identifiers": identifiers,
        "lineage_events": lineage_events,
        "positions": positions,
        "coverages": coverages,
        "skill_bindings": skill_bindings,
        "topics": topics,
        "topic_attachments": topic_attachments,
        "claims": claims,
        "conflict_sets": conflict_sets,
        "memories": memories,
        "watchlists": watchlists,
        "knowledge_items": knowledge_items,
        "revoked_principals": revoked_principals,
    }


async def seed_store_from_fixtures(
    store: StorePort,
    *,
    repo_root: Path | None = None,
) -> dict[str, int]:
    """Deterministically seed any StorePort implementation from fixtures/."""
    await store.initialize()
    bundle = load_fixture_bundle(repo_root=repo_root)

    if hasattr(store, "import_state"):
        await store.import_state(bundle, reset_epochs=True)  # type: ignore[attr-defined]
    else:
        for e in bundle["entities"]:
            await store.upsert_entity(e)
        for ident in bundle["identifiers"]:
            await store.upsert_identifier(ident)
        for ev in bundle["lineage_events"]:
            await store.add_lineage_event(ev)
        for pos in bundle["positions"]:
            await store.upsert_position(pos)
        for cov in bundle["coverages"]:
            await store.upsert_coverage(cov)
        for sb in bundle["skill_bindings"]:
            await store.upsert_skill_binding(sb)
        for t in bundle["topics"]:
            await store.upsert_topic(t)
        for att in bundle["topic_attachments"]:
            await store.attach_topic(att)
        for ki in bundle["knowledge_items"]:
            await store.upsert_knowledge_item(ki)
        if hasattr(store, "seed_claim_raw"):
            for c in bundle["claims"]:
                await store.seed_claim_raw(c)  # type: ignore[attr-defined]
        if hasattr(store, "seed_conflict_raw"):
            for cs in bundle["conflict_sets"]:
                await store.seed_conflict_raw(cs)  # type: ignore[attr-defined]
        if hasattr(store, "seed_memory_raw"):
            for m in bundle["memories"]:
                await store.seed_memory_raw(m)  # type: ignore[attr-defined]
        if hasattr(store, "seed_watchlist_raw"):
            for wl in bundle["watchlists"]:
                await store.seed_watchlist_raw(wl)  # type: ignore[attr-defined]
        for uid, reason in bundle["revoked_principals"]:
            await store.revoke_principal(uid, reason=reason, actor="system:seed")

    return {
        "entities": len(bundle["entities"]),
        "identifiers": len(bundle["identifiers"]),
        "lineage_events": len(bundle["lineage_events"]),
        "positions": len(bundle["positions"]),
        "coverages": len(bundle["coverages"]),
        "skill_bindings": len(bundle["skill_bindings"]),
        "topics": len(bundle["topics"]),
        "topic_attachments": len(bundle["topic_attachments"]),
        "claims": len(bundle["claims"]),
        "conflict_sets": len(bundle["conflict_sets"]),
        "knowledge_items": len(bundle["knowledge_items"]),
        "memories": len(bundle["memories"]),
        "watchlists": len(bundle["watchlists"]),
        "revoked_principals": len(bundle["revoked_principals"]),
    }
