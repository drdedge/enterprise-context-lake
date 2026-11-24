"""Oracle 9: LangSmith Context Hub export, import and migration (ADR-008).

Runs fully offline against the local folder emulation of Context Hub:
  1. Export at a release is public-side only: no MNPI, side-letter or personal-memory canary
     appears in any exported file, and private, personal and unclassifiable records are refused.
  2. Classification fails closed on missing or mismatched wall sides and read groups.
  3. Claims never carry evidence excerpts, and restricted evidence is not linked.
  4. Migration records the Context Hub commit on the release, and re-export is idempotent.
  5. Edits made in Context Hub block the next export until they are imported, arrive as a
     proposed change set that the importer cannot approve, and merge after a steward approves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import load_json_fixture, mock_headers
from context_adapters.context_hub_clients import FolderHubClient
from context_core.context_hub import (
    MANIFEST_PATH,
    build_export_plan,
    build_import_plan,
    classify_version,
    split_frontmatter,
)
from context_core.versioning import VersionRecord

STEWARD = mock_headers("user:fund-steward")
SECOND_STEWARD = mock_headers("user:overage-director", groups_overage=True)


def _canaries() -> list[str]:
    return [c["token"] for c in load_json_fixture("principals.json")["canaries"]]


@pytest.fixture
def hub(api_app: Any, tmp_path: Path) -> FolderHubClient:
    from context_core.context_hub_service import ContextHubService

    client = FolderHubClient(tmp_path / "hub")
    api_app.state.context_hub = ContextHubService(versions=api_app.state.runtime.registry.versions, client=client)
    return client


async def _cut(client: httpx.AsyncClient, notes: str = "") -> int:
    r = await client.post("/api/v1/releases", json={"notes": notes}, headers=STEWARD)
    assert r.status_code == 201, r.text
    return r.json()["release"]["release_id"]


def _all_text(root: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in root.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# Pure planning
# ---------------------------------------------------------------------------


def _v(**kw: Any) -> VersionRecord:
    base: dict[str, Any] = {
        "record_type": "knowledge_item",
        "record_id": "kf:x",
        "version_no": 1,
        "change_id": "cs-1",
        "op": "create",
        "recorded_at": "2026-01-01T00:00:00Z",
        "wall_side": "public_side",
        "read_acl_groups": ["firm-all"],
        "payload": {"wall_side": "public_side", "read_acl_groups": ["firm-all"]},
    }
    base.update(kw)
    return VersionRecord(**base)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({}, None),
        ({"wall_side": None}, "unclassified_wall_side"),
        ({"wall_side": "private_side"}, "not_public_side"),
        ({"wall_side": "above_the_wall"}, "not_public_side"),
        ({"read_acl_groups": None}, "unclassified_read_groups"),
        ({"read_acl_groups": []}, "unclassified_read_groups"),
        ({"owner_user_id": "user:pm-credit"}, "personal"),
        ({"payload": {"wall_side": "private_side", "read_acl_groups": ["firm-all"]}}, "payload_wall_side_mismatch"),
        ({"payload": {"read_acl_groups": ["firm-all"]}}, "payload_wall_side_mismatch"),
        ({"payload": {"wall_side": "public_side", "read_acl_groups": ["other"]}}, "payload_read_groups_mismatch"),
        ({"record_type": "memory"}, "not_exported_type:memory"),
        ({"payload": None}, "deleted"),
    ],
)
def test_classification_fails_closed(kwargs: dict[str, Any], reason: str | None) -> None:
    assert classify_version(_v(**kwargs)) == reason


def _claim_payload(**acl: Any) -> dict[str, Any]:
    return {
        "claim_id": "clm-1",
        "subject_id": "ent:a",
        "predicate": "admin_code",
        "object_value": "ADM-1",
        "context_purpose": "fund_admin",
        "provenance": {
            "source_system": "custodian-feed",
            "evidence_uri": "s3://bucket/side_letter.pdf",
            "span_locator": "page=1",
            "excerpt": "SECRET EXCERPT TEXT",
        },
        "acl": {"wall_side": "public_side", "read_acl_groups": ["firm-all"], **acl},
    }


def test_claims_never_export_excerpts_and_hide_restricted_evidence() -> None:
    entity = _v(
        record_type="entity",
        record_id="ent:a",
        payload={
            "entity_id": "ent:a",
            "entity_type": "issuer",
            "canonical_key": "issuer:a",
            "display_name": "A Corp",
            "wall_side": "public_side",
            "read_acl_groups": ["firm-all"],
        },
    )
    restricted = _v(record_type="claim", record_id="slot-1", payload=_claim_payload(evidence_acl_groups=["legal"]))
    plan = build_export_plan({"entity": [entity], "claim": [restricted]})
    text = "\n".join(plan.repos[0].text_files().values())
    assert "ADM-1" in text
    assert "SECRET EXCERPT TEXT" not in text
    assert "side_letter.pdf" not in text
    assert "restricted (held in the lake)" in text

    open_evidence = _v(record_type="claim", record_id="slot-1", payload=_claim_payload())
    text = "\n".join(build_export_plan({"entity": [entity], "claim": [open_evidence]}).repos[0].text_files().values())
    assert "s3://bucket/side_letter.pdf" in text
    assert "SECRET EXCERPT TEXT" not in text


def test_import_never_touches_records_outside_the_repo_scope() -> None:
    private = _v(record_id="kf:private", wall_side="private_side", read_acl_groups=["deal-team"],
                 payload={"body_markdown": "old", "wall_side": "private_side", "read_acl_groups": ["deal-team"]})
    other_group = _v(record_id="kf:other", read_acl_groups=["pm-credit"],
                     payload={"body_markdown": "old", "wall_side": "public_side", "read_acl_groups": ["pm-credit"]})
    lookup = {"kf:private": private, "kf:other": other_group}

    def file(rid: str) -> str:
        return f"---\nlake:\n  record_type: knowledge_item\n  record_id: {rid}\n---\n\nnew body\n"

    plan = build_import_plan(
        repo_handle="context-lake-firm-all",
        kind="agent",
        commit_hash="abc12345",
        files={
            "knowledge/domain_knowledge/p.md": file("kf:private"),
            "knowledge/domain_knowledge/o.md": file("kf:other"),
            "knowledge/domain_knowledge/n.md": "# New note\n\nbody\n",
            "entities/issuer/a.md": "edited",
            "notes/random.md": "x",
        },
        read_acl_groups=["firm-all"],
        latest=lambda rt, rid: lookup.get(rid),
    )
    reasons = {s["path"]: s["reason"] for s in plan.skipped}
    assert reasons["knowledge/domain_knowledge/p.md"] == "target_not_in_this_repo_scope"
    assert reasons["knowledge/domain_knowledge/o.md"] == "target_not_in_this_repo_scope"
    assert reasons["entities/issuer/a.md"] == "export_only"
    assert reasons["notes/random.md"] == "unrecognised_path"
    assert [i.record_id for i in plan.items] == ["kf:hub-context-lake-firm-all-n"]
    new = plan.items[0].payload
    assert new["wall_side"] == "public_side" and new["read_acl_groups"] == ["firm-all"]


# ---------------------------------------------------------------------------
# End to end through the API
# ---------------------------------------------------------------------------


async def test_export_is_public_side_only_with_no_canaries(api_client: httpx.AsyncClient, hub: FolderHubClient) -> None:
    rid = await _cut(api_client, "first migration")
    r = await api_client.get(f"/api/v1/context-hub/releases/{rid}/plan", headers=STEWARD)
    assert r.status_code == 200, r.text
    plan = r.json()["plan"]
    refused = plan["refused_by_reason"]
    assert refused.get("not_public_side", 0) >= 4
    assert refused.get("not_exported_type:memory", 0) >= 1
    assert all(repo["read_acl_groups"] for repo in plan["repos"])

    r = await api_client.post(f"/api/v1/context-hub/releases/{rid}/export", json={}, headers=STEWARD)
    assert r.status_code == 200, r.text
    commits = r.json()["hub_commits"]
    assert "context-lake-firm-all" in commits and "context-lake-pm-credit" in commits

    text = _all_text(hub.root)
    for token in _canaries():
        assert token not in text, f"CANARY EXPORTED TO CONTEXT HUB: {token}"
    for marker in ("mnpi-project-cobalt", "private-credit-deal-team"):
        assert marker not in text, marker

    for repo_dir in hub.root.iterdir():
        manifest = repo_dir / MANIFEST_PATH
        if manifest.is_file():
            assert "wall_side: public_side" in manifest.read_text(encoding="utf-8")
        for md in repo_dir.rglob("*.md"):
            fm, _ = split_frontmatter(md.read_text(encoding="utf-8"))
            if "lake" in fm:
                assert fm["lake"]["wall_side"] == "public_side"

    releases = (await api_client.get("/api/v1/releases", headers=STEWARD)).json()["releases"]
    rel = next(x for x in releases if x["release_id"] == rid)
    assert rel["hub_commit"] and "context-lake-firm-all" in rel["hub_commit"]

    again = await api_client.post(f"/api/v1/context-hub/releases/{rid}/export", json={}, headers=STEWARD)
    assert again.json().get("already_exported") is True


async def test_only_release_managers_can_export_or_import(api_client: httpx.AsyncClient, hub: FolderHubClient) -> None:
    rid = await _cut(api_client)
    for user in ("user:public-analyst", "user:private-deal-team", "user:pm-credit"):
        h = mock_headers(user)
        assert (await api_client.post(f"/api/v1/context-hub/releases/{rid}/export", json={}, headers=h)).status_code == 403
        assert (await api_client.get(f"/api/v1/context-hub/releases/{rid}/plan", headers=h)).status_code == 403
        r = await api_client.post("/api/v1/context-hub/import", json={"repo_handle": "context-lake-firm-all"}, headers=h)
        assert r.status_code == 403


async def test_hub_edits_block_export_until_imported_and_approved(api_client: httpx.AsyncClient, hub: FolderHubClient) -> None:
    rid1 = await _cut(api_client)
    first = await api_client.post(f"/api/v1/context-hub/releases/{rid1}/export", json={}, headers=STEWARD)
    assert first.status_code == 200, first.text

    # Someone edits a knowledge note directly in Context Hub.
    handle = "context-lake-firm-all"
    pulled = hub.pull(handle, "agent")
    path = "knowledge/domain_knowledge/alpha-credit-lme-playbook.md"
    fm_text = pulled.files[path]
    assert isinstance(fm_text, str)
    edited = fm_text.rstrip("\n") + "\n\n## Added in Context Hub\n\nCheck covenant baskets first.\n"
    hub.push(handle, "agent", {path: edited, "entities/issuer/issuer-apex-industrial.md": "vandalised"},
             parent_commit=pulled.commit_hash)

    # The next export refuses to overwrite those edits.
    rid2 = await _cut(api_client)
    blocked = await api_client.post(f"/api/v1/context-hub/releases/{rid2}/export", json={}, headers=STEWARD)
    assert blocked.status_code == 409, blocked.text

    # Import files them as a proposal; entity edits are ignored.
    imp = await api_client.post("/api/v1/context-hub/import", json={"repo_handle": handle}, headers=STEWARD)
    assert imp.status_code == 201, imp.text
    body = imp.json()
    assert body["import"]["proposed_items"] == 1
    assert {"path": "entities/issuer/issuer-apex-industrial.md", "reason": "export_only"} in body["import"]["skipped"]
    cs = body["change_set"]
    assert cs["status"] == "proposed" and cs["kind"] == "import" and cs["source"] == "context_hub"
    assert "context-hub-import" in cs["rule_ids"]

    # The importer cannot approve their own import; a second steward can.
    own = await api_client.post(f"/api/v1/change-sets/{cs['change_id']}/approve", json={}, headers=STEWARD)
    assert own.status_code == 403, own.text
    ok = await api_client.post(f"/api/v1/change-sets/{cs['change_id']}/approve", json={}, headers=SECOND_STEWARD)
    assert ok.status_code == 200, ok.text
    assert ok.json()["change_set"]["status"] == "merged"

    # A release cut after the merge exports on top of the Hub edits and carries them.
    rid3 = await _cut(api_client)
    done = await api_client.post(f"/api/v1/context-hub/releases/{rid3}/export", json={}, headers=STEWARD)
    assert done.status_code == 200, done.text
    latest = hub.pull(handle, "agent").files
    assert "Check covenant baskets first." in str(latest[path])
    assert latest["entities/issuer/issuer-apex-industrial.md"] != "vandalised"


async def test_import_of_unknown_repo_needs_explicit_readers(api_client: httpx.AsyncClient, hub: FolderHubClient) -> None:
    hub.push("team-notes", "agent", {"knowledge/domain_knowledge/tips.md": "# Tips\n\nUse the lake.\n"}, parent_commit=None)
    r = await api_client.post("/api/v1/context-hub/import", json={"repo_handle": "team-notes"}, headers=STEWARD)
    assert r.status_code == 422, r.text
    r = await api_client.post(
        "/api/v1/context-hub/import",
        json={"repo_handle": "team-notes", "read_acl_groups": ["firm-all"]},
        headers=STEWARD,
    )
    assert r.status_code == 201, r.text
    cs = r.json()["change_set"]
    assert cs["status"] == "proposed"
    item = cs["items"][0]
    assert item["after"]["wall_side"] == "public_side"
    assert item["after"]["read_acl_groups"] == ["firm-all"]


def test_langsmith_client_maps_entries_and_refuses_moved_heads() -> None:
    from types import SimpleNamespace

    from langsmith.schemas import FileEntry, SkillEntry

    from context_adapters.context_hub_clients import HubConflictError, LangSmithHubClient
    from context_core.context_hub import SkillLink

    class FakeSdk:
        def __init__(self) -> None:
            self.head = "aaaaaaaa11111111"
            self.pushed: dict[str, Any] = {}

        def agent_exists(self, ident: str) -> bool:
            return True

        def pull_agent(self, ident: str) -> Any:
            return SimpleNamespace(
                commit_hash=self.head,
                files={"AGENTS.md": FileEntry(content="hi"), "skills/x": SkillEntry(repo_handle="sk-x", commit_hash="bbbbbbbb")},
            )

        def push_agent(self, ident: str, **kw: Any) -> str:
            self.pushed = {"ident": ident, **kw}
            return "https://smith.langchain.com/context/repo/cccccccc?organizationId=o"

    sdk = FakeSdk()
    client = LangSmithHubClient(client=sdk, owner="acme")
    pulled = client.pull("repo", "agent")
    assert pulled.files["AGENTS.md"] == "hi" and pulled.files["skills/x"] == SkillLink("sk-x", "bbbbbbbb")

    with pytest.raises(HubConflictError):
        client.push("repo", "agent", {"AGENTS.md": "new"}, parent_commit="deadbeef")

    commit = client.push(
        "repo", "agent", {"AGENTS.md": "new", "old.md": None, "skills/y": SkillLink("sk-y", "dddddddd")},
        parent_commit="aaaaaaaa",
    )
    assert commit == "cccccccc"
    assert sdk.pushed["ident"] == "acme/repo"
    assert sdk.pushed["parent_commit"] == "aaaaaaaa11111111"
    assert sdk.pushed["is_public"] is False
    files = sdk.pushed["files"]
    assert isinstance(files["AGENTS.md"], FileEntry) and files["old.md"] is None
    assert isinstance(files["skills/y"], SkillEntry) and files["skills/y"].repo_handle == "sk-y"
