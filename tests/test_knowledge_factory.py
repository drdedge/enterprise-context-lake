"""Oracle 5: Knowledge Factory, Zero-LLM SHA Staleness, Curator & DeepAgents Harness.

Verifies:
  1. Zero-LLM SHA-256 staleness detection when an evidence file / artifact hash changes
     (including CRLF->LF equivalence producing 0 false positives, while actual edits flag stale).
  2. flag_knowledge_gap (kind: gap | stale | wrong | observation) -> POST /api/v1/curator/run ->
     Curator emits reviewable CuratorDraftPatch draft diffs without silently mutating live approved_fact
     and maintaining strict wall_side isolation.
  3. DeepAgents ContextLakeBackend(BackendProtocol) + CompositeBackend mount topology:
     - /knowledge/ (read-only, ACL + wall filtered, evidence redacted)
     - /skills/ (SKILL.md with parsed metadata.include_tools)
     - /memories/ (caller-scoped read-write; disabled on channel_mode=shared / actor_mode=service)
     - / (ephemeral StateBackend)
  4. Viewport Agent deterministic citation grounding (100% offline, zero leaks).
"""

from __future__ import annotations

import inspect
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from deepagents.backends import BackendProtocol, CompositeBackend

from conftest import (
    FIXTURES_DIR,
    build_principal_from_fixture,
    mock_headers,
    resolve_symbol,
)


@pytest.mark.asyncio
async def test_zero_llm_sha256_staleness_detection_and_lf_determinism(tmp_path: Path) -> None:
    """Zero-LLM SHA-256 staleness diff detects mutated evidence while ignoring pure CRLF/LF changes."""
    OkfMarkdownVaultStore = resolve_symbol(
        [
            ("context_adapters.okf_store", "OkfMarkdownVaultStore"),
            ("context_adapters.okf_vault", "OkfMarkdownVaultStore"),
            ("context_adapters.stores.okf", "OkfMarkdownVaultStore"),
            ("context_adapters", "OkfMarkdownVaultStore"),
        ]
    )
    PolicyEngine = resolve_symbol(
        [
            ("context_core.policy", "PolicyEngine"),
            ("context_adapters.policy", "PolicyEngine"),
            ("context_core", "PolicyEngine"),
        ]
    )
    seed_fn = resolve_symbol(
        [
            ("context_core.seed", "seed_store_from_fixtures"),
            ("context_core.seeder", "seed_store_from_fixtures"),
            ("context_core.seed", "seed_fixtures"),
            ("context_core.seeder", "seed_fixtures"),
            ("context_core", "seed_store_from_fixtures"),
            ("context_core", "seed_fixtures"),
        ]
    )

    # Copy fixtures into temp workspace so we can safely mutate a copy without touching repo fixtures!
    temp_repo = tmp_path / "workspace"
    temp_fixtures = temp_repo / "fixtures"
    shutil.copytree(FIXTURES_DIR, temp_fixtures)

    vault_dir = tmp_path / "okf_vault"
    policy = PolicyEngine()
    sig = inspect.signature(OkfMarkdownVaultStore)
    kw: dict[str, Any] = {}
    for k, v in (
        ("vault_dir", vault_dir),
        ("root_dir", vault_dir),
        ("base_dir", vault_dir),
        ("path", vault_dir),
        ("policy", policy),
        ("repo_root", temp_repo),
        ("evidence_root", temp_repo),
    ):
        if k in sig.parameters:
            kw[k] = v
    store = OkfMarkdownVaultStore(**kw)
    try:
        await store.initialize()
        if inspect.iscoroutinefunction(seed_fn):
            ssig = inspect.signature(seed_fn)
            if "fixtures_dir" in ssig.parameters:
                await seed_fn(store, fixtures_dir=temp_fixtures)
            else:
                await seed_fn(store)
        else:
            seed_fn(store)

        diff_fn = getattr(store, "diff_staleness", None) or getattr(store, "check_staleness", None)
        assert diff_fn is not None, "OkfMarkdownVaultStore must expose diff_staleness()"

        async def _run_diff(root: Path) -> Any:
            dsig = inspect.signature(diff_fn)
            dkw = {k: root for k in ("evidence_root", "repo_root", "base_dir") if k in dsig.parameters}
            res = diff_fn(**dkw)
            return await res if inspect.isawaitable(res) else res

        # 1. Clean baseline -> 0 stale items
        report0 = await _run_diff(temp_repo)
        stale0 = report0.stale_items if hasattr(report0, "stale_items") else report0.get("stale_items", [])
        assert len(stale0) == 0, f"Fresh vault should have 0 stale items, got {stale0}"

        # 2. Pure LF -> CRLF line-ending change on a text evidence file MUST NOT trigger false staleness!
        target_ev = temp_fixtures / "uploads" / "analyst_forecast_memo_cinder_logistics.md"
        orig_bytes = target_ev.read_bytes()
        target_ev.write_bytes(orig_bytes.replace(b"\n", b"\r\n"))
        report_crlf = await _run_diff(temp_repo)
        stale_crlf = report_crlf.stale_items if hasattr(report_crlf, "stale_items") else report_crlf.get("stale_items", [])
        assert len(stale_crlf) == 0, f"Pure CRLF line-ending flip falsely triggered staleness: {stale_crlf}"

        # 3. Actual semantic content mutation MUST deterministically flag staleness (zero LLM calls!)
        target_ev.write_bytes(orig_bytes + b"\n## Addendum\nRevised FY2026E leverage to 7.1x.\n")
        report_mut = await _run_diff(temp_repo)
        stale_mut = report_mut.stale_items if hasattr(report_mut, "stale_items") else report_mut.get("stale_items", [])
        assert len(stale_mut) >= 1, "Mutating evidence file content MUST trigger SHA-256 staleness!"
        assert any("cinder" in str(item).lower() for item in stale_mut)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_flag_knowledge_gap_and_curator_draft_patches_without_mutating_facts(api_client, api_app) -> None:
    """flag_knowledge_gap (4 kinds) -> POST /api/v1/curator/run -> reviewable CuratorDraftPatch, wall-isolated."""
    steward_hdrs = mock_headers("user:fund-steward")
    pub_hdrs = mock_headers("user:public-analyst")
    deal_hdrs = mock_headers("user:private-deal-team")

    # 1. Record all 4 knowledge gap kinds (gap, stale, wrong, observation)
    for kind, subject, desc in (
        ("gap", "issuer:cinder-logistics", "Missing Q1 2026 freight spot index covenant cushion metric"),
        ("stale", "veh:alpha-credit-offshore-lp", "Custodian wire instruction contact table needs Q1 refresh"),
        ("wrong", "veh:alpha-credit-offshore-lp", "GIPS sub-account alias ADM-904-GIPS conflicts with Northern Trust"),
        ("observation", "issuer:verve-cloud-infra", "Spin-off TSA agreement terminates Q3 2026"),
    ):
        r = await api_client.post(
            "/api/v1/claims",
            headers=pub_hdrs,
            json={
                "subject_canonical_key": subject,
                "predicate": f"knowledge_{kind}",
                "object_value": desc,
                "kind": kind,
                "context_purpose": "knowledge_factory",
                "scope_type": "firm",
                "scope_id": "firm",
                "channel": "fact",
                "epistemic_status": "unverified_observation",
                "source_system": "agent-gap-flag",
            },
        )
        assert r.status_code in (200, 201), f"Failed recording {kind}: {r.text}"

    # Also plant a private-side observation with a unique canary to verify wall isolation in Curator!
    priv_canary = "CANARY_CURATOR_PRIVATE_GAP_7731"
    r_priv_gap = await api_client.post(
        "/api/v1/claims",
        headers=deal_hdrs,
        json={
            "subject_canonical_key": "issuer:solis-medtech",
            "predicate": "knowledge_gap",
            "object_value": f"Private deal team diligence gap: {priv_canary}",
            "kind": "gap",
            "context_purpose": "private_underwriting",
            "scope_type": "firm",
            "scope_id": "firm",
            "channel": "fact",
            "epistemic_status": "unverified_observation",
            "source_system": "private-dataroom",
        },
    )
    assert r_priv_gap.status_code in (200, 201), r_priv_gap.text

    # 2. Non-steward CANNOT trigger curator run (403)
    r_forbid = await api_client.post("/api/v1/curator/run", headers=pub_hdrs, json={"wall_side": "public_side"})
    assert r_forbid.status_code == 403

    # 3. Steward runs Curator on public_side -> emits reviewable draft patches, ZERO mutation to approved_fact, ZERO private canary!
    r_cur = await api_client.post("/api/v1/curator/run", headers=steward_hdrs, json={"wall_side": "public_side"})
    assert r_cur.status_code == 200, r_cur.text
    cur_body = r_cur.json()
    assert "staleness_report" in cur_body
    patches = cur_body["created_patches"]
    assert len(patches) >= 1, f"Expected Curator to emit >=1 reviewable CuratorDraftPatch: {cur_body}"
    for p in patches:
        assert p["status"] == "pending_steward_review"
        assert p.get("wall_side", "public_side") == "public_side"
        assert priv_canary not in json.dumps(p), "WALL BREACH: Private-side gap leaked into public_side CuratorPatch!"

    # 4. Verify live approved_fact ADM-904 was NOT silently mutated by Curator
    veh = (await api_client.get("/api/v1/entities/veh:alpha-credit-offshore-lp", headers=steward_hdrs)).json()
    approved = [f for f in veh["facts"] if f["predicate"] == "has_admin_code" and f["epistemic_status"] == "approved_fact"]
    assert len(approved) == 1 and approved[0]["object_value"] == "ADM-904"

    # 5. GET /api/v1/curator/patches returns persisted draft patches (public analyst sees only public_side)
    r_list_pub = await api_client.get("/api/v1/curator/patches", headers=pub_hdrs)
    assert r_list_pub.status_code == 200
    assert priv_canary not in r_list_pub.text
    assert len(r_list_pub.json()["patches"]) >= 1


@pytest.mark.asyncio
async def test_deepagents_context_lake_backend_and_composite_mounts(api_app) -> None:
    """Verify ContextLakeBackend(BackendProtocol) & CompositeBackend (/knowledge, /skills, /memories, /)."""
    from context_agents import ContextLakeBackend, create_context_composite_backend

    registry = api_app.state.runtime.registry
    store = registry.store
    policy = registry.policy

    pub_principal = build_principal_from_fixture("user:public-analyst", channel_mode="direct")
    pm_direct = build_principal_from_fixture("user:pm-credit", channel_mode="direct")
    pm_shared = build_principal_from_fixture("user:pm-credit", channel_mode="shared")

    def _make_composite(principal: Any) -> Any:
        return create_context_composite_backend(store, policy, principal)

    comp_pub = _make_composite(pub_principal)
    assert isinstance(comp_pub, CompositeBackend)

    # Verify underlying mount backends implement DeepAgents BackendProtocol
    routes = getattr(comp_pub, "routes", {})
    assert any("/knowledge" in r for r in routes)
    assert any("/skills" in r for r in routes)
    assert any("/memories" in r for r in routes)
    for route_backend in routes.values():
        assert isinstance(route_backend, (BackendProtocol, ContextLakeBackend))

    # 1. /knowledge/ is readable, ACL-filtered, evidence-redacted, and STRICTLY READ-ONLY
    k_entries = await comp_pub.als("/knowledge/") if hasattr(comp_pub, "als") else comp_pub.ls("/knowledge/")
    k_text = json.dumps(k_entries, default=str)
    assert "CANARY_MNPI_COBALT_LME_9174" not in k_text
    assert "CANARY_MNPI_SOLIS_PIPELINE_5382" not in k_text
    assert "project-cobalt" not in k_text.lower()
    assert "solis-medtech" not in k_text.lower()

    # Attempting to write into read-only /knowledge/ MUST fail (error result or PermissionError)
    try:
        w_res = (
            await comp_pub.awrite("/knowledge/illegal_poison.md", "poisoned fact")
            if hasattr(comp_pub, "awrite")
            else comp_pub.write("/knowledge/illegal_poison.md", "poisoned fact")
        )
        err = getattr(w_res, "error", None) if not isinstance(w_res, dict) else w_res.get("error")
        assert err, f"Write to read-only /knowledge/ must fail, got {w_res!r}"
    except PermissionError:
        pass

    # 2. /skills/ exposes all 3 SKILL.md files with YAML metadata.include_tools
    for skill_slug in ("crm-fund-mapping", "gips-portfolio-reporting", "cashflow-wire-reconciliation"):
        skill_path = f"/skills/{skill_slug}/SKILL.md"
        s_content = await comp_pub.aread(skill_path) if hasattr(comp_pub, "aread") else comp_pub.read(skill_path)
        s_str = s_content if isinstance(s_content, str) else getattr(s_content, "content", str(s_content))
        assert "include_tools" in s_str, f"SKILL.md missing metadata.include_tools at {skill_path}: {s_str[:200]}"
        # Parse frontmatter if raw markdown (or strip line numbers if deepagents formatted)
        if "---\n" in s_str:
            fm_block = s_str.split("---\n", 2)[1]
            parsed = yaml.safe_load(fm_block)
            if isinstance(parsed, dict) and "metadata" in parsed:
                assert isinstance(parsed["metadata"].get("include_tools"), list)
                assert len(parsed["metadata"]["include_tools"]) >= 3

    # 3. /memories/ is caller-scoped read-write in direct mode & BLOCKED in shared channel!
    comp_pm_direct = _make_composite(pm_direct)
    comp_pm_shared = _make_composite(pm_shared)

    pm_mem_ls = await comp_pm_direct.als("/memories/") if hasattr(comp_pm_direct, "als") else comp_pm_direct.ls("/memories/")
    pm_mem_entries = pm_mem_ls.entries if hasattr(pm_mem_ls, "entries") else pm_mem_ls
    assert len(pm_mem_entries) >= 1

    # Write into PM's own /memories/ in direct mode succeeds
    m_write = (
        await comp_pm_direct.awrite("/memories/deepagent_scratch.md", "PM thesis update on Apex LME")
        if hasattr(comp_pm_direct, "awrite")
        else comp_pm_direct.write("/memories/deepagent_scratch.md", "PM thesis update on Apex LME")
    )
    m_err = getattr(m_write, "error", None) if not isinstance(m_write, dict) else m_write.get("error")
    assert not m_err, f"Expected direct-mode /memories/ write to succeed, got {m_write!r}"

    # Shared channel: /memories/ MUST be empty AND writes MUST be rejected!
    shared_ls = await comp_pm_shared.als("/memories/") if hasattr(comp_pm_shared, "als") else comp_pm_shared.ls("/memories/")
    shared_entries = shared_ls.entries if hasattr(shared_ls, "entries") else shared_ls
    assert len(shared_entries) == 0, f"Shared channel /memories/ must be empty, got {shared_entries}"
    assert "CANARY_PERSONAL_PM_THESIS_8826" not in json.dumps(shared_entries, default=str)

    try:
        sh_write = (
            await comp_pm_shared.awrite("/memories/blocked_shared.md", "illegal")
            if hasattr(comp_pm_shared, "awrite")
            else comp_pm_shared.write("/memories/blocked_shared.md", "illegal")
        )
        sh_err = getattr(sh_write, "error", None) if not isinstance(sh_write, dict) else sh_write.get("error")
        assert sh_err, f"Shared channel /memories/ write must fail, got {sh_write!r}"
    except PermissionError:
        pass


@pytest.mark.asyncio
async def test_viewport_agent_deterministic_citation_grounding_and_zero_leaks(api_client) -> None:
    """Viewport Agent returns grounded citations & receipt offline with zero MNPI / URI leakage."""
    pub_hdrs = mock_headers("user:public-analyst")
    steward_hdrs = mock_headers("user:fund-steward")

    # 1. Public analyst asks about Alpha Credit Offshore LP admin code conflict in viewport
    r_pub = await api_client.post(
        "/api/v1/agent/chat",
        headers=pub_hdrs,
        json={
            "message": "What is the approved admin code for Alpha Credit Offshore LP, what conflicts exist, and is there any MNPI on Project Cobalt?",
            "viewport": {
                "lens": "fund_hierarchy",
                "visible_entity_ids": ["ent:veh-alpha-offshore", "ent:grp-alpha-gips", "ent:issuer-apex-industrial"],
                "selected_entity_id": "ent:veh-alpha-offshore",
                "as_of": "2026-03-15T00:00:00Z",
                "recorded_as_of": "2026-03-15T12:00:00Z",
            },
        },
    )
    assert r_pub.status_code == 200, r_pub.text
    body = r_pub.json()
    assert body["answer_markdown"]
    assert "ADM-904" in body["answer_markdown"]
    assert len(body["citations"]) >= 1
    assert any(c["kind"] in ("entity", "claim", "conflict") for c in body["citations"])
    assert body["receipt"]["receipt_id"]
    assert body["receipt"]["principal_id"] == "user:public-analyst"

    # Zero leakage of restricted evidence URI or MNPI canaries!
    raw = r_pub.text
    for forbidden in (
        "CANARY_MNPI_COBALT_LME_9174",
        "CANARY_MNPI_SOLIS_PIPELINE_5382",
        "CANARY_SIDELETTER_MFN_FEE_4091",
        "alpha_credit_lp_side_letter.pdf",
        "CANARY_PERSONAL_PM_THESIS_8826",
    ):
        assert forbidden not in raw, f"Viewport Agent leaked {forbidden!r} to public-analyst!"

    # 2. Alias endpoint /api/v1/agent/query also works identically
    r_alias = await api_client.post(
        "/api/v1/agent/query",
        headers=steward_hdrs,
        json={
            "message": "Summarize Q4 2025 restated NAV for Alpha Credit composite.",
            "viewport": {
                "lens": "topic",
                "visible_entity_ids": ["ent:grp-alpha-gips"],
                "selected_entity_id": "ent:grp-alpha-gips",
                "as_of": "2025-12-31T23:59:59Z",
                "recorded_as_of": "2026-02-25T00:00:00Z",
            },
        },
    )
    assert r_alias.status_code == 200, r_alias.text
    assert r_alias.json()["receipt"]["receipt_id"]
