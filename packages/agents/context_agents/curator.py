"""Background Knowledge Factory Curator: zero-LLM SHA-256 staleness pre-pass & wall-isolated draft patches."""

from __future__ import annotations

import difflib
import uuid
from pathlib import Path
from typing import Any

from context_core.compression import lf_sha256_file, utc_now
from context_core.exceptions import AuthorizationError
from context_core.models import (
    CuratorDraftPatch,
    Principal,
    StalenessItem,
    StalenessReport,
    WallSide,
)
from context_core.policy import wall_allows
from context_core.ports import PolicyPort, StalenessCheckable, StorePort


def _is_steward(principal: Principal) -> bool:
    if principal.revoked:
        return False
    return bool(
        principal.groups.intersection({"fund-stewards", "compliance-oversight"})
        or principal.roles.intersection({"fund_steward", "compliance_reviewer", "managing_director"})
    )


def _build_unified_diff(target_label: str, before_lines: list[str], after_lines: list[str]) -> str:
    diff = difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile=f"a/{target_label}",
        tofile=f"b/{target_label}",
        lineterm="",
    )
    return "\n".join(diff)


class BackgroundCurator:
    """Two-stage Knowledge Factory Curator (ADR-003 §2, ADR-004 §2).

    Stage 1 (Zero-LLM SHA-256 Staleness Pre-Pass):
      - Compares expected LF-normalized/binary SHA-256 hashes on stored Claims and KnowledgeItems
        against source files on disk (and OKF `.sha256.json` sidecars when StorePort supports it).
    Stage 2 (Wall-Isolated Draft Patch Synthesis):
      - Emits reviewable `CuratorDraftPatch` diffs (`status="pending_steward_review"`) for stale
        evidence and quarantined `unverified_observation` / `flag_knowledge_gap` items.
      - Strictly partitioned by `wall_side`: `private_side` items NEVER cross into `public_side` patches.
      - NEVER auto-mutates any live `approved_fact` claim!
    """

    def __init__(
        self,
        *,
        store: StorePort,
        policy: PolicyPort,
        repo_root: Path | None = None,
    ) -> None:
        self._store = store
        self._policy = policy
        self._repo_root = repo_root or Path(__file__).resolve().parents[3]

    async def run_staleness_prepass(
        self,
        *,
        principal: Principal,
        wall_side: WallSide | str | None = None,
    ) -> StalenessReport:
        """Stage 1: Pure deterministic zero-LLM SHA-256 staleness scan partitioned by wall_side."""
        target_ws: WallSide | None = None
        if wall_side is not None:
            target_ws = wall_side if isinstance(wall_side, WallSide) else WallSide(str(wall_side))
            if not wall_allows(principal.wall_side, target_ws):
                raise AuthorizationError("Cannot run curator scan across an unauthorized wall_side")

        checked = 0
        stale_items: list[StalenessItem] = []
        seen_keys: set[tuple[str | None, str | None, str]] = set()

        # Delegate to StorePort.diff_staleness when available (e.g., OkfMarkdownVaultStore sidecars)
        if isinstance(self._store, StalenessCheckable):
            store_rep = await self._store.diff_staleness(
                wall_side=target_ws.value if target_ws else None
            )
            checked += store_rep.checked_count
            for item in store_rep.stale_items:
                if not wall_allows(principal.wall_side, item.wall_side):
                    continue
                if target_ws is not None and item.wall_side != target_ws:
                    continue
                key = (item.claim_id, item.item_id, item.evidence_path)
                if key not in seen_keys:
                    seen_keys.add(key)
                    stale_items.append(item)

        # Verify Claims carrying local evidence_uri + expected content_sha256
        claims = await self._store.query_claims(principal=principal)
        for clm in claims:
            if target_ws is not None and clm.acl.wall_side != target_ws:
                continue
            ev_uri = clm.provenance.evidence_uri
            expected_sha = clm.provenance.content_sha256
            if not ev_uri or not expected_sha or "://" in ev_uri:
                continue
            path = Path(ev_uri)
            if not path.is_absolute():
                path = self._repo_root / ev_uri
            checked += 1
            key = (clm.claim_id, None, ev_uri)
            if not path.is_file():
                if key not in seen_keys:
                    seen_keys.add(key)
                    stale_items.append(
                        StalenessItem(
                            claim_id=clm.claim_id,
                            evidence_path=ev_uri,
                            expected_sha256=expected_sha,
                            actual_sha256=None,
                            reason="missing_source",
                            wall_side=clm.acl.wall_side,
                        )
                    )
                continue
            actual_sha = lf_sha256_file(path)
            if actual_sha != expected_sha and key not in seen_keys:
                seen_keys.add(key)
                stale_items.append(
                    StalenessItem(
                        claim_id=clm.claim_id,
                        evidence_path=ev_uri,
                        expected_sha256=expected_sha,
                        actual_sha256=actual_sha,
                        reason="hash_mismatch",
                        wall_side=clm.acl.wall_side,
                    )
                )

        # Verify KnowledgeItems with local source_path + content_sha256
        kitems = await self._store.list_knowledge_items(principal=principal)
        for ki in kitems:
            if target_ws is not None and ki.wall_side != target_ws:
                continue
            if not ki.source_path or not ki.content_sha256 or "://" in ki.source_path:
                continue
            path = Path(ki.source_path)
            if not path.is_absolute():
                path = self._repo_root / ki.source_path
            checked += 1
            key = (None, ki.item_id, ki.source_path)
            if not path.is_file():
                if key not in seen_keys:
                    seen_keys.add(key)
                    stale_items.append(
                        StalenessItem(
                            item_id=ki.item_id,
                            evidence_path=ki.source_path,
                            expected_sha256=ki.content_sha256,
                            actual_sha256=None,
                            reason="missing_source",
                            wall_side=ki.wall_side,
                        )
                    )
                continue
            actual_sha = lf_sha256_file(path)
            if actual_sha != ki.content_sha256 and key not in seen_keys:
                seen_keys.add(key)
                stale_items.append(
                    StalenessItem(
                        item_id=ki.item_id,
                        evidence_path=ki.source_path,
                        expected_sha256=ki.content_sha256,
                        actual_sha256=actual_sha,
                        reason="hash_mismatch",
                        wall_side=ki.wall_side,
                    )
                )

        return StalenessReport(
            checked_count=checked,
            stale_items=stale_items,
            wall_side=target_ws,
            checked_at=utc_now(),
        )

    async def run(
        self,
        *,
        principal: Principal,
        wall_side: WallSide | str | None = None,
    ) -> tuple[StalenessReport, list[CuratorDraftPatch]]:
        """Execute Stage 1 (zero-LLM SHA pre-pass) + Stage 2 (wall-isolated draft patch synthesis)."""
        if not _is_steward(principal):
            raise AuthorizationError("Steward authorization required to run background curator")

        target_ws: WallSide | None = None
        if wall_side is not None:
            target_ws = wall_side if isinstance(wall_side, WallSide) else WallSide(str(wall_side))

        staleness = await self.run_staleness_prepass(principal=principal, wall_side=target_ws)
        existing_patches = await self._store.list_curator_patches(principal=principal, status="all")
        existing_sigs = {
            (p.target_id, p.wall_side.value, tuple(sorted(p.source_claim_ids)), tuple(sorted(p.stale_evidence_paths)))
            for p in existing_patches
            if p.status == "pending_steward_review"
        }

        created: list[CuratorDraftPatch] = []

        # 1. Draft patches from SHA-256 staleness findings (strictly partitioned per wall_side)
        for stale in staleness.stale_items:
            tid = stale.claim_id or stale.item_id or stale.evidence_path
            sig = (tid, stale.wall_side.value, (), (stale.evidence_path,))
            if sig in existing_sigs:
                continue
            before = [
                f"target: {tid}",
                f"evidence_path: {stale.evidence_path}",
                f"content_sha256: {stale.expected_sha256}",
                "status: verified",
            ]
            after = [
                f"target: {tid}",
                f"evidence_path: {stale.evidence_path}",
                f"content_sha256: {stale.actual_sha256 or 'MISSING'}",
                f"status: needs_reverification ({stale.reason})",
            ]
            patch = CuratorDraftPatch(
                patch_id=f"patch-{uuid.uuid4().hex[:12]}",
                wall_side=stale.wall_side,
                read_acl_groups=(
                    ["firm-all"]
                    if stale.wall_side == WallSide.public_side
                    else ["private-credit-deal-team", "compliance-oversight"]
                ),
                target_type="claim" if stale.claim_id else "knowledge_item",
                target_id=tid,
                title=f"Re-verify stale evidence ({stale.reason}): {stale.evidence_path}",
                rationale=(
                    f"Zero-LLM SHA-256 pre-pass detected {stale.reason} on {stale.evidence_path} "
                    f"(expected {stale.expected_sha256[:12]}, actual {(stale.actual_sha256 or 'none')[:12]})."
                ),
                unified_diff=_build_unified_diff(stale.evidence_path, before, after),
                proposed_changes={
                    "reason": stale.reason,
                    "expected_sha256": stale.expected_sha256,
                    "actual_sha256": stale.actual_sha256,
                },
                source_claim_ids=[stale.claim_id] if stale.claim_id else [],
                stale_evidence_paths=[stale.evidence_path],
                status="pending_steward_review",
                created_by="agent:curator",
            )
            await self._store.save_curator_patch(patch)
            existing_sigs.add(sig)
            created.append(patch)

        # 2. Draft patches from quarantined unverified_observation / flag_knowledge_gap items
        # Strictly partition by (wall_side, subject_id) so private_side NEVER mixes with public_side!
        observations = await self._store.query_claims(
            principal=principal,
            epistemic_statuses=["unverified_observation", "candidate_sop"],
        )
        grouped: dict[tuple[WallSide, str], list[Any]] = {}
        for obs in observations:
            if target_ws is not None and obs.acl.wall_side != target_ws:
                continue
            grouped.setdefault((obs.acl.wall_side, obs.subject_id), []).append(obs)

        for (ws, subject_id), obs_list in sorted(grouped.items(), key=lambda kv: (kv[0][0].value, kv[0][1])):
            claim_ids = sorted(o.claim_id for o in obs_list)
            sig = (subject_id, ws.value, tuple(claim_ids), ())
            if sig in existing_sigs:
                continue

            entity = await self._store.get_entity(subject_id, principal=principal)
            label = entity.canonical_key if entity else subject_id
            active_facts = await self._store.query_claims(
                principal=principal,
                subject_ids=[subject_id],
                epistemic_statuses=["approved_fact"],
            )
            before_lines = [f"{f.predicate}: {f.object_value} [{f.claim_id}]" for f in active_facts[:10]] or [
                "# No active approved_fact for proposed predicate"
            ]
            after_lines = list(before_lines)
            for o in obs_list:
                after_lines.append(
                    f"+ proposed ({o.kind or 'observation'}): {o.predicate} = {o.object_value} [{o.claim_id}]"
                )

            read_groups = list(entity.read_acl_groups) if entity else ["firm-all"]
            patch = CuratorDraftPatch(
                patch_id=f"patch-{uuid.uuid4().hex[:12]}",
                wall_side=ws,
                read_acl_groups=read_groups,
                target_type="entity",
                target_id=subject_id,
                title=f"Review {len(obs_list)} quarantined observation(s) for {label}",
                rationale=(
                    f"Synthesized from {len(obs_list)} quarantined observation/gap report(s) on "
                    f"wall `{ws.value}` without modifying any live approved_fact."
                ),
                unified_diff=_build_unified_diff(f"{label}.okf.md", before_lines, after_lines),
                proposed_changes={
                    "subject_id": subject_id,
                    "subject_canonical_key": label,
                    "observations": [
                        {
                            "claim_id": o.claim_id,
                            "kind": o.kind or "observation",
                            "predicate": o.predicate,
                            "object_value": o.object_value,
                            "source_system": o.provenance.source_system,
                        }
                        for o in obs_list
                    ],
                },
                source_claim_ids=claim_ids,
                stale_evidence_paths=[],
                status="pending_steward_review",
                created_by="agent:curator",
            )
            await self._store.save_curator_patch(patch)
            existing_sigs.add(sig)
            created.append(patch)

        return staleness, created
