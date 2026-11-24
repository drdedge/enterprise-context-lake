"""Editable taxonomy and in-query access checks (ADR-008).

Store tests run against both adapters with the seeded institutional fixtures; API tests run against
the real seeded FastAPI app, like the other suites.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from conftest import mock_headers
from context_core.exceptions import ValidationError
from context_core.models import ClaimCreateInput, Entity, Principal, WallSide
from context_core.policy import PolicyEngine
from context_core.seeder import seed_store_from_fixtures
from context_core.vocabulary import VocabTerm, VocabularyIndex, default_terms
from context_adapters.okf_store import OkfMarkdownVaultStore
from context_adapters.sqlite_store import SqliteBitemporalStore


STEWARD = Principal(
    user_id="user:fund-steward", display_name="Steward", email="s@x",
    groups=frozenset({"firm-all", "fund-stewards", "custodian-legal-docs", "reporting-readers", "compliance-oversight"}),
    wall_side=WallSide.above_the_wall,
)
ANALYST = Principal(
    user_id="user:public-analyst", display_name="Analyst", email="a@x",
    groups=frozenset({"firm-all", "public-research"}), wall_side=WallSide.public_side,
)
DEAL = Principal(
    user_id="user:private-deal-team", display_name="Deal", email="d@x",
    groups=frozenset({"firm-all", "private-credit-deal-team", "mnpi-project-cobalt", "custodian-legal-docs"}),
    wall_side=WallSide.private_side,
)


def _make(kind: str, base: Path) -> Any:
    policy = PolicyEngine()
    if kind == "sqlite":
        return SqliteBitemporalStore(base / "lake.sqlite3", policy=policy)
    return OkfMarkdownVaultStore(base / "vault", policy=policy)


@pytest.fixture(params=["sqlite", "okf"])
async def store(request: Any, tmp_path: Path) -> Any:
    s = _make(request.param, tmp_path)
    await s.initialize()
    await seed_store_from_fixtures(s)
    s.drain_new_vocab_terms()
    try:
        yield s
    finally:
        await s.close()


# ---------------------------------------------------------------------------
# Vocabulary model
# ---------------------------------------------------------------------------


def test_default_tree_places_every_seeded_type_under_thing() -> None:
    idx = VocabularyIndex(default_terms())
    assert idx.ancestors("entity_type", "issuer") == ["organisation", "party", "thing"]
    assert {"issuer", "client", "legal_party", "user"} <= idx.descendants("entity_type", "party")
    for t in idx.terms("entity_type"):
        assert t.key == "thing" or "thing" in idx.ancestors("entity_type", t.key), t.key


def test_aliases_and_merges_resolve_to_the_surviving_term() -> None:
    terms = default_terms() + [
        VocabTerm(vocab="entity_type", key="borrower", parent_key="organisation", aliases=["Obligor"]),
        VocabTerm(vocab="entity_type", key="debtor", parent_key="thing", merged_into_key="borrower"),
    ]
    idx = VocabularyIndex(terms)
    assert idx.canonical("entity_type", "obligor") == "borrower"
    assert idx.canonical("entity_type", "debtor") == "borrower"
    assert {"borrower", "debtor", "obligor"} <= idx.descendants("entity_type", "organisation")


# ---------------------------------------------------------------------------
# Stores: seeded terms, unknown values, tree filters, validation, round trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_registers_fixture_values_and_defaults(store: Any) -> None:
    types = {t.key: t for t in await store.list_vocab_terms(vocab="entity_type")}
    assert types["issuer"].status == "active" and types["issuer"].parent_key == "organisation"
    predicates = {t.key for t in await store.list_vocab_terms(vocab="predicate")}
    assert {"has_admin_code", "maps_as", "includes_vehicle"} <= predicates
    schemes = {t.key for t in await store.list_vocab_terms(vocab="identifier_scheme")}
    assert {"lei", "isin", "admin_code"} <= schemes


@pytest.mark.asyncio
async def test_unknown_entity_type_is_stored_and_proposed(store: Any) -> None:
    ent = Entity(
        entity_id="ent:admin-citco", entity_type="Fund Administrator", canonical_key="adm:citco",
        display_name="Citco Fund Services", read_acl_groups=["firm-all"],
    )
    await store.upsert_entity(ent)
    assert (await store.get_entity("ent:admin-citco", principal=ANALYST)).entity_type == "fund_administrator"

    term = await store.get_vocab_term("entity_type:fund_administrator")
    assert term is not None and term.status == "proposed" and term.parent_key == "thing"
    assert [t.term_id for t in store.drain_new_vocab_terms()] == ["entity_type:fund_administrator"]

    # Still found under the root, and under organisation once a steward re-parents it
    assert "ent:admin-citco" in {e.entity_id for e in await store.list_entities(principal=ANALYST, entity_types=["thing"], limit=None)}
    assert "ent:admin-citco" not in {e.entity_id for e in await store.list_entities(principal=ANALYST, entity_types=["organisation"], limit=None)}
    await store.upsert_vocab_term(term.model_copy(update={"parent_key": "organisation", "status": "active"}))
    for limit in (None, 500):
        found = await store.list_entities(principal=ANALYST, entity_types=["organisation"], limit=limit)
        assert "ent:admin-citco" in {e.entity_id for e in found}


@pytest.mark.asyncio
async def test_broad_type_filter_returns_narrower_types(store: Any) -> None:
    parties = await store.list_entities(principal=STEWARD, entity_types=["party"], limit=None)
    kinds = {e.entity_type for e in parties}
    assert {"issuer", "client", "legal_party"} <= kinds
    assert not kinds & {"legal_vehicle", "instrument", "topic"}


@pytest.mark.asyncio
async def test_new_predicate_and_unit_on_a_claim_are_proposed(store: Any) -> None:
    await store.write_claim(
        ClaimCreateInput(
            subject_id="ent:fam-alpha-credit", predicate="Gross Leverage Limit", object_value="2.5",
            facets={"modality": "quantitative", "numeric_value": 2.5, "unit": "turns"},
            epistemic_status="unverified_observation",
        ),
        principal=STEWARD,
    )
    proposed = {t.term_id for t in await store.list_vocab_terms(statuses=["proposed"])}
    assert {"predicate:gross_leverage_limit", "unit:turns"} <= proposed


@pytest.mark.asyncio
async def test_bad_parent_or_loop_is_rejected(store: Any) -> None:
    with pytest.raises(ValidationError):
        await store.upsert_vocab_term(VocabTerm(vocab="entity_type", key="borrower", parent_key="no_such_type"))
    with pytest.raises(ValidationError):
        # party -> organisation -> party would loop
        await store.upsert_vocab_term(VocabTerm(vocab="entity_type", key="party", parent_key="organisation"))


@pytest.mark.asyncio
async def test_terms_survive_export_import_across_adapters(store: Any, tmp_path: Path) -> None:
    await store.upsert_vocab_term(VocabTerm(vocab="entity_type", key="borrower", parent_key="organisation"))
    other = _make("okf" if isinstance(store, SqliteBitemporalStore) else "sqlite", tmp_path / "other")
    await other.initialize()
    try:
        await other.import_state(await store.export_state())
        got = await other.get_vocab_term("entity_type:borrower")
        assert got is not None and got.parent_key == "organisation"
    finally:
        await other.close()


# ---------------------------------------------------------------------------
# SQLite: access checks inside the query
# ---------------------------------------------------------------------------


@pytest.fixture
async def sqlite_store(tmp_path: Path) -> Any:
    s = _make("sqlite", tmp_path)
    await s.initialize()
    await seed_store_from_fixtures(s)
    try:
        yield s
    finally:
        await s.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("who", [ANALYST, DEAL, STEWARD], ids=["public", "private", "steward"])
async def test_paged_reads_match_the_policy_engine(sqlite_store: Any, who: Principal) -> None:
    everything = await sqlite_store.list_entities(principal=STEWARD.model_copy(update={"groups": STEWARD.groups | DEAL.groups}), limit=None)
    expected = [e.entity_id for e in sorted(everything, key=lambda e: e.canonical_key) if PolicyEngine().can_read_entity(who, e)]
    whole = [e.entity_id for e in await sqlite_store.list_entities(principal=who, limit=None)]
    assert whole == expected
    # Pages come from the query (access, order, limit, offset in SQL) and are always full
    paged: list[str] = []
    offset = 0
    while True:
        page = await sqlite_store.list_entities(principal=who, limit=7, offset=offset)
        paged += [e.entity_id for e in page]
        if len(page) < 7:
            break
        offset += 7
    assert paged == expected


@pytest.mark.asyncio
async def test_private_side_entities_never_reach_public_pages(sqlite_store: Any) -> None:
    public = {e.entity_id for e in await sqlite_store.list_entities(principal=ANALYST, limit=1000)}
    private = {e.entity_id for e in await sqlite_store.list_entities(principal=DEAL, limit=1000)}
    assert private - public, "fixtures should hold private-side entities"
    for eid in private - public:
        ent = await sqlite_store.get_entity_raw(eid)
        assert not PolicyEngine().can_read_entity(ANALYST, ent)


@pytest.mark.asyncio
@pytest.mark.parametrize("who", [ANALYST, DEAL, STEWARD], ids=["public", "private", "steward"])
async def test_filtered_claim_query_matches_unfiltered(sqlite_store: Any, who: Principal) -> None:
    ents = await sqlite_store.list_entities(principal=STEWARD.model_copy(update={"groups": STEWARD.groups | DEAL.groups}), limit=None)
    unfiltered = {c.claim_id for c in await sqlite_store.query_claims(principal=who)}
    filtered = {c.claim_id for c in await sqlite_store.query_claims(principal=who, subject_ids=[e.entity_id for e in ents])}
    assert filtered == unfiltered
    assert unfiltered, "every principal sees some claims"


@pytest.mark.asyncio
async def test_old_database_gets_acl_column_and_backfill(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    first = SqliteBitemporalStore(path, policy=PolicyEngine())
    await first.initialize()
    await seed_store_from_fixtures(first)
    before = [e.entity_id for e in await first.list_entities(principal=ANALYST, limit=50)]
    await first.close()

    # Make it look like a database from before ADR-008
    with sqlite3.connect(path) as raw:
        raw.execute("DROP INDEX IF EXISTS ix_entities_acl_set_id")
        raw.execute("ALTER TABLE entities DROP COLUMN acl_set_id")
        raw.execute("UPDATE claims SET acl_set_id = NULL")

    second = SqliteBitemporalStore(path, policy=PolicyEngine())
    await second.initialize()
    try:
        assert [e.entity_id for e in await second.list_entities(principal=ANALYST, limit=50)] == before
        assert await second.query_claims(principal=ANALYST, subject_ids=before)
        with sqlite3.connect(path) as raw:
            assert raw.execute("SELECT COUNT(*) FROM entities WHERE acl_set_id IS NULL").fetchone()[0] == 0
            assert raw.execute("SELECT COUNT(*) FROM claims WHERE acl_set_id IS NULL").fetchone()[0] == 0
    finally:
        await second.close()


# ---------------------------------------------------------------------------
# API: steward edits go through change sets
# ---------------------------------------------------------------------------

STEWARD_ID = "user:fund-steward"
ANALYST_ID = "user:public-analyst"


def q(value: str) -> str:
    return quote(value, safe="")


@pytest.mark.asyncio
async def test_api_lists_vocabularies_and_tree(api_client: Any) -> None:
    r = await api_client.get("/api/v1/vocabularies", headers=mock_headers(ANALYST_ID))
    assert r.status_code == 200, r.text
    names = {v["vocab"] for v in r.json()["vocabularies"]}
    assert {"entity_type", "predicate", "identifier_scheme", "unit"} <= names

    r = await api_client.get("/api/v1/vocabularies/entity_type/terms?tree=true", headers=mock_headers(ANALYST_ID))
    roots = r.json()["terms"]
    assert [n["key"] for n in roots] == ["thing"]

    r = await api_client.get("/api/v1/vocabularies/entity_type/terms/issuer", headers=mock_headers(ANALYST_ID))
    assert r.json()["ancestors"] == ["organisation", "party", "thing"]


@pytest.mark.asyncio
async def test_api_non_steward_edit_waits_for_a_steward(api_client: Any) -> None:
    r = await api_client.put(
        "/api/v1/vocabularies/entity_type/terms/borrower",
        json={"parent_key": "organisation", "description": "A company we lend to"},
        headers=mock_headers(ANALYST_ID),
    )
    assert r.status_code == 200, r.text
    cs = r.json()["change_set"]
    assert cs["status"] == "proposed" and cs["rule_ids"] == ["vocabulary"]
    r = await api_client.get("/api/v1/vocabularies/entity_type/terms/borrower", headers=mock_headers(ANALYST_ID))
    assert r.status_code == 404

    r = await api_client.post(f"/api/v1/change-sets/{cs['change_id']}/approve", json={"comment": "ok"}, headers=mock_headers(STEWARD_ID))
    assert r.status_code == 200, r.text
    r = await api_client.get("/api/v1/vocabularies/entity_type/terms/borrower", headers=mock_headers(ANALYST_ID))
    assert r.status_code == 200 and r.json()["term"]["parent_key"] == "organisation"

    r = await api_client.get(f"/api/v1/history/vocab_term/{q('entity_type:borrower')}", headers=mock_headers(ANALYST_ID))
    assert r.status_code == 200, r.text
    assert r.json()["versions"]


@pytest.mark.asyncio
async def test_api_steward_approves_a_proposed_term(api_client: Any) -> None:
    # A new value in data lands as a proposed term ...
    r = await api_client.post(
        "/api/v1/claims",
        json={
            "subject_id": "ent:fam-alpha-credit", "predicate": "esg_exclusion_list", "object_value": "tobacco",
            "epistemic_status": "unverified_observation",
        },
        headers=mock_headers(STEWARD_ID),
    )
    assert r.status_code in (200, 201), r.text
    r = await api_client.get("/api/v1/vocabularies/proposed", headers=mock_headers(ANALYST_ID))
    assert "predicate:esg_exclusion_list" in {t["term_id"] for t in r.json()["terms"]}

    # ... which a steward approves directly (the steward-self rule merges it)
    r = await api_client.put(
        "/api/v1/vocabularies/predicate/terms/esg_exclusion_list",
        json={"status": "active", "attributes": {"object": "literal"}},
        headers=mock_headers(STEWARD_ID),
    )
    assert r.status_code == 200, r.text
    assert r.json()["change_set"]["status"] == "merged"
    r = await api_client.get("/api/v1/vocabularies/predicate/terms/esg_exclusion_list", headers=mock_headers(ANALYST_ID))
    assert r.json()["term"]["status"] == "active"


@pytest.mark.asyncio
async def test_api_rejects_a_loop(api_client: Any) -> None:
    r = await api_client.put(
        "/api/v1/vocabularies/entity_type/terms/party", json={"parent_key": "issuer"}, headers=mock_headers(STEWARD_ID)
    )
    assert r.status_code in (400, 422), r.text
