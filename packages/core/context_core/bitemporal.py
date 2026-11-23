"""Bitemporal query predicate, conflict-window overlap detection, and stratified two-channel precedence (ADR-001)."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Sequence

from context_core.compression import ensure_utc, utc_now
from context_core.models import Claim, ConflictSet, EntityIdentifier, Principal

SCOPE_RANK: dict[str, int] = {
    "run": 1,
    "user_x_skill": 2,
    "user": 3,
    "skill": 4,
    "project": 5,
    "firm": 6,
}

FACT_EPISTEMIC_RANK: dict[str, int] = {
    "approved_fact": 1,
    "disputed": 2,
    "project_convention": 3,
    "skill_convention": 3,
    "personal_preference": 4,
    "unverified_observation": 5,
    "candidate_sop": 5,
    "revoked": 99,
}


def is_bitemporal_visible(
    *,
    valid_from: datetime,
    valid_to: datetime | None,
    recorded_at: datetime,
    superseded_at: datetime | None,
    as_of: datetime | None = None,
    recorded_as_of: datetime | None = None,
    include_superseded: bool = False,
) -> bool:
    """True bitemporal visibility predicate (ADR-001 §2, fixing PDF Defect #2).

    Evaluates BOTH business time (`as_of`) AND system time (`recorded_as_of`):
        valid_from <= as_of
        AND (valid_to IS NULL OR valid_to > as_of)
        AND recorded_at <= recorded_as_of
        AND (include_superseded OR superseded_at IS NULL OR superseded_at > recorded_as_of)
    """
    eff_as_of = ensure_utc(as_of) or utc_now()
    eff_rec = ensure_utc(recorded_as_of) or utc_now()

    vf = ensure_utc(valid_from)
    vt = ensure_utc(valid_to)
    rec = ensure_utc(recorded_at)
    sup = ensure_utc(superseded_at)
    assert vf is not None and rec is not None

    if vf > eff_as_of:
        return False
    if vt is not None and vt <= eff_as_of:
        return False
    if rec > eff_rec:
        return False
    if not include_superseded and sup is not None and sup <= eff_rec:
        return False
    return True


def is_claim_visible(
    claim: Claim,
    *,
    as_of: datetime | None = None,
    recorded_as_of: datetime | None = None,
    include_superseded: bool = False,
) -> bool:
    return is_bitemporal_visible(
        valid_from=claim.valid_from,
        valid_to=claim.valid_to,
        recorded_at=claim.recorded_at,
        superseded_at=claim.superseded_at,
        as_of=as_of,
        recorded_as_of=recorded_as_of,
        include_superseded=include_superseded,
    )


def is_identifier_visible(
    ident: EntityIdentifier,
    *,
    as_of: datetime | None = None,
    recorded_as_of: datetime | None = None,
) -> bool:
    return is_bitemporal_visible(
        valid_from=ident.valid_from,
        valid_to=ident.valid_to,
        recorded_at=ident.recorded_at,
        superseded_at=ident.superseded_at,
        as_of=as_of,
        recorded_as_of=recorded_as_of,
        include_superseded=False,
    )


def windows_overlap(
    start_a: datetime,
    end_a: datetime | None,
    start_b: datetime,
    end_b: datetime | None,
) -> bool:
    """Return True iff half-open business-time intervals [start_a, end_a) and [start_b, end_b) overlap."""
    sa = ensure_utc(start_a)
    ea = ensure_utc(end_a)
    sb = ensure_utc(start_b)
    eb = ensure_utc(end_b)
    assert sa is not None and sb is not None
    if ea is not None and ea <= sb:
        return False
    if eb is not None and eb <= sa:
        return False
    return True


def claim_collision_key(claim: Claim) -> tuple[str, str, str, str, str]:
    """Collision tuple `(subject_id, predicate, context_purpose, scope_type, scope_id)` (ADR-001 §2)."""
    return (
        claim.subject_id,
        claim.predicate,
        claim.context_purpose,
        claim.scope_type,
        claim.scope_id,
    )


def claims_conflict(existing: Claim, incoming: Claim) -> bool:
    """Return True iff two active claims genuinely collide (same collision tuple, overlapping window, distinct value).

    Valid difference-of-scope (`context_purpose` or `scope_type`/`scope_id` differs) or non-overlapping
    `[valid_from, valid_to)` intervals NEVER constitute a conflict.
    """
    if existing.claim_id == incoming.claim_id:
        return False
    if existing.channel != "fact" or incoming.channel != "fact":
        return False
    if existing.superseded_at is not None or incoming.superseded_at is not None:
        return False
    if existing.epistemic_status == "revoked" or incoming.epistemic_status == "revoked":
        return False
    if claim_collision_key(existing) != claim_collision_key(incoming):
        return False
    if not windows_overlap(existing.valid_from, existing.valid_to, incoming.valid_from, incoming.valid_to):
        return False
    return existing.object_value != incoming.object_value


def _directive_applies_to_context(
    claim: Claim,
    *,
    principal: Principal,
    skill_id: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
    include_shared: bool = True,
) -> bool:
    st = claim.scope_type
    if st == "firm":
        return include_shared
    if st == "project":
        if not include_shared:
            return False
        if project_id is None:
            return True
        return claim.scope_id == project_id
    if st == "skill":
        if not include_shared:
            return False
        if skill_id is None:
            return True
        return claim.scope_id == skill_id
    if st == "user":
        return claim.scope_id == principal.user_id
    if st == "user_x_skill":
        if not claim.scope_id.startswith(f"{principal.user_id}:"):
            return False
        if skill_id is None:
            return True
        return claim.scope_id == f"{principal.user_id}:{skill_id}"
    if st == "run":
        return run_id is not None and claim.scope_id == run_id
    return False


def resolve_stratified_claims(
    claims: Iterable[Claim],
    *,
    principal: Principal,
    skill_id: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
    include_shared: bool = True,
    include_unverified: bool = False,
    open_conflicts: Sequence[ConflictSet] = (),
) -> tuple[list[Claim], list[Claim], list[ConflictSet]]:
    """Two-channel stratified precedence engine (ADR-001 §3, resolving Contradiction (d)).

    Returns ``(facts, directives, active_conflicts)``:
    - ``facts``: Epistemic-tier-first (`approved_fact > disputed > unverified_observation`),
      NEVER overridden by working preferences or conventions. Preserves all competing claims
      participating in any active ``ConflictSet``.
    - ``directives``: Mandatory directives (`is_mandatory=True`) pinned first, followed by
      narrowest-scope-wins (`run > user_x_skill > user > skill > project > firm`).
    """
    facts: list[Claim] = []
    directives: list[Claim] = []
    visible_claim_ids: set[str] = set()

    for claim in claims:
        if claim.epistemic_status == "revoked":
            continue
        if claim.channel == "fact":
            if not include_unverified and claim.epistemic_status in (
                "unverified_observation",
                "candidate_sop",
            ):
                continue
            if not include_shared and claim.scope_type in ("firm", "project", "skill"):
                continue
            facts.append(claim)
            visible_claim_ids.add(claim.claim_id)
        else:
            if not _directive_applies_to_context(
                claim,
                principal=principal,
                skill_id=skill_id,
                project_id=project_id,
                run_id=run_id,
                include_shared=include_shared,
            ):
                continue
            directives.append(claim)
            visible_claim_ids.add(claim.claim_id)

    # Sort facts: epistemic tier first, then narrowest scope, then latest valid_from & recorded_at, then ID
    facts.sort(
        key=lambda c: (
            FACT_EPISTEMIC_RANK.get(c.epistemic_status, 50),
            SCOPE_RANK.get(c.scope_type, 50),
            -c.valid_from.timestamp(),
            -c.recorded_at.timestamp(),
            c.claim_id,
        )
    )

    # Sort directives: mandatory pinned first, then narrowest scope wins, then latest valid_from & recorded_at
    directives.sort(
        key=lambda c: (
            0 if c.is_mandatory else 1,
            SCOPE_RANK.get(c.scope_type, 50),
            -c.valid_from.timestamp(),
            -c.recorded_at.timestamp(),
            c.claim_id,
        )
    )

    # Retain only open conflicts whose visible claims intersect this bitemporal slice
    active_conflicts: list[ConflictSet] = []
    for cs in open_conflicts:
        if cs.status != "open":
            continue
        matching_ids = [cid for cid in cs.claim_ids if cid in visible_claim_ids]
        if len(matching_ids) >= 2:
            filtered_claims = [c for c in cs.claims if c.claim_id in visible_claim_ids]
            active_conflicts.append(
                cs.model_copy(
                    update={
                        "claim_ids": matching_ids,
                        "claims": filtered_claims,
                    }
                )
            )

    return facts, directives, active_conflicts
