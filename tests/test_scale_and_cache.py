"""Regression tests for the performance work: no silent truncation at scale, and the SQLite
scan cache never serves data older than the last write (in-process or from another connection)."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from conftest import REPO_ROOT, build_principal_from_fixture

sys.path.insert(0, str(REPO_ROOT / "benchmarks"))


def _scaled_bundle(k: int) -> dict:
    import importlib.util

    spec = importlib.util.spec_from_file_location("_bench", REPO_ROOT / "benchmarks" / "bench_context_lake.py")
    assert spec and spec.loader
    # bench module parses argv at import; give it harmless values
    saved = sys.argv
    sys.argv = ["bench", str(REPO_ROOT), str(k)]
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.argv = saved
    from context_core.seeder import load_fixture_bundle

    return mod.scale_bundle(load_fixture_bundle(REPO_ROOT), k)


@pytest.fixture
async def sqlite_store(tmp_path: Path):
    from context_adapters.sqlite_store import SqliteBitemporalStore

    store = SqliteBitemporalStore(tmp_path / "scale.sqlite3", repo_root=REPO_ROOT)
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


async def test_no_truncation_above_2000_entities(sqlite_store) -> None:
    from context_adapters.retrieval import LexicalManifestRetriever
    from context_core.graph import GraphProjectionService

    bundle = _scaled_bundle(50)  # ~2,400 entities
    assert len(bundle["entities"]) > 2000
    await sqlite_store.import_state(bundle, reset_epochs=True)
    steward = build_principal_from_fixture("user:fund-steward")

    all_visible = await sqlite_store.list_entities(principal=steward, limit=None)
    assert len(all_visible) > 2000

    # The fixture vehicle sorts after the 2,000th canonical key; it used to vanish from search/graph
    retr = LexicalManifestRetriever(sqlite_store)
    hits = await retr.search("veh:alpha-credit-offshore-lp", principal=steward, limit=5)
    assert any(h.canonical_key == "veh:alpha-credit-offshore-lp" for h in hits)
    neigh = await retr.neighbours("veh:alpha-credit-offshore-lp", depth=1, direction="both", principal=steward)
    assert neigh.nodes

    graph = await GraphProjectionService(sqlite_store).project_graph(principal=steward)
    assert len(graph.nodes) == len(all_visible)


async def test_scan_cache_sees_in_process_writes(sqlite_store) -> None:
    from context_core.seeder import load_fixture_bundle

    await sqlite_store.import_state(load_fixture_bundle(REPO_ROOT), reset_epochs=True)
    steward = build_principal_from_fixture("user:fund-steward")

    before = {e.canonical_key: e for e in await sqlite_store.list_entities(principal=steward, limit=None)}
    target = before["veh:alpha-credit-offshore-lp"]
    await sqlite_store.upsert_entity(target.model_copy(update={"display_name": "Renamed In Process"}))
    after = {e.entity_id: e for e in await sqlite_store.list_entities(principal=steward, limit=None)}
    assert after[target.entity_id].display_name == "Renamed In Process"

    # Mutating a returned object must not leak into later reads (cache hands out copies)
    after[target.entity_id].display_name = "MUTATED"
    again = {e.entity_id: e for e in await sqlite_store.list_entities(principal=steward, limit=None)}
    assert again[target.entity_id].display_name == "Renamed In Process"


async def test_scan_cache_sees_writes_from_other_connections(sqlite_store, tmp_path: Path) -> None:
    from context_core.seeder import load_fixture_bundle

    await sqlite_store.import_state(load_fixture_bundle(REPO_ROOT), reset_epochs=True)
    steward = build_principal_from_fixture("user:fund-steward")
    claims = await sqlite_store.query_claims(principal=steward)  # warms the cache
    some = claims[0]

    raw = sqlite3.connect(tmp_path / "scale.sqlite3")
    try:
        raw.execute("UPDATE claims SET object_value = ? WHERE claim_id = ?", ("changed-elsewhere", some.claim_id))
        raw.execute("UPDATE entities SET display_name = ? WHERE entity_id = ?", ("Changed Elsewhere", some.subject_id))
        raw.commit()
    finally:
        raw.close()

    claims2 = {c.claim_id: c for c in await sqlite_store.query_claims(principal=steward)}
    assert claims2[some.claim_id].object_value == "changed-elsewhere"
    ents = {e.entity_id: e for e in await sqlite_store.list_entities(principal=steward, limit=None)}
    assert ents[some.subject_id].display_name == "Changed Elsewhere"


async def test_revocation_applies_immediately_with_warm_cache(sqlite_store) -> None:
    from context_core.seeder import load_fixture_bundle

    await sqlite_store.import_state(load_fixture_bundle(REPO_ROOT), reset_epochs=True)
    analyst = build_principal_from_fixture("user:public-analyst")
    assert await sqlite_store.list_entities(principal=analyst, limit=None)
    assert await sqlite_store.query_claims(principal=analyst)
    await sqlite_store.revoke_principal(analyst.user_id, reason="test", actor="system:test")
    assert await sqlite_store.list_entities(principal=analyst, limit=None) == []
    assert await sqlite_store.query_claims(principal=analyst) == []


def test_clone_model_is_deep() -> None:
    from context_core.models import clone_model
    from context_core.seeder import load_fixture_bundle

    bundle = load_fixture_bundle(REPO_ROOT)
    for key in ("entities", "claims", "conflict_sets", "topics", "knowledge_items", "memories", "watchlists"):
        for obj in bundle[key]:
            copy = clone_model(obj)
            assert copy == obj
            assert copy.model_dump() == obj.model_copy(deep=True).model_dump()
    claim = bundle["claims"][0]
    copy = clone_model(claim)
    copy.provenance.evidence_uri = "mutated://x"
    copy.acl.read_acl_groups.append("mutated-group")
    assert claim.provenance.evidence_uri != "mutated://x"
    assert "mutated-group" not in claim.acl.read_acl_groups
