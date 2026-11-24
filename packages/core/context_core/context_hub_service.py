"""Context Hub export, import and migration on top of the version-control ledger (ADR-008).

- **Export / migrate** a named release: build the public-side plan at the release's cut, push skill
  repos then agent repos (each with the previous export's commit as parent), and record the
  resulting commits on the release (``Release.hub_commit``, a JSON map of repo -> commit).
- **Import** a Context Hub repo: turn changed knowledge files into a proposed change set
  (kind ``import``, source ``context_hub``). The approval rules always send it to a steward.

Only release managers may export or import.
"""

from __future__ import annotations

import json
from typing import Any

from context_core.context_hub import (
    EXPORTED_RECORD_TYPES,
    MANIFEST_PATH,
    ExportPlan,
    HubRepoBundle,
    SkillLink,
    build_export_plan,
    build_import_plan,
    deletions_since,
    split_frontmatter,
)
from context_core.exceptions import AuthorizationError, NotFoundError, ValidationError
from context_core.models import Principal
from context_core.versioning import RECORD_TYPES, Release, VersionRecord

IMPORT_SOURCE = "context_hub"


def parse_hub_commits(release: Release | None) -> dict[str, str]:
    if release is None or not release.hub_commit:
        return {}
    try:
        data = json.loads(release.hub_commit)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if not str(k).startswith("_")}


def _export_incomplete(release: Release) -> bool:
    try:
        return bool(json.loads(release.hub_commit or "{}").get("_incomplete"))
    except (TypeError, ValueError, AttributeError):
        return False


class ContextHubService:
    def __init__(self, *, versions: Any, client: Any, prefix: str = "context-lake") -> None:
        self._svc = versions
        self.client = client
        self.prefix = prefix

    @property
    def ledger(self) -> Any:
        return self._svc.ledger

    def _require_release_manager(self, principal: Principal) -> None:
        groups = self._svc.rules.release_manager_groups
        if principal.revoked or not principal.groups.intersection(groups):
            raise AuthorizationError("Only release managers can export to or import from Context Hub: " + ", ".join(groups))

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _versions_at(self, cut_at: str | None) -> dict[str, list[VersionRecord]]:
        out: dict[str, list[VersionRecord]] = {}
        for rtype in RECORD_TYPES:
            rows: list[VersionRecord] = []
            for rid in sorted(self.ledger.known_record_ids(rtype)):
                v = self.ledger.latest_version(rtype, rid) if cut_at is None else self.ledger.version_at(rtype, rid, cut_at)
                if v is not None:
                    rows.append(v)
            out[rtype] = rows
        return out

    def _release(self, release_id: int) -> Release:
        rel = self.ledger.get_release(release_id)
        if rel is None:
            raise NotFoundError(f"Release not found: {release_id}")
        return rel

    def plan_release(self, release_id: int, principal: Principal) -> ExportPlan:
        self._require_release_manager(principal)
        rel = self._release(release_id)
        return build_export_plan(self._versions_at(rel.cut_at), prefix=self.prefix, release_id=rel.release_id, cut_at=rel.cut_at)

    def _previous_exports(self, release_id: int) -> tuple[dict[str, str], dict[str, HubRepoBundle]]:
        """Last commit the lake pushed to each repo before this release, and the bundle it pushed."""
        parents: dict[str, str] = {}
        bundles: dict[str, HubRepoBundle] = {}
        plans: dict[int, ExportPlan] = {}
        for rel in self.ledger.list_releases():
            if rel.release_id >= release_id:
                continue
            commits = parse_hub_commits(rel)
            if not commits:
                continue
            if rel.release_id not in plans:
                plans[rel.release_id] = build_export_plan(
                    self._versions_at(rel.cut_at), prefix=self.prefix, release_id=rel.release_id, cut_at=rel.cut_at
                )
            for handle, commit in commits.items():
                parents[handle] = commit
                bundle = plans[rel.release_id].repo(handle)
                if bundle is not None:
                    bundles[handle] = bundle
                else:
                    bundles.pop(handle, None)
        return parents, bundles

    def _latest_exported_release(self) -> int | None:
        done = [r.release_id for r in self.ledger.list_releases() if parse_hub_commits(r)]
        return max(done) if done else None

    def export_release(
        self,
        release_id: int,
        principal: Principal,
        *,
        dry_run: bool = False,
        accept_hub_heads: bool = False,
    ) -> dict[str, Any]:
        """Push a release's public-side context to Context Hub (the one-off migration is release N)."""
        plan = self.plan_release(release_id, principal)
        rel = self._release(release_id)
        existing = parse_hub_commits(rel)
        resuming = _export_incomplete(rel)
        if existing and not resuming and not dry_run:
            return {"release_id": release_id, "already_exported": True, "hub_commits": existing, "plan": plan.summary()}
        latest = self._latest_exported_release()
        if latest is not None and latest > release_id:
            raise ValidationError(
                f"Release {latest} is already in Context Hub; exporting the older release {release_id} would roll it back"
            )
        parents, prev_bundles = self._previous_exports(release_id)
        if dry_run:
            return {"release_id": release_id, "dry_run": True, "plan": plan.summary(), "parents": parents}

        commits: dict[str, str] = dict(existing) if resuming else {}
        try:
            self._push_plan(plan, prev_bundles, self._resolve_parents(plan, prev_bundles, parents, accept_hub_heads), commits)
        except Exception:
            # Record what already landed, so a retry resumes instead of conflicting with itself.
            if commits:
                self.ledger.update_release(
                    rel.model_copy(update={"hub_commit": json.dumps({**commits, "_incomplete": True}, sort_keys=True)})
                )
            raise
        rel = rel.model_copy(update={"hub_commit": json.dumps(commits, sort_keys=True)})
        self.ledger.update_release(rel)
        return {"release_id": release_id, "hub_commits": commits, "plan": plan.summary()}

    def _decided_import_commits(self) -> set[str]:
        decided = self.ledger.list_change_sets(statuses=["merged", "rejected"], include_implicit=False, limit=1000)
        return {cs.run_id for cs in decided if cs.kind == "import" and cs.source == IMPORT_SOURCE and cs.run_id}

    def _resolve_parents(
        self,
        plan: ExportPlan,
        prev_bundles: dict[str, HubRepoBundle],
        parents: dict[str, str],
        accept_hub_heads: bool,
    ) -> dict[str, str]:
        """Parent commit per repo.

        Normally the commit the lake last exported. If someone edited the repo in Context Hub since,
        the head moved and the push is refused, unless those edits were imported and a steward
        decided the resulting change set, or the release manager passed ``accept_hub_heads``.
        """
        handles = {r.handle: r.kind for r in plan.repos}
        handles.update({h: b.kind for h, b in prev_bundles.items()})
        decided = self._decided_import_commits()
        resolved = dict(parents)
        for handle, kind in handles.items():
            head = self.client.head(handle, kind)
            if head is None:
                continue
            known = parents.get(handle)
            if known and known[: min(len(known), len(head))] == head[: min(len(known), len(head))]:
                continue
            if accept_hub_heads or any(head.startswith(c) or c.startswith(head) for c in decided):
                resolved[handle] = head
        return resolved

    def _push_plan(
        self,
        plan: ExportPlan,
        prev_bundles: dict[str, HubRepoBundle],
        parents: dict[str, str],
        commits: dict[str, str],
    ) -> None:
        for repo in plan.repos:  # skills first, so agent repos can pin them
            if repo.handle in commits:
                continue
            files: dict[str, Any] = {}
            for path, value in repo.files.items():
                if isinstance(value, SkillLink) and value.repo_handle in commits:
                    value = SkillLink(value.repo_handle, commits[value.repo_handle])
                files[path] = value
            files.update(deletions_since(prev_bundles.get(repo.handle), repo))
            commits[repo.handle] = self.client.push(
                repo.handle, repo.kind, files, parent_commit=parents.get(repo.handle), description=repo.description
            )
        # Repos that held context last time but hold none now are emptied, not left stale.
        for old in prev_bundles.values():
            if plan.repo(old.handle) is None and old.handle not in commits:
                commits[old.handle] = self.client.push(
                    old.handle, old.kind, {p: None for p in old.files},
                    parent_commit=parents.get(old.handle), description=old.description,
                )

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    async def import_repo(
        self,
        handle: str,
        principal: Principal,
        *,
        kind: str = "agent",
        version: str | None = None,
        read_acl_groups: list[str] | None = None,
    ) -> dict[str, Any]:
        """Pull a Context Hub repo and file its knowledge edits as one proposed change set."""
        self._require_release_manager(principal)
        if kind not in ("agent", "skill"):
            raise ValidationError("kind must be 'agent' or 'skill'")
        pulled = self.client.pull(handle, kind, version)  # type: ignore[arg-type]
        groups = self._repo_groups(pulled.files, kind, read_acl_groups)
        plan = build_import_plan(
            repo_handle=handle,
            kind=kind,  # type: ignore[arg-type]
            commit_hash=pulled.commit_hash,
            files=pulled.files,
            read_acl_groups=groups,
            latest=self.ledger.latest_version,
        )
        result: dict[str, Any] = {"import": plan.summary(), "read_acl_groups": groups, "change_set": None}
        if not plan.items:
            return result
        cs = await self._svc.propose(
            title=f"Import from Context Hub: {handle}@{pulled.commit_hash[:8]}",
            reason=f"Edits made in LangSmith Context Hub repo {handle} (commit {pulled.commit_hash}).",
            items=plan.items,
            principal=principal,
            kind="import",
            source=IMPORT_SOURCE,
            run_id=pulled.commit_hash,
        )
        result["change_set"] = self._svc.view(cs, principal)
        return result

    @staticmethod
    def _repo_groups(files: dict[str, Any], kind: str, requested: list[str] | None) -> list[str]:
        """Read groups come from the lake's own manifest when the repo was exported by the lake."""
        declared: list[str] | None = None
        if kind == "agent" and isinstance(files.get(MANIFEST_PATH), str):
            import yaml

            data = yaml.safe_load(files[MANIFEST_PATH]) or {}
            if isinstance(data, dict) and data.get("wall_side") == "public_side":
                declared = sorted(data.get("read_acl_groups") or [])
        elif kind == "skill" and isinstance(files.get("SKILL.md"), str):
            fm, _ = split_frontmatter(files["SKILL.md"])
            lake = fm.get("lake") if isinstance(fm.get("lake"), dict) else {}
            if lake.get("wall_side") == "public_side" and lake.get("read_acl_groups"):
                declared = sorted(lake["read_acl_groups"])
        req = sorted({g for g in (requested or []) if g}) or None
        if declared and req and declared != req:
            raise ValidationError(f"Repo declares readers {declared}, not {req}")
        groups = declared or req
        if not groups:
            raise ValidationError("This repo was not exported by the lake; pass read_acl_groups for the imported items")
        return groups


__all__ = ["ContextHubService", "parse_hub_commits", "IMPORT_SOURCE", "EXPORTED_RECORD_TYPES"]
