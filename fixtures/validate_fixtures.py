"""Deterministic institutional fixture validator for Enterprise Context Lake.

Runs cleanly on Windows and Linux to verify:
- All required fixture files exist and parse cleanly (JSON, YAML frontmatter, CSV, PDF via pypdf)
- Strict LF line endings (no CRLF) and deterministic LF SHA-256 hashes across all text fixtures
- Complete referential integrity across entities, identifiers, lineage, positions, coverages,
  claims, N-way conflict sets, skill bindings, dynamic hierarchical SKOS topics, attachments,
  legacy skill lookup tables, historical baselines, personal memories, and watchlists
- Full coverage of all 4 ontology planes, all 5 issuer coverage statuses, PE-sponsored borrowers,
  dual-role legal party (Issuer + LP + Counterparty), historical rename/LME/merger/spin-off lineage,
  3-way admin-code conflict without overwriting approved_fact, bitemporal restatement baselines,
  all 8 principal personas (incl. Revoked and >200-group Entra overage), and unique canary tokens.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml
from pypdf import PdfReader

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "fixtures"

REQUIRED_FILES = [
    "principals.json",
    "extracts/admin_extract.json",
    "extracts/crm_extract.json",
    "extracts/oms_pms_extract.json",
    "extracts/gips_reporting_extract.json",
    "extracts/operations_and_ontology_seed.json",
    "knowledge_factory/domain_knowledge/alpha_credit_lme_playbook.md",
    "knowledge_factory/domain_knowledge/dual_role_party_policy.md",
    "knowledge_factory/skills/crm-fund-mapping/SKILL.md",
    "knowledge_factory/skills/gips-portfolio-reporting/SKILL.md",
    "knowledge_factory/skills/cashflow-wire-reconciliation/SKILL.md",
    "knowledge_factory/data_access_guidance/live_oms_pms_positions.md",
    "knowledge_factory/data_access_guidance/live_admin_nav_api.md",
    "legacy_skills/crm_fund_lookup.csv",
    "legacy_skills/reporting_group_lookup.json",
    "legacy_skills/cashflow_vehicle_lookup.csv",
    "baselines/baseline_q4_2025_pre_restatement.json",
    "baselines/baseline_q4_2025_post_restatement.json",
    "baselines/baseline_2024_pre_rename_fund_map.json",
    "uploads/alpha_credit_lp_side_letter.pdf",
    "uploads/manual_admin_code_reconciliation.csv",
    "uploads/analyst_forecast_memo_cinder_logistics.md",
]

TEXT_SUFFIXES = {".json", ".md", ".csv", ".yml", ".yaml", ".txt", ".py"}


def lf_sha256(path: Path) -> str:
    raw = path.read_bytes()
    if path.suffix.lower() in TEXT_SUFFIXES:
        raw = raw.replace(b"\r\n", b"\n")
    return hashlib.sha256(raw).hexdigest()


def parse_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise AssertionError(f"{path.relative_to(REPO_ROOT)} missing YAML frontmatter")
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        raise AssertionError(f"{path.relative_to(REPO_ROOT)} malformed YAML frontmatter")
    fm = yaml.safe_load(parts[1])
    if not isinstance(fm, dict):
        raise AssertionError(f"{path.relative_to(REPO_ROOT)} frontmatter must be a mapping")
    return fm, parts[2].strip()


def validate() -> dict[str, Any]:
    errors: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    # 1. File existence, LF hygiene & deterministic hashes
    file_hashes: dict[str, str] = {}
    for rel in REQUIRED_FILES:
        p = FIXTURES_DIR / rel
        if not p.is_file():
            errors.append(f"Missing required fixture file: fixtures/{rel}")
            continue
        raw = p.read_bytes()
        if p.suffix.lower() in TEXT_SUFFIXES:
            check(b"\r\n" not in raw, f"CRLF detected in text fixture: fixtures/{rel}")
            # Confirm LF hash == raw hash when LF-normalized
            h_raw = hashlib.sha256(raw).hexdigest()
            h_lf = lf_sha256(p)
            check(h_raw == h_lf, f"Non-deterministic LF hash in fixtures/{rel}")
            file_hashes[f"fixtures/{rel}"] = h_lf
        else:
            file_hashes[f"fixtures/{rel}"] = hashlib.sha256(raw).hexdigest()

    if errors:
        raise AssertionError("\n".join(errors))

    # 2. Load JSON extracts & principals
    principals_doc = json.loads((FIXTURES_DIR / "principals.json").read_text(encoding="utf-8"))
    admin = json.loads((FIXTURES_DIR / "extracts/admin_extract.json").read_text(encoding="utf-8"))
    crm = json.loads((FIXTURES_DIR / "extracts/crm_extract.json").read_text(encoding="utf-8"))
    oms = json.loads((FIXTURES_DIR / "extracts/oms_pms_extract.json").read_text(encoding="utf-8"))
    gips = json.loads((FIXTURES_DIR / "extracts/gips_reporting_extract.json").read_text(encoding="utf-8"))
    ops = json.loads((FIXTURES_DIR / "extracts/operations_and_ontology_seed.json").read_text(encoding="utf-8"))

    # Aggregate records
    entities: dict[str, dict[str, Any]] = {}
    entity_keys: dict[str, str] = {}
    for src_name, doc in [("admin", admin), ("crm", crm), ("oms", oms), ("ops", ops)]:
        for e in doc.get("entities", []):
            eid = e["entity_id"]
            ckey = e["canonical_key"]
            check(eid not in entities, f"Duplicate entity_id {eid} in {src_name}")
            check(ckey not in entity_keys, f"Duplicate canonical_key {ckey} in {src_name}")
            entities[eid] = e
            entity_keys[ckey] = eid

    identifiers = admin["entity_identifiers"] + crm["entity_identifiers"] + oms["entity_identifiers"]
    lineage_events = admin["lineage_events"] + oms["lineage_events"]
    positions = oms["positions"]
    coverages = oms["coverages"]
    skill_bindings = ops["skill_bindings"]
    topics = {t["topic_id"]: t for t in ops["topics"]}
    topic_attachments = ops["topic_attachments"]
    claims_list = (
        admin["structural_claims"]
        + crm["claims"]
        + oms["claims"]
        + gips["claims"]
        + ops["claims"]
    )
    claims: dict[str, dict[str, Any]] = {}
    for c in claims_list:
        cid = c["claim_id"]
        check(cid not in claims, f"Duplicate claim_id: {cid}")
        claims[cid] = c
    conflict_sets = gips["conflict_sets"]
    memories = ops["personal_memories"]
    watchlists = ops["personal_watchlists"]

    # 3. Plane 1 Ontology Checks
    entity_types_present = {e["entity_type"] for e in entities.values()}
    required_entity_types = {
        "strategy",
        "fund_family",
        "reporting_group",
        "legal_vehicle",
        "share_class",
        "ultimate_parent",
        "pe_sponsor",
        "issuer",
        "instrument",
        "legal_party",
        "client",
        "consultant",
        "commitment",
        "opportunity",
        "app",
        "skill",
        "user",
        "project",
        "data_source",
    }
    missing_types = required_entity_types - entity_types_present
    check(not missing_types, f"Missing Plane 1 entity types: {sorted(missing_types)}")

    # Dual-role legal party check (same legal_party acts as Issuer + LP + Counterparty)
    gafg_preds = {
        c["predicate"]
        for c in claims.values()
        if c["subject_id"] == "ent:party-global-atlantic"
    }
    check(
        {"acts_as_issuer", "acts_as_lp_client", "acts_as_counterparty"}.issubset(gafg_preds),
        f"Dual-role party missing required role claims: {gafg_preds}",
    )

    # All 5 issuer coverage statuses (held, watchlist, pipeline, benchmark, passed)
    cov_statuses = {c["coverage_status"] for c in coverages}
    check(
        cov_statuses == {"held", "watchlist", "pipeline", "benchmark", "passed"},
        f"Expected all 5 coverage statuses, got {cov_statuses}",
    )
    held_issuers = {p["issuer_entity_id"] for p in positions}
    for c in coverages:
        check(c["issuer_entity_id"] in entities, f"Coverage references unknown issuer {c['issuer_entity_id']}")
        check(c["analyst_entity_id"] in entities, f"Coverage references unknown analyst {c['analyst_entity_id']}")
        if c["coverage_status"] != "held":
            check(
                c["issuer_entity_id"] not in held_issuers,
                f"Unheld coverage issuer {c['issuer_entity_id']} ({c['coverage_status']}) should not have a held position",
            )

    # Lineage event types check
    lineage_types = {ev["event_type"] for ev in lineage_events}
    for req_lt in ("renamed_from", "lme_restructured_to", "merged_into", "spun_off_from"):
        check(req_lt in lineage_types, f"Missing lineage event type: {req_lt}")
    for ev in lineage_events:
        check(ev["source_entity_id"] in entities, f"Lineage source missing: {ev['source_entity_id']}")
        check(ev["target_entity_id"] in entities, f"Lineage target missing: {ev['target_entity_id']}")

    # 4. Plane 2 Referential Integrity, N-Way 3-Source Conflict & Valid Scope Difference
    for ident in identifiers:
        check(ident["entity_id"] in entities, f"Identifier references unknown entity: {ident['entity_id']}")
    for pos in positions:
        for field in ("vehicle_entity_id", "instrument_entity_id", "issuer_entity_id"):
            check(pos[field] in entities, f"Position {pos['position_id']} missing {field}: {pos[field]}")
    for sb in skill_bindings:
        check(sb["skill_entity_id"] in entities, f"SkillBinding unknown skill: {sb['skill_entity_id']}")
        check(sb["target_entity_id"] in entities, f"SkillBinding unknown target: {sb['target_entity_id']}")
    for cid, c in claims.items():
        check(c["subject_id"] in entities, f"Claim {cid} unknown subject_id: {c['subject_id']}")
        if c.get("object_entity_id"):
            check(c["object_entity_id"] in entities, f"Claim {cid} unknown object_entity_id: {c['object_entity_id']}")
        ev_uri = c.get("provenance", {}).get("evidence_uri")
        if ev_uri and ev_uri.startswith("fixtures/"):
            check((REPO_ROOT / ev_uri).is_file(), f"Claim {cid} evidence_uri missing on disk: {ev_uri}")

    # 3-way conflict check: 3 distinct sources, approved_fact untouched
    check(len(conflict_sets) >= 1, "Expected at least 1 N-way ConflictSet")
    cs = conflict_sets[0]
    check(len(cs["claim_ids"]) == 3, f"Expected 3-way conflict set, got {len(cs['claim_ids'])} claims")
    cs_sources = set()
    approved_count = 0
    for cid in cs["claim_ids"]:
        check(cid in claims, f"ConflictSet references unknown claim {cid}")
        cl = claims[cid]
        cs_sources.add(cl["provenance"]["source_system"])
        if cl["epistemic_status"] == "approved_fact" and cl["superseded_at"] is None:
            approved_count += 1
    check(len(cs_sources) == 3, f"3-way conflict must have 3 distinct sources, got {cs_sources}")
    check(approved_count == 1, f"3-way conflict must preserve exactly 1 active approved_fact, got {approved_count}")

    # 5. Plane 3 Orthogonal Facets Coverage
    domain_natures = {c["facets"]["domain_nature"] for c in claims.values()}
    modalities = {c["facets"]["modality"] for c in claims.values()}
    epistemic_temporals = {c["facets"]["epistemic_temporal"] for c in claims.values()}
    actual_stages = {c["facets"].get("actual_stage") for c in claims.values() if c["facets"].get("actual_stage")}
    ingestion_channels = {c["facets"]["ingestion_channel"] for c in claims.values()}
    check(domain_natures == {"financial", "non_financial"}, f"Incomplete domain_nature coverage: {domain_natures}")
    check(modalities == {"quantitative", "qualitative"}, f"Incomplete modality coverage: {modalities}")
    check(
        {"actual", "estimate", "forecast", "guidance", "pro_forma"}.issubset(epistemic_temporals),
        f"Incomplete epistemic_temporal coverage: {epistemic_temporals}",
    )
    check(
        {"prelim", "unaudited", "audited", "restated"}.issubset(actual_stages),
        f"Incomplete actual_stage coverage: {actual_stages}",
    )
    check(
        {"automated_feed", "manual_upload", "human_authored", "agent_extracted"}.issubset(ingestion_channels),
        f"Incomplete ingestion_channel coverage: {ingestion_channels}",
    )

    # 6. Plane 4 Dynamic Hierarchical SKOS Topics (financial & non_financial roots + 11 subtopics + emergent/merged)
    topic_slugs = {t["slug"] for t in topics.values()}
    required_topic_slugs = {
        "financial",
        "performance-returns",
        "nav-valuation",
        "cashflows-liquidity",
        "leverage-covenants",
        "fees-terms",
        "non-financial",
        "legal-side-letters",
        "esg-sustainability",
        "governance-controls",
        "operational-admin",
        "client-commercial",
        "investment-thesis",
    }
    check(required_topic_slugs.issubset(topic_slugs), f"Missing core topics: {required_topic_slugs - topic_slugs}")
    check(any(t["topic_kind"] == "emergent_candidate" for t in topics.values()), "Missing emergent_candidate topic")
    check(any(t["merged_into_topic_id"] in topics for t in topics.values() if t["merged_into_topic_id"]), "Missing merged topic")
    for t in topics.values():
        if t["broader_topic_id"]:
            check(t["broader_topic_id"] in topics, f"Topic {t['topic_id']} unknown broader_topic_id")

    # Knowledge Factory items + SKILL.md validation
    kf_ids: set[str] = set()
    skill_md_paths = [
        FIXTURES_DIR / "knowledge_factory/skills/crm-fund-mapping/SKILL.md",
        FIXTURES_DIR / "knowledge_factory/skills/gips-portfolio-reporting/SKILL.md",
        FIXTURES_DIR / "knowledge_factory/skills/cashflow-wire-reconciliation/SKILL.md",
    ]
    for sp in skill_md_paths:
        fm, body = parse_frontmatter(sp)
        check(bool(fm.get("name")), f"SKILL.md missing name: {sp}")
        tools = fm.get("metadata", {}).get("include_tools")
        check(isinstance(tools, list) and len(tools) >= 3, f"SKILL.md missing metadata.include_tools: {sp}")
        check(len(body.split()) >= 60, f"SKILL.md too terse (<60 words): {sp}")

    for kp in [
        FIXTURES_DIR / "knowledge_factory/domain_knowledge/alpha_credit_lme_playbook.md",
        FIXTURES_DIR / "knowledge_factory/domain_knowledge/dual_role_party_policy.md",
        FIXTURES_DIR / "knowledge_factory/data_access_guidance/live_oms_pms_positions.md",
        FIXTURES_DIR / "knowledge_factory/data_access_guidance/live_admin_nav_api.md",
    ]:
        fm, body = parse_frontmatter(kp)
        check(fm.get("okf_version") == "0.2", f"Knowledge item missing okf_version 0.2: {kp}")
        check(len(body.split()) >= 60, f"Knowledge item too terse (<60 words): {kp}")
        kf_ids.add(fm["id"])

    # Polymorphic topic attachments referential integrity
    att_target_types = set()
    for att in topic_attachments:
        check(att["topic_id"] in topics, f"Attachment {att['attachment_id']} unknown topic {att['topic_id']}")
        att_target_types.add(att["target_type"])
        tid = att["target_id"]
        if att["target_type"] == "claim":
            check(tid in claims, f"Attachment {att['attachment_id']} unknown claim {tid}")
        elif att["target_type"] == "artifact":
            check(tid in kf_ids, f"Attachment {att['attachment_id']} unknown artifact {tid}")
        else:
            check(tid in entities, f"Attachment {att['attachment_id']} unknown entity {tid}")
    check(
        {"fund", "claim", "issuer", "opportunity", "artifact", "entity"}.issubset(att_target_types),
        f"Incomplete polymorphic attachment types: {att_target_types}",
    )

    # 7. Legacy Skills, Baselines, and Messy Uploads (PDF, CSV, Markdown)
    for csv_rel in [
        "legacy_skills/crm_fund_lookup.csv",
        "legacy_skills/cashflow_vehicle_lookup.csv",
        "uploads/manual_admin_code_reconciliation.csv",
    ]:
        with (FIXTURES_DIR / csv_rel).open("r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
            check(len(rows) >= 2, f"CSV {csv_rel} must have >= 2 data rows")

    rep_legacy = json.loads((FIXTURES_DIR / "legacy_skills/reporting_group_lookup.json").read_text(encoding="utf-8"))
    check(len(rep_legacy["mappings"]) >= 2, "reporting_group_lookup.json must have >= 2 mappings")

    for b_rel in [
        "baselines/baseline_q4_2025_pre_restatement.json",
        "baselines/baseline_q4_2025_post_restatement.json",
        "baselines/baseline_2024_pre_rename_fund_map.json",
    ]:
        b = json.loads((FIXTURES_DIR / b_rel).read_text(encoding="utf-8"))
        check("as_of" in b and "recorded_as_of" in b, f"Baseline {b_rel} missing bitemporal timestamps")
        for ck, preds in b["expected_facts"].items():
            for _, spec in preds.items():
                if isinstance(spec, dict) and "claim_id" in spec:
                    check(spec["claim_id"] in claims, f"Baseline {b_rel} unknown claim_id {spec['claim_id']}")

    # Real PDF parse verification via pypdf
    pdf_path = FIXTURES_DIR / "uploads/alpha_credit_lp_side_letter.pdf"
    reader = PdfReader(str(pdf_path))
    check(len(reader.pages) >= 1, "Side letter PDF must have >= 1 page")
    pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    check("ADM-904X" in pdf_text, "Side letter PDF missing ADM-904X text")
    check("CANARY_SIDELETTER_MFN_FEE_4091" in pdf_text, "Side letter PDF missing canary token")
    check("(Cayman)" in pdf_text and "(MFN)" in pdf_text, "Side letter PDF parentheses did not extract cleanly")
    check("\\(" not in pdf_text and "\\)" not in pdf_text, "Side letter PDF has double-escaped parentheses")
    check(bool(reader.metadata and reader.metadata.title), "Side letter PDF missing title metadata")

    # Baseline canonical key check
    for b_rel in [
        "baselines/baseline_q4_2025_pre_restatement.json",
        "baselines/baseline_q4_2025_post_restatement.json",
        "baselines/baseline_2024_pre_rename_fund_map.json",
    ]:
        b = json.loads((FIXTURES_DIR / b_rel).read_text(encoding="utf-8"))
        for ck in b["expected_facts"]:
            check(ck in entity_keys, f"Baseline {b_rel} key must be a canonical_key, got {ck}")

    # Analyst forecast memo frontmatter
    memo_fm, memo_body = parse_frontmatter(FIXTURES_DIR / "uploads/analyst_forecast_memo_cinder_logistics.md")
    check(memo_fm.get("epistemic_temporal") == "forecast", "Analyst memo missing forecast facet")
    check(len(memo_body.split()) >= 50, "Analyst memo too terse")

    # 8. Principals (all 8 personas), Revoked empty memory/watchlist, >200-group overage & Unique Canaries
    principals = {p["user_id"]: p for p in principals_doc["principals"]}
    check(len(principals) == 8, f"Expected exactly 8 principal personas, got {len(principals)}")
    check(any(p.get("revoked") is True for p in principals.values()), "Missing revoked principal")
    overage_users = [p for p in principals.values() if p.get("groups_overage") is True]
    check(len(overage_users) == 1, "Expected 1 >200-group Entra overage principal")
    ov = overage_users[0]
    total_ov_groups = len(ov["directory_core_groups"]) + int(ov["directory_extra_group_count"])
    check(total_ov_groups > 200, f"Overage principal must resolve >200 groups, got {total_ov_groups}")

    # Revoked user must have zero personal memories and zero watchlists
    revoked_ids = {pid for pid, p in principals.items() if p.get("revoked")}
    for m in memories:
        check(m["user_id"] in principals, f"Memory {m['memory_id']} unknown user_id")
        check(m["user_id"] not in revoked_ids, f"Revoked user {m['user_id']} must have empty memories")
    watchlist_users = set()
    for wl in watchlists:
        check(wl["user_id"] in principals, f"Watchlist unknown user_id {wl['user_id']}")
        check(wl["user_id"] not in revoked_ids, f"Revoked user {wl['user_id']} must have empty watchlist")
        watchlist_users.add(wl["user_id"])
        for eid in wl["entity_ids"]:
            check(eid in entities, f"Watchlist for {wl['user_id']} unknown entity {eid}")
        for tid in wl["topic_ids"]:
            check(tid in topics, f"Watchlist for {wl['user_id']} unknown topic {tid}")
        for sid in wl["source_ids"]:
            check(sid in entities, f"Watchlist for {wl['user_id']} unknown source {sid}")
    for req_wl_user in ("user:fund-steward", "user:pm-credit", "user:public-analyst"):
        check(req_wl_user in watchlist_users, f"Missing starter watchlist for {req_wl_user}")

    # Canary uniqueness & planting verification
    canaries = principals_doc["canaries"]
    tokens = [c["token"] for c in canaries]
    check(len(tokens) == len(set(tokens)) and len(tokens) >= 5, f"Canary tokens must be unique (>=5), got {tokens}")
    corpus_blob = json.dumps({"claims": claims_list, "memories": memories}) + "\n" + pdf_text
    for tok in tokens:
        check(tok in corpus_blob, f"Canary token not planted in fixtures: {tok}")

    # 9. Automated ADR-003 Policy Matrix Verification Pass
    def resolved_groups(p: dict[str, Any]) -> set[str]:
        if p.get("groups_overage"):
            core = list(p["directory_core_groups"])
            prefix = p["directory_extra_group_prefix"]
            extra = [f"{prefix}{i:03d}" for i in range(1, int(p["directory_extra_group_count"]) + 1)]
            return set(core + extra)
        return set(p.get("groups", []))

    def wall_allows(principal_wall: str, record_wall: str) -> bool:
        if principal_wall == "above_the_wall":
            return True
        if principal_wall == "private_side":
            return record_wall in ("public_side", "private_side")
        return record_wall == "public_side"

    def can_read_entity(p: dict[str, Any], ent: dict[str, Any]) -> bool:
        if p.get("revoked"):
            return False
        if not wall_allows(p["wall_side"], ent["wall_side"]):
            return False
        return bool(resolved_groups(p) & set(ent["read_acl_groups"]))

    def can_read_claim(p: dict[str, Any], clm: dict[str, Any], *, channel_mode: str = "direct", actor_mode: str = "obo") -> bool:
        if p.get("revoked"):
            return False
        if clm["scope_type"] in ("user", "user_x_skill"):
            if channel_mode != "direct" or actor_mode != "obo":
                return False
            if clm["scope_type"] == "user" and clm["scope_id"] != p["user_id"]:
                return False
            if clm["scope_type"] == "user_x_skill" and not clm["scope_id"].startswith(f"{p['user_id']}:"):
                return False
        acl = clm["acl"]
        if not wall_allows(p["wall_side"], acl["wall_side"]):
            return False
        return bool(resolved_groups(p) & set(acl["read_acl_groups"]))

    def can_read_evidence(p: dict[str, Any], clm: dict[str, Any]) -> bool:
        if not can_read_claim(p, clm):
            return False
        ev_groups = clm["acl"].get("evidence_acl_groups") or []
        return (not ev_groups) or bool(resolved_groups(p) & set(ev_groups))

    def can_steward_write(p: dict[str, Any], ent: dict[str, Any]) -> bool:
        if p.get("revoked"):
            return False
        if not wall_allows(p["wall_side"], ent["wall_side"]):
            return False
        return bool(resolved_groups(p) & set(ent["write_acl_groups"]))

    memories_by_id = {m["memory_id"]: m for m in memories}
    policy_checks = 0
    for c_spec in canaries:
        cat = c_spec["category"]
        planted = c_spec["planted_in"]
        if cat == "mnpi_private_side":
            clm = claims[planted]
            for uid in c_spec["authorized_users"]:
                policy_checks += 1
                check(can_read_claim(principals[uid], clm), f"Policy mismatch: {uid} MUST read {planted} ({c_spec['token']})")
            for uid in c_spec["forbidden_users"]:
                policy_checks += 1
                check(not can_read_claim(principals[uid], clm), f"Policy mismatch: {uid} MUST NOT read {planted} ({c_spec['token']})")
        elif cat == "restricted_evidence":
            clm = claims[planted]
            for uid in c_spec["authorized_evidence_users"]:
                policy_checks += 1
                check(can_read_evidence(principals[uid], clm), f"Policy mismatch: {uid} MUST read evidence on {planted}")
            for uid in c_spec["redacted_stub_users"]:
                policy_checks += 1
                check(
                    can_read_claim(principals[uid], clm) and not can_read_evidence(principals[uid], clm),
                    f"Policy mismatch: {uid} MUST see claim {planted} with redacted evidence",
                )
            for uid in c_spec["forbidden_users"]:
                policy_checks += 1
                check(not can_read_claim(principals[uid], clm), f"Policy mismatch: {uid} MUST NOT read claim {planted}")
        elif cat == "personal_memory":
            mem = memories_by_id[planted]
            for uid in c_spec["authorized_users"]:
                policy_checks += 1
                check(not principals[uid].get("revoked") and mem["user_id"] == uid, f"Memory owner mismatch: {uid} vs {planted}")
            for uid in c_spec["forbidden_users"]:
                policy_checks += 1
                check(mem["user_id"] != uid or principals[uid].get("revoked"), f"Forbidden user {uid} owns {planted}")

    # Verify expected_visibility matrix against ADR-003 rules
    alpha_offshore = entities[entity_keys["veh:alpha-credit-offshore-lp"]]
    for uid, exp in principals_doc["expected_visibility"].items():
        p = principals[uid]
        for ck in exp.get("can_see_entities", []):
            policy_checks += 1
            check(can_read_entity(p, entities[entity_keys[ck]]), f"expected_visibility mismatch: {uid} should see {ck}")
        for ck in exp.get("cannot_see_entities", []):
            policy_checks += 1
            check(not can_read_entity(p, entities[entity_keys[ck]]), f"expected_visibility mismatch: {uid} must NOT see {ck}")
        for cid in exp.get("redacted_evidence_claims", []):
            policy_checks += 1
            check(
                can_read_claim(p, claims[cid]) and not can_read_evidence(p, claims[cid]),
                f"expected_visibility mismatch: {uid} should get redacted evidence on {cid}",
            )
        for cid in exp.get("can_see_evidence_claims", []):
            policy_checks += 1
            check(can_read_evidence(p, claims[cid]), f"expected_visibility mismatch: {uid} should read evidence on {cid}")
        if "can_force_supersede" in exp:
            policy_checks += 1
            check(
                can_steward_write(p, alpha_offshore) == exp["can_force_supersede"],
                f"expected_visibility mismatch: {uid} steward write mismatch on alpha-credit-offshore-lp",
            )

    # Verify user_x_skill claim gating on shared channel
    pm_pref_claim = claims["clm-dir-user-pm-format-pref"]
    check(can_read_claim(principals["user:pm-credit"], pm_pref_claim, channel_mode="direct"), "PM must read own user_x_skill claim in direct channel")
    check(not can_read_claim(principals["user:pm-credit"], pm_pref_claim, channel_mode="shared"), "PM user_x_skill claim MUST be hidden on shared channel")
    check(not can_read_claim(principals["user:public-analyst"], pm_pref_claim, channel_mode="direct"), "Other user MUST NOT read PM user_x_skill claim")

    if errors:
        raise AssertionError("Fixture validation failed:\n- " + "\n- ".join(errors))

    return {
        "status": "ok",
        "files_verified": len(REQUIRED_FILES),
        "entities": len(entities),
        "entity_types": len(entity_types_present),
        "identifiers": len(identifiers),
        "lineage_events": len(lineage_events),
        "positions": len(positions),
        "coverages": len(coverages),
        "claims": len(claims),
        "conflict_sets": len(conflict_sets),
        "skill_bindings": len(skill_bindings),
        "topics": len(topics),
        "topic_attachments": len(topic_attachments),
        "principals": len(principals),
        "overage_group_count": total_ov_groups,
        "personal_memories": len(memories),
        "personal_watchlists": len(watchlists),
        "canaries_verified": len(tokens),
        "policy_matrix_checks": policy_checks,
        "sample_lf_sha256": {
            "principals.json": file_hashes["fixtures/principals.json"][:16],
            "alpha_credit_lp_side_letter.pdf": file_hashes["fixtures/uploads/alpha_credit_lp_side_letter.pdf"][:16],
        },
    }


if __name__ == "__main__":
    try:
        summary = validate()
        print(json.dumps(summary, indent=2))
    except AssertionError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
