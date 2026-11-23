"""Oracle 4: Institutional Domain Workflows Verification.

Verifies:
  1. Unheld watchlist / pipeline / benchmark / passed Issuers exist independently of Funds.
  2. Dual-role legal party (Global Atlantic Financial Group PLC: Issuer + LP + ISDA Counterparty).
  3. Bidirectional reverse navigation: Topic + Facet -> Issuer -> Position/Fund -> Client/Opportunity -> Skill.
  4. 3-way admin-code conflict lifecycle + skill_bindings blast-radius preview + untouched approved_fact
     + valid difference-of-scope (no false conflicts).
  5. Manual upload quarantine (PDF side letter, CSV reconciliation, Markdown forecast memo ->
     immutable LF/binary SHA-256 -> quarantined unverified_observation -> steward approve/reject).
  6. Personal Memory CRUD, watchlist live conflict alerts, compare vs firm, and promotion to review queue.
"""

from __future__ import annotations

import pytest

from conftest import FIXTURES_DIR, lf_sha256, mock_headers


@pytest.mark.asyncio
async def test_unheld_issuers_exist_independently_of_funds(api_client) -> None:
    """Watchlist, pipeline, benchmark, and passed issuers exist with coverages and ZERO fund positions."""
    deal_hdrs = mock_headers("user:private-deal-team")

    unheld_expected = {
        "issuer:cinder-logistics": "watchlist",
        "issuer:solis-medtech": "pipeline",
        "issuer:northline-telecom": "benchmark",
        "issuer:redwood-retail": "passed",
    }
    for key, expected_status in unheld_expected.items():
        r = await api_client.get(f"/api/v1/entities/{key}", headers=deal_hdrs)
        assert r.status_code == 200, f"Unheld issuer {key} missing: {r.text}"
        card = r.json()
        assert card["entity"]["entity_type"] == "issuer"
        assert card.get("positions", []) == [], f"Unheld {expected_status} issuer {key} should have 0 positions"
        covs = card.get("coverages", [])
        assert any(c["coverage_status"] == expected_status for c in covs), (
            f"Expected coverage_status={expected_status!r} on {key}, got {covs}"
        )

    # Confirm held issuer DOES have positions across funds
    r_apex = await api_client.get("/api/v1/entities/issuer:apex-industrial", headers=deal_hdrs)
    assert r_apex.status_code == 200
    apex_card = r_apex.json()
    assert len(apex_card.get("positions", [])) >= 2
    assert any(c["coverage_status"] == "held" for c in apex_card.get("coverages", []))


@pytest.mark.asyncio
async def test_dual_role_legal_party_separation(api_client) -> None:
    """Global Atlantic Financial Group PLC acts simultaneously as Issuer + LP Client + ISDA Counterparty."""
    hdrs = mock_headers("user:fund-steward")
    r = await api_client.get("/api/v1/entities/party:global-atlantic-bank", headers=hdrs)
    assert r.status_code == 200, r.text
    card = r.json()

    assert card["entity"]["entity_type"] == "legal_party"
    leis = [i["identifier_value"] for i in card["identifiers"] if i["scheme"] == "lei"]
    assert "5493008G7K2M9X1Q4L88" in leis

    predicates = {c["predicate"] for c in card["facts"]}
    assert {"acts_as_issuer", "acts_as_lp_client", "acts_as_counterparty"}.issubset(predicates)

    # Verify links reach Issuer role, LP Commitment ($125M Alpha Credit Offshore), and ISDA Counterparty (Alpha Master)
    targets = {c["predicate"]: c["object_value"] for c in card["facts"]}
    assert targets["acts_as_issuer"] == "issuer:global-atlantic-bank"
    assert targets["acts_as_lp_client"] == "commit:global-atlantic-alpha-offshore-125m"
    assert targets["acts_as_counterparty"] == "veh:alpha-credit-master-lp"


@pytest.mark.asyncio
async def test_bidirectional_reverse_navigation_topic_to_skill(api_client) -> None:
    """Topic + Facet -> Issuer -> Position/Fund -> Client/Opportunity -> Skill reverse traversal."""
    hdrs = mock_headers("user:fund-steward")

    # 1. Start at Plane 4 Topic: lme-restructuring -> discovers held Issuer Apex Industrial
    r_topic = await api_client.get("/api/v1/topics/lme-restructuring/items", headers=hdrs)
    assert r_topic.status_code == 200, r_topic.text
    topic_items = r_topic.json()
    topic_ent_keys = {e["canonical_key"] for e in topic_items.get("entities", [])}
    assert "issuer:apex-industrial" in topic_ent_keys

    # 2. Orthogonal Plane 3 facet filter on entities: financial + quantitative + actual
    r_facet = await api_client.get(
        "/api/v1/entities",
        headers=hdrs,
        params={"domain_nature": "financial", "modality": "quantitative", "epistemic_temporal": "actual"},
    )
    assert r_facet.status_code == 200, r_facet.text
    assert len(r_facet.json()["items"]) >= 1

    # 3. Reversible Impact BFS from Issuer Apex Industrial -> Positions -> Funds -> Clients/Opportunities -> Skills
    r_imp_apex = await api_client.get("/api/v1/map/impact/ent:issuer-apex-industrial", headers=hdrs)
    assert r_imp_apex.status_code == 200, r_imp_apex.text
    imp_apex = r_imp_apex.json()
    layers = imp_apex["layers"]
    assert len(layers.get("positions", [])) >= 2, f"Expected >=2 positions impacted by Apex: {layers}"
    fund_blob = str(layers.get("funds", [])) + str(imp_apex.get("nodes", []))
    assert "alpha-credit-master-lp" in fund_blob or "ent:veh-alpha-master" in fund_blob
    assert "northstar-dl-ii-lp" in fund_blob or "ent:veh-northstar-dl-ii" in fund_blob
    assert imp_apex["total_affected"] >= 3
    assert imp_apex["receipt_id"]

    # 4. Reverse impact from Alpha Credit Offshore LP reaches Commitments/Clients, Skills & Steward Watchlist!
    r_imp_veh = await api_client.get("/api/v1/map/impact/ent:veh-alpha-offshore", headers=hdrs)
    assert r_imp_veh.status_code == 200, r_imp_veh.text
    imp_veh = r_imp_veh.json()
    veh_layers = imp_veh["layers"]
    skills_blob = str(veh_layers.get("skills_apps", [])) + str(imp_veh.get("nodes", []))
    assert "cashflow-wire-reconciliation" in skills_blob or "ent:skill-cashflow-recon" in skills_blob
    clients_blob = str(veh_layers.get("clients_opportunities", [])) + str(imp_veh.get("nodes", []))
    assert "nordic" in clients_blob.lower() or "global-atlantic" in clients_blob.lower()
    assert len(veh_layers.get("watchlist_hits", [])) >= 1


@pytest.mark.asyncio
async def test_three_way_conflict_lifecycle_blast_radius_and_scope_difference(api_client) -> None:
    """3-way admin-code conflict, blast-radius preview, untouched approved_fact, and valid scope difference."""
    steward_hdrs = mock_headers("user:fund-steward")

    # 1. Valid difference-of-scope: CRM (fund_family), Reporting (consolidated_group), Cash-Flow (legal_vehicle)
    #    MUST NOT trigger a conflict set!
    r_conflicts = await api_client.get("/api/v1/conflicts", headers=steward_hdrs, params={"status": "open"})
    assert r_conflicts.status_code == 200, r_conflicts.text
    open_conflicts = r_conflicts.json()["conflicts"]
    assert not any(c["predicate"] == "maps_as" for c in open_conflicts), (
        f"Valid difference-of-scope mappings falsely triggered a ConflictSet: {open_conflicts}"
    )

    # 2. Seeded 3-way conflict on veh:alpha-credit-offshore-lp has_admin_code
    c3 = next(c for c in open_conflicts if c["conflict_set_id"] == "conflict-alpha-admin-code-3way")
    assert len(c3["claim_ids"]) == 3
    assert set(c3["claim_ids"]) == {
        "clm-admin-code-approved",
        "clm-admin-code-custodian",
        "clm-admin-code-gips",
    }

    # Verify approved_fact ADM-904 is still UNTOUCHED before resolution
    veh_card = (await api_client.get("/api/v1/entities/veh:alpha-credit-offshore-lp", headers=steward_hdrs)).json()
    approved_codes = [
        f for f in veh_card["facts"]
        if f["predicate"] == "has_admin_code" and f["epistemic_status"] == "approved_fact"
    ]
    assert len(approved_codes) == 1 and approved_codes[0]["object_value"] == "ADM-904"

    # 3. Blast-radius preview shows skill:cashflow-wire-reconciliation
    r_blast = await api_client.get(
        "/api/v1/conflicts/conflict-alpha-admin-code-3way/blast-radius",
        headers=steward_hdrs,
    )
    assert r_blast.status_code == 200, r_blast.text
    assert "cashflow" in r_blast.text.lower()

    # 4. Writing a 4th conflicting observation MUST NOT overwrite approved_fact ADM-904
    r_new_dispute = await api_client.post(
        "/api/v1/claims",
        headers=mock_headers("user:public-analyst"),
        json={
            "subject_canonical_key": "veh:alpha-credit-offshore-lp",
            "predicate": "has_admin_code",
            "object_value": "ADM-904-ANALYST-GUESS",
            "context_purpose": "legal_vehicle",
            "scope_type": "firm",
            "scope_id": "firm",
            "channel": "fact",
            "epistemic_status": "unverified_observation",
            "source_system": "analyst-email",
        },
    )
    assert r_new_dispute.status_code in (200, 201), r_new_dispute.text
    veh_card_after = (await api_client.get("/api/v1/entities/veh:alpha-credit-offshore-lp", headers=steward_hdrs)).json()
    still_approved = [
        f for f in veh_card_after["facts"]
        if f["predicate"] == "has_admin_code" and f["epistemic_status"] == "approved_fact"
    ]
    assert len(still_approved) == 1 and still_approved[0]["object_value"] == "ADM-904"

    # 5. Resolve conflict as Fund Steward with valid expected_data_epoch
    current_epoch = (await api_client.get("/healthz", headers=steward_hdrs)).json()["data_epoch"]
    r_res = await api_client.post(
        "/api/v1/conflicts/conflict-alpha-admin-code-3way/resolve",
        headers=steward_hdrs,
        json={
            "winning_claim_id": "clm-admin-code-custodian",
            "rationale": "Confirmed BNY Custodian Side Letter Addendum effective 2026-01-01.",
            "expected_data_epoch": current_epoch,
        },
    )
    assert r_res.status_code == 200, r_res.text
    res_body = r_res.json()
    assert res_body["conflict"]["status"] == "resolved"
    assert res_body["approved_claim"]["object_value"] == "ADM-904X"
    assert res_body["approved_claim"]["epistemic_status"] == "approved_fact"
    assert any("cashflow" in str(s).lower() for s in res_body.get("affected_skills", []))


@pytest.mark.asyncio
async def test_manual_upload_quarantine_and_steward_triage(api_client) -> None:
    """Upload PDF side letter, CSV reconciliation, and MD memo -> LF SHA-256 -> quarantine -> steward approve/reject."""
    analyst_hdrs = mock_headers("user:public-analyst")
    steward_hdrs = mock_headers("user:fund-steward")

    upload_cases = [
        ("uploads/alpha_credit_lp_side_letter.pdf", "application/pdf", "veh:alpha-credit-offshore-lp", "legal-side-letters"),
        ("uploads/manual_admin_code_reconciliation.csv", "text/csv", "veh:alpha-credit-offshore-lp", "operational-admin"),
        ("uploads/analyst_forecast_memo_cinder_logistics.md", "text/markdown", "issuer:cinder-logistics", "investment-thesis"),
    ]
    quarantined_ids: list[str] = []

    for rel_path, mime, subj_key, topic_slug in upload_cases:
        fpath = FIXTURES_DIR / rel_path
        expected_sha = lf_sha256(fpath)
        r_up = await api_client.post(
            "/api/v1/uploads",
            headers=analyst_hdrs,
            files={"file": (fpath.name, fpath.read_bytes(), mime)},
            data={
                "subject_canonical_key": subj_key,
                "topic_slug": topic_slug,
                "wall_side": "public_side",
            },
        )
        assert r_up.status_code == 201, f"Upload failed for {rel_path}: {r_up.text}"
        body = r_up.json()
        assert body["content_sha256"] == expected_sha, f"SHA-256 mismatch for {rel_path}"
        assert body["epistemic_status"] == "unverified_observation"
        assert len(body["quarantined_claim_ids"]) >= 1
        quarantined_ids.extend(body["quarantined_claim_ids"])

    # Uploads MUST NOT overwrite active approved_fact ADM-904
    veh = (await api_client.get("/api/v1/entities/veh:alpha-credit-offshore-lp", headers=steward_hdrs)).json()
    approved_admin = [
        f for f in veh["facts"]
        if f["predicate"] == "has_admin_code" and f["epistemic_status"] == "approved_fact"
    ]
    assert len(approved_admin) == 1 and approved_admin[0]["object_value"] == "ADM-904"

    # Quarantined inbox visible; non-steward CANNOT approve (403); steward CAN approve & reject
    r_inbox = await api_client.get("/api/v1/inbox", headers=steward_hdrs)
    assert r_inbox.status_code == 200, r_inbox.text
    inbox_ids = {c["claim_id"] for c in r_inbox.json().get("items", r_inbox.json().get("claims", []))}
    assert quarantined_ids[0] in inbox_ids

    r_unauth_approve = await api_client.post(f"/api/v1/inbox/{quarantined_ids[0]}/approve", headers=analyst_hdrs, json={})
    assert r_unauth_approve.status_code == 403

    r_approve = await api_client.post(f"/api/v1/inbox/{quarantined_ids[-1]}/approve", headers=steward_hdrs, json={"rationale": "Verified analyst forecast"})
    assert r_approve.status_code == 200, r_approve.text

    if len(quarantined_ids) >= 2:
        r_reject = await api_client.post(f"/api/v1/inbox/{quarantined_ids[0]}/reject", headers=steward_hdrs, json={"rationale": "Duplicate side letter"})
        assert r_reject.status_code == 200, r_reject.text


@pytest.mark.asyncio
async def test_personal_memory_crud_watchlist_alerts_compare_and_promote(api_client) -> None:
    """Personal memory CRUD, watchlist live conflict alerts, compare vs firm, and promote to quarantined inbox."""
    pm_hdrs = mock_headers("user:pm-credit", actor_mode="obo", channel_mode="direct")
    other_hdrs = mock_headers("user:public-analyst", actor_mode="obo", channel_mode="direct")

    # 1. Create personal note
    r_create = await api_client.post(
        "/api/v1/memory/notes",
        headers=pm_hdrs,
        json={
            "scope_type": "user",
            "key": "alpha-offshore-wire-note",
            "content": "Watch ADM-904 vs ADM-904X discrepancy on Alpha Credit Offshore LP ahead of Q1 distribution.",
            "tags": ["wires", "alpha-credit"],
            "linked_entity_ids": ["ent:veh-alpha-offshore"],
            "linked_topic_ids": ["topic:cashflows-liquidity"],
        },
    )
    assert r_create.status_code == 201, r_create.text
    note = r_create.json()["memory"]
    mid = note["memory_id"]

    # Other user CANNOT see or delete PM's note
    r_other_list = await api_client.get("/api/v1/memory/notes", headers=other_hdrs)
    assert all(m["memory_id"] != mid for m in r_other_list.json()["items"])
    r_other_del = await api_client.delete(f"/api/v1/memory/notes/{mid}", headers=other_hdrs)
    assert r_other_del.status_code in (403, 404)

    # 2. Watchlist PUT + automatic live conflict alert on watched ent:veh-alpha-offshore
    r_wl = await api_client.put(
        "/api/v1/memory/watchlist",
        headers=pm_hdrs,
        json={
            "entity_ids": ["ent:veh-alpha-offshore", "ent:issuer-apex-industrial"],
            "topic_ids": ["topic:cashflows-liquidity", "topic:lme-restructuring"],
            "source_ids": ["ent:src-admin-feed"],
        },
    )
    assert r_wl.status_code == 200, r_wl.text
    wl_body = r_wl.json()
    assert wl_body["memory_enabled"] is True
    assert "ent:veh-alpha-offshore" in wl_body["entity_ids"]
    assert len(wl_body["alerts"]) >= 1, f"Expected live conflict alert for watched ent:veh-alpha-offshore: {wl_body}"
    assert any("alpha" in str(a).lower() or "conflict" in str(a).lower() for a in wl_body["alerts"])

    # 3. Compare personal note vs firm claims
    r_cmp = await api_client.post(f"/api/v1/memory/notes/{mid}/compare", headers=pm_hdrs, json={})
    assert r_cmp.status_code == 200, r_cmp.text
    cmp_body = r_cmp.json()
    assert cmp_body["note"]["memory_id"] == mid
    assert len(cmp_body["matching_firm_claims"]) >= 1
    assert cmp_body["receipt_id"]

    # 4. Promote personal note -> quarantined unverified_observation (NEVER auto-approving!)
    r_prom = await api_client.post(
        f"/api/v1/memory/notes/{mid}/promote",
        headers=pm_hdrs,
        json={
            "subject_canonical_key": "veh:alpha-credit-offshore-lp",
            "predicate": "wire_reconciliation_note",
            "rationale": "Promote PM personal observation to steward review queue",
        },
    )
    assert r_prom.status_code == 201, r_prom.text
    prom_body = r_prom.json()
    assert prom_body["epistemic_status"] == "unverified_observation"
    assert prom_body["quarantined_claim"]["epistemic_status"] == "unverified_observation"

    # 5. Delete personal note
    r_del = await api_client.delete(f"/api/v1/memory/notes/{mid}", headers=pm_hdrs)
    assert r_del.status_code == 200 and r_del.json()["deleted"] is True
