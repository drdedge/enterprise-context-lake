"""Version control for context (ADR-007): change sets, approval rules, redline, revert, releases, barrier.

Runs against the real seeded FastAPI app (no mocks), like the other API suites.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import pytest

from conftest import mock_headers
from context_core.models import WallSide

STEWARD = "user:fund-steward"
DIRECTOR = "user:overage-director"  # second steward + compliance (groups resolved from directory)
PM = "user:pm-credit"
ANALYST = "user:public-analyst"
DEAL = "user:private-deal-team"

NOTE = "kf:dag-live-admin-nav"


def H(user: str, **extra: str) -> dict[str, str]:
    headers = mock_headers(user, groups_overage=user == DIRECTOR)
    headers.update(extra)
    return headers


def q(value: str) -> str:
    return quote(value, safe="")


async def _history(client: Any, user: str, record_type: str, record_id: str) -> Any:
    return await client.get(f"/api/v1/history/{record_type}/{q(record_id)}", headers=H(user))


async def _propose(client: Any, user: str, title: str, items: list[dict[str, Any]], **headers: str) -> dict[str, Any]:
    r = await client.post("/api/v1/change-sets", json={"title": title, "items": items}, headers=H(user, **headers))
    assert r.status_code == 201, r.text
    return r.json()["change_set"]


def _note_edit(body: str) -> dict[str, Any]:
    return {"record_type": "knowledge_item", "op": "upsert", "record_id": NOTE, "payload": {"body_markdown": body}}


@pytest.mark.asyncio
async def test_seed_creates_baseline_history(api_client: Any) -> None:
    r = await _history(api_client, ANALYST, "entity", "ent:fam-alpha-credit")
    assert r.status_code == 200, r.text
    versions = r.json()["versions"]
    assert len(versions) == 1
    assert versions[0]["op"] == "baseline" and versions[0]["is_head"]


@pytest.mark.asyncio
async def test_steward_edit_is_auto_approved_with_redline(api_client: Any) -> None:
    cs = await _propose(api_client, STEWARD, "Clarify NAV source", [_note_edit("NAV comes from the administrator feed, daily.")])
    assert cs["status"] == "merged" and cs["approval"] == "auto"
    assert "steward-self" in cs["rule_ids"]
    ops = {seg["op"] for seg in cs["items"][0]["redline"]}
    assert {"delete", "insert"} <= ops

    hist = (await _history(api_client, ANALYST, "knowledge_item", NOTE)).json()
    assert [v["version_no"] for v in hist["versions"]] == [2, 1]
    assert hist["versions"][0]["author_id"] == STEWARD and hist["versions"][0]["title"] == "Clarify NAV source"

    diff = (await api_client.get(f"/api/v1/history/knowledge_item/{q(NOTE)}/diff", headers=H(ANALYST))).json()
    assert diff["from_version"] == 1 and diff["to_version"] == 2 and not diff["restricted"]
    inserted = "".join(s["text"] for s in diff["redline"] if s["op"] == "insert")
    assert "administrator feed" in inserted


@pytest.mark.asyncio
async def test_non_steward_needs_review_and_cannot_self_approve(api_client: Any) -> None:
    cs = await _propose(api_client, PM, "Fix typo", [_note_edit("Proposed by the PM.")])
    assert cs["status"] == "proposed" and cs["approvers"] == ["steward"]

    r = await api_client.post(f"/api/v1/change-sets/{cs['change_id']}/approve", json={}, headers=H(PM))
    assert r.status_code == 403
    r = await api_client.post(f"/api/v1/change-sets/{cs['change_id']}/approve", json={}, headers=H(ANALYST))
    assert r.status_code == 403

    hist = (await _history(api_client, ANALYST, "knowledge_item", NOTE)).json()
    assert hist["proposals"][0]["change_id"] == cs["change_id"]
    assert len(hist["versions"]) == 1  # not merged yet

    r = await api_client.post(f"/api/v1/change-sets/{cs['change_id']}/approve", json={"comment": "ok"}, headers=H(STEWARD))
    assert r.status_code == 200, r.text
    assert r.json()["change_set"]["status"] == "merged"
    hist = (await _history(api_client, ANALYST, "knowledge_item", NOTE)).json()
    assert hist["versions"][0]["approvals"][0]["approver_id"] == STEWARD


@pytest.mark.asyncio
async def test_stale_proposal_is_refused_as_conflict(api_client: Any) -> None:
    a = await _propose(api_client, PM, "Edit A", [_note_edit("Version A")])
    b = await _propose(api_client, PM, "Edit B", [_note_edit("Version B")])
    assert (await api_client.post(f"/api/v1/change-sets/{a['change_id']}/approve", json={}, headers=H(STEWARD))).status_code == 200
    r = await api_client.post(f"/api/v1/change-sets/{b['change_id']}/approve", json={}, headers=H(STEWARD))
    assert r.status_code == 409
    got = (await api_client.get(f"/api/v1/change-sets/{b['change_id']}", headers=H(STEWARD))).json()["change_set"]
    assert got["status"] == "conflict"


@pytest.mark.asyncio
async def test_agent_claims_low_risk_auto_privileged_needs_steward_then_revert(api_client: Any) -> None:
    agent = {"X-Change-Source": "agent", "X-Agent-Id": "curator-bot", "X-Run-Id": "run-123"}
    obs = await _propose(
        api_client, PM, "Observed leverage",
        [{"record_type": "claim", "op": "claim_create", "payload": {
            "subject_id": "ent:issuer-cinder-logistics", "predicate": "observed_net_leverage", "object_value": "4.1x",
            "epistemic_status": "unverified_observation"}}],
        **agent,
    )
    assert obs["status"] == "merged" and obs["rule_ids"] == ["agent-proposal-low-risk"]
    assert obs["source"] == "agent" and obs["agent_id"] == "curator-bot" and obs["run_id"] == "run-123"

    edit = await _propose(
        api_client, STEWARD, "Agent restates EBITDA guidance",
        [{"record_type": "claim", "op": "claim_update", "payload": {"claim_id": "clm-verve-ebitda-guidance", "object_value": "USD 999m"}}],
        **agent,
    )
    assert edit["status"] == "proposed" and edit["rule_ids"] == ["agent-privileged"]
    r = await api_client.post(f"/api/v1/change-sets/{edit['change_id']}/approve", json={}, headers=H(DIRECTOR))
    assert r.status_code == 200, r.text
    assert r.json()["change_set"]["status"] == "merged"

    claims = (await api_client.get("/api/v1/claims", params={"subject_id": "ent:issuer-verve-cloud", "predicate": "management_ebitda_guidance_fy26"}, headers=H(ANALYST))).json()["claims"]
    current = [c for c in claims if c["superseded_at"] is None]
    assert [c["object_value"] for c in current] == ["USD 999m"]
    old_value = (await api_client.get("/api/v1/claims/clm-verve-ebitda-guidance", headers=H(ANALYST))).json()

    rev = await api_client.post(f"/api/v1/change-sets/{edit['change_id']}/revert", json={"reason": "bad restatement"}, headers=H(STEWARD))
    assert rev.status_code == 201, rev.text
    rev_cs = rev.json()["change_set"]
    assert rev_cs["status"] == "merged" and rev_cs["reverts_change_id"] == edit["change_id"]
    claims = (await api_client.get("/api/v1/claims", params={"subject_id": "ent:issuer-verve-cloud", "predicate": "management_ebitda_guidance_fy26"}, headers=H(ANALYST))).json()["claims"]
    current = [c for c in claims if c["superseded_at"] is None]
    original = old_value.get("claim", old_value)["object_value"]
    assert [c["object_value"] for c in current] == [original]
    again = await api_client.post(f"/api/v1/change-sets/{edit['change_id']}/revert", json={}, headers=H(STEWARD))
    assert again.status_code == 422  # already reverted


@pytest.mark.asyncio
async def test_private_side_history_hidden_across_the_wall(api_client: Any) -> None:
    slot = "ent:issuer-solis-medtech|underwriting_term_sheet_spread|private_underwriting|firm|firm"
    assert (await _history(api_client, DEAL, "claim", slot)).status_code == 200
    assert (await _history(api_client, ANALYST, "claim", slot)).status_code == 404
    assert (await api_client.get(f"/api/v1/history/claim/{q(slot)}/diff", headers=H(ANALYST))).status_code == 404
    # A public-side author cannot even propose a change to a record they cannot read
    r = await api_client.post(
        "/api/v1/change-sets",
        json={"title": "peek", "items": [{"record_type": "entity", "op": "upsert", "record_id": "ent:issuer-solis-medtech", "payload": {"description": "x"}}]},
        headers=H(ANALYST),
    )
    assert r.status_code in (403, 404)
    slots = (await api_client.get(f"/api/v1/history/claim-slots/{q('ent:issuer-solis-medtech')}", headers=H(ANALYST))).json()["slots"]
    assert slots == []


@pytest.mark.asyncio
async def test_wall_crossing_needs_compliance_and_steward(api_client: Any) -> None:
    cs = await _propose(
        api_client, STEWARD, "Move Cinder to private side",
        [{"record_type": "entity", "op": "upsert", "record_id": "ent:issuer-cinder-logistics",
          "payload": {"wall_side": "private_side", "read_acl_groups": ["private-credit-deal-team", "fund-stewards"]}}],
    )
    assert cs["status"] == "proposed" and cs["rule_ids"] == ["wall-crossing"]
    assert cs["min_approvals"] == 2
    assert (await api_client.post(f"/api/v1/change-sets/{cs['change_id']}/approve", json={}, headers=H(STEWARD))).status_code == 403
    r = await api_client.post(f"/api/v1/change-sets/{cs['change_id']}/approve", json={}, headers=H(DIRECTOR))
    assert r.status_code == 200 and r.json()["change_set"]["status"] == "proposed"  # still needs a second approver
    # Public-side users cannot see the pending reclassification at all
    assert (await api_client.get(f"/api/v1/change-sets/{cs['change_id']}", headers=H(ANALYST))).status_code == 404


@pytest.mark.asyncio
async def test_reclassified_record_history_disappears_for_public_side(api_client: Any) -> None:
    ctx = api_client._transport.app.state.runtime  # type: ignore[attr-defined]
    store = ctx.registry.store
    ent = await store.get_entity_raw("ent:issuer-cinder-logistics")
    assert (await _history(api_client, ANALYST, "entity", ent.entity_id)).status_code == 200
    await store.upsert_entity(ent.model_copy(update={"wall_side": WallSide.private_side, "read_acl_groups": ["private-credit-deal-team"]}))
    assert (await _history(api_client, ANALYST, "entity", ent.entity_id)).status_code == 404
    hist = (await _history(api_client, DEAL, "entity", ent.entity_id)).json()
    # The deal team sees both versions (the old public one and the new private one)
    assert [v["version_no"] for v in hist["versions"]] == [2, 1]
    assert hist["versions"][0]["source"] == "system" and hist["versions"][0]["kind"] == "direct"


@pytest.mark.asyncio
async def test_releases_and_channels_pin_what_agents_read(api_client: Any) -> None:
    predicate = {"subject_id": "ent:issuer-verve-cloud", "predicate": "management_ebitda_guidance_fy26"}
    assert (await api_client.post("/api/v1/releases", json={}, headers=H(PM))).status_code == 403
    r = await api_client.post("/api/v1/releases", json={"notes": "first"}, headers=H(STEWARD))
    assert r.status_code == 201, r.text
    rel1 = r.json()["release"]["release_id"]
    r = await api_client.put("/api/v1/channels/production", json={"release_id": rel1, "reason": "evals passed"}, headers=H(STEWARD))
    assert r.status_code == 200

    before = [c["object_value"] for c in (await api_client.get("/api/v1/claims", params=predicate, headers=H(ANALYST))).json()["claims"] if c["superseded_at"] is None]
    await _propose(api_client, STEWARD, "New guidance", [{"record_type": "claim", "op": "claim_update", "payload": {"claim_id": "clm-verve-ebitda-guidance", "object_value": "USD 1.2bn"}}])

    dev = [c["object_value"] for c in (await api_client.get("/api/v1/claims", params=predicate, headers=H(ANALYST))).json()["claims"] if c["superseded_at"] is None]
    # Read as of the production release: superseded_at is set now, but was not at the release cut
    prod = [c["object_value"] for c in (await api_client.get("/api/v1/claims", params={**predicate, "channel": "production"}, headers=H(ANALYST))).json()["claims"]]
    assert dev == ["USD 1.2bn"]
    assert prod == before

    slot = "ent:issuer-verve-cloud|management_ebitda_guidance_fy26|credit_research|firm|firm"
    hist = (await _history(api_client, ANALYST, "claim", slot)).json()
    assert hist["versions"][0]["channels"] == [] and "production" in hist["versions"][1]["channels"]
    at_prod = (await api_client.get(f"/api/v1/history/claim/{q(slot)}/at", params={"channel": "production"}, headers=H(ANALYST))).json()
    assert at_prod["payload"]["object_value"] == before[0]

    r2 = (await api_client.post("/api/v1/releases", json={}, headers=H(STEWARD))).json()["release"]
    assert r2["release_id"] == rel1 + 1 and r2["change_ids"]
    listing = (await api_client.get("/api/v1/releases", headers=H(ANALYST))).json()
    assert {c["name"]: c["release_id"] for c in listing["channels"]}["dev"] == r2["release_id"]


@pytest.mark.asyncio
async def test_personal_memory_history_is_owner_only(api_client: Any) -> None:
    cs = await _propose(api_client, PM, "Remember", [{"record_type": "memory", "op": "upsert", "payload": {"key": "pref", "content": "Use bps"}}])
    assert cs["status"] == "merged" and cs["rule_ids"] == ["personal-owner"]
    mem_id = cs["items"][0]["record_id"]
    assert (await _history(api_client, PM, "memory", mem_id)).status_code == 200
    assert (await _history(api_client, STEWARD, "memory", mem_id)).status_code == 404
    assert (await api_client.get(f"/api/v1/change-sets/{cs['change_id']}", headers=H(STEWARD))).status_code == 404


@pytest.mark.asyncio
async def test_direct_writes_are_attributed(api_client: Any) -> None:
    r = await api_client.patch(
        "/api/v1/topics/topic:nav-valuation", json={"alt_labels": ["NAV"]},
        headers=H(STEWARD, **{"X-Change-Source": "api", "X-Run-Id": "run-42"}),
    )
    assert r.status_code == 200, r.text
    hist = (await _history(api_client, ANALYST, "topic", "topic:nav-valuation")).json()
    head = hist["versions"][0]
    assert head["source"] == "api" and head["run_id"] == "run-42" and head["author_id"] == STEWARD
    listed = (await api_client.get("/api/v1/change-sets", params={"include_direct": "true"}, headers=H(STEWARD))).json()
    assert any(c["change_id"] == head["change_id"] for c in listed["change_sets"])


AGENT = {"X-Change-Source": "agent", "X-Agent-Id": "curator-bot", "X-Run-Id": "run-7"}


def _signoff(status: str, value: str) -> dict[str, Any]:
    return {
        "subject_canonical_key": "veh:alpha-credit-master-lp",
        "predicate": "official_nav_audit_signoff",
        "object_value": value,
        "context_purpose": "legal_vehicle",
        "scope_type": "firm",
        "scope_id": "firm",
        "channel": "fact",
        "epistemic_status": status,
        "source_system": "admin-feed",
    }


@pytest.mark.asyncio
async def test_existing_topic_endpoint_files_agent_edit_as_proposal(api_client: Any) -> None:
    r = await api_client.patch(
        "/api/v1/topics/topic:nav-valuation", json={"alt_labels": ["NAV"]},
        headers=H(STEWARD, **{"X-Change-Source": "harness", "X-Agent-Id": "claude-code"}),
    )
    assert r.status_code == 202, r.text
    cs = r.json()["pending_approval"]
    assert cs["status"] == "proposed" and cs["rule_ids"] == ["agent-privileged"] and cs["source"] == "harness"
    assert cs["items"][0]["op"] == "patch" and cs["items"][0]["redline"] is not None
    # Nothing written yet
    topics = (await api_client.get("/api/v1/topics", headers=H(STEWARD))).json()["topics"]
    assert next(t for t in topics if t["topic_id"] == "topic:nav-valuation")["alt_labels"] != ["NAV"]
    # Another steward merges it; it lands through update_topic and is filed under the change set
    r = await api_client.post(f"/api/v1/change-sets/{cs['change_id']}/approve", json={}, headers=H(DIRECTOR))
    assert r.status_code == 200, r.text
    assert r.json()["change_set"]["status"] == "merged"
    topics = (await api_client.get("/api/v1/topics", headers=H(STEWARD))).json()["topics"]
    assert next(t for t in topics if t["topic_id"] == "topic:nav-valuation")["alt_labels"] == ["NAV"]
    head = (await _history(api_client, ANALYST, "topic", "topic:nav-valuation")).json()["versions"][0]
    assert head["change_id"] == cs["change_id"] and head["agent_id"] == "claude-code"


@pytest.mark.asyncio
async def test_existing_claim_endpoint_gates_agent_fact_but_not_steward(api_client: Any) -> None:
    # An agent acting for a steward: a low-risk observation goes straight in...
    r = await api_client.post("/api/v1/claims", json=_signoff("unverified_observation", "SIGNOFF-AGENT-NOTE"), headers=H(STEWARD, **AGENT))
    assert r.status_code in (200, 201), r.text
    # ...but an approved fact waits for a steward
    r = await api_client.post("/api/v1/claims", json=_signoff("approved_fact", "SIGNOFF-AGENT"), headers=H(STEWARD, **AGENT))
    assert r.status_code == 202, r.text
    cs = r.json()["pending_approval"]
    assert cs["agent_id"] == "curator-bot" and cs["run_id"] == "run-7" and cs["approval"] == "review"
    r = await api_client.post(f"/api/v1/change-sets/{cs['change_id']}/approve", json={}, headers=H(DIRECTOR))
    assert r.status_code == 200 and r.json()["change_set"]["status"] == "merged", r.text
    # The same write made by the steward in the UI is auto-approved, as before
    r = await api_client.post("/api/v1/claims", json=_signoff("approved_fact", "SIGNOFF-UI"), headers=H(STEWARD, **{"X-Change-Source": "ui"}))
    assert r.status_code in (200, 201), r.text


@pytest.mark.asyncio
async def test_existing_claim_endpoint_still_refuses_unauthorised_writes(api_client: Any) -> None:
    # Not parked as a proposal: a write the caller may not make is still refused outright
    r = await api_client.post("/api/v1/claims", json=_signoff("approved_fact", "SIGNOFF-ANALYST"), headers=H(ANALYST, **AGENT))
    assert r.status_code == 403, r.text
    listed = (await api_client.get("/api/v1/change-sets", params={"status": "proposed"}, headers=H(STEWARD))).json()
    assert not any(c["author_id"] == ANALYST for c in listed["change_sets"])
