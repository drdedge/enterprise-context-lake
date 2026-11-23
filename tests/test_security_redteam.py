"""Oracle 2: Exhaustive Security Red-Team & Zero-Leakage Matrix (All 9 Surfaces).

Verifies zero canary-token leakage, zero existence-oracle leakage, and zero
count-oracle inflation across all 9 attack surfaces:
  1. REST entity / claim / stream / context bundle endpoints
  2. Search / typeahead (GET /api/v1/search)
  3. Graph nodes & neighbors (GET /api/v1/graph & GET /api/v1/map/graph)
  4. Reverse impact / backlinks (GET /api/v1/map/impact/{node_id})
  5. Pre-aggregation facet / topic / degree badge counts
  6. Compiled .sqlite.zst binary file inspection (raw bytes + tables)
  7. Raw FastMCP primitives (/mcp: filter, search, read, neighbours, lookup, resolve_context, context://memory/me)
  8. Server-Side Code Mode MCP (/mcp/codemode: Node --permission sandbox escape & SSRF probes)
  9. Shared-channel & service-actor personal-memory gating

Plus:
  - Entra >200-group overage resolution (user:overage-director)
  - Revoked principal hard 401 rejection (user:revoked-analyst + live revocation)
  - Split claim-vs-evidence ACL redaction ("Restricted — Request Access", zero URI/canary leakage)
  - Steward RBAC on force_supersede & conflict resolution (403 non-steward, 409 TOCTOU stale epoch)
"""

from __future__ import annotations

import compression.zstd as zstd
import inspect
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    FIXTURES_DIR,
    build_principal_from_fixture,
    load_json_fixture,
    mock_headers,
    resolve_symbol,
)


def _assert_no_tokens(blob: str, forbidden_tokens: list[str], *, context: str) -> None:
    for tok in forbidden_tokens:
        assert tok not in blob, f"SECURITY LEAKAGE ({context}): forbidden token {tok!r} appeared in payload!"


async def _call_mcp_tool(mcp_server_or_client: Any, tool_name: str, arguments: dict[str, Any], principal: Any) -> Any:
    """Invoke a FastMCP tool directly or over MCP helper with caller Principal."""
    # Try FastMCP v2/v4 direct tool dispatch or helper function in context_mcp
    if hasattr(mcp_server_or_client, "call_tool"):
        sig = inspect.signature(mcp_server_or_client.call_tool)
        if "principal" in sig.parameters:
            return await mcp_server_or_client.call_tool(tool_name, arguments, principal=principal)
        return await mcp_server_or_client.call_tool(tool_name, arguments)
    raise AssertionError(f"Unsupported MCP dispatcher object: {mcp_server_or_client!r}")


@pytest.mark.asyncio
async def test_surface_1_rest_entities_claims_bundles_and_split_evidence(api_client, principals_fixture) -> None:
    """Surface 1: REST entity/claim/bundle zero existence leakage + split evidence redaction."""
    canaries = principals_fixture["canaries"]
    cobalt_tok = "CANARY_MNPI_COBALT_LME_9174"
    solis_tok = "CANARY_MNPI_SOLIS_PIPELINE_5382"
    sideletter_tok = "CANARY_SIDELETTER_MFN_FEE_4091"
    pm_mem_tok = "CANARY_PERSONAL_PM_THESIS_8826"
    deal_mem_tok = "CANARY_PERSONAL_DEAL_NOTES_6419"

    # 1a. Unauthorized direct GET on restricted MNPI entities MUST return 404 (same as nonexistent), NEVER 403
    for uid in ("user:product-lead", "user:pm-credit", "user:public-analyst", "user:rp-ir"):
        hdrs = mock_headers(uid)
        r_nonexistent = await api_client.get("/api/v1/entities/ent:does-not-exist-xyz", headers=hdrs)
        assert r_nonexistent.status_code == 404

        for restricted_key in ("proj:project-cobalt-lme", "ent:proj-project-cobalt-lme", "issuer:solis-medtech", "ent:issuer-solis-medtech"):
            r_res = await api_client.get(f"/api/v1/entities/{restricted_key}", headers=hdrs)
            assert r_res.status_code == 404, f"{uid} got {r_res.status_code} on {restricted_key}; expected 404 zero existence leakage"
            _assert_no_tokens(r_res.text, [cobalt_tok, solis_tok, "Project Cobalt", "Solis MedTech"], context=f"GET {restricted_key} as {uid}")

        # List entities, stream, and context bundle MUST omit all unauthorized canaries
        for method, path, body in (
            ("GET", "/api/v1/entities", None),
            ("GET", "/api/v1/stream", None),
            ("GET", "/api/v1/conflicts", None),
            (
                "POST",
                "/api/v1/context/bundle",
                {"entity_keys": ["veh:alpha-credit-offshore-lp", "proj:project-cobalt-lme", "issuer:solis-medtech"]},
            ),
        ):
            r = (
                await api_client.get(path, headers=hdrs)
                if method == "GET"
                else await api_client.post(path, headers=hdrs, json=body)
            )
            assert r.status_code == 200, f"{method} {path} as {uid} failed: {r.text}"
            forbidden = [cobalt_tok, solis_tok, sideletter_tok, "alpha_credit_lp_side_letter.pdf", deal_mem_tok]
            if uid != "user:pm-credit":
                forbidden.append(pm_mem_tok)
            _assert_no_tokens(r.text, forbidden, context=f"{method} {path} as {uid}")

    # 1b. Split claim-vs-evidence ACL redaction on clm-admin-code-custodian
    for uid in ("user:product-lead", "user:pm-credit", "user:public-analyst", "user:rp-ir"):
        hdrs = mock_headers(uid)
        r = await api_client.get("/api/v1/entities/veh:alpha-credit-offshore-lp", headers=hdrs)
        assert r.status_code == 200, r.text
        _assert_no_tokens(
            r.text,
            [sideletter_tok, "alpha_credit_lp_side_letter.pdf", "CANARY_SIDELETTER"],
            context=f"split-evidence entity card as {uid}",
        )
        card = r.json()
        all_claims = list(card.get("facts", [])) + list(card.get("directives", []))
        for cset in card.get("conflicts", []):
            all_claims.extend(cset.get("claims", []))
        custodian_claims = [c for c in all_claims if c.get("claim_id") == "clm-admin-code-custodian"]
        assert custodian_claims, f"{uid} should see clm-admin-code-custodian claim (read_acl_groups=['firm-all'])"
        clm = custodian_claims[0]
        assert clm["object_value"] == "ADM-904X"
        assert clm["evidence_redacted"] is True
        assert clm["evidence_redaction_label"] == "Restricted — Request Access"
        assert clm["provenance"]["evidence_uri"] is None
        assert clm["provenance"]["excerpt"] is None
        assert clm["provenance"]["span_locator"] is None

    # Authorized evidence readers DO see evidence_uri & excerpt
    for uid, ov in (("user:private-deal-team", False), ("user:fund-steward", False), ("user:overage-director", True)):
        hdrs = mock_headers(uid, groups_overage=ov)
        r = await api_client.get("/api/v1/entities/veh:alpha-credit-offshore-lp", headers=hdrs)
        assert r.status_code == 200, r.text
        card = r.json()
        all_claims = list(card.get("facts", []))
        for cset in card.get("conflicts", []):
            all_claims.extend(cset.get("claims", []))
        clm = next(c for c in all_claims if c.get("claim_id") == "clm-admin-code-custodian")
        assert clm["evidence_redacted"] is False
        assert clm["provenance"]["evidence_uri"] == "fixtures/uploads/alpha_credit_lp_side_letter.pdf"

    # Fund Steward is above_the_wall, sees Cobalt (compliance-oversight) but NOT Solis (private-credit-deal-team only!)
    steward_hdrs = mock_headers("user:fund-steward")
    r_cobalt = await api_client.get("/api/v1/entities/proj:project-cobalt-lme", headers=steward_hdrs)
    assert r_cobalt.status_code == 200
    assert cobalt_tok in r_cobalt.text

    r_solis_steward = await api_client.get("/api/v1/entities/issuer:solis-medtech", headers=steward_hdrs)
    assert r_solis_steward.status_code == 404
    _assert_no_tokens(r_solis_steward.text, [solis_tok], context="fund-steward Solis check")
    assert canaries  # fixture loaded


@pytest.mark.asyncio
async def test_surface_2_search_typeahead_zero_leakage(api_client, principals_fixture) -> None:
    """Surface 2: GET /api/v1/search omits all unauthorized entities, claims, excerpts & canaries."""
    probes = [
        "CANARY_MNPI_COBALT_LME_9174",
        "CANARY_MNPI_SOLIS_PIPELINE_5382",
        "CANARY_SIDELETTER_MFN_FEE_4091",
        "CANARY_PERSONAL_PM_THESIS_8826",
        "CANARY_PERSONAL_DEAL_NOTES_6419",
        "Project Cobalt",
        "Solis MedTech",
        "92.5 cents",
        "SOFR+575",
        "alpha_credit_lp_side_letter",
    ]
    for uid in ("user:product-lead", "user:public-analyst", "user:rp-ir"):
        hdrs = mock_headers(uid)
        for q in probes:
            r = await api_client.get("/api/v1/search", headers=hdrs, params={"q": q})
            assert r.status_code == 200, r.text
            _assert_no_tokens(
                r.text,
                [
                    "CANARY_MNPI_COBALT_LME_9174",
                    "CANARY_MNPI_SOLIS_PIPELINE_5382",
                    "CANARY_SIDELETTER_MFN_FEE_4091",
                    "CANARY_PERSONAL_PM_THESIS_8826",
                    "CANARY_PERSONAL_DEAL_NOTES_6419",
                    "proj:project-cobalt-lme",
                    "issuer:solis-medtech",
                    "alpha_credit_lp_side_letter.pdf",
                ],
                context=f"search q={q!r} as {uid}",
            )
            if q.startswith("CANARY_") or q in ("92.5 cents", "SOFR+575", "alpha_credit_lp_side_letter"):
                assert len(r.json().get("hits", [])) == 0, f"{uid} got search hits for forbidden query {q!r}: {r.json()}"
            elif q in ("Project Cobalt", "Solis MedTech"):
                # Public entities issuer:apex-industrial & sponsor:kestrel-equity-partners mention
                # these strings in their public descriptions/aliases, but the private-side entities
                # and MNPI claims MUST NEVER appear in hits!
                hit_ids = {h.get("id", "") for h in r.json().get("hits", [])} | {
                    h.get("canonical_key", "") for h in r.json().get("hits", [])
                }
                assert "proj:project-cobalt-lme" not in hit_ids
                assert "ent:proj-project-cobalt-lme" not in hit_ids
                assert "clm-mnpi-cobalt-lme-terms" not in hit_ids
                assert "issuer:solis-medtech" not in hit_ids
                assert "ent:issuer-solis-medtech" not in hit_ids
                assert "clm-mnpi-solis-tlb-pricing" not in hit_ids

    # Authorized private deal team CAN search Project Cobalt & Solis MedTech
    deal_hdrs = mock_headers("user:private-deal-team")
    r_deal = await api_client.get("/api/v1/search", headers=deal_hdrs, params={"q": "Solis MedTech"})
    assert r_deal.status_code == 200
    assert len(r_deal.json()["hits"]) >= 1


@pytest.mark.asyncio
async def test_surface_3_and_4_graph_and_reverse_impact_zero_leakage(api_client) -> None:
    """Surfaces 3 & 4: Graph (/api/v1/graph & /api/v1/map/graph) and reverse impact (/api/v1/map/impact/{node_id})."""
    pub_hdrs = mock_headers("user:public-analyst")
    deal_hdrs = mock_headers("user:private-deal-team")
    forbidden_ids = {"ent:proj-project-cobalt-lme", "ent:issuer-solis-medtech", "proj:project-cobalt-lme", "issuer:solis-medtech"}
    forbidden_tokens = [
        "CANARY_MNPI_COBALT_LME_9174",
        "CANARY_MNPI_SOLIS_PIPELINE_5382",
        "CANARY_SIDELETTER_MFN_FEE_4091",
        "CANARY_PERSONAL_PM_THESIS_8826",
        "CANARY_PERSONAL_DEAL_NOTES_6419",
        "Project Cobalt",
        "Solis MedTech",
    ]

    for graph_path in ("/api/v1/graph", "/api/v1/map/graph"):
        for lens in ("topic", "issuer", "project", "fund_hierarchy"):
            r_pub = await api_client.get(graph_path, headers=pub_hdrs, params={"lens": lens})
            assert r_pub.status_code == 200, f"{graph_path}?lens={lens} failed: {r_pub.text}"
            _assert_no_tokens(r_pub.text, forbidden_tokens, context=f"{graph_path}?lens={lens} as public-analyst")
            node_ids = {n["id"] for n in r_pub.json().get("nodes", [])} | {
                n.get("canonical_key", "") for n in r_pub.json().get("nodes", [])
            }
            assert not (node_ids & forbidden_ids), f"Forbidden MNPI nodes leaked into {graph_path}?lens={lens}: {node_ids & forbidden_ids}"

    # Verify private deal team DOES see Cobalt and Solis in graph
    r_deal_graph = await api_client.get("/api/v1/graph", headers=deal_hdrs, params={"lens": "topic"})
    assert r_deal_graph.status_code == 200
    deal_keys = {n.get("canonical_key") for n in r_deal_graph.json().get("nodes", [])}
    assert "proj:project-cobalt-lme" in deal_keys or "issuer:solis-medtech" in deal_keys

    # Surface 4: Reverse impact traversal
    for start_node in ("topic:lme-restructuring", "topic:investment-thesis", "ent:issuer-apex-industrial", "ent:sponsor-kestrel"):
        r_imp = await api_client.get(f"/api/v1/map/impact/{start_node}", headers=pub_hdrs)
        assert r_imp.status_code == 200, f"impact/{start_node} failed: {r_imp.text}"
        _assert_no_tokens(r_imp.text, forbidden_tokens, context=f"impact/{start_node} as public-analyst")
        imp_node_ids = {n["id"] for n in r_imp.json().get("nodes", [])} | {
            n.get("canonical_key", "") for n in r_imp.json().get("nodes", [])
        }
        assert not (imp_node_ids & forbidden_ids), f"MNPI node leaked into impact/{start_node}: {imp_node_ids & forbidden_ids}"

    # Direct impact request on an unauthorized node MUST return 404 (zero existence leakage)
    r_imp_unauth = await api_client.get("/api/v1/map/impact/ent:proj-project-cobalt-lme", headers=pub_hdrs)
    assert r_imp_unauth.status_code == 404


@pytest.mark.asyncio
async def test_surface_5_pre_aggregation_count_oracle_protection(api_client) -> None:
    """Surface 5: Pre-aggregation topic, facet, entity, and graph counts MUST NOT be inflated by restricted records."""
    pub_hdrs = mock_headers("user:public-analyst")
    deal_hdrs = mock_headers("user:private-deal-team")
    overage_hdrs = mock_headers("user:overage-director", groups_overage=True)

    # 1. Entity total count: public-analyst lacks proj:project-cobalt-lme & issuer:solis-medtech (2 private entities)
    ent_pub = (await api_client.get("/api/v1/entities", headers=pub_hdrs)).json()
    ent_deal = (await api_client.get("/api/v1/entities", headers=deal_hdrs)).json()
    ent_ov = (await api_client.get("/api/v1/entities", headers=overage_hdrs)).json()

    assert ent_pub["total"] == len(ent_pub["items"])
    assert ent_deal["total"] == len(ent_deal["items"])
    assert ent_pub["total"] < ent_deal["total"], (
        f"Count oracle bug: public-analyst total ({ent_pub['total']}) should be < private-deal-team ({ent_deal['total']})"
    )
    assert ent_deal["total"] - ent_pub["total"] >= 2
    assert ent_ov["total"] == ent_deal["total"]

    # 2. Topic attachment badge counts: lme-restructuring has private att-07; investment-thesis has private att-08
    topics_pub = {t["slug"]: t for t in (await api_client.get("/api/v1/topics", headers=pub_hdrs)).json()["topics"]}
    topics_deal = {t["slug"]: t for t in (await api_client.get("/api/v1/topics", headers=deal_hdrs)).json()["topics"]}

    def _topic_count(t: dict[str, Any]) -> int:
        for k in ("item_count", "attachment_count", "count", "entity_count"):
            if k in t and isinstance(t[k], int):
                return t[k]
        raise AssertionError(f"Topic DTO missing integer count field: {t}")

    assert _topic_count(topics_pub["lme-restructuring"]) < _topic_count(topics_deal["lme-restructuring"]), (
        f"Topic count oracle leak on lme-restructuring: pub={topics_pub['lme-restructuring']} vs deal={topics_deal['lme-restructuring']}"
    )
    assert _topic_count(topics_pub["investment-thesis"]) < _topic_count(topics_deal["investment-thesis"]), (
        f"Topic count oracle leak on investment-thesis: pub={topics_pub['investment-thesis']} vs deal={topics_deal['investment-thesis']}"
    )

    # 3. Graph stats node_count strictly post-ACL
    g_pub = (await api_client.get("/api/v1/graph", headers=pub_hdrs, params={"lens": "topic"})).json()
    g_deal = (await api_client.get("/api/v1/graph", headers=deal_hdrs, params={"lens": "topic"})).json()
    assert g_pub["stats"]["node_count"] == len(g_pub["nodes"])
    assert g_pub["stats"]["node_count"] < g_deal["stats"]["node_count"]


@pytest.mark.asyncio
async def test_surface_6_compiled_sqlite_zst_binary_inspection(tmp_path: Path) -> None:
    """Surface 6: Decompress compiled .sqlite.zst partitions and inspect RAW SQLite bytes & tables."""
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

    db_path = tmp_path / "snap_audit.sqlite3"
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    policy = PolicyEngine()
    store = SqliteBitemporalStore(db_path=db_path, policy=policy) if "policy" in inspect.signature(SqliteBitemporalStore).parameters else SqliteBitemporalStore(db_path)

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

        csig = inspect.signature(SnapshotCompiler)
        ckw = {
            k: v
            for k, v in (
                ("store", store),
                ("policy", policy),
                ("output_dir", snap_dir),
                ("snapshot_dir", snap_dir),
                ("snapshots_dir", snap_dir),
                ("storage_dir", snap_dir),
                ("base_dir", snap_dir),
            )
            if k in csig.parameters
        }
        compiler = SnapshotCompiler(**ckw)

        pub_manifest = await compiler.compile_and_publish("firm-all", "public_side")
        pub_ver = pub_manifest.version_id if hasattr(pub_manifest, "version_id") else pub_manifest["version_id"]
        pub_zst = await compiler.get_snapshot_bytes("firm-all", "public_side", pub_ver)
        pub_raw_sqlite = zstd.decompress(pub_zst)

        # Raw byte-level grep over entire decompressed SQLite image!
        forbidden_bytes = [
            b"CANARY_MNPI_COBALT_LME_9174",
            b"CANARY_MNPI_SOLIS_PIPELINE_5382",
            b"CANARY_SIDELETTER_MFN_FEE_4091",
            b"CANARY_PERSONAL_PM_THESIS_8826",
            b"CANARY_PERSONAL_DEAL_NOTES_6419",
            b"alpha_credit_lp_side_letter.pdf",
            b"proj:project-cobalt-lme",
            b"issuer:solis-medtech",
        ]
        for needle in forbidden_bytes:
            assert pub_raw_sqlite.find(needle) == -1, (
                f"BINARY SNAPSHOT LEAK in firm-all__public_side v{pub_ver}.sqlite.zst: found {needle!r}!"
            )

        # Deserialize in RAM and inspect SQLite tables directly
        conn = sqlite3.connect(":memory:")
        try:
            conn.deserialize(pub_raw_sqlite)
            cur = conn.cursor()
            # Confirm clm-admin-code-custodian exists in public snapshot with redacted evidence
            cols = {r[1] for r in cur.execute("PRAGMA table_info(claims)").fetchall()}
            sel_cols = ["object_value"]
            for opt_col in ("evidence_redacted", "evidence_uri", "excerpt", "provenance_json"):
                if opt_col in cols:
                    sel_cols.append(opt_col)
            row = cur.execute(
                f"SELECT {', '.join(sel_cols)} FROM claims WHERE claim_id = ?",
                ("clm-admin-code-custodian",),
            ).fetchone()
            assert row is not None, "Expected clm-admin-code-custodian in firm-all public snapshot"
            row_map = dict(zip(sel_cols, row))
            assert row_map["object_value"] == "ADM-904X"
            if "evidence_redacted" in row_map:
                assert bool(row_map["evidence_redacted"]) is True
            if "evidence_uri" in row_map:
                assert row_map["evidence_uri"] in (None, "")
            if "excerpt" in row_map:
                assert row_map["excerpt"] in (None, "")
            if "provenance_json" in row_map and row_map["provenance_json"]:
                prov = json.loads(row_map["provenance_json"])
                assert prov.get("evidence_uri") in (None, "")
                assert prov.get("excerpt") in (None, "")

            # Confirm personal memories NEVER compile into shared snapshots
            tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            for t in tables:
                rows_dump = repr(cur.execute(f"SELECT * FROM {t}").fetchall())
                _assert_no_tokens(
                    rows_dump,
                    [
                        "CANARY_MNPI_COBALT_LME_9174",
                        "CANARY_MNPI_SOLIS_PIPELINE_5382",
                        "CANARY_SIDELETTER_MFN_FEE_4091",
                        "CANARY_PERSONAL_PM_THESIS_8826",
                        "CANARY_PERSONAL_DEAL_NOTES_6419",
                    ],
                    context=f"snapshot table {t}",
                )
        finally:
            conn.close()

        # Private deal team partition SHOULD contain Solis (private-credit-deal-team), STILL ZERO personal memories!
        priv_manifest = await compiler.compile_and_publish("private-credit-deal-team", "private_side")
        priv_ver = priv_manifest.version_id if hasattr(priv_manifest, "version_id") else priv_manifest["version_id"]
        priv_raw = zstd.decompress(await compiler.get_snapshot_bytes("private-credit-deal-team", "private_side", priv_ver))
        assert b"CANARY_MNPI_SOLIS_PIPELINE_5382" in priv_raw
        assert b"CANARY_PERSONAL_PM_THESIS_8826" not in priv_raw
        assert b"CANARY_PERSONAL_DEAL_NOTES_6419" not in priv_raw
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_surface_7_raw_fastmcp_primitives_zero_leakage(api_app) -> None:
    """Surface 7: Raw FastMCP primitives (filter, search, read, neighbours, lookup, resolve_context, memory)."""
    import context_mcp

    pub_principal = build_principal_from_fixture("user:public-analyst", channel_mode="direct")
    pm_shared = build_principal_from_fixture("user:pm-credit", channel_mode="shared")
    pm_direct = build_principal_from_fixture("user:pm-credit", channel_mode="direct")

    mcp_runtime = api_app.state.runtime.mcp_runtime

    tool_map = {
        "filter": context_mcp.tool_filter,
        "search": context_mcp.tool_search,
        "read": context_mcp.tool_read,
        "neighbours": context_mcp.tool_neighbours,
        "lookup": context_mcp.tool_lookup,
        "resolve_context": context_mcp.tool_resolve_context,
        "memory_upsert": context_mcp.tool_memory_upsert,
    }

    async def _invoke(name: str, args: dict[str, Any], principal: Any) -> Any:
        fn = tool_map[name]
        return await fn(mcp_runtime, principal, **args)

    forbidden = [
        "CANARY_MNPI_COBALT_LME_9174",
        "CANARY_MNPI_SOLIS_PIPELINE_5382",
        "CANARY_SIDELETTER_MFN_FEE_4091",
        "CANARY_PERSONAL_PM_THESIS_8826",
        "CANARY_PERSONAL_DEAL_NOTES_6419",
        "alpha_credit_lp_side_letter.pdf",
    ]

    for tool_name, args in (
        ("filter", {"entity_types": ["project", "issuer"]}),
        ("search", {"query": "CANARY_MNPI"}),
        ("search", {"query": "Project Cobalt"}),
        ("read", {"id_or_key": "proj:project-cobalt-lme"}),
        ("read", {"id_or_key": "veh:alpha-credit-offshore-lp"}),
        ("neighbours", {"id_or_key": "issuer:apex-industrial", "depth": 2}),
        ("lookup", {"canonical_key": "veh:alpha-credit-offshore-lp", "predicate": "has_admin_code", "context_purpose": "legal_vehicle"}),
        ("resolve_context", {"entity_keys": ["veh:alpha-credit-offshore-lp", "proj:project-cobalt-lme"]}),
    ):
        res = await _invoke(tool_name, args, pub_principal)
        res_json = json.dumps(res, default=str)
        _assert_no_tokens(res_json, forbidden, context=f"FastMCP {tool_name}({args}) as public-analyst")

    # Resource context://memory/me — direct vs shared channel gating
    mem_direct = await context_mcp.read_memory_me_resource(mcp_runtime, pm_direct)
    assert "CANARY_PERSONAL_PM_THESIS_8826" in json.dumps(mem_direct, default=str)

    mem_shared = await context_mcp.read_memory_me_resource(mcp_runtime, pm_shared)
    mem_shared_str = json.dumps(mem_shared, default=str)
    _assert_no_tokens(mem_shared_str, ["CANARY_PERSONAL_PM_THESIS_8826"], context="context://memory/me on shared channel")
    if isinstance(mem_shared, dict):
        assert mem_shared.get("memory_enabled") is False
        assert mem_shared.get("notes", []) == []

    # Surface-gated memory_upsert MUST fail on shared channel
    with pytest.raises(Exception) as exc_info:
        await _invoke(
            "memory_upsert",
            {"scope_type": "user", "key": "illegal-shared-write", "content": "should fail"},
            pm_shared,
        )
    assert any(tok in str(exc_info.value).lower() for tok in ("403", "forbidden", "shared", "disabled", "surface", "policy", "not allowed"))


@pytest.mark.asyncio
async def test_surface_8_codemode_sandbox_escape_ssrf_and_caps(api_app) -> None:
    """Surface 8: Server-Side Code Mode Node --permission sandbox escape, SSRF, env exfil, cross-wall RPC & >64KiB cap."""
    import context_mcp

    mcp_runtime = api_app.state.runtime.mcp_runtime

    async def _exec(code: str, principal: Any, timeout_ms: int = 3000) -> dict[str, Any]:
        res = await context_mcp.execute_code(
            code,
            runtime=mcp_runtime,
            principal=principal,
            timeout_ms=timeout_ms,
        )
        return res if isinstance(res, dict) else res.model_dump()

    pub_principal = build_principal_from_fixture("user:public-analyst", channel_mode="direct")
    pm_shared = build_principal_from_fixture("user:pm-credit", channel_mode="shared")

    # Plant a canary secret in host process env to verify zero env inheritance into Node isolate!
    host_secret = "HOST_SECRET_CANARY_DO_NOT_EXFIL_9911"
    os.environ["ECL_REDTEAM_HOST_SECRET"] = host_secret
    try:
        # 1. Host filesystem read attempts
        fs_attempts = [
            "const fs = require('fs'); return fs.readFileSync('fixtures/principals.json', 'utf8');",
            "const fs = await import('node:fs'); return fs.readFileSync('fixtures/principals.json', 'utf8');",
            "return globalThis.process?.mainModule?.require('fs')?.readFileSync('fixtures/principals.json', 'utf8');",
        ]
        for code in fs_attempts:
            res = await _exec(code, pub_principal)
            blob = json.dumps(res, default=str)
            assert "CANARY_MNPI_COBALT_LME_9174" not in blob, f"SANDBOX FS ESCAPE: {code}"
            assert "rachel.kim@meridian.local" not in blob, f"SANDBOX FS ESCAPE: {code}"

        # 2. process.env secret exfiltration attempt
        env_attempts = [
            "return JSON.stringify(typeof process !== 'undefined' ? process.env : {});",
            "const c = this.constructor.constructor('return process.env')(); return JSON.stringify(c);",
        ]
        for code in env_attempts:
            res = await _exec(code, pub_principal)
            blob = json.dumps(res, default=str)
            assert host_secret not in blob, f"SANDBOX ENV EXFIL ESCAPE: {code}"

        # 3. child_process spawn attempt
        cp_res = await _exec(
            "const cp = require('child_process'); return cp.execSync('echo PWNED_SHELL').toString();",
            pub_principal,
        )
        assert "PWNED_SHELL" not in json.dumps(cp_res, default=str)

        # 4. Network / SSRF attempts (direct fetch + SSRF URI via host RPC)
        net_res = await _exec(
            "const r = await fetch('http://169.254.169.254/latest/meta-data/'); return await r.text();",
            pub_principal,
        )
        assert net_res.get("ok") is False or net_res.get("error"), f"fetch() must be blocked in sandbox: {net_res}"

        ssrf_rpc_res = await _exec(
            """
            return await lake.proposeUpdate({
                subject_canonical_key: "veh:alpha-credit-offshore-lp",
                kind: "observation",
                predicate: "ssrf_probe",
                object_value: "test",
                rationale: "ssrf test",
                evidence_uri: "http://169.254.169.254/latest/meta-data/?token=SECRET_TOKEN_123"
            });
            """,
            pub_principal,
        )
        blob_ssrf = json.dumps(ssrf_rpc_res, default=str)
        assert "SECRET_TOKEN_123" not in blob_ssrf
        assert ssrf_rpc_res.get("ok") is False or ssrf_rpc_res.get("error") or "blocked" in blob_ssrf.lower() or "ssrf" in blob_ssrf.lower()

        # 5. Cross-wall & cross-user RPC attempts inside sandbox
        cross_wall = await _exec(
            """
            const a = await lake.read({ id_or_key: "proj:project-cobalt-lme" });
            const b = await lake.search({ query: "CANARY_MNPI_COBALT_LME_9174" });
            const c = await lake.read({ id_or_key: "veh:alpha-credit-offshore-lp" });
            return { a, b, c };
            """,
            pub_principal,
        )
        cw_res = cross_wall.get("result", {})
        assert cw_res["a"].get("found") is False
        assert cw_res["b"].get("hits") == []
        # Strip only the caller's own echoed search query string before scanning entire response for leaks
        b_without_echoed_query = {k: v for k, v in cw_res["b"].items() if k != "query"}
        sanitized_check_blob = json.dumps(
            {"a": cw_res["a"], "b": b_without_echoed_query, "c": cw_res["c"], "logs": cross_wall.get("logs")},
            default=str,
        )
        _assert_no_tokens(
            sanitized_check_blob,
            [
                "CANARY_MNPI_COBALT_LME_9174",
                "CANARY_MNPI_SOLIS_PIPELINE_5382",
                "CANARY_SIDELETTER_MFN_FEE_4091",
                "alpha_credit_lp_side_letter.pdf",
                "CANARY_PERSONAL_PM_THESIS_8826",
            ],
            context="CodeMode cross-wall RPC as public-analyst",
        )

        # 6. Shared-channel memoryUpsert blocked inside Code Mode
        shared_mem = await _exec(
            """
            return await lake.memoryUpsert({
                scope_type: "user",
                key: "shared-leak",
                content: "should fail"
            });
            """,
            pm_shared,
        )
        assert shared_mem.get("ok") is False or shared_mem.get("error"), f"Shared-channel memoryUpsert must fail: {shared_mem}"

        # 7. >64 KiB output truncation cap
        big_res = await _exec("return 'A'.repeat(120000);", pub_principal)
        assert big_res.get("truncated") is True, f"Expected >64KiB truncation flag: {big_res.keys()}"
        serialized_result = json.dumps(big_res.get("result", big_res.get("output", "")))
        assert len(serialized_result.encode("utf-8")) <= 66560

        # 8. Timeout enforcement (infinite loop killed cleanly)
        loop_res = await _exec("while (true) {}", pub_principal, timeout_ms=500)
        assert loop_res.get("ok") is False or loop_res.get("timed_out") is True or loop_res.get("error")
    finally:
        os.environ.pop("ECL_REDTEAM_HOST_SECRET", None)


@pytest.mark.asyncio
async def test_surface_9_shared_channel_and_service_actor_memory_gating(api_client) -> None:
    """Surface 9: channel_mode=shared and actor_mode=service hide ALL personal memory & user_x_skill claims."""
    # Direct OBO mode: PM sees own personal memory + user_x_skill preference
    direct_hdrs = mock_headers("user:pm-credit", actor_mode="obo", channel_mode="direct")
    r_dir_notes = await api_client.get("/api/v1/memory/notes", headers=direct_hdrs)
    assert r_dir_notes.status_code == 200
    assert r_dir_notes.json()["memory_enabled"] is True
    assert "CANARY_PERSONAL_PM_THESIS_8826" in r_dir_notes.text

    # Shared channel & Service actor: personal notes, watchlist, bundle memories, user_x_skill claims ALL hidden!
    for mode_label, hdrs in (
        ("shared_channel", mock_headers("user:pm-credit", actor_mode="obo", channel_mode="shared")),
        ("service_actor", mock_headers("user:pm-credit", actor_mode="service", channel_mode="direct")),
    ):
        r_notes = await api_client.get("/api/v1/memory/notes", headers=hdrs)
        assert r_notes.status_code == 200
        assert r_notes.json()["memory_enabled"] is False
        assert r_notes.json()["items"] == []
        _assert_no_tokens(r_notes.text, ["CANARY_PERSONAL_PM_THESIS_8826"], context=f"memory/notes {mode_label}")

        r_post = await api_client.post(
            "/api/v1/memory/notes",
            headers=hdrs,
            json={"scope_type": "user", "key": "blocked", "content": "blocked"},
        )
        assert r_post.status_code == 403, f"Expected 403 on memory write in {mode_label}, got {r_post.status_code}"

        r_wl = await api_client.get("/api/v1/memory/watchlist", headers=hdrs)
        assert r_wl.status_code == 200
        assert r_wl.json()["memory_enabled"] is False
        assert r_wl.json().get("entity_ids", []) == []

        r_bundle = await api_client.post(
            "/api/v1/context/bundle",
            headers=hdrs,
            json={
                "entity_keys": ["grp:alpha-credit-gips-composite"],
                "skill_id": "skill:gips-portfolio-reporting",
            },
        )
        assert r_bundle.status_code == 200
        bundle = r_bundle.json()
        assert bundle.get("memories", []) == []
        assert bundle["receipt"]["memory_ids"] == []
        dir_ids = {d["claim_id"] for d in bundle["directives"]}
        assert "clm-dir-user-pm-format-pref" not in dir_ids, (
            f"user_x_skill claim leaked in {mode_label}!"
        )
        _assert_no_tokens(r_bundle.text, ["CANARY_PERSONAL_PM_THESIS_8826"], context=f"bundle {mode_label}")

        r_search = await api_client.get("/api/v1/search", headers=hdrs, params={"q": "CANARY_PERSONAL_PM_THESIS_8826"})
        assert r_search.status_code == 200
        assert len(r_search.json()["hits"]) == 0


@pytest.mark.asyncio
async def test_entra_overage_revocation_steward_rbac_and_toctou(api_client) -> None:
    """Verify >200-group overage, revoked principal 401, steward write RBAC 403, and TOCTOU 409."""
    # 1. Entra >200-group overage resolution (user:overage-director)
    ov_hdrs = mock_headers("user:overage-director", groups_overage=True)
    me_ov = await api_client.get("/api/v1/me", headers=ov_hdrs)
    assert me_ov.status_code == 200, me_ov.text
    me_body = me_ov.json()
    assert me_body["groups_overage_resolved"] is True
    assert len(me_body["groups"]) >= 201
    assert "mnpi-project-cobalt" in me_body["groups"]
    assert "private-credit-deal-team" in me_body["groups"]
    assert (await api_client.get("/api/v1/entities/proj:project-cobalt-lme", headers=ov_hdrs)).status_code == 200
    assert (await api_client.get("/api/v1/entities/issuer:solis-medtech", headers=ov_hdrs)).status_code == 200

    # 2. Revoked principal hard 401 rejection across all endpoints
    rev_hdrs = mock_headers("user:revoked-analyst")
    for method, path in (
        ("GET", "/api/v1/me"),
        ("GET", "/api/v1/entities"),
        ("GET", "/api/v1/entities/veh:alpha-credit-offshore-lp"),
        ("GET", "/api/v1/search?q=Alpha"),
        ("GET", "/api/v1/graph?lens=topic"),
        ("GET", "/api/v1/memory/notes"),
        ("GET", "/api/v1/memory/watchlist"),
    ):
        r = await api_client.request(method, path, headers=rev_hdrs)
        assert r.status_code == 401, f"Revoked principal got {r.status_code} on {method} {path}; expected 401"

    # 3. Steward RBAC (PDF Defect #1 fix): non-steward gets 403 on force_supersede, approved_fact, and conflict resolve
    pub_hdrs = mock_headers("user:public-analyst")
    r_forbid_claim = await api_client.post(
        "/api/v1/claims",
        headers=pub_hdrs,
        json={
            "subject_canonical_key": "veh:alpha-credit-offshore-lp",
            "predicate": "has_admin_code",
            "object_value": "ADM-HACKED",
            "context_purpose": "legal_vehicle",
            "scope_type": "firm",
            "scope_id": "firm",
            "channel": "fact",
            "epistemic_status": "approved_fact",
            "source_system": "rogue-user",
            "force_supersede": True,
        },
    )
    assert r_forbid_claim.status_code == 403, f"Non-steward force_supersede must return 403, got {r_forbid_claim.status_code}"

    r_forbid_resolve = await api_client.post(
        "/api/v1/conflicts/conflict-alpha-admin-code-3way/resolve",
        headers=pub_hdrs,
        json={"winning_claim_id": "clm-admin-code-approved", "rationale": "unauthorized"},
    )
    assert r_forbid_resolve.status_code == 403, f"Non-steward conflict resolve must return 403, got {r_forbid_resolve.status_code}"

    # 4. TOCTOU stale expected_data_epoch MUST return 409 Conflict
    steward_hdrs = mock_headers("user:fund-steward")
    health = (await api_client.get("/healthz", headers=steward_hdrs)).json()
    current_epoch = health["data_epoch"]
    assert isinstance(current_epoch, int) and current_epoch >= 1

    r_stale_claim = await api_client.post(
        "/api/v1/claims",
        headers=steward_hdrs,
        json={
            "subject_canonical_key": "veh:alpha-credit-offshore-lp",
            "predicate": "has_admin_code",
            "object_value": "ADM-STALE-CHECK",
            "context_purpose": "legal_vehicle",
            "scope_type": "firm",
            "scope_id": "firm",
            "channel": "fact",
            "epistemic_status": "approved_fact",
            "source_system": "admin-feed",
            "force_supersede": True,
            "expected_data_epoch": current_epoch - 1,
        },
    )
    assert r_stale_claim.status_code == 409, f"Stale expected_data_epoch must return 409, got {r_stale_claim.status_code}"

    r_stale_resolve = await api_client.post(
        "/api/v1/conflicts/conflict-alpha-admin-code-3way/resolve",
        headers=steward_hdrs,
        json={
            "winning_claim_id": "clm-admin-code-approved",
            "rationale": "stale epoch test",
            "expected_data_epoch": current_epoch - 1,
        },
    )
    assert r_stale_resolve.status_code == 409, f"Stale conflict resolve must return 409, got {r_stale_resolve.status_code}"
