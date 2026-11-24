"""LangSmith Context Hub export/import planning (ADR-008).

Pure functions, no network. They turn ledger versions at a release cut into Context Hub repo
bundles, and turn pulled Context Hub files back into change-set items.

Export is fail-closed:

- Only records whose ledger version is explicitly ``public_side``, with a non-empty
  ``read_acl_groups`` list, are exported.
- Personal records (memory, watchlist), topics, deleted records, superseded claims and anything
  that cannot be classified are refused, and each refusal is reported with a reason.
- Claims never carry evidence excerpts or span locators. They carry an evidence link only when
  the evidence has no separate ACL.

Each read-group set gets its own agent repo, because Context Hub controls access per workspace
and repo, not per file. Skill procedures become skill repos, linked from their agent repo.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

import yaml

from context_core.models import Claim, Entity, KnowledgeItem
from context_core.versioning import ChangeItemInput, VersionRecord, wall_value

FORMAT_VERSION = 1
EXPORTABLE_WALL_SIDE = "public_side"
REPO_HANDLE_RE = re.compile(r"^[a-z][a-z0-9-_]*$")
MANIFEST_PATH = ".lake/repo.json"
EXPORTED_RECORD_TYPES = ("knowledge_item", "entity", "claim")
KNOWLEDGE_DIR_KINDS = ("domain_knowledge", "data_access_guidance")

HubKind = Literal["agent", "skill"]


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillLink:
    """A link from an agent repo to a skill repo (Context Hub ``SkillEntry``)."""

    repo_handle: str
    commit_hash: str | None = None


HubFileValue = str | SkillLink | None  # None deletes the path in a commit


@dataclass
class HubRepoBundle:
    handle: str
    kind: HubKind
    read_acl_groups: tuple[str, ...]
    files: dict[str, HubFileValue] = field(default_factory=dict)
    description: str = ""
    record_ids: list[str] = field(default_factory=list)

    def text_files(self) -> dict[str, str]:
        return {p: v for p, v in self.files.items() if isinstance(v, str)}


@dataclass(frozen=True)
class Refusal:
    record_type: str
    record_id: str
    reason: str


@dataclass
class ExportPlan:
    release_id: int | None
    cut_at: str | None
    repos: list[HubRepoBundle] = field(default_factory=list)
    refused: list[Refusal] = field(default_factory=list)

    def repo(self, handle: str) -> HubRepoBundle | None:
        return next((r for r in self.repos if r.handle == handle), None)

    def summary(self) -> dict[str, Any]:
        reasons: dict[str, int] = {}
        for r in self.refused:
            reasons[r.reason] = reasons.get(r.reason, 0) + 1
        return {
            "release_id": self.release_id,
            "cut_at": self.cut_at,
            "repos": [
                {
                    "handle": r.handle,
                    "kind": r.kind,
                    "read_acl_groups": list(r.read_acl_groups),
                    "file_count": len(r.text_files()),
                    "linked_skills": sorted(p for p, v in r.files.items() if isinstance(v, SkillLink)),
                }
                for r in self.repos
            ],
            "exported_records": sum(len(r.record_ids) for r in self.repos),
            "refused_count": len(self.refused),
            "refused_by_reason": dict(sorted(reasons.items())),
        }


# ---------------------------------------------------------------------------
# Naming and rendering helpers
# ---------------------------------------------------------------------------


def _slug(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return s or "item"


def repo_handle_for_groups(prefix: str, groups: Iterable[str]) -> str:
    handle = f"{_slug(prefix)}-{_slug('-'.join(sorted(groups)))}"
    if not REPO_HANDLE_RE.match(handle):
        handle = "r-" + handle
    return handle[:100]


def skill_repo_handle(prefix: str, slug: str) -> str:
    handle = f"{_slug(prefix)}-skill-{_slug(slug)}"
    if not REPO_HANDLE_RE.match(handle):
        handle = "r-" + handle
    return handle[:100]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def render_markdown(frontmatter: dict[str, Any], body: str) -> str:
    fm = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True, default_flow_style=False).strip()
    body_lf = body.replace("\r\n", "\n").strip("\n")
    return f"---\n{fm}\n---\n\n{body_lf}\n"


_FM_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    text = text.replace("\r\n", "\n")
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    return data, text[m.end():].lstrip("\n")


def _lake_block(v: VersionRecord, release_id: int | None, **extra: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "format": FORMAT_VERSION,
        "record_type": v.record_type,
        "record_id": v.record_id,
        "version_no": v.version_no,
        "wall_side": EXPORTABLE_WALL_SIDE,
        "read_acl_groups": sorted(v.read_acl_groups or []),
    }
    if release_id is not None:
        block["release_id"] = release_id
    block.update({k: val for k, val in extra.items() if val not in (None, [], {}, "")})
    return block


# ---------------------------------------------------------------------------
# Classification (fail-closed)
# ---------------------------------------------------------------------------


def classify_version(v: VersionRecord) -> str | None:
    """Return a refusal reason, or None when the version may be exported."""
    if v.record_type not in EXPORTED_RECORD_TYPES:
        return f"not_exported_type:{v.record_type}"
    if v.payload is None:
        return "deleted"
    if v.owner_user_id is not None:
        return "personal"
    wall = wall_value(v.wall_side)
    if wall is None:
        return "unclassified_wall_side"
    if wall != EXPORTABLE_WALL_SIDE:
        return "not_public_side"
    if not v.read_acl_groups:
        return "unclassified_read_groups"
    payload_wall = wall_value(v.payload.get("wall_side") or (v.payload.get("acl") or {}).get("wall_side"))
    if payload_wall != EXPORTABLE_WALL_SIDE:
        return "payload_wall_side_mismatch"
    payload_groups = v.payload.get("read_acl_groups")
    if payload_groups is None:
        payload_groups = (v.payload.get("acl") or {}).get("read_acl_groups")
    if sorted(payload_groups or []) != sorted(v.read_acl_groups):
        return "payload_read_groups_mismatch"
    return None


# ---------------------------------------------------------------------------
# Export planning
# ---------------------------------------------------------------------------


def _knowledge_file(item: KnowledgeItem, v: VersionRecord, release_id: int | None) -> tuple[str, str]:
    kind = item.knowledge_kind
    path = f"knowledge/{kind}/{_slug(item.slug)}.md"
    fm = {
        "title": item.display_name,
        "lake": _lake_block(
            v,
            release_id,
            knowledge_kind=kind,
            slug=item.slug,
            topics=list(item.topics),
            related_entities=list(item.related_entities),
            valid_from=item.valid_from.isoformat(),
            content_sha256=item.content_sha256,
        ),
    }
    return path, render_markdown(fm, item.body_markdown)


def _skill_file(item: KnowledgeItem, v: VersionRecord, release_id: int | None) -> str:
    meta = {k: val for k, val in (item.metadata or {}).items() if k not in ("lake",)}
    fm: dict[str, Any] = {
        "name": str(meta.pop("name", item.slug)),
        "description": str(meta.pop("description", item.display_name)),
    }
    if meta:
        fm["metadata"] = meta
    fm["lake"] = _lake_block(
        v,
        release_id,
        knowledge_kind=item.knowledge_kind,
        slug=item.slug,
        display_name=item.display_name,
        topics=list(item.topics),
        related_entities=list(item.related_entities),
        content_sha256=item.content_sha256,
    )
    return render_markdown(fm, item.body_markdown)


def _claim_row(c: Claim) -> str:
    def cell(value: Any) -> str:
        return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")

    evidence = ""
    prov = c.provenance
    if not c.acl.evidence_acl_groups and prov.evidence_uri:
        evidence = prov.evidence_uri
    elif c.acl.evidence_acl_groups:
        evidence = "restricted (held in the lake)"
    valid_from = c.valid_from.date().isoformat() if c.valid_from else ""
    return (
        f"| {cell(c.predicate)} | {cell(c.object_value)} | {cell(c.epistemic_status)} | "
        f"{cell(c.context_purpose)} | {cell(valid_from)} | {cell(prov.source_system)} | {cell(evidence)} | {cell(c.claim_id)} |"
    )


def _entity_page(
    entity: Entity | None,
    entity_id: str,
    entity_v: VersionRecord | None,
    claims: list[tuple[Claim, VersionRecord]],
    release_id: int | None,
) -> tuple[str, str]:
    etype = entity.entity_type.value if entity is not None else "entity"
    key = entity.canonical_key if entity is not None else entity_id
    path = f"entities/{_slug(etype)}/{_slug(key)}.md"
    lake: dict[str, Any] = {
        "format": FORMAT_VERSION,
        "record_type": "entity",
        "record_id": entity_id,
        "export_only": True,
        "wall_side": EXPORTABLE_WALL_SIDE,
    }
    if entity_v is not None:
        lake["version_no"] = entity_v.version_no
        lake["read_acl_groups"] = sorted(entity_v.read_acl_groups or [])
        lake["canonical_key"] = key
        lake["entity_type"] = etype
    if release_id is not None:
        lake["release_id"] = release_id
    lake["claims"] = [{"claim_id": c.claim_id, "slot": cv.record_id, "version_no": cv.version_no} for c, cv in claims]
    title = entity.display_name if entity is not None else entity_id
    lines = [f"# {title}", ""]
    if entity is not None and entity.description:
        lines += [entity.description, ""]
    if claims:
        lines += [
            "## Claims",
            "",
            "| Predicate | Value | Status | Purpose | Valid from | Source | Evidence | Claim |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        lines += [_claim_row(c) for c, _ in sorted(claims, key=lambda t: (t[0].predicate, t[0].claim_id))]
        lines.append("")
    lines.append("_Export-only copy. Edit claims in the Context Lake; changes made here are not imported._")
    return path, render_markdown({"title": title, "lake": lake}, "\n".join(lines))


def _agents_md(groups: tuple[str, ...], release_id: int | None, cut_at: str | None, files: dict[str, HubFileValue]) -> str:
    lines = [
        "# Enterprise Context Lake",
        "",
        f"Exported from Context Lake release {release_id if release_id is not None else '(working copy)'}"
        + (f", cut at {cut_at}." if cut_at else "."),
        f"Readers: {', '.join(groups)}. Public-side context only.",
        "",
        "Edits to files under `knowledge/` and to linked skills come back to the lake as proposals that a",
        "steward reviews. Entity pages under `entities/` are export-only.",
        "",
        "## Contents",
        "",
    ]
    for path in sorted(files):
        if path in ("AGENTS.md", MANIFEST_PATH):
            continue
        val = files[path]
        if isinstance(val, SkillLink):
            lines.append(f"- `{path}` (skill repo `{val.repo_handle}`)")
        elif isinstance(val, str):
            fm, _ = split_frontmatter(val)
            lines.append(f"- `{path}`: {fm.get('title') or fm.get('name') or path}")
    return "\n".join(lines) + "\n"


def build_export_plan(
    versions_by_type: dict[str, list[VersionRecord]],
    *,
    prefix: str = "context-lake",
    release_id: int | None = None,
    cut_at: str | None = None,
) -> ExportPlan:
    """Plan Context Hub repos from ledger versions (as of a release cut). Pure and deterministic."""
    plan = ExportPlan(release_id=release_id, cut_at=cut_at)
    agent_repos: dict[tuple[str, ...], HubRepoBundle] = {}

    def agent_repo(groups: tuple[str, ...]) -> HubRepoBundle:
        repo = agent_repos.get(groups)
        if repo is None:
            repo = HubRepoBundle(
                handle=repo_handle_for_groups(prefix, groups),
                kind="agent",
                read_acl_groups=groups,
                description=f"Enterprise Context Lake public-side context for {', '.join(groups)}",
            )
            agent_repos[groups] = repo
        return repo

    # Non-exported record types are refused up front so the report is complete.
    for rtype, versions in versions_by_type.items():
        if rtype in EXPORTED_RECORD_TYPES:
            continue
        for v in versions:
            if v.payload is not None:
                plan.refused.append(Refusal(rtype, v.record_id, f"not_exported_type:{rtype}"))

    def accepted(rtype: str) -> list[VersionRecord]:
        out = []
        for v in sorted(versions_by_type.get(rtype, []), key=lambda x: x.record_id):
            reason = classify_version(v)
            if reason is None:
                out.append(v)
            elif reason != "deleted":
                plan.refused.append(Refusal(rtype, v.record_id, reason))
        return out

    # Knowledge items and skills
    skill_repos: list[HubRepoBundle] = []
    for v in accepted("knowledge_item"):
        try:
            item = KnowledgeItem.model_validate(v.payload)
        except Exception:
            plan.refused.append(Refusal("knowledge_item", v.record_id, "invalid_payload"))
            continue
        groups = tuple(sorted(v.read_acl_groups or []))
        if item.knowledge_kind == "skill_procedure":
            skill = HubRepoBundle(
                handle=skill_repo_handle(prefix, item.slug),
                kind="skill",
                read_acl_groups=groups,
                description=str((item.metadata or {}).get("description") or item.display_name)[:500],
                record_ids=[v.record_id],
            )
            skill.files["SKILL.md"] = _skill_file(item, v, release_id)
            skill_repos.append(skill)
            agent_repo(groups).files[f"skills/{_slug(item.slug)}"] = SkillLink(skill.handle)
        else:
            path, text = _knowledge_file(item, v, release_id)
            repo = agent_repo(groups)
            repo.files[path] = text
            repo.record_ids.append(v.record_id)

    # Entities and claims (one export-only page per entity per repo)
    entities: dict[str, tuple[Entity, VersionRecord]] = {}
    for v in accepted("entity"):
        try:
            entities[v.record_id] = (Entity.model_validate(v.payload), v)
        except Exception:
            plan.refused.append(Refusal("entity", v.record_id, "invalid_payload"))

    claims_by_repo: dict[tuple[tuple[str, ...], str], list[tuple[Claim, VersionRecord]]] = {}
    for v in accepted("claim"):
        try:
            claim = Claim.model_validate(v.payload)
        except Exception:
            plan.refused.append(Refusal("claim", v.record_id, "invalid_payload"))
            continue
        if claim.superseded_at is not None:
            plan.refused.append(Refusal("claim", v.record_id, "superseded"))
            continue
        if claim.subject_id not in entities:
            plan.refused.append(Refusal("claim", v.record_id, "subject_not_exported"))
            continue
        groups = tuple(sorted(v.read_acl_groups or []))
        claims_by_repo.setdefault((groups, claim.subject_id), []).append((claim, v))

    pages: set[tuple[tuple[str, ...], str]] = set(claims_by_repo)
    for eid, (_, ev) in entities.items():
        pages.add((tuple(sorted(ev.read_acl_groups or [])), eid))
    for groups, eid in sorted(pages):
        entity, ev = entities[eid]
        same_groups = tuple(sorted(ev.read_acl_groups or [])) == groups
        path, text = _entity_page(
            entity if same_groups else None,
            eid,
            ev if same_groups else None,
            claims_by_repo.get((groups, eid), []),
            release_id,
        )
        repo = agent_repo(groups)
        repo.files[path] = text
        if same_groups:
            repo.record_ids.append(eid)
        repo.record_ids.extend(cv.record_id for _, cv in claims_by_repo.get((groups, eid), []))

    for groups, repo in sorted(agent_repos.items()):
        repo.files[MANIFEST_PATH] = (
            yaml.safe_dump(
                {
                    "format": FORMAT_VERSION,
                    "lake_repo": True,
                    "kind": "agent",
                    "wall_side": EXPORTABLE_WALL_SIDE,
                    "read_acl_groups": list(groups),
                    "release_id": release_id,
                    "cut_at": cut_at,
                },
                sort_keys=False,
            )
        )
        repo.files["AGENTS.md"] = _agents_md(groups, release_id, cut_at, repo.files)
    plan.repos = sorted(skill_repos, key=lambda r: r.handle) + [agent_repos[g] for g in sorted(agent_repos)]
    return plan


def deletions_since(previous: HubRepoBundle | None, current: HubRepoBundle) -> dict[str, None]:
    """Paths present in the previously exported bundle but gone now (deleted in the next commit)."""
    if previous is None:
        return {}
    return {p: None for p in previous.files if p not in current.files}


# ---------------------------------------------------------------------------
# Import planning
# ---------------------------------------------------------------------------


@dataclass
class ImportPlan:
    repo_handle: str
    commit_hash: str
    items: list[ChangeItemInput] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    unchanged: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "repo_handle": self.repo_handle,
            "commit_hash": self.commit_hash,
            "proposed_items": len(self.items),
            "unchanged": self.unchanged,
            "skipped": self.skipped,
        }


def _knowledge_kind_from_path(path: str) -> str | None:
    parts = path.split("/")
    if len(parts) == 3 and parts[0] == "knowledge" and parts[2].endswith(".md") and parts[1] in KNOWLEDGE_DIR_KINDS:
        return parts[1]
    return None


def build_import_plan(
    *,
    repo_handle: str,
    kind: HubKind,
    commit_hash: str,
    files: dict[str, Any],
    read_acl_groups: list[str],
    latest: Any,
) -> ImportPlan:
    """Turn pulled Context Hub files into knowledge-item upserts.

    ``latest(record_type, record_id) -> VersionRecord | None`` reads the lake's current version.
    Imports may only touch public-side knowledge items readable by exactly ``read_acl_groups``;
    anything else is skipped with a reason. Everything imported is public side.
    """
    plan = ImportPlan(repo_handle=repo_handle, commit_hash=commit_hash)
    groups = sorted({g for g in read_acl_groups if g})

    def skip(path: str, reason: str) -> None:
        plan.skipped.append({"path": path, "reason": reason})

    if not groups:
        skip("*", "read_acl_groups_required")
        return plan

    candidates: list[tuple[str, str, str]] = []  # (path, text, knowledge_kind)
    for path, value in sorted(files.items()):
        if not isinstance(value, str):
            skip(path, "linked_repo_import_separately")
            continue
        if path in ("AGENTS.md", MANIFEST_PATH) or path.startswith(".lake/"):
            continue
        if kind == "skill":
            if path == "SKILL.md":
                candidates.append((path, value, "skill_procedure"))
            else:
                skip(path, "skill_supporting_file_not_imported")
            continue
        if path.startswith("entities/"):
            skip(path, "export_only")
            continue
        k = _knowledge_kind_from_path(path)
        if k is None:
            skip(path, "unrecognised_path")
            continue
        candidates.append((path, value, k))

    for path, text, kkind in candidates:
        fm, body = split_frontmatter(text)
        lake = fm.get("lake") if isinstance(fm.get("lake"), dict) else {}
        record_id = lake.get("record_id") if lake.get("record_type") == "knowledge_item" else None
        if record_id is not None and not str(record_id).startswith("kf:"):
            skip(path, "invalid_record_id")
            continue
        slug = str(lake.get("slug") or (fm.get("name") if kkind == "skill_procedure" else None) or path.rsplit("/", 1)[-1].removesuffix(".md"))
        if record_id is None:
            record_id = f"kf:hub-{_slug(repo_handle)}-{_slug(slug)}"
        current = latest("knowledge_item", record_id)
        if current is not None and current.payload is not None:
            if wall_value(current.wall_side) != EXPORTABLE_WALL_SIDE or sorted(current.read_acl_groups or []) != groups:
                skip(path, "target_not_in_this_repo_scope")
                continue
            if (current.payload.get("body_markdown") or "").strip() == body.strip():
                plan.unchanged += 1
                continue
        payload: dict[str, Any] = {
            "item_id": record_id,
            "knowledge_kind": kkind,
            "body_markdown": body,
            "wall_side": EXPORTABLE_WALL_SIDE,
            "read_acl_groups": groups,
            "content_sha256": sha256_text(body),
        }
        if current is None or current.payload is None:
            payload.update(
                {
                    "slug": slug,
                    "display_name": str(fm.get("title") or lake.get("display_name") or fm.get("description") or slug),
                    "source_path": f"contexthub://{repo_handle}/{path}",
                }
            )
        if kkind == "skill_procedure":
            meta = dict(fm.get("metadata") or {}) if isinstance(fm.get("metadata"), dict) else {}
            for key in ("name", "description"):
                if fm.get(key):
                    meta[key] = fm[key]
            base_meta = dict((current.payload.get("metadata") or {})) if current is not None and current.payload else {}
            payload["metadata"] = {**base_meta, **meta}
        plan.items.append(
            ChangeItemInput(
                record_type="knowledge_item",
                op="upsert",
                record_id=record_id,
                base_version=current.version_no if current is not None else None,
                payload=payload,
            )
        )
    return plan
