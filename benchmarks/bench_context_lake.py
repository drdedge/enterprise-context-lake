"""Enterprise Context Lake speed benchmark.

Usage: python benchmarks/bench_context_lake.py . <scale_k> [--store sqlite|okf] [--profile out.prof]

Example: python benchmarks/bench_context_lake.py . 50   (about 2,400 entities)

Scales the fixture bundle k times (entities, identifiers, claims, conflicts,
attachments, positions, coverages, lineage are cloned with suffixed ids; the
topic taxonomy, knowledge items and principals are shared), loads it into a
live create_app() runtime, then times the main paths in-process.
"""
from __future__ import annotations

import asyncio, json, os, statistics as st, sys, tempfile, time, cProfile
from pathlib import Path

REPO = Path(sys.argv[1]).resolve()
K = int(sys.argv[2])
STORE = "sqlite"
PROFILE = None
args = sys.argv[3:]
if "--store" in args:
    STORE = args[args.index("--store") + 1]
if "--profile" in args:
    PROFILE = args[args.index("--profile") + 1]
for p in ("core", "adapters", "api", "mcp", "sdk", "agents"):
    sys.path.insert(0, str(REPO / "packages" / p))

os.environ.update(AUTH_MODE="mock", LLM_MODE="deterministic", CONTEXT_LAKE_OFFLINE="1", LANGSMITH_TRACING="false")
for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "LANGSMITH_API_KEY"):
    os.environ.pop(k, None)

import httpx
from context_core.seeder import load_fixture_bundle
from context_core.models import Principal, SearchFilter

sys.path.insert(0, str(REPO / "tests"))
from conftest import build_principal_from_fixture, mock_headers  # noqa: E402


def scale_bundle(b: dict, k: int) -> dict:
    if k <= 1:
        return b
    ent_ids = {e.entity_id for e in b["entities"]}
    out = {key: [] for key in b}
    out["topics"] = b["topics"]
    out["knowledge_items"] = b["knowledge_items"]
    out["memories"] = b["memories"]
    out["watchlists"] = b["watchlists"]
    out["revoked_principals"] = b["revoked_principals"]
    claim_ids = {c.claim_id for c in b["claims"]}
    for i in range(k):
        sfx = "" if i == 0 else f"~s{i}"
        rm = lambda x: (x + sfx) if (x in ent_ids) else x  # noqa: E731
        rc = lambda x: (x + sfx) if (x in claim_ids) else x  # noqa: E731
        for e in b["entities"]:
            out["entities"].append(e.model_copy(update={"entity_id": e.entity_id + sfx, "canonical_key": e.canonical_key + sfx,
                                                        "display_name": e.display_name + (f" {i}" if i else "")}))
        for x in b["identifiers"]:
            out["identifiers"].append(x.model_copy(update={"id": x.id + sfx, "entity_id": rm(x.entity_id), "identifier_value": x.identifier_value + sfx}))
        for x in b["lineage_events"]:
            out["lineage_events"].append(x.model_copy(update={"event_id": x.event_id + sfx, "source_entity_id": rm(x.source_entity_id), "target_entity_id": rm(x.target_entity_id)}))
        for x in b["positions"]:
            out["positions"].append(x.model_copy(update={"position_id": x.position_id + sfx, "vehicle_entity_id": rm(x.vehicle_entity_id),
                                                         "instrument_entity_id": rm(x.instrument_entity_id) if x.instrument_entity_id else None,
                                                         "issuer_entity_id": rm(x.issuer_entity_id) if x.issuer_entity_id else None}))
        for x in b["coverages"]:
            out["coverages"].append(x.model_copy(update={"coverage_id": x.coverage_id + sfx, "analyst_entity_id": rm(x.analyst_entity_id), "issuer_entity_id": rm(x.issuer_entity_id)}))
        for x in b["skill_bindings"]:
            out["skill_bindings"].append(x.model_copy(update={"binding_id": x.binding_id + sfx, "skill_entity_id": rm(x.skill_entity_id), "target_entity_id": rm(x.target_entity_id)}))
        for x in b["topic_attachments"]:
            tid = rc(x.target_id) if x.target_type == "claim" else rm(x.target_id)
            out["topic_attachments"].append(x.model_copy(update={"attachment_id": x.attachment_id + sfx, "target_id": tid}))
        for c in b["claims"]:
            upd = {"claim_id": c.claim_id + sfx, "subject_id": rm(c.subject_id)}
            if c.object_entity_id:
                upd["object_entity_id"] = rm(c.object_entity_id)
            for f in ("supersedes_claim_id", "superseded_by_claim_id", "conflict_set_id"):
                if hasattr(c, f) and getattr(c, f):
                    upd[f] = getattr(c, f) + sfx if f == "conflict_set_id" else rc(getattr(c, f))
            out["claims"].append(c.model_copy(update=upd))
        for cs in b["conflict_sets"]:
            out["conflict_sets"].append(cs.model_copy(update={"conflict_set_id": cs.conflict_set_id + sfx, "subject_id": rm(cs.subject_id),
                                                              "claim_ids": [rc(x) for x in cs.claim_ids],
                                                              "winning_claim_id": rc(cs.winning_claim_id) if cs.winning_claim_id else None}))
    return out


ERRORS: dict = {}


async def timeit(fn, reps=5, warm=1, name=None):
    try:
        await fn()
    except Exception as exc:
        ERRORS[name or str(fn)] = f"{type(exc).__name__}: {exc}"[:200]
        return None, None
    xs = []
    res = None
    for _ in range(reps):
        t = time.perf_counter()
        res = await fn()
        xs.append((time.perf_counter() - t) * 1000)
    return round(st.median(xs), 2), res


async def main() -> dict:
    from context_api.app import create_app
    import context_mcp
    R: dict = {"scale_k": K, "store": STORE}
    base = load_fixture_bundle(REPO)
    t = time.perf_counter()
    bundle = scale_bundle(base, K)
    R["build_bundle_ms"] = round((time.perf_counter() - t) * 1000, 1)
    R["n_entities"] = len(bundle["entities"])
    R["n_claims"] = len(bundle["claims"])
    R["n_identifiers"] = len(bundle["identifiers"])

    with tempfile.TemporaryDirectory(prefix="eclb_") as td:
        tp = Path(td)
        os.environ.update(CONTEXT_LAKE_DB_PATH=str(tp / "b.sqlite3"), CONTEXT_LAKE_DATA_DIR=str(tp),
                          CONTEXT_LAKE_SNAPSHOT_DIR=str(tp / "snapshots"), CONTEXT_LAKE_VAULT_DIR=str(tp / "vault"),
                          CONTEXT_LAKE_UPLOAD_DIR=str(tp / "uploads"))
        t = time.perf_counter()
        app = create_app(db_path=tp / "b.sqlite3", runtime_dir=tp, repo_root=REPO, llm_mode="deterministic",
                         auth_mode="mock", seed_fixtures=True, compile_initial_snapshots=False)
        async with app.router.lifespan_context(app):
            R["app_startup_with_fixture_seed_ms"] = round((time.perf_counter() - t) * 1000, 1)
            rt = app.state.runtime
            reg = rt.registry
            if STORE == "okf":
                t = time.perf_counter()
                await reg.swap(store="okf_markdown_vault")
                R["hot_swap_to_okf_ms"] = round((time.perf_counter() - t) * 1000, 1)
            store = reg.store
            # ---- bulk ingest
            t = time.perf_counter()
            await store.import_state(bundle, reset_epochs=True)
            R["bulk_import_ms"] = round((time.perf_counter() - t) * 1000, 1)
            R["bulk_import_per_record_us"] = round(R["bulk_import_ms"] * 1000 / max(1, sum(len(v) for v in bundle.values() if isinstance(v, list))), 1)

            steward = build_principal_from_fixture("user:fund-steward")
            analyst = build_principal_from_fixture("user:public-analyst") if True else steward
            retr = reg.retrieval

            prof = cProfile.Profile() if PROFILE else None

            # ---- single policy-checked claim write
            from context_core.models import ClaimCreateInput
            fields = ClaimCreateInput.model_fields
            subj = "veh:alpha-credit-offshore-lp"
            n_w = 30
            xs = []
            for i in range(n_w):
                payload = ClaimCreateInput.model_validate({
                    "subject_canonical_key": subj, "predicate": f"bench_note_{i}", "object_value": f"v{i}",
                    "context_purpose": "general", "scope_type": "firm", "scope_id": "firm",
                    "epistemic_status": "unverified_observation", "facets": {},
                    **({"source_system": "bench"} if "source_system" in fields else {}),
                })
                t = time.perf_counter()
                try:
                    await store.write_claim(payload, principal=steward)
                except Exception as exc:
                    R["write_claim_error"] = f"{type(exc).__name__}: {exc}"[:300]
                    break
                xs.append((time.perf_counter() - t) * 1000)
            if xs:
                R["write_claim_ms_median"] = round(st.median(xs), 2)

            # ---- retrieval (store-level)
            if prof: prof.enable()
            R["search_phrase_ms"], hits = await timeit(lambda: retr.search("alpha credit", principal=steward, limit=25), reps=3, name="search_phrase_ms")
            R["search_phrase_hits"] = len(hits or [])
            R["search_exact_key_ms"], _ = await timeit(lambda: retr.search("veh:alpha-credit-offshore-lp", principal=steward, limit=25), reps=3, name="search_exact_key_ms")
            R["filter_items_ms"], _ = await timeit(lambda: retr.filter_items(SearchFilter(entity_types=["vehicle"], limit=25), principal=steward), reps=3, name="filter_items_ms")
            R["neighbours_d2_ms"], _ = await timeit(lambda: retr.neighbours("veh:alpha-credit-offshore-lp", depth=2, direction="both", principal=steward), reps=3, name="neighbours_d2_ms")
            if prof: prof.disable()
            # store primitives
            R["store_list_entities_ms"], ents = await timeit(lambda: store.list_entities(principal=steward, limit=100000), reps=3, name="store_list_entities_ms")
            R["store_query_claims_all_ms"], cl = await timeit(lambda: store.query_claims(principal=steward), reps=3, name="store_query_claims_all_ms")
            R["store_query_claims_one_subject_ms"], _ = await timeit(lambda: store.query_claims(principal=steward, subject_ids=["veh:alpha-credit-offshore-lp"]), reps=5, name="store_query_claims_one_subject_ms")
            R["store_get_entity_ms"], _ = await timeit(lambda: store.get_entity("veh:alpha-credit-offshore-lp", principal=steward), reps=10, name="store_get_entity_ms")

            # ---- MCP primitives (in-process, no transport)
            mrt = rt.mcp_runtime
            R["mcp_search_ms"], _ = await timeit(lambda: context_mcp.tool_search(mrt, steward, query="alpha credit"), reps=3, name="mcp_search_ms")
            R["mcp_read_ms"], _ = await timeit(lambda: context_mcp.tool_read(mrt, steward, id_or_key="veh:alpha-credit-offshore-lp"), reps=3, name="mcp_read_ms")
            R["mcp_resolve_context_ms"], _ = await timeit(lambda: context_mcp.tool_resolve_context(mrt, steward, entity_keys=["veh:alpha-credit-offshore-lp"]), reps=3, name="mcp_resolve_context_ms")
            try:
                R["mcp_codemode_exec_trivial_ms"], _ = await timeit(lambda: context_mcp.execute_code("return 1;", runtime=mrt, principal=steward, timeout_ms=8000), reps=3, name="mcp_codemode_exec_trivial_ms")
                R["mcp_codemode_exec_read_ms"], cmr = await timeit(lambda: context_mcp.execute_code(
                    'const c = await lake.read({id_or_key:"veh:alpha-credit-offshore-lp"}); return (c.facts||[]).length;',
                    runtime=mrt, principal=steward, timeout_ms=15000), reps=3)
                R["codemode_result_ok"] = cmr is not None and bool((cmr if isinstance(cmr, dict) else cmr.model_dump()).get("ok", True))
            except Exception as exc:
                R["codemode_error"] = f"{type(exc).__name__}: {exc}"[:300]

            # ---- REST via ASGI (no network)
            tr = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=tr, base_url="http://t", timeout=600) as cli:
                h = mock_headers("user:fund-steward")
                async def g(url, **kw):
                    r = await cli.get(url, headers=h, **kw)
                    r.raise_for_status()
                    return r
                R["api_healthz_ms"], _ = await timeit(lambda: g("/healthz"), reps=20, name="api_healthz_ms")
                R["api_entity_card_ms"], _ = await timeit(lambda: g("/api/v1/entities/veh:alpha-credit-offshore-lp"), reps=3, name="api_entity_card_ms")
                R["api_search_ms"], r = await timeit(lambda: g("/api/v1/search", params={"q": "alpha credit"}), reps=3, name="api_search_ms")
                R["api_search_bytes"] = len(r.content) if r is not None else None
                R["api_entities_list_ms"], r = await timeit(lambda: g("/api/v1/entities"), reps=3, name="api_entities_list_ms")
                R["api_entities_list_bytes"] = len(r.content) if r is not None else None
                R["api_context_resolve_ms"], _ = await timeit(lambda: g("/api/v1/context/resolve", params={"entity_keys": "veh:alpha-credit-offshore-lp"}), reps=3, name="api_context_resolve_ms")
                try:
                    R["api_graph_ms"], r = await timeit(lambda: g("/api/v1/map/graph"), reps=2, name="api_graph_ms")
                    R["api_graph_bytes"] = len(r.content) if r is not None else None
                except Exception as exc:
                    R["api_graph_error"] = f"{type(exc).__name__}: {exc}"[:200]

            # ---- snapshot compile + SDK
            snaps = reg.snapshots
            if snaps is not None:
                t = time.perf_counter()
                man = await snaps.compile_and_publish("firm-all", "public_side")
                R["snapshot_compile_ms"] = round((time.perf_counter() - t) * 1000, 1)
                R["snapshot_bytes_zst"] = man.byte_size
                R["snapshot_entities"] = man.entity_count; R["snapshot_claims"] = man.claim_count
                from context_sdk import ContextClient
                t = time.perf_counter()
                cc = ContextClient(snapshot_dir=snaps.snapshot_dir)
                R["sdk_load_ms"] = round((time.perf_counter() - t) * 1000, 1)
                try:
                    async def _l():
                        return cc.lookup("veh:alpha-credit-offshore-lp", "administrator")
                    R["sdk_lookup_ms"], _ = await timeit(_l, reps=20, name="sdk_lookup_ms")
                    async def _rc():
                        return cc.resolve_context(entity_keys=["veh:alpha-credit-offshore-lp"])
                    R["sdk_resolve_context_ms"], _ = await timeit(_rc, reps=20, name="sdk_resolve_context_ms")
                    async def _ge():
                        return cc.get_entity("veh:alpha-credit-offshore-lp")
                    R["sdk_get_entity_ms"], _ = await timeit(_ge, reps=50, name="sdk_get_entity_ms")
                except Exception as exc:
                    R["sdk_error"] = f"{type(exc).__name__}: {exc}"[:300]
                finally:
                    cc.close()
            if prof:
                prof.dump_stats(PROFILE)
    R["errors"] = ERRORS
    return R


if __name__ == "__main__":
    out = asyncio.run(main())
    print(json.dumps(out))
