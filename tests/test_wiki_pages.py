"""Oracle 10: entity pages built from claims (ADR-009).

Runs against the real seeded FastAPI app, like the other API suites:
  1. Every statement on a page cites a claim; restricted evidence is cited without its location.
  2. Pages are cut by wall side: a public-side page never carries private-side material, a caller
     cannot ask for a side they may not see, and no public page anywhere carries an MNPI canary.
  3. External models never receive private-side material, and model text that cites anything other
     than the statements it was given is thrown away.
  4. Pages are rebuilt as soon as a claim behind them changes, and channels pin what pages show.
  5. Page templates are records: stewards' edits merge, other edits wait for review, broken
     templates fall back to the default, and type lookup walks the taxonomy parents.
  6. The MCP tools and REST endpoints return the same pages.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from conftest import build_principal_from_fixture, load_json_fixture, mock_headers
from context_core.model_boundary import external_model_allowed
from context_core.models import Entity, WallSide
from context_core.wiki import (
    BUILTIN_TEMPLATE,
    NarratorConfig,
    NumberedStatement,
    PageTemplate,
    SectionSpec,
    WikiService,
    citations_valid,
    render_markdown,
    resolve_template,
)

STEWARD = "user:fund-steward"
ANALYST = "user:public-analyst"
DEAL = "user:private-deal-team"
VEH = "veh:alpha-credit-offshore-lp"
VEH_ID = "ent:veh-alpha-offshore"
PRIVATE_CANARY = "CANARY_WIKI_PRIVATE_SIDE_7731"


def H(user: str) -> dict[str, str]:
    return mock_headers(user)


def _forbidden_public_tokens() -> list[str]:
    tokens = [c["token"] for c in load_json_fixture("principals.json")["canaries"]]
    return tokens + ["alpha_credit_lp_side_letter.pdf", PRIVATE_CANARY]


async def _plant_private_claim(client: Any) -> None:
    """A private-side claim on a public-side entity, written by a steward."""
    item = {
        "record_type": "claim",
        "op": "claim_create",
        "payload": {
            "subject_id": VEH_ID,
            "predicate": "confidential_refinancing_note",
            "object_value": f"Lender group exploring amend-and-extend; ref {PRIVATE_CANARY}",
            "context_purpose": "private_underwriting",
            "epistemic_status": "approved_fact",
            "source_system": "private-dataroom",
            "evidence_uri": "dataroom://alpha/refi-memo.pdf",
            "wall_side": "private_side",
            "read_acl_groups": ["private-credit-deal-team", "fund-stewards"],
        },
    }
    r = await client.post("/api/v1/change-sets", json={"title": "Private note", "items": [item]}, headers=H(STEWARD))
    assert r.status_code == 201, r.text
    assert r.json()["change_set"]["status"] == "merged"


async def _page(client: Any, user: str, key: str = VEH, **params: Any) -> Any:
    return await client.get(f"/api/v1/wiki/pages/{key}", params=params, headers=H(user))


# ---------------------------------------------------------------------------
# 1. Citations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_statement_cites_a_claim_and_restricted_evidence_has_no_location(api_client: Any) -> None:
    r = await _page(api_client, ANALYST)
    assert r.status_code == 200, r.text
    page = r.json()["page"]
    assert page["side"] == "public_side" and page["template_id"] == "default"
    numbers = {c["n"] for c in page["citations"]}
    statements = [st for sec in page["sections"] for st in sec["statements"]]
    assert statements and all(st["cites"] and set(st["cites"]) <= numbers for st in statements)

    by_claim = {c["claim_id"]: c for c in page["citations"]}
    restricted = by_claim["clm-admin-code-custodian"]  # its evidence is the restricted side letter
    assert restricted["evidence_restricted"] is True
    assert restricted["evidence_uri"] is None and restricted["span_locator"] is None

    kinds = {sec["kind"]: sec for sec in page["sections"]}
    assert kinds["overview"]["text"] and "[1]" in kinds["overview"]["text"]
    assert kinds["diagram"]["mermaid"].startswith("flowchart LR")
    assert kinds["conflicts"]["statements"], "the admin-code conflict is fully public, so it is shown"
    md = r.json()["markdown"]
    assert "## Sources" in md and "evidence restricted" in md


# ---------------------------------------------------------------------------
# 2. Information barrier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pages_are_cut_by_wall_side(api_client: Any) -> None:
    await _plant_private_claim(api_client)

    public = await _page(api_client, ANALYST)
    assert PRIVATE_CANARY not in public.text

    deal_default = await _page(api_client, DEAL)
    assert deal_default.json()["page"]["side"] == "private_side"
    assert PRIVATE_CANARY in deal_default.text

    deal_public = await _page(api_client, DEAL, side="public_side")
    assert PRIVATE_CANARY not in deal_public.text
    assert deal_public.json()["page"]["fingerprint"] != deal_default.json()["page"]["fingerprint"]
    # The public cut is identical whoever asks for it (same evidence rights aside)
    assert public.json()["page"]["side"] == deal_public.json()["page"]["side"] == "public_side"

    # A public-side caller may not ask for the private side, and private-only entities are 404
    assert (await _page(api_client, ANALYST, side="private_side")).status_code == 403
    assert (await _page(api_client, ANALYST, "issuer:solis-medtech")).status_code == 404
    assert (await _page(api_client, DEAL, "issuer:solis-medtech", side="public_side")).status_code == 404
    assert (await _page(api_client, DEAL, "issuer:solis-medtech")).status_code == 200


@pytest.mark.asyncio
async def test_no_public_page_or_page_list_carries_mnpi(api_client: Any) -> None:
    await _plant_private_claim(api_client)
    forbidden = _forbidden_public_tokens()
    for user, side in ((ANALYST, None), (DEAL, "public_side"), (STEWARD, "public_side")):
        params = {"side": side} if side else {}
        listing = await api_client.get("/api/v1/wiki/pages", params={**params, "limit": 1000}, headers=H(user))
        assert listing.status_code == 200, listing.text
        body = listing.json()
        assert body["side"] == "public_side"
        blob = json.dumps(body)
        assert "solis" not in blob.lower() and "cobalt" not in blob.lower()
        for group in body["groups"]:
            for p in group["pages"]:
                r = await _page(api_client, user, p["canonical_key"], **params)
                assert r.status_code == 200, (p, r.text)
                for tok in forbidden:
                    if tok == "alpha_credit_lp_side_letter.pdf" and user != ANALYST:
                        continue  # evidence rights differ by caller; the analyst may not see it
                    assert tok not in r.text, f"{tok} leaked on {p['canonical_key']} for {user}"


@pytest.mark.asyncio
async def test_ask_respects_the_side(api_client: Any) -> None:
    await _plant_private_claim(api_client)
    q = {"question": "Is there a refinancing note for Alpha Credit Offshore?", "entity_keys": [VEH]}
    pub = await api_client.post("/api/v1/wiki/ask", json=q, headers=H(ANALYST))
    assert pub.status_code == 200 and PRIVATE_CANARY not in pub.text
    priv = await api_client.post("/api/v1/wiki/ask", json=q, headers=H(DEAL))
    assert priv.status_code == 200 and PRIVATE_CANARY in priv.text
    assert all(s["n"] for s in priv.json()["sources"])
    assert (await api_client.post("/api/v1/wiki/ask", json={**q, "side": "private_side"}, headers=H(ANALYST))).status_code == 403


# ---------------------------------------------------------------------------
# 3. Model boundary
# ---------------------------------------------------------------------------


class RecordingNarrator:
    def __init__(self, name: str, boundary: str, reply: str | None = None) -> None:
        self.name = name
        self.boundary = boundary
        self.reply = reply
        self.seen: list[str] = []

    async def overview(self, *, entity: Entity, statements: list[NumberedStatement]) -> str | None:
        self.seen.append(entity.description + "\n" + "\n".join(s.text for s in statements))
        return self.reply or f"{entity.display_name} has cited facts [{statements[0].n}]."

    async def answer(self, *, question: str, statements: list[NumberedStatement]) -> str | None:
        self.seen.append("\n".join(s.text for s in statements))
        return self.reply or f"See [{statements[0].n}]."


def _wiki(api_app: Any, narrators: NarratorConfig) -> WikiService:
    reg = api_app.state.runtime.registry
    return WikiService(get_store=lambda: reg.store, get_retrieval=lambda: reg.retrieval, narrators=narrators)


@pytest.mark.asyncio
async def test_external_models_only_ever_see_public_side_material(api_client: Any, api_app: Any) -> None:
    await _plant_private_claim(api_client)
    external = RecordingNarrator("fake-external", "external")
    internal = RecordingNarrator("fake-internal", "internal")
    wiki = _wiki(api_app, NarratorConfig(public=external, private=internal))
    deal = build_principal_from_fixture(DEAL)

    private_page = await wiki.build_page(VEH, deal)
    assert private_page.narrator == "fake-internal"
    assert any(PRIVATE_CANARY in s for s in internal.seen)
    assert external.seen == []

    public_page = await wiki.build_page(VEH, deal, side="public_side")
    assert public_page.narrator == "fake-external"
    assert external.seen and all(PRIVATE_CANARY not in s for s in external.seen)

    # Private-only entity: even on a misconfigured public narrator, nothing private goes out
    await wiki.ask("Solis unitranche pricing", deal, entity_keys=["issuer:solis-medtech"])
    assert all("CANARY" not in s for s in external.seen)

    # An external model can never be configured for the private side
    with pytest.raises(ValueError):
        NarratorConfig(private=RecordingNarrator("bad", "external"))


@pytest.mark.asyncio
async def test_model_text_with_invented_citations_is_discarded(api_client: Any, api_app: Any) -> None:
    liar = RecordingNarrator("fake-external", "external", reply="It has 40 billion in assets [999].")
    wiki = _wiki(api_app, NarratorConfig(public=liar))
    page = await wiki.build_page(VEH, build_principal_from_fixture(ANALYST))
    assert page.narrator == "deterministic"
    assert "[999]" not in render_markdown(page)

    assert citations_valid("Fact one [1].\n\n- Fact two [2]", {1, 2})
    assert not citations_valid("Fact one [1].\n\nUncited paragraph.", {1, 2})
    assert not citations_valid("No citations at all.", {1})


def test_boundary_check_fails_closed() -> None:
    assert external_model_allowed([WallSide.public_side, "public_side"])
    assert not external_model_allowed([WallSide.private_side])
    assert not external_model_allowed([None])
    assert not external_model_allowed([object()])


@pytest.mark.asyncio
async def test_viewport_agent_never_sends_private_context_to_gemini(api_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import context_agents.viewport_agent as va

    calls: list[str] = []

    async def fake_gemini(self: Any, **kw: Any) -> str:
        calls.append(json.dumps([c.object_value for c in kw["claims"]]))
        return "gemini answer"

    monkeypatch.setattr(va, "_resolve_gemini_api_key", lambda: "test-key")
    monkeypatch.setattr(va.ViewportGroundedAgent, "_try_gemini_answer", fake_gemini)
    reg = api_app.state.runtime.registry
    agent = va.ViewportGroundedAgent(store=reg.store, retrieval=reg.retrieval, policy=reg.policy, graph=reg.graph, llm_mode="gemini")

    deal = build_principal_from_fixture(DEAL)
    res = await agent.answer(message="Solis pricing", viewport=va.ViewportContext(visible_entity_ids=["ent:issuer-solis-medtech"]), principal=deal)
    assert res.llm_mode_used == "deterministic" and calls == []

    analyst = build_principal_from_fixture(ANALYST)
    res = await agent.answer(message="admin code", viewport=va.ViewportContext(visible_entity_ids=[VEH_ID]), principal=analyst)
    assert res.llm_mode_used == "gemini" and len(calls) == 1


# ---------------------------------------------------------------------------
# 4. Freshness and channels
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_page_is_rebuilt_when_a_claim_changes(api_client: Any) -> None:
    first = (await _page(api_client, ANALYST)).json()["page"]
    again = (await _page(api_client, ANALYST)).json()["page"]
    assert again["fingerprint"] == first["fingerprint"] and again["narration_cached"] is True

    item = {
        "record_type": "claim",
        "op": "claim_create",
        "payload": {"subject_id": VEH_ID, "predicate": "domicile", "object_value": "Cayman Islands", "epistemic_status": "approved_fact"},
    }
    r = await api_client.post("/api/v1/change-sets", json={"title": "Domicile", "items": [item]}, headers=H(STEWARD))
    assert r.status_code == 201, r.text
    after = (await _page(api_client, ANALYST)).json()
    assert after["page"]["fingerprint"] != first["fingerprint"]
    assert after["page"]["narration_cached"] is False
    assert "Domicile: Cayman Islands" in after["markdown"]


@pytest.mark.asyncio
async def test_channels_pin_what_pages_show(api_client: Any) -> None:
    r = await api_client.post("/api/v1/releases", json={"notes": "before domicile"}, headers=H(STEWARD))
    rel = r.json()["release"]["release_id"]
    assert (await api_client.put("/api/v1/channels/production", json={"release_id": rel}, headers=H(STEWARD))).status_code == 200
    item = {
        "record_type": "claim",
        "op": "claim_create",
        "payload": {"subject_id": VEH_ID, "predicate": "domicile", "object_value": "Cayman Islands", "epistemic_status": "approved_fact"},
    }
    assert (await api_client.post("/api/v1/change-sets", json={"title": "Domicile", "items": [item]}, headers=H(STEWARD))).status_code == 201
    dev = await _page(api_client, ANALYST, format="markdown")
    prod = await _page(api_client, ANALYST, format="markdown", channel="production")
    assert dev.headers["content-type"].startswith("text/markdown")
    assert "Domicile: Cayman Islands" in dev.text
    assert "Domicile: Cayman Islands" not in prod.text


# ---------------------------------------------------------------------------
# 5. Templates
# ---------------------------------------------------------------------------


def _template_body(**over: Any) -> dict[str, Any]:
    body = {
        "name": "Vehicle page",
        "entity_types": ["legal_vehicle"],
        "sections": [
            {"id": "overview", "title": "Summary", "kind": "overview"},
            {"id": "codes", "title": "Codes", "kind": "facts", "predicates": ["has_admin_code"]},
        ],
        "reason": "Vehicles only need their codes",
    }
    body.update(over)
    return body


@pytest.mark.asyncio
async def test_template_edits_go_through_change_sets(api_client: Any) -> None:
    # A non-steward's template is filed for review and changes nothing yet
    r = await api_client.put("/api/v1/wiki/templates/vehicle", json=_template_body(), headers=H(ANALYST))
    assert r.status_code == 202, r.text
    assert r.json()["change_set"]["status"] == "proposed"
    assert (await _page(api_client, ANALYST)).json()["page"]["template_id"] == "default"

    # A steward's template merges and is used straight away, with a redline in the change set
    r = await api_client.put("/api/v1/wiki/templates/vehicle", json=_template_body(), headers=H(STEWARD))
    assert r.status_code == 201, r.text
    cs = r.json()["change_set"]
    assert cs["status"] == "merged" and cs["items"][0]["record_type"] == "knowledge_item"
    page = (await _page(api_client, ANALYST)).json()["page"]
    assert page["template_id"] == "vehicle"
    assert [s["title"] for s in page["sections"]] == ["Summary", "Codes"]
    codes = page["sections"][1]["statements"]
    assert codes and all("Admin code" in st["text"] for st in codes)

    listed = (await api_client.get("/api/v1/wiki/templates", headers=H(ANALYST))).json()
    assert {t["template_id"] for t in listed["templates"]} >= {"default", "issuer", "vehicle"}

    # Invalid section kinds are refused at the endpoint
    bad = _template_body(sections=[{"id": "x", "title": "X", "kind": "gossip"}])
    assert (await api_client.put("/api/v1/wiki/templates/vehicle", json=bad, headers=H(STEWARD))).status_code == 422


@pytest.mark.asyncio
async def test_broken_template_falls_back_and_is_reported(api_client: Any) -> None:
    item = {
        "record_type": "knowledge_item",
        "op": "upsert",
        "record_id": "kf:page-template-issuer",
        "payload": {"body_markdown": "No yaml block here any more."},
    }
    r = await api_client.post("/api/v1/change-sets", json={"title": "Break it", "items": [item]}, headers=H(STEWARD))
    assert r.status_code == 201, r.text
    listed = (await api_client.get("/api/v1/wiki/templates", headers=H(ANALYST))).json()
    assert any(e["item_id"] == "kf:page-template-issuer" for e in listed["errors"])
    page = (await _page(api_client, ANALYST, "issuer:apex-industrial")).json()["page"]
    assert page["template_id"] == "default"


def test_template_lookup_walks_taxonomy_parents() -> None:
    sec = [SectionSpec(id="overview", title="Overview", kind="overview")]
    credit = PageTemplate(template_id="credit", name="Credit", entity_types=["credit_issuer"], sections=sec)
    star = PageTemplate(template_id="star", name="All", entity_types=["*"], sections=sec)
    parents = {"bank_loan_issuer": "credit_issuer", "credit_issuer": "issuer"}.get
    assert resolve_template("bank_loan_issuer", [credit, star], type_parent=parents).template_id == "credit"
    assert resolve_template("fund", [credit, star], type_parent=parents).template_id == "star"
    assert resolve_template("fund", [credit]) is BUILTIN_TEMPLATE
    loop = {"a": "b", "b": "a"}.get
    assert resolve_template("a", [star], type_parent=loop).template_id == "star"


# ---------------------------------------------------------------------------
# 6. MCP tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_tools_match_rest_and_hold_the_barrier(api_client: Any, api_app: Any) -> None:
    import context_mcp

    await _plant_private_claim(api_client)
    rt = api_app.state.runtime.mcp_runtime
    analyst = build_principal_from_fixture(ANALYST)
    deal = build_principal_from_fixture(DEAL)

    structure = await context_mcp.tool_wiki_structure(rt, analyst, entity_types=["legal_vehicle"])
    keys = [p["canonical_key"] for g in structure["groups"] for p in g["pages"]]
    assert VEH in keys

    page = await context_mcp.tool_wiki_read(rt, analyst, id_or_key=VEH)
    assert page["found"] and PRIVATE_CANARY not in json.dumps(page)
    rest = (await _page(api_client, ANALYST)).json()["page"]
    assert page["page"]["fingerprint"] == rest["fingerprint"]

    assert (await context_mcp.tool_wiki_read(rt, analyst, id_or_key="proj:project-cobalt-lme"))["found"] is False
    assert PRIVATE_CANARY in json.dumps(await context_mcp.tool_wiki_read(rt, deal, id_or_key=VEH))

    ans = await context_mcp.tool_wiki_ask(rt, analyst, question="What is the admin code of Alpha Credit Offshore?")
    assert ans["sources"] and "[" in ans["answer_markdown"]
    for tok in _forbidden_public_tokens():
        assert tok not in json.dumps(ans)


@pytest.mark.asyncio
async def test_internal_narrator_and_env_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from context_agents.wiki_narrators import InternalChatNarrator, narrators_from_env

    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "Fund vehicle [1]."}}]})

    narrator = InternalChatNarrator("http://models.internal/v1", "firm-model", transport=httpx.MockTransport(handler))
    ent = Entity(entity_id="e", entity_type="issuer", canonical_key="issuer:e", display_name="E")
    out = await narrator.overview(entity=ent, statements=[NumberedStatement(n=1, text="Domicile: Cayman", section="facts")])
    assert out == "Fund vehicle [1]." and seen[0]["model"] == "firm-model"
    assert "[1] Domicile: Cayman" in seen[0]["messages"][1]["content"]

    monkeypatch.delenv("LLM_MODE", raising=False)
    monkeypatch.setenv("CONTEXT_WIKI_PRIVATE_NARRATOR", "gemini")
    with pytest.raises(ValueError):
        narrators_from_env("auto")
    monkeypatch.setenv("CONTEXT_WIKI_PRIVATE_NARRATOR", "internal")
    monkeypatch.setenv("CONTEXT_WIKI_INTERNAL_MODEL_URL", "http://models.internal/v1")
    monkeypatch.setenv("CONTEXT_WIKI_INTERNAL_MODEL", "firm-model")
    monkeypatch.setenv("CONTEXT_WIKI_NARRATOR", "gemini")  # no Gemini key in tests, so deterministic
    cfg = narrators_from_env("auto")
    assert cfg.private.name == "internal" and cfg.public.name == "deterministic"
    assert narrators_from_env("deterministic").private.name == "deterministic"
