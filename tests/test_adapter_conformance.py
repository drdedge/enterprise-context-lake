"""Oracle 3: Hexagonal Adapter Conformance Suite (pytest + Hypothesis).

Executes the EXACT SAME behavioral contract suite via parametrization against:
  - BOTH StorePort adapters: SqliteBitemporalStore AND OkfMarkdownVaultStore
  - BOTH RetrievalPort adapters: LexicalManifestRetriever AND FacetGraphRetriever
Plus:
  - Hypothesis property-based tests for bitemporal windows and ACL/wall_side matrices
  - OKF v0.2 vault invariants (POSIX '/' paths, LF-normalized SHA-256, _index.md <= 200 lines)
  - Live runtime adapter hot-swap via GET/PUT /api/v1/config/adapters
"""

from __future__ import annotations

import asyncio
import inspect
import os
import uuid
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import (
    FIXTURES_DIR,
    build_principal_from_fixture,
    mock_headers,
    parse_utc,
    resolve_symbol,
)


#: SQL Server joins the matrix when a test server is configured, e.g.
#: LAKE_TEST_SQLSERVER_URL="mssql+aioodbc://user:pw@127.0.0.1:1433/BSPAI?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes"
SQLSERVER_TEST_URL = os.environ.get("LAKE_TEST_SQLSERVER_URL", "").strip()
STORE_KINDS = ["sqlite_bitemporal", "okf_markdown_vault"] + (["sqlserver_bitemporal"] if SQLSERVER_TEST_URL else [])


def _instantiate_store(adapter_kind: str, base_dir: Path, policy: Any) -> Any:
    if adapter_kind == "sqlserver_bitemporal":
        from context_adapters.sqlserver_store import SqlServerBitemporalStore, SqlServerSettings

        # Each store gets a throwaway schema (dropped by _close_store), encrypted with a local key
        settings = SqlServerSettings(
            url=SQLSERVER_TEST_URL,
            schema=f"lake_test_{uuid.uuid4().hex[:10]}",
            encryption="local",
            local_master_key=os.urandom(32),
        )
        return SqlServerBitemporalStore(settings, policy=policy)
    if adapter_kind == "sqlite_bitemporal":
        cls = resolve_symbol(
            [
                ("context_adapters.sqlite_store", "SqliteBitemporalStore"),
                ("context_adapters.stores.sqlite", "SqliteBitemporalStore"),
                ("context_adapters", "SqliteBitemporalStore"),
            ]
        )
        db_path = base_dir / "conformance.sqlite3"
        sig = inspect.signature(cls)
        kw: dict[str, Any] = {}
        for k, v in (("db_path", db_path), ("database_path", db_path), ("path", db_path), ("policy", policy)):
            if k in sig.parameters:
                kw[k] = v
        return cls(**kw) if kw else cls(db_path)

    if adapter_kind == "okf_markdown_vault":
        cls = resolve_symbol(
            [
                ("context_adapters.okf_store", "OkfMarkdownVaultStore"),
                ("context_adapters.okf_vault", "OkfMarkdownVaultStore"),
                ("context_adapters.stores.okf", "OkfMarkdownVaultStore"),
                ("context_adapters", "OkfMarkdownVaultStore"),
            ]
        )
        vault_dir = base_dir / "okf_vault"
        sig = inspect.signature(cls)
        kw = {}
        for k, v in (("vault_dir", vault_dir), ("root_dir", vault_dir), ("base_dir", vault_dir), ("path", vault_dir), ("policy", policy)):
            if k in sig.parameters:
                kw[k] = v
        return cls(**kw) if kw else cls(vault_dir)

    raise ValueError(f"Unknown store adapter: {adapter_kind}")


def _instantiate_retriever(retriever_kind: str, store: Any, policy: Any) -> Any:
    if retriever_kind == "lexical_manifest":
        cls = resolve_symbol(
            [
                ("context_adapters.lexical_retriever", "LexicalManifestRetriever"),
                ("context_adapters.retrieval.lexical", "LexicalManifestRetriever"),
                ("context_adapters.retrievers", "LexicalManifestRetriever"),
                ("context_adapters", "LexicalManifestRetriever"),
            ]
        )
    elif retriever_kind == "facet_graph":
        cls = resolve_symbol(
            [
                ("context_adapters.facet_graph_retriever", "FacetGraphRetriever"),
                ("context_adapters.retrieval.facet_graph", "FacetGraphRetriever"),
                ("context_adapters.retrievers", "FacetGraphRetriever"),
                ("context_adapters", "FacetGraphRetriever"),
            ]
        )
    else:
        raise ValueError(f"Unknown retriever adapter: {retriever_kind}")

    sig = inspect.signature(cls)
    kw: dict[str, Any] = {}
    for k, v in (("store", store), ("policy", policy)):
        if k in sig.parameters:
            kw[k] = v
    return cls(**kw) if kw else cls(store)


async def _close_store(store: Any) -> None:
    try:
        if hasattr(store, "drop_all_for_tests"):
            await store.drop_all_for_tests()
    finally:
        await store.close()


async def _seed(store: Any) -> None:
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
    if inspect.iscoroutinefunction(seed_fn):
        sig = inspect.signature(seed_fn)
        if "fixtures_dir" in sig.parameters:
            await seed_fn(store, fixtures_dir=FIXTURES_DIR)
        else:
            await seed_fn(store)
    else:
        seed_fn(store)


@pytest.fixture(params=STORE_KINDS)
async def seeded_store(request, tmp_path: Path):
    """Yields each seeded StorePort adapter identically, with guaranteed teardown."""
    PolicyEngine = resolve_symbol(
        [
            ("context_core.policy", "PolicyEngine"),
            ("context_adapters.policy", "PolicyEngine"),
            ("context_core", "PolicyEngine"),
        ]
    )
    policy = PolicyEngine()
    adapter_kind = request.param
    sub_dir = tmp_path / adapter_kind
    sub_dir.mkdir(parents=True, exist_ok=True)
    store = _instantiate_store(adapter_kind, sub_dir, policy)
    try:
        await store.initialize()
        await _seed(store)
        yield adapter_kind, store, policy, sub_dir
    finally:
        await _close_store(store)


@pytest.mark.asyncio
async def test_store_bitemporal_and_restatement_conformance(seeded_store) -> None:
    """Identical bitemporal replay semantics across SqliteBitemporalStore & OkfMarkdownVaultStore."""
    _kind, store, _policy, _sub_dir = seeded_store
    steward = build_principal_from_fixture("user:fund-steward")

    # 1. Pre-restatement Q4 2025 NAV
    pre_claims = await store.query_claims(
        principal=steward,
        subject_ids=["ent:grp-alpha-gips"],
        predicates=["composite_nav_usd"],
        as_of=parse_utc("2025-12-31T23:59:59Z"),
        recorded_as_of=parse_utc("2026-01-20T00:00:00Z"),
    )
    assert len(pre_claims) == 1
    assert pre_claims[0].claim_id == "clm-q4-2025-nav-prelim"
    assert pre_claims[0].object_value == "1284500000"

    # 2. Post-restatement Q4 2025 NAV
    post_claims = await store.query_claims(
        principal=steward,
        subject_ids=["ent:grp-alpha-gips"],
        predicates=["composite_nav_usd"],
        as_of=parse_utc("2025-12-31T23:59:59Z"),
        recorded_as_of=parse_utc("2026-02-25T00:00:00Z"),
    )
    assert len(post_claims) == 1
    assert post_claims[0].claim_id == "clm-q4-2025-nav-restated"
    assert post_claims[0].object_value == "1281900000"

    # 3. Pre-rename 2024 legal display name
    pre_name = await store.query_claims(
        principal=steward,
        subject_ids=["ent:veh-alpha-offshore"],
        predicates=["legal_display_name"],
        as_of=parse_utc("2024-06-15T00:00:00Z"),
        recorded_as_of=parse_utc("2024-06-20T00:00:00Z"),
    )
    assert len(pre_name) == 1
    assert pre_name[0].object_value == "Meridian Opportunistic Credit Offshore LP"


@pytest.mark.asyncio
async def test_store_acl_wall_evidence_and_memory_conformance(seeded_store) -> None:
    """Identical ACL, wall_side, split evidence redaction, topics, conflicts & personal memory across both stores."""
    kind, store, _policy, sub_dir = seeded_store
    pub = build_principal_from_fixture("user:public-analyst", channel_mode="direct")
    deal = build_principal_from_fixture("user:private-deal-team", channel_mode="direct")
    pm_direct = build_principal_from_fixture("user:pm-credit", channel_mode="direct")
    pm_shared = build_principal_from_fixture("user:pm-credit", channel_mode="shared")
    steward = build_principal_from_fixture("user:fund-steward", channel_mode="direct")

    # 1. Wall & ACL zero-leakage
    assert await store.get_entity("proj:project-cobalt-lme", principal=pub) is None
    assert await store.get_entity("issuer:solis-medtech", principal=pub) is None
    assert await store.get_entity("proj:project-cobalt-lme", principal=deal) is not None
    assert await store.get_entity("issuer:solis-medtech", principal=deal) is not None

    # 2. Split claim-vs-evidence redaction
    pub_clm = await store.get_claim("clm-admin-code-custodian", principal=pub)
    assert pub_clm is not None
    assert pub_clm.object_value == "ADM-904X"
    assert pub_clm.evidence_redacted is True
    assert pub_clm.evidence_redaction_label == "Restricted — Request Access"
    assert pub_clm.provenance.evidence_uri is None
    assert pub_clm.provenance.excerpt is None

    stew_clm = await store.get_claim("clm-admin-code-custodian", principal=steward)
    assert stew_clm is not None
    assert stew_clm.evidence_redacted is False
    assert stew_clm.provenance.evidence_uri == "fixtures/uploads/alpha_credit_lp_side_letter.pdf"

    # 3. Conflict preservation & non-overwrite
    conflicts = await store.list_conflicts(principal=steward, status="open")
    assert any(c.conflict_set_id == "conflict-alpha-admin-code-3way" and len(c.claim_ids) == 3 for c in conflicts)

    # 4. Dynamic Topics CRUD + ACL-filtered attachments
    Topic = resolve_symbol([("context_core.models", "Topic")])
    TopicPatchInput = resolve_symbol([("context_core.models", "TopicPatchInput")])
    new_topic = Topic(
        topic_id="topic:test-conformance-subtopic",
        slug="test-conformance-subtopic",
        pref_label="Conformance Subtopic",
        alt_labels=["Conf Sub"],
        broader_topic_id="topic:financial",
        topic_kind="emergent_candidate",
        owner_group="fund-stewards",
        status="proposed",
    )
    await store.upsert_topic(new_topic)
    patched = await store.update_topic(
        "topic:test-conformance-subtopic",
        TopicPatchInput(status="active", pref_label="Conformance Subtopic Active"),
        principal=steward,
    )
    assert patched.status == "active"
    assert patched.pref_label == "Conformance Subtopic Active"

    pub_att = await store.list_topic_attachments(principal=pub, topic_id="topic:lme-restructuring")
    deal_att = await store.list_topic_attachments(principal=deal, topic_id="topic:lme-restructuring")
    assert len(pub_att) < len(deal_att)

    # 5. Personal Memory CRUD & shared-channel isolation
    MemoryUpsertInput = resolve_symbol([("context_core.models", "MemoryUpsertInput")])
    pm_notes = await store.list_memories(principal=pm_direct)
    assert any(m.memory_id == "mem-pm-credit-001" for m in pm_notes)
    assert await store.list_memories(principal=pm_shared) == []
    assert not any(m.memory_id == "mem-pm-credit-001" for m in await store.list_memories(principal=pub))

    created = await store.upsert_memory(
        MemoryUpsertInput(
            scope_type="user",
            key="conformance-note",
            content="Temporary conformance note",
            tags=["test"],
        ),
        principal=pm_direct,
    )
    assert await store.get_memory(created.memory_id, principal=pm_direct) is not None
    assert await store.get_memory(created.memory_id, principal=pub) is None
    assert await store.delete_memory(created.memory_id, principal=pm_direct) is True

    # 6. OKF v0.2 vault hygiene checks when running on OkfMarkdownVaultStore
    if kind == "okf_markdown_vault":
        vault_dir = sub_dir / "okf_vault"
        md_files = list(vault_dir.rglob("*.md"))
        assert len(md_files) >= 5, f"Expected OKF markdown notes in {vault_dir}"
        for mf in md_files:
            raw = mf.read_bytes()
            assert b"\r\n" not in raw, f"CRLF detected in OKF note {mf}"
            if mf.name == "_index.md":
                assert len(raw.decode("utf-8").splitlines()) <= 200


@pytest.mark.asyncio
@pytest.mark.parametrize("store_kind", STORE_KINDS)
@pytest.mark.parametrize("retriever_kind", ["lexical_manifest", "facet_graph"])
async def test_retrieval_adapter_conformance_matrix(store_kind: str, retriever_kind: str, tmp_path: Path) -> None:
    """2x2 matrix: BOTH RetrievalPort adapters over BOTH StorePort adapters produce equivalent authorized results."""
    PolicyEngine = resolve_symbol(
        [
            ("context_core.policy", "PolicyEngine"),
            ("context_adapters.policy", "PolicyEngine"),
            ("context_core", "PolicyEngine"),
        ]
    )
    SearchFilter = resolve_symbol([("context_core.models", "SearchFilter")])
    ContextBundleQuery = resolve_symbol([("context_core.models", "ContextBundleQuery")])

    policy = PolicyEngine()
    sub_dir = tmp_path / f"{store_kind}_{retriever_kind}"
    sub_dir.mkdir(parents=True, exist_ok=True)
    store = _instantiate_store(store_kind, sub_dir, policy)
    try:
        await store.initialize()
        await _seed(store)
        retriever = _instantiate_retriever(retriever_kind, store, policy)

        pub = build_principal_from_fixture("user:public-analyst")
        deal = build_principal_from_fixture("user:private-deal-team")

        # 1. Search Alpha Credit Offshore LP -> found for both
        hits_pub = await retriever.search("Alpha Credit Offshore", principal=pub, limit=15)
        assert any("alpha-credit-offshore" in (h.canonical_key or h.id) for h in hits_pub)

        # 2. Search MNPI canary -> 0 hits for public analyst, >=1 hit for private deal team
        mnpi_pub = await retriever.search("CANARY_MNPI_SOLIS_PIPELINE_5382", principal=pub, limit=15)
        assert len(mnpi_pub) == 0, f"{retriever_kind}/{store_kind} leaked MNPI canary to public analyst!"

        mnpi_deal = await retriever.search("Solis MedTech", principal=deal, limit=15)
        assert len(mnpi_deal) >= 1

        # 3. Facet / entity_type filter
        issuers_pub = await retriever.filter_items(
            SearchFilter(entity_types=["issuer"]),
            principal=pub,
        )
        pub_keys = {h.canonical_key for h in issuers_pub}
        assert "issuer:apex-industrial" in pub_keys
        assert "issuer:cinder-logistics" in pub_keys
        assert "issuer:solis-medtech" not in pub_keys

        # 4. Bounded-hop neighbours
        neigh = await retriever.neighbours("issuer:apex-industrial", principal=pub, depth=2, direction="both")
        neigh_keys = {n.canonical_key for n in neigh.nodes}
        assert "issuer:apex-industrial" in neigh_keys
        assert "proj:project-cobalt-lme" not in neigh_keys

        # 5. Bitemporal resolve_context pre-restatement
        bundle = await retriever.resolve_context(
            ContextBundleQuery(
                entity_keys=["grp:alpha-credit-gips-composite"],
                as_of=parse_utc("2025-12-31T23:59:59Z"),
                recorded_as_of=parse_utc("2026-01-20T00:00:00Z"),
            ),
            principal=pub,
        )
        navs = [f for f in bundle.facts if f.predicate == "composite_nav_usd"]
        assert len(navs) == 1 and navs[0].object_value == "1284500000"
    finally:
        await _close_store(store)


@given(
    valid_offset_days=st.integers(min_value=0, max_value=30),
    valid_span_days=st.integers(min_value=10, max_value=90),
    query_valid_offset=st.integers(min_value=-5, max_value=130),
    superseded_gap_seconds=st.integers(min_value=5, max_value=600),
    query_rec_offset=st.integers(min_value=-5, max_value=700),
)
@settings(max_examples=20, deadline=None)
def test_hypothesis_bitemporal_equivalence_across_both_stores(
    valid_offset_days: int,
    valid_span_days: int,
    query_valid_offset: int,
    superseded_gap_seconds: int,
    query_rec_offset: int,
) -> None:
    """Property test: SqliteBitemporalStore & OkfMarkdownVaultStore agree on ANY (as_of, recorded_as_of)."""
    PolicyEngine = resolve_symbol(
        [
            ("context_core.policy", "PolicyEngine"),
            ("context_adapters.policy", "PolicyEngine"),
            ("context_core", "PolicyEngine"),
        ]
    )
    Entity = resolve_symbol([("context_core.models", "Entity")])
    Claim = resolve_symbol([("context_core.models", "Claim")])
    ClaimFacets = resolve_symbol([("context_core.models", "ClaimFacets")])
    Provenance = resolve_symbol([("context_core.models", "Provenance")])
    ClaimACL = resolve_symbol([("context_core.models", "ClaimACL")])

    t0 = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    v_from = t0 + timedelta(days=valid_offset_days)
    v_to = v_from + timedelta(days=valid_span_days)
    q_as_of = t0 + timedelta(days=query_valid_offset)

    rec1 = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    sup1 = rec1 + timedelta(seconds=superseded_gap_seconds)
    q_rec = rec1 + timedelta(seconds=query_rec_offset)

    expected_v1 = (v_from <= q_as_of < v_to) and (rec1 <= q_rec < sup1)
    expected_v2 = (v_from <= q_as_of < v_to) and (sup1 <= q_rec)

    async def _run() -> None:
        with tempfile.TemporaryDirectory(prefix="hyp_bitemp_") as tmp_dir_str:
            base_dir = Path(tmp_dir_str)
            policy = PolicyEngine()
            steward = build_principal_from_fixture("user:fund-steward")

            ent = Entity(
                entity_id="ent:hyp-veh",
                entity_type="legal_vehicle",
                canonical_key="veh:hyp-test-lp",
                display_name="Hypothesis Test Vehicle",
                aliases=[],
                description="Property test entity",
                owner_group="fund-stewards",
                wall_side="public_side",
                read_acl_groups=["firm-all"],
                write_acl_groups=["fund-stewards"],
                attributes={},
                status="active",
                created_at=t0,
                updated_at=t0,
            )
            c1 = Claim(
                claim_id="clm-hyp-v1",
                subject_id=ent.entity_id,
                predicate="nav_usd",
                object_value="100",
                context_purpose="general",
                scope_type="firm",
                scope_id="firm",
                channel="fact",
                epistemic_status="approved_fact",
                is_mandatory=False,
                valid_from=v_from,
                valid_to=v_to,
                recorded_at=rec1,
                superseded_at=sup1,
                superseded_by="clm-hyp-v2",
                facets=ClaimFacets(
                    domain_nature="financial",
                    modality="quantitative",
                    numeric_value=100.0,
                    unit="usd",
                    currency="USD",
                    epistemic_temporal="actual",
                    actual_stage="prelim",
                    periodicity="point_in_time",
                    ingestion_channel="automated_feed",
                ),
                provenance=Provenance(source_system="test-feed"),
                acl=ClaimACL(
                    wall_side="public_side",
                    read_acl_groups=["firm-all"],
                    write_acl_groups=["fund-stewards"],
                    evidence_acl_groups=["firm-all"],
                ),
            )
            c2 = c1.model_copy(
                update={
                    "claim_id": "clm-hyp-v2",
                    "object_value": "200",
                    "recorded_at": sup1,
                    "superseded_at": None,
                    "superseded_by": None,
                }
            )

            results_by_adapter: dict[str, list[str]] = {}
            for kind in STORE_KINDS:
                sdir = base_dir / kind
                sdir.mkdir(parents=True, exist_ok=True)
                store = _instantiate_store(kind, sdir, policy)
                try:
                    await store.initialize()
                    await store.upsert_entity(ent)
                    for clm in (c1, c2):
                        if hasattr(store, "seed_claim_raw"):
                            await store.seed_claim_raw(clm)
                        elif hasattr(store, "upsert_claim"):
                            await store.upsert_claim(clm)
                        elif hasattr(store, "save_claim"):
                            await store.save_claim(clm)
                        else:
                            raise AssertionError(f"{kind} missing seed_claim_raw/upsert_claim for fixture loading")
                    got = await store.query_claims(
                        principal=steward,
                        subject_ids=[ent.entity_id],
                        predicates=["nav_usd"],
                        as_of=q_as_of,
                        recorded_as_of=q_rec,
                    )
                    results_by_adapter[kind] = sorted(c.claim_id for c in got)
                finally:
                    await _close_store(store)

            expected_ids = sorted(
                (["clm-hyp-v1"] if expected_v1 else []) + (["clm-hyp-v2"] if expected_v2 else [])
            )
            assert results_by_adapter["sqlite_bitemporal"] == expected_ids
            assert results_by_adapter["okf_markdown_vault"] == expected_ids
            if "sqlserver_bitemporal" in results_by_adapter:
                assert results_by_adapter["sqlserver_bitemporal"] == expected_ids

    asyncio.run(_run())


@given(
    principal_wall=st.sampled_from(["public_side", "private_side", "above_the_wall"]),
    record_wall=st.sampled_from(["public_side", "private_side", "above_the_wall"]),
    principal_groups=st.frozensets(st.sampled_from(["firm-all", "mnpi-cobalt", "custodian-legal", "fund-stewards"]), min_size=0, max_size=4),
    read_groups=st.frozensets(st.sampled_from(["firm-all", "mnpi-cobalt", "custodian-legal"]), min_size=1, max_size=3),
    evidence_groups=st.frozensets(st.sampled_from(["firm-all", "custodian-legal", "mnpi-cobalt"]), min_size=0, max_size=2),
    revoked=st.booleans(),
)
@settings(max_examples=50, deadline=None)
def test_hypothesis_policy_engine_wall_and_split_evidence_invariants(
    principal_wall: str,
    record_wall: str,
    principal_groups: frozenset[str],
    read_groups: frozenset[str],
    evidence_groups: frozenset[str],
    revoked: bool,
) -> None:
    """Property test verifying ADR-003 PolicyEngine fail-closed truth table across random inputs."""
    PolicyEngine = resolve_symbol(
        [
            ("context_core.policy", "PolicyEngine"),
            ("context_adapters.policy", "PolicyEngine"),
            ("context_core", "PolicyEngine"),
        ]
    )
    Principal = resolve_symbol([("context_core.models", "Principal")])
    Claim = resolve_symbol([("context_core.models", "Claim")])
    ClaimFacets = resolve_symbol([("context_core.models", "ClaimFacets")])
    Provenance = resolve_symbol([("context_core.models", "Provenance")])
    ClaimACL = resolve_symbol([("context_core.models", "ClaimACL")])

    now = datetime(2026, 3, 15, tzinfo=timezone.utc)
    policy = PolicyEngine()
    principal = Principal(
        user_id="user:hyp",
        display_name="Hyp User",
        email="hyp@meridian.local",
        roles=frozenset({"reader"}),
        groups=principal_groups,
        wall_side=principal_wall,
        auth_mode="local_mock",
        revoked=revoked,
    )
    claim = Claim(
        claim_id="clm-hyp-acl",
        subject_id="ent:veh-alpha-offshore",
        predicate="secret_test",
        object_value="VALUE",
        context_purpose="general",
        scope_type="firm",
        scope_id="firm",
        channel="fact",
        epistemic_status="approved_fact",
        valid_from=now,
        recorded_at=now,
        facets=ClaimFacets(
            domain_nature="non_financial",
            modality="qualitative",
            epistemic_temporal="actual",
            periodicity="evergreen",
            ingestion_channel="automated_feed",
        ),
        provenance=Provenance(
            source_system="test",
            evidence_uri="s3://restricted/doc.pdf",
            excerpt="CONFIDENTIAL_EXCERPT_CANARY",
            span_locator="page=1",
        ),
        acl=ClaimACL(
            wall_side=record_wall,
            read_acl_groups=sorted(read_groups),
            write_acl_groups=["fund-stewards"],
            evidence_acl_groups=sorted(evidence_groups),
        ),
    )

    wall_ok = (
        principal_wall == "above_the_wall"
        or (principal_wall == "private_side" and record_wall in ("public_side", "private_side"))
        or (principal_wall == "public_side" and record_wall == "public_side")
    )
    expected_read = (not revoked) and wall_ok and bool(principal_groups & read_groups)
    expected_ev = expected_read and ((not evidence_groups) or bool(principal_groups & evidence_groups))

    assert policy.can_read_claim(principal, claim) is expected_read
    assert policy.can_read_evidence(principal, claim) is expected_ev

    sanitized = policy.sanitize_claim_for_principal(principal, claim)
    if not expected_read:
        assert sanitized is None
    else:
        assert sanitized is not None
        if expected_ev:
            assert sanitized.evidence_redacted is False
            assert sanitized.provenance.evidence_uri == "s3://restricted/doc.pdf"
        else:
            assert sanitized.evidence_redacted is True
            assert sanitized.evidence_redaction_label == "Restricted — Request Access"
            assert sanitized.provenance.evidence_uri is None
            assert sanitized.provenance.excerpt is None
            assert sanitized.provenance.span_locator is None


@pytest.mark.asyncio
async def test_runtime_adapter_hotswap_via_rest_api(api_client) -> None:
    """Verify GET/PUT /api/v1/config/adapters hot-swaps Store & Retrieval live with identical behavior."""
    steward_hdrs = mock_headers("user:fund-steward")
    pub_hdrs = mock_headers("user:public-analyst")

    cfg0 = (await api_client.get("/api/v1/config/adapters", headers=steward_hdrs)).json()
    assert cfg0["store"] in STORE_KINDS
    back_to = cfg0["store"] if cfg0["store"] != "okf_markdown_vault" else "sqlite_bitemporal"
    assert cfg0["retrieval"] in ("lexical_manifest", "facet_graph")

    # Baseline query on default adapters
    base_ent = (await api_client.get("/api/v1/entities/veh:alpha-credit-offshore-lp", headers=pub_hdrs)).json()
    base_nav = (
        await api_client.post(
            "/api/v1/context/bundle",
            headers=steward_hdrs,
            json={
                "entity_keys": ["grp:alpha-credit-gips-composite"],
                "as_of": "2025-12-31T23:59:59Z",
                "recorded_as_of": "2026-01-20T00:00:00Z",
            },
        )
    ).json()

    # Hot-swap to okf_markdown_vault + facet_graph
    swap1 = await api_client.put(
        "/api/v1/config/adapters",
        headers=steward_hdrs,
        json={"store": "okf_markdown_vault", "retrieval": "facet_graph", "llm_mode": "deterministic"},
    )
    assert swap1.status_code == 200, swap1.text
    assert swap1.json()["store"] == "okf_markdown_vault"
    assert swap1.json()["retrieval"] == "facet_graph"

    swapped_ent = (await api_client.get("/api/v1/entities/veh:alpha-credit-offshore-lp", headers=pub_hdrs)).json()
    assert swapped_ent["entity"]["canonical_key"] == base_ent["entity"]["canonical_key"]
    assert "CANARY_SIDELETTER_MFN_FEE_4091" not in str(swapped_ent)
    assert (await api_client.get("/api/v1/entities/proj:project-cobalt-lme", headers=pub_hdrs)).status_code == 404

    swapped_nav = (
        await api_client.post(
            "/api/v1/context/bundle",
            headers=steward_hdrs,
            json={
                "entity_keys": ["grp:alpha-credit-gips-composite"],
                "as_of": "2025-12-31T23:59:59Z",
                "recorded_as_of": "2026-01-20T00:00:00Z",
            },
        )
    ).json()
    nav_val_before = next(f["object_value"] for f in base_nav["facts"] if f["predicate"] == "composite_nav_usd")
    nav_val_after = next(f["object_value"] for f in swapped_nav["facts"] if f["predicate"] == "composite_nav_usd")
    assert nav_val_before == nav_val_after == "1284500000"

    # Swap back to the starting relational store (SQLite, or SQL Server when the suite runs on it)
    swap2 = await api_client.put(
        "/api/v1/config/adapters",
        headers=steward_hdrs,
        json={"store": back_to, "retrieval": "lexical_manifest"},
    )
    assert swap2.status_code == 200, swap2.text
    assert swap2.json()["store"] == back_to
    assert swap2.json()["retrieval"] == "lexical_manifest"
