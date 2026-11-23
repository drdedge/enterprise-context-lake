"""Oracle 1: Bitemporal Historical Replay, Precedence, Receipts & SDK Isolation.

Verifies:
  1. 0-diff historical replay across all 3 legacy skills (CRM, Reporting, Cash-Flow)
     and all 3 frozen baselines in fixtures/baselines/.
  2. Strict fix of PDF Defect #2 (superseded_at IS NULL bug on historical as_of reads)
     both on seeded Q4 2025 restatement and live steward supersession.
  3. Two-channel stratified precedence (facts epistemic-tier-first vs directives
     mandatory-pinned then narrowest-scope-wins).
  4. ContextAssemblyReceipt determinism and persistence.
  5. context_sdk in-RAM sqlite3.deserialize() (zero temp SQLite files), ref-counted
     atomic swap under concurrent reader storm (PDF Defect #5), and pinned_version
     isolation under background polling (PDF Defect #6).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import csv
import inspect
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    FIXTURES_DIR,
    build_principal_from_fixture,
    load_json_fixture,
    mock_headers,
    parse_utc,
    resolve_symbol,
)


def _index_facts_by_subject_pred(facts: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    idx: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for f in facts:
        subj = f.get("subject_canonical_key") or f.get("subject_id") or ""
        pred = f.get("predicate") or ""
        idx.setdefault((subj, pred), []).append(f)
    return idx


def _find_fact(
    facts: list[dict[str, Any]],
    *,
    predicate: str,
    claim_id: str | None = None,
    epistemic_status: str | None = None,
) -> dict[str, Any]:
    matches = [f for f in facts if f.get("predicate") == predicate]
    if claim_id is not None:
        by_id = [f for f in matches if f.get("claim_id") == claim_id]
        if by_id:
            return by_id[0]
    if epistemic_status is not None:
        by_ep = [f for f in matches if f.get("epistemic_status") == epistemic_status]
        if by_ep:
            return by_ep[0]
    assert matches, f"Expected fact with predicate={predicate!r} (claim_id={claim_id!r}); got {[ (f.get('claim_id'), f.get('predicate'), f.get('object_value')) for f in facts ]}"
    return matches[0]


@pytest.mark.asyncio
async def test_baseline_2024_pre_rename_zero_diff(api_client) -> None:
    """0-diff replay of Mid-Year 2024 pre-rename fund map baseline."""
    baseline = load_json_fixture("baselines/baseline_2024_pre_rename_fund_map.json")
    as_of = baseline["as_of"]
    recorded_as_of = baseline["recorded_as_of"]
    headers = mock_headers("user:fund-steward")

    # 1. Context bundle replay at pre-rename business & system time
    resp = await api_client.post(
        "/api/v1/context/bundle",
        headers=headers,
        json={
            "entity_keys": ["veh:alpha-credit-offshore-lp", "veh:meridian-opp-credit-offshore-legacy"],
            "as_of": as_of,
            "recorded_as_of": recorded_as_of,
        },
    )
    assert resp.status_code == 200, resp.text
    bundle = resp.json()
    expected_name = baseline["expected_facts"]["veh:alpha-credit-offshore-lp"]["legal_display_name"]
    fact = _find_fact(bundle["facts"], predicate="legal_display_name", claim_id=expected_name["claim_id"])
    assert fact["object_value"] == expected_name["object_value"]
    assert fact["claim_id"] == expected_name["claim_id"]

    # 2. Verify entity card preserves lineage event & historical MOC-704 identifier
    ent_resp = await api_client.get(
        "/api/v1/entities/veh:alpha-credit-offshore-lp",
        headers=headers,
        params={"as_of": as_of, "recorded_as_of": recorded_as_of},
    )
    assert ent_resp.status_code == 200, ent_resp.text
    ent_card = ent_resp.json()
    lineage_ids = {ev["event_id"] for ev in ent_card.get("lineage", [])}
    assert "lin-alpha-offshore-rename-2024" in lineage_ids
    assert any(ev["event_type"] == "renamed_from" for ev in ent_card["lineage"])

    legacy_resp = await api_client.get(
        "/api/v1/entities/veh:meridian-opp-credit-offshore-legacy",
        headers=headers,
        params={"as_of": as_of, "recorded_as_of": recorded_as_of},
    )
    assert legacy_resp.status_code == 200, legacy_resp.text
    legacy_card = legacy_resp.json()
    admin_codes = [
        i["identifier_value"]
        for i in legacy_card.get("identifiers", [])
        if i.get("scheme") == "admin_code"
    ]
    assert baseline["expected_facts"]["veh:meridian-opp-credit-offshore-legacy"]["admin_code_identifier"] in admin_codes

    # 3. Verify post-rename business date returns new legal_display_name
    post_resp = await api_client.post(
        "/api/v1/context/bundle",
        headers=headers,
        json={
            "entity_keys": ["veh:alpha-credit-offshore-lp"],
            "as_of": "2025-12-31T23:59:59Z",
            "recorded_as_of": "2026-01-20T00:00:00Z",
        },
    )
    assert post_resp.status_code == 200, post_resp.text
    post_fact = _find_fact(post_resp.json()["facts"], predicate="legal_display_name")
    assert post_fact["object_value"] == "Alpha Credit Offshore LP (Cayman)"
    assert post_fact["claim_id"] == "clm-alpha-offshore-name-post-rename"


@pytest.mark.asyncio
async def test_q4_2025_pre_vs_post_restatement_fixes_pdf_superseded_bug(api_client) -> None:
    """Proves PDF Defect #2 (superseded_at IS NULL on historical reads) is fixed.

    Same business time (as_of=2025-12-31T23:59:59Z), different system time (recorded_as_of):
      - 2026-01-20 -> prelim NAV 1,284,500,000 & IRR 11.42% (even though superseded on 2026-02-20!)
      - 2026-02-25 -> restated NAV 1,281,900,000 & IRR 11.28%
      - Neither returns March 2026 disputed admin codes (ADM-904X / ADM-904-GIPS).
    """
    pre = load_json_fixture("baselines/baseline_q4_2025_pre_restatement.json")
    post = load_json_fixture("baselines/baseline_q4_2025_post_restatement.json")
    headers = mock_headers("user:fund-steward")

    # Pre-restatement replay
    pre_resp = await api_client.post(
        "/api/v1/context/bundle",
        headers=headers,
        json={
            "entity_keys": ["grp:alpha-credit-gips-composite", "veh:alpha-credit-offshore-lp"],
            "as_of": pre["as_of"],
            "recorded_as_of": pre["recorded_as_of"],
        },
    )
    assert pre_resp.status_code == 200, pre_resp.text
    pre_bundle = pre_resp.json()
    pre_facts = pre_bundle["facts"]

    exp_pre_nav = pre["expected_facts"]["grp:alpha-credit-gips-composite"]["composite_nav_usd"]
    exp_pre_irr = pre["expected_facts"]["grp:alpha-credit-gips-composite"]["composite_net_irr_pct"]
    exp_pre_code = pre["expected_facts"]["veh:alpha-credit-offshore-lp"]["has_admin_code"]

    nav_pre = _find_fact(pre_facts, predicate="composite_nav_usd")
    irr_pre = _find_fact(pre_facts, predicate="composite_net_irr_pct")
    code_pre = _find_fact(pre_facts, predicate="has_admin_code")

    assert nav_pre["object_value"] == exp_pre_nav["object_value"]
    assert nav_pre["claim_id"] == exp_pre_nav["claim_id"]
    assert nav_pre["facets"]["actual_stage"] == exp_pre_nav["actual_stage"]

    assert irr_pre["object_value"] == exp_pre_irr["object_value"]
    assert irr_pre["claim_id"] == exp_pre_irr["claim_id"]
    assert irr_pre["facets"]["actual_stage"] == exp_pre_irr["actual_stage"]

    assert code_pre["object_value"] == exp_pre_code["object_value"]
    assert code_pre["claim_id"] == exp_pre_code["claim_id"]

    # Must NOT leak March 2026 disputed codes into January 2026 system time
    pre_code_values = {f["object_value"] for f in pre_facts if f["predicate"] == "has_admin_code"}
    assert "ADM-904X" not in pre_code_values
    assert "ADM-904-GIPS" not in pre_code_values

    # Post-restatement replay (same as_of, later recorded_as_of)
    post_resp = await api_client.post(
        "/api/v1/context/bundle",
        headers=headers,
        json={
            "entity_keys": ["grp:alpha-credit-gips-composite", "veh:alpha-credit-offshore-lp"],
            "as_of": post["as_of"],
            "recorded_as_of": post["recorded_as_of"],
        },
    )
    assert post_resp.status_code == 200, post_resp.text
    post_facts = post_resp.json()["facts"]

    exp_post_nav = post["expected_facts"]["grp:alpha-credit-gips-composite"]["composite_nav_usd"]
    exp_post_irr = post["expected_facts"]["grp:alpha-credit-gips-composite"]["composite_net_irr_pct"]

    nav_post = _find_fact(post_facts, predicate="composite_nav_usd")
    irr_post = _find_fact(post_facts, predicate="composite_net_irr_pct")

    assert nav_post["object_value"] == exp_post_nav["object_value"]
    assert nav_post["claim_id"] == exp_post_nav["claim_id"]
    assert nav_post["facets"]["actual_stage"] == exp_post_nav["actual_stage"]

    assert irr_post["object_value"] == exp_post_irr["object_value"]
    assert irr_post["claim_id"] == exp_post_irr["claim_id"]
    assert irr_post["facets"]["actual_stage"] == exp_post_irr["actual_stage"]

    # Confirm strict separation between pre and post restatement
    assert nav_pre["object_value"] != nav_post["object_value"]
    assert irr_pre["object_value"] != irr_post["object_value"]


@pytest.mark.asyncio
async def test_live_steward_supersession_preserves_bitemporal_history(api_client) -> None:
    """Live force_supersede must NOT break historical recorded_as_of reads."""
    headers = mock_headers("user:fund-steward")
    t_before = datetime.now(timezone.utc)

    # Create an initial approved fact
    create_resp = await api_client.post(
        "/api/v1/claims",
        headers=headers,
        json={
            "subject_canonical_key": "veh:alpha-credit-master-lp",
            "predicate": "official_nav_audit_signoff",
            "object_value": "SIGNOFF-V1-PRELIM",
            "context_purpose": "legal_vehicle",
            "scope_type": "firm",
            "scope_id": "firm",
            "channel": "fact",
            "epistemic_status": "approved_fact",
            "source_system": "admin-feed",
            "valid_from": "2026-01-01T00:00:00Z",
            "force_supersede": True,
        },
    )
    assert create_resp.status_code in (200, 201), create_resp.text
    v1_claim = create_resp.json()["claim"]
    t_between = datetime.now(timezone.utc)
    await asyncio.sleep(0.05)

    # Supersede with v2
    sup_resp = await api_client.post(
        "/api/v1/claims",
        headers=headers,
        json={
            "subject_canonical_key": "veh:alpha-credit-master-lp",
            "predicate": "official_nav_audit_signoff",
            "object_value": "SIGNOFF-V2-FINAL",
            "context_purpose": "legal_vehicle",
            "scope_type": "firm",
            "scope_id": "firm",
            "channel": "fact",
            "epistemic_status": "approved_fact",
            "source_system": "admin-feed",
            "valid_from": "2026-01-01T00:00:00Z",
            "force_supersede": True,
        },
    )
    assert sup_resp.status_code in (200, 201), sup_resp.text
    t_after = datetime.now(timezone.utc)

    # Query at t_between -> MUST return V1 (even though now superseded!)
    r_mid = await api_client.post(
        "/api/v1/context/bundle",
        headers=headers,
        json={
            "entity_keys": ["veh:alpha-credit-master-lp"],
            "as_of": "2026-03-01T00:00:00Z",
            "recorded_as_of": t_between.isoformat(),
        },
    )
    assert r_mid.status_code == 200, r_mid.text
    f_mid = _find_fact(r_mid.json()["facts"], predicate="official_nav_audit_signoff")
    assert f_mid["object_value"] == "SIGNOFF-V1-PRELIM"
    assert f_mid["claim_id"] == v1_claim["claim_id"]

    # Query at t_after -> MUST return V2
    r_now = await api_client.post(
        "/api/v1/context/bundle",
        headers=headers,
        json={
            "entity_keys": ["veh:alpha-credit-master-lp"],
            "as_of": "2026-03-01T00:00:00Z",
            "recorded_as_of": t_after.isoformat(),
        },
    )
    assert r_now.status_code == 200, r_now.text
    f_now = _find_fact(r_now.json()["facts"], predicate="official_nav_audit_signoff")
    assert f_now["object_value"] == "SIGNOFF-V2-FINAL"

    # Query before t_before -> MUST NOT exist yet
    r_early = await api_client.post(
        "/api/v1/context/bundle",
        headers=headers,
        json={
            "entity_keys": ["veh:alpha-credit-master-lp"],
            "as_of": "2026-03-01T00:00:00Z",
            "recorded_as_of": t_before.isoformat(),
        },
    )
    assert r_early.status_code == 200
    assert not any(f["predicate"] == "official_nav_audit_signoff" for f in r_early.json()["facts"])


@pytest.mark.asyncio
async def test_all_three_legacy_skills_zero_diff_reproduction(api_client) -> None:
    """Verify all 3 legacy skills reproduce their frozen lookup tables with 0 diff."""
    headers = mock_headers("user:product-lead", actor_mode="service")

    # 1. CRM Fund Mapping Skill (crm_fund_lookup.csv)
    with (FIXTURES_DIR / "legacy_skills/crm_fund_lookup.csv").open("r", encoding="utf-8", newline="") as fh:
        crm_rows = list(csv.DictReader(fh))
    alpha_crm = next(r for r in crm_rows if r["fund_query_name"] == "Alpha Credit")

    crm_bundle_resp = await api_client.post(
        "/api/v1/context/bundle",
        headers=headers,
        json={
            "entity_keys": ["skill:crm-fund-mapping", alpha_crm["crm_family_key"]],
            "skill_id": "skill:crm-fund-mapping",
            "as_of": "2025-12-31T23:59:59Z",
            "recorded_as_of": "2026-01-20T00:00:00Z",
        },
    )
    assert crm_bundle_resp.status_code == 200, crm_bundle_resp.text
    crm_map = _find_fact(crm_bundle_resp.json()["facts"], predicate="maps_as")
    assert crm_map["object_value"] == alpha_crm["crm_family_key"]
    assert crm_map["context_purpose"] == "fund_family"

    fam_card = (await api_client.get(f"/api/v1/entities/{alpha_crm['crm_family_key']}", headers=headers)).json()
    crm_ids = [i["identifier_value"] for i in fam_card["identifiers"] if i["scheme"] == "crm_id"]
    assert alpha_crm["crm_family_id"] in crm_ids

    # 2. GIPS Portfolio Reporting Skill (reporting_group_lookup.json)
    rep_legacy = load_json_fixture("legacy_skills/reporting_group_lookup.json")
    alpha_rep = next(m for m in rep_legacy["mappings"] if m["fund_query_name"] == "Alpha Credit")

    rep_bundle_resp = await api_client.post(
        "/api/v1/context/bundle",
        headers=headers,
        json={
            "entity_keys": ["skill:gips-portfolio-reporting", alpha_rep["reporting_group_key"]],
            "skill_id": "skill:gips-portfolio-reporting",
            "as_of": "2025-12-31T23:59:59Z",
            "recorded_as_of": "2026-01-20T00:00:00Z",
        },
    )
    assert rep_bundle_resp.status_code == 200, rep_bundle_resp.text
    rep_facts = rep_bundle_resp.json()["facts"]
    rep_map = _find_fact(rep_facts, predicate="maps_as")
    assert rep_map["object_value"] == alpha_rep["reporting_group_key"]
    assert rep_map["context_purpose"] == "consolidated_group"

    included_vehicles = {f["object_value"] for f in rep_facts if f["predicate"] == "includes_vehicle"}
    assert set(alpha_rep["included_vehicles"]).issubset(included_vehicles)

    # 3. Cash-Flow Wire Reconciliation Skill (cashflow_vehicle_lookup.csv)
    with (FIXTURES_DIR / "legacy_skills/cashflow_vehicle_lookup.csv").open("r", encoding="utf-8", newline="") as fh:
        cf_rows = list(csv.DictReader(fh))
    alpha_cf = next(r for r in cf_rows if r["legal_vehicle_key"] == "veh:alpha-credit-offshore-lp")

    cf_bundle_resp = await api_client.post(
        "/api/v1/context/bundle",
        headers=headers,
        json={
            "entity_keys": ["skill:cashflow-wire-reconciliation", alpha_cf["legal_vehicle_key"]],
            "skill_id": "skill:cashflow-wire-reconciliation",
            "as_of": "2026-03-15T00:00:00Z",
            "recorded_as_of": "2026-03-15T12:00:00Z",
        },
    )
    assert cf_bundle_resp.status_code == 200, cf_bundle_resp.text
    cf_bundle = cf_bundle_resp.json()
    cf_map = _find_fact(cf_bundle["facts"], predicate="maps_as")
    assert cf_map["object_value"] == alpha_cf["legal_vehicle_key"]
    assert cf_map["context_purpose"] == "legal_vehicle"
    # Even with the open 3-way conflict, the active approved_fact stays pinned to ADM-904!
    approved_code = _find_fact(
        cf_bundle["facts"],
        predicate="has_admin_code",
        epistemic_status="approved_fact",
    )
    assert approved_code["object_value"] == alpha_cf["admin_code"]

    # Verify other vehicles' official admin_code crosswalks match cashflow_vehicle_lookup.csv
    for row in cf_rows:
        if row["legal_vehicle_key"] in ("veh:alpha-credit-onshore-lp", "veh:northstar-dl-ii-lp"):
            vcard = (await api_client.get(f"/api/v1/entities/{row['legal_vehicle_key']}", headers=headers)).json()
            codes = [i["identifier_value"] for i in vcard["identifiers"] if i["scheme"] == "admin_code"]
            assert row["admin_code"] in codes


@pytest.mark.asyncio
async def test_two_channel_stratified_precedence_and_receipt_determinism(api_client) -> None:
    """Verify two-channel precedence (facts vs directives) and receipt determinism."""
    headers = mock_headers("user:pm-credit", actor_mode="obo", channel_mode="direct")
    payload = {
        "entity_keys": ["grp:alpha-credit-gips-composite"],
        "skill_id": "skill:gips-portfolio-reporting",
        "project_id": "proj:q1-2026-audit",
        "as_of": "2026-03-15T00:00:00Z",
        "recorded_as_of": "2026-03-15T12:00:00Z",
    }

    r1 = await api_client.post("/api/v1/context/bundle", headers=headers, json=payload)
    r2 = await api_client.post("/api/v1/context/bundle", headers=headers, json=payload)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text

    b1 = r1.json()
    b2 = r2.json()

    # Two-channel separation: facts contain authoritative approved_fact NAV/IRR
    assert any(f["claim_id"] == "clm-q4-2025-nav-restated" for f in b1["facts"])
    assert all(f.get("channel", "fact") == "fact" for f in b1["facts"])

    # Directives channel: mandatory firm rule pinned FIRST, then user_x_skill > project
    directives = b1["directives"]
    dir_ids = [d["claim_id"] for d in directives]
    assert "clm-dir-firm-mandatory-gips-disclaimer" in dir_ids
    assert "clm-dir-user-pm-format-pref" in dir_ids
    assert "clm-dir-project-audit-rounding" in dir_ids
    assert dir_ids.index("clm-dir-firm-mandatory-gips-disclaimer") < dir_ids.index("clm-dir-user-pm-format-pref")
    assert dir_ids.index("clm-dir-user-pm-format-pref") < dir_ids.index("clm-dir-project-audit-rounding")

    # Receipt completeness & determinism
    rec1 = b1["receipt"]
    rec2 = b2["receipt"]
    for rec in (rec1, rec2):
        assert rec["receipt_id"]
        assert rec["principal_id"] == "user:pm-credit"
        assert rec["as_of"]
        assert rec["recorded_as_of"]
        assert isinstance(rec["policy_epoch"], int)
        assert isinstance(rec["data_epoch"], int)
        assert isinstance(rec["claim_ids"], list) and len(rec["claim_ids"]) > 0
        assert "redacted_evidence_claim_ids" in rec
        assert rec["receipt_sha256"] and len(rec["receipt_sha256"]) == 64

    # Deterministic claim sets across repeated identical queries
    assert sorted(rec1["claim_ids"]) == sorted(rec2["claim_ids"])
    assert sorted(rec1["redacted_evidence_claim_ids"]) == sorted(rec2["redacted_evidence_claim_ids"])
    assert rec1["policy_epoch"] == rec2["policy_epoch"]
    assert rec1["data_epoch"] == rec2["data_epoch"]

    # Receipt persisted & retrievable
    get_rec = await api_client.get(f"/api/v1/receipts/{rec1['receipt_id']}", headers=headers)
    assert get_rec.status_code == 200, get_rec.text
    fetched = get_rec.json().get("receipt", get_rec.json())
    assert fetched["receipt_id"] == rec1["receipt_id"]
    assert fetched["receipt_sha256"] == rec1["receipt_sha256"]


@pytest.mark.asyncio
async def test_context_sdk_pinned_version_and_refcounted_atomic_swap(tmp_path: Path) -> None:
    """Verify PDF Defects #4, #5, #6 fixes in SnapshotCompiler & context_sdk.

    - In-RAM sqlite3.deserialize() with zero temp .sqlite files created on disk
    - Ref-counted atomic pointer swap under concurrent reader threads (0 closed-DB errors)
    - pinned_version isolation: live poller updates v1 -> v2, pinned client stays locked on v1
    """
    SqliteBitemporalStore = resolve_symbol(
        [
            ("context_adapters.sqlite_store", "SqliteBitemporalStore"),
            ("context_adapters.stores.sqlite", "SqliteBitemporalStore"),
            ("context_adapters", "SqliteBitemporalStore"),
        ]
    )
    PolicyEngine = resolve_symbol(
        [
            ("context_core.policy", "PolicyEngine"),
            ("context_adapters.policy", "PolicyEngine"),
            ("context_core", "PolicyEngine"),
        ]
    )
    seed_fixtures = resolve_symbol(
        [
            ("context_core.seed", "seed_store_from_fixtures"),
            ("context_core.seeder", "seed_store_from_fixtures"),
            ("context_core.seed", "seed_fixtures"),
            ("context_core.seeder", "seed_fixtures"),
            ("context_core", "seed_store_from_fixtures"),
            ("context_core", "seed_fixtures"),
        ]
    )
    SnapshotCompiler = resolve_symbol(
        [
            ("context_sdk.compiler", "SnapshotCompiler"),
            ("context_sdk.snapshot_compiler", "SnapshotCompiler"),
            ("context_sdk", "SnapshotCompiler"),
        ]
    )
    ContextSDK = resolve_symbol(
        [
            ("context_sdk.client", "ContextSDK"),
            ("context_sdk.client", "ContextClient"),
            ("context_sdk", "ContextSDK"),
            ("context_sdk", "ContextClient"),
        ]
    )
    ClaimCreateInput = resolve_symbol([("context_core.models", "ClaimCreateInput")])

    db_path = tmp_path / "sdk_test.sqlite3"
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    policy = PolicyEngine()
    store = SqliteBitemporalStore(db_path=db_path, policy=policy) if "policy" in inspect.signature(SqliteBitemporalStore).parameters else SqliteBitemporalStore(db_path)

    live_sdk = None
    pinned_sdk = None
    try:
        await store.initialize()
        if inspect.iscoroutinefunction(seed_fixtures):
            sig = inspect.signature(seed_fixtures)
            if "fixtures_dir" in sig.parameters:
                await seed_fixtures(store, fixtures_dir=FIXTURES_DIR)
            else:
                await seed_fixtures(store)
        else:
            seed_fixtures(store)

        compiler_sig = inspect.signature(SnapshotCompiler)
        ckwargs: dict[str, Any] = {}
        for k, v in (
            ("store", store),
            ("policy", policy),
            ("output_dir", snap_dir),
            ("snapshot_dir", snap_dir),
            ("snapshots_dir", snap_dir),
            ("storage_dir", snap_dir),
            ("base_dir", snap_dir),
        ):
            if k in compiler_sig.parameters:
                ckwargs[k] = v
        compiler = SnapshotCompiler(**ckwargs)

        # Compile snapshot v1
        m1 = await compiler.compile_and_publish("firm-all", "public_side")
        v1 = m1.version_id if hasattr(m1, "version_id") else m1["version_id"]
        assert v1 >= 1

        # Build both a pinned client (locked to v1) and a live polling client
        sdk_sig = inspect.signature(ContextSDK)

        def _make_sdk(pinned_version: int | None, poll_interval: float) -> Any:
            kw: dict[str, Any] = {}
            for k, v in (
                ("snapshot_dir", snap_dir),
                ("snapshots_dir", snap_dir),
                ("storage_dir", snap_dir),
                ("base_dir", snap_dir),
                ("acl_tier", "firm-all"),
                ("wall_side", "public_side"),
                ("pinned_version", pinned_version),
                ("poll_interval_seconds", poll_interval),
                ("poll_interval_s", poll_interval),
                ("poll_interval", poll_interval),
            ):
                if k in sdk_sig.parameters:
                    kw[k] = v
            return ContextSDK(**kw)

        # Snapshot disk files before SDK initialization to verify zero temp .sqlite files
        before_sqlite_files = {p.name for p in tmp_path.rglob("*.sqlite*")}

        pinned_sdk = _make_sdk(pinned_version=v1, poll_interval=0.05)
        live_sdk = _make_sdk(pinned_version=None, poll_interval=0.05)

        after_sqlite_files = {p.name for p in tmp_path.rglob("*.sqlite*")}
        # No extra uncompressed .sqlite files created by deserializing .sqlite.zst!
        new_uncompressed = [
            f for f in (after_sqlite_files - before_sqlite_files)
            if not f.endswith(".zst") and not f.endswith("-wal") and not f.endswith("-shm")
        ]
        assert not new_uncompressed, f"SDK must deserialize in-RAM with zero temp SQLite files; found {new_uncompressed}"

        def _lookup_admin_code(sdk_obj: Any, as_of: datetime, recorded_as_of: datetime | None = None) -> str:
            for meth_name in ("get_mapping", "lookup", "lookup_claim"):
                fn = getattr(sdk_obj, meth_name, None)
                if fn is None:
                    continue
                sig = inspect.signature(fn)
                kw: dict[str, Any] = {}
                for k, v in (
                    ("canonical_key_or_id", "veh:alpha-credit-offshore-lp"),
                    ("canonical_key", "veh:alpha-credit-offshore-lp"),
                    ("entity_key", "veh:alpha-credit-offshore-lp"),
                    ("subject_key", "veh:alpha-credit-offshore-lp"),
                    ("predicate", "has_admin_code"),
                    ("context_purpose", "legal_vehicle"),
                    ("as_of", as_of),
                    ("recorded_as_of", recorded_as_of),
                ):
                    if k in sig.parameters:
                        kw[k] = v
                res = fn(**kw)
                if isinstance(res, dict):
                    return str(res.get("object_value") or res.get("value"))
                if hasattr(res, "object_value"):
                    return str(res.object_value)
                if hasattr(res, "value"):
                    return str(res.value)
                return str(res)
            raise AssertionError(f"ContextSDK missing lookup/get_mapping method: {dir(sdk_obj)}")

        now_ts = datetime(2026, 3, 20, tzinfo=timezone.utc)
        assert _lookup_admin_code(pinned_sdk, now_ts) == "ADM-904"
        assert _lookup_admin_code(live_sdk, now_ts) == "ADM-904"

        # Mutate approved admin code as Fund Steward & compile v2
        steward = build_principal_from_fixture("user:fund-steward")
        await store.write_claim(
            ClaimCreateInput(
                subject_canonical_key="veh:alpha-credit-offshore-lp",
                predicate="has_admin_code",
                object_value="ADM-904-NEWV2",
                context_purpose="legal_vehicle",
                scope_type="firm",
                scope_id="firm",
                channel="fact",
                epistemic_status="approved_fact",
                source_system="admin-feed",
                valid_from=parse_utc("2026-01-01T00:00:00Z"),
                force_supersede=True,
            ),
            principal=steward,
        )
        m2 = await compiler.compile_and_publish("firm-all", "public_side")
        v2 = m2.version_id if hasattr(m2, "version_id") else m2["version_id"]
        assert v2 > v1

        # Concurrent reader storm during atomic swap (proves Defect #5 ref-counting!)
        stop_event = threading.Event()
        errors: list[Exception] = []
        reads_completed = 0
        count_lock = threading.Lock()

        def reader_worker() -> None:
            nonlocal reads_completed
            while not stop_event.is_set():
                try:
                    val = _lookup_admin_code(live_sdk, now_ts)
                    assert val in ("ADM-904", "ADM-904-NEWV2"), f"Unexpected value during swap: {val}"
                    with count_lock:
                        reads_completed += 1
                except Exception as exc:
                    errors.append(exc)
                    return

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futs = [pool.submit(reader_worker) for _ in range(6)]
            # Explicitly refresh (and allow background poller to run) multiple times
            for _ in range(5):
                if hasattr(live_sdk, "refresh_snapshot"):
                    live_sdk.refresh_snapshot()
                elif hasattr(live_sdk, "refresh"):
                    live_sdk.refresh()
                if hasattr(pinned_sdk, "refresh_snapshot"):
                    pinned_sdk.refresh_snapshot()
                elif hasattr(pinned_sdk, "refresh"):
                    pinned_sdk.refresh()
                time.sleep(0.06)
            stop_event.set()
            for fut in futs:
                fut.result(timeout=5.0)

        assert not errors, f"Concurrent reader errors during atomic swap: {errors}"
        assert reads_completed >= 20

        # Live client MUST have swapped to v2; pinned client MUST stay on v1!
        assert _lookup_admin_code(live_sdk, now_ts) == "ADM-904-NEWV2"
        assert _lookup_admin_code(pinned_sdk, now_ts) == "ADM-904"
    finally:
        if live_sdk is not None and hasattr(live_sdk, "close"):
            live_sdk.close()
        if pinned_sdk is not None and hasattr(pinned_sdk, "close"):
            pinned_sdk.close()
        await store.close()
