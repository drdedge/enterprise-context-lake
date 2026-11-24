"""Git-like version control for context: versions, change sets, approval rules, redlines, releases.

Pure domain logic only (no storage). See docs/adr/ADR-007-context-version-control.md.

Concepts (git analogue in brackets):
  - VersionRecord   [commit of one file]  one immutable version of one record.
  - ChangeSet       [pull request]        one or more record edits, proposed, approved and merged.
  - ApprovalRule    [branch protection]   decides whether a change set is approved automatically.
  - Release         [tag]                 a numbered cut of the whole lake in system time.
  - Channel         [moving tag]          dev / staging / production pointers to releases.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from context_core.compression import ensure_utc, utc_now
from context_core.models import Claim, Principal, WallSide
from context_core.policy import PolicyEngine, wall_allows

RecordType = Literal["entity", "knowledge_item", "topic", "claim", "memory", "watchlist"]
RECORD_TYPES: tuple[str, ...] = ("entity", "knowledge_item", "topic", "claim", "memory", "watchlist")
ChangeOp = Literal["upsert", "delete", "claim_create", "claim_update", "patch"]
ChangeSource = Literal["ui", "api", "harness", "agent", "upload", "import", "system"]
ChangeStatus = Literal["proposed", "merged", "rejected", "conflict", "failed"]
VersionOp = Literal["baseline", "create", "update", "delete", "import"]
ChannelName = Literal["dev", "staging", "production"]
CHANNEL_NAMES: tuple[str, ...] = ("dev", "staging", "production")

# Fields that change on every write without changing meaning; ignored for no-op detection and diffs.
VOLATILE_FIELDS = frozenset({"updated_at", "alerts"})


def ts(dt: datetime | str | None = None) -> str:
    """Fixed-width UTC timestamp so that string order equals time order in the ledger."""
    value = ensure_utc(dt) if dt is not None else utc_now()
    assert value is not None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def content_hash(payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    stable = {k: v for k, v in payload.items() if k not in VOLATILE_FIELDS}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()


def claim_slot_id(claim: Claim | dict[str, Any]) -> str:
    """A claim's history key: the 'file' that successive claims about the same thing rewrite."""
    c = claim if isinstance(claim, dict) else claim.model_dump(mode="json")
    return "|".join(
        str(c.get(k) or "")
        for k in ("subject_id", "predicate", "context_purpose", "scope_type", "scope_id")
    )


# ---------------------------------------------------------------------------
# Attribution (who/what is writing) carried through async calls
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Attribution:
    source: str = "api"
    agent_id: str | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class ActiveChange:
    """Set while a change set is being merged, so that the store records versions under it."""

    change_id: str


current_attribution: ContextVar[Attribution | None] = ContextVar("vc_attribution", default=None)
active_change: ContextVar[ActiveChange | None] = ContextVar("vc_active_change", default=None)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class VersionRecord(BaseModel):
    record_type: str
    record_id: str
    version_no: int
    change_id: str
    op: VersionOp
    recorded_at: str
    wall_side: str | None = None
    read_acl_groups: list[str] | None = None  # None = no record ACL (topics)
    owner_user_id: str | None = None  # personal records: only the owner may see them
    label: str = ""
    payload: dict[str, Any] | None = None  # None for a deletion tombstone
    content_hash: str | None = None


class ChangeItemInput(BaseModel):
    """One proposed edit. ``payload`` is the full new record (upsert), or a claim input."""

    record_type: RecordType
    op: ChangeOp
    record_id: str | None = None
    base_version: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ChangeItem(ChangeItemInput):
    seq: int = 0
    label: str = ""
    before_version: int | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None  # the record as it will look (preview for redline)
    wall_side_before: str | None = None
    wall_side_after: str | None = None
    read_acl_after: list[str] | None = None
    owner_user_id: str | None = None
    steward_groups: list[str] = Field(default_factory=list)
    rule_id: str | None = None
    applied_version: int | None = None


class Approval(BaseModel):
    approver_id: str
    approver_name: str = ""
    decision: Literal["approve", "reject"]
    comment: str = ""
    at: str = Field(default_factory=ts)
    covers: list[str] = Field(default_factory=list)


class ChangeSet(BaseModel):
    change_id: str
    title: str
    reason: str = ""
    kind: Literal["change", "revert", "import", "direct"] = "change"
    source: str = "api"
    author_id: str
    author_name: str = ""
    author: dict[str, Any] | None = None  # principal snapshot, used to apply the change
    agent_id: str | None = None
    run_id: str | None = None
    status: ChangeStatus = "proposed"
    approval: Literal["auto", "review"] = "review"
    rule_ids: list[str] = Field(default_factory=list)
    approvers: list[str] = Field(default_factory=list)
    min_approvals: int = 0
    allow_self_approval: bool = False
    items: list[ChangeItem] = Field(default_factory=list)
    approvals: list[Approval] = Field(default_factory=list)
    created_at: str = Field(default_factory=ts)
    decided_at: str | None = None
    merged_at: str | None = None
    merged_by: str | None = None
    reverts_change_id: str | None = None
    reverted_by_change_id: str | None = None
    error: str | None = None
    implicit: bool = False  # recorded from a direct write that did not go through review


class Release(BaseModel):
    release_id: int
    cut_at: str
    data_epoch: int = 0
    change_ids: list[str] = Field(default_factory=list)
    notes: str = ""
    created_by: str
    hub_commit: str | None = None  # LangSmith Context Hub commit hash once exported


class ChannelPointer(BaseModel):
    name: str
    release_id: int | None = None
    moved_at: str | None = None
    moved_by: str | None = None


class ChannelMove(BaseModel):
    name: str
    from_release: int | None = None
    to_release: int
    moved_by: str
    moved_at: str = Field(default_factory=ts)
    reason: str = ""


# ---------------------------------------------------------------------------
# Approval rules
# ---------------------------------------------------------------------------


class RuleMatch(BaseModel):
    kinds: list[str] | None = None
    record_types: list[str] | None = None
    ops: list[str] | None = None
    sources: list[str] | None = None
    wall_sides: list[str] | None = None
    epistemic_statuses: list[str] | None = None
    privileged: bool | None = None
    crosses_wall: bool | None = None
    author_is_owner: bool | None = None
    author_is_steward: bool | None = None


class ApprovalRule(BaseModel):
    id: str
    description: str = ""
    match: RuleMatch = Field(default_factory=RuleMatch)
    approval: Literal["auto", "review"] = "review"
    approvers: list[str] = Field(default_factory=lambda: ["steward"])
    min_approvals: int = 1
    allow_self_approval: bool = False

    @field_validator("approvers")
    @classmethod
    def _check_approvers(cls, value: list[str]) -> list[str]:
        for a in value:
            if a != "steward" and not a.startswith("group:"):
                raise ValueError(f"Unknown approver kind: {a!r} (use 'steward' or 'group:<name>')")
        return value


class RuleSet(BaseModel):
    release_manager_groups: list[str] = Field(default_factory=lambda: ["fund-stewards"])
    rules: list[ApprovalRule]


DEFAULT_RULES_PATH = Path(__file__).with_name("approval_rules.yaml")


def load_rules(path: str | Path | None = None) -> RuleSet:
    """Load approval rules from YAML/JSON (env CONTEXT_LAKE_APPROVAL_RULES overrides the default)."""
    p = Path(path or os.environ.get("CONTEXT_LAKE_APPROVAL_RULES") or DEFAULT_RULES_PATH)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    rules = [ApprovalRule.model_validate(r) for r in raw.get("rules", [])]
    if not rules or rules[-1].match.model_dump(exclude_none=True):
        # Fail closed: anything no rule matches needs a steward
        rules.append(ApprovalRule(id="implicit-default-review", approval="review"))
    groups = (raw.get("release_managers") or {}).get("groups") or ["fund-stewards"]
    return RuleSet(release_manager_groups=list(groups), rules=rules)


@dataclass(frozen=True)
class ItemFacts:
    kind: str
    record_type: str
    op: str
    source: str
    wall_side: str | None
    epistemic_status: str | None
    privileged: bool
    crosses_wall: bool
    author_is_owner: bool
    author_is_steward: bool


def _matches(m: RuleMatch, f: ItemFacts) -> bool:
    def _in(allowed: list[str] | None, value: str | None) -> bool:
        return allowed is None or value in allowed

    def _eq(expected: bool | None, value: bool) -> bool:
        return expected is None or expected == value

    return (
        _in(m.kinds, f.kind)
        and _in(m.record_types, f.record_type)
        and _in(m.ops, f.op)
        and _in(m.sources, f.source)
        and _in(m.wall_sides, f.wall_side)
        and _in(m.epistemic_statuses, f.epistemic_status)
        and _eq(m.privileged, f.privileged)
        and _eq(m.crosses_wall, f.crosses_wall)
        and _eq(m.author_is_owner, f.author_is_owner)
        and _eq(m.author_is_steward, f.author_is_steward)
    )


def match_rule(rules: RuleSet, facts: ItemFacts) -> ApprovalRule:
    for rule in rules.rules:
        if _matches(rule.match, facts):
            return rule
    return rules.rules[-1]


@dataclass
class Decision:
    approval: Literal["auto", "review"]
    rule_ids: list[str]
    approvers: list[str]
    min_approvals: int
    allow_self_approval: bool


def combine_decisions(rules: list[ApprovalRule]) -> Decision:
    """A change set needs the strictest outcome of its items."""
    review = [r for r in rules if r.approval == "review"]
    if not review:
        return Decision("auto", [r.id for r in rules], [], 0, False)
    approvers: list[str] = []
    for r in review:
        for a in r.approvers:
            if a not in approvers:
                approvers.append(a)
    return Decision(
        approval="review",
        rule_ids=list(dict.fromkeys(r.id for r in rules)),
        approvers=approvers,
        min_approvals=max(max(r.min_approvals for r in review), 1),
        allow_self_approval=all(r.allow_self_approval for r in review),
    )


def approver_kinds_covered(principal: Principal, cs: ChangeSet) -> list[str]:
    """Which of the change set's approver kinds this principal satisfies (for every item)."""
    covered: list[str] = []
    for kind in cs.approvers:
        if kind == "steward":
            if cs.items and all(principal.groups.intersection(i.steward_groups) for i in cs.items):
                covered.append(kind)
        elif kind.startswith("group:") and kind.split(":", 1)[1] in principal.groups:
            covered.append(kind)
    return covered


def approvals_satisfied(cs: ChangeSet) -> bool:
    approvals = [a for a in cs.approvals if a.decision == "approve"]
    distinct = {a.approver_id for a in approvals}
    if len(distinct) < max(cs.min_approvals, 1):
        return False
    covered = {k for a in approvals for k in a.covers}
    return all(k in covered for k in cs.approvers)


# ---------------------------------------------------------------------------
# Visibility (information barrier applied to every version)
# ---------------------------------------------------------------------------


def can_view_version(policy: PolicyEngine, principal: Principal, v: VersionRecord | None) -> bool:
    if v is None:
        return True
    if principal.revoked:
        return False
    if v.owner_user_id is not None:
        return v.owner_user_id == principal.user_id
    if v.record_type == "claim" and v.payload is not None:
        return policy.can_read_claim(principal, Claim.model_validate(v.payload))
    if v.read_acl_groups is None:
        return v.wall_side is None or wall_allows(principal.wall_side, v.wall_side)
    return policy.can_read_record(principal, wall_side=v.wall_side or "public_side", read_acl_groups=v.read_acl_groups)


def can_view_history(
    policy: PolicyEngine, principal: Principal, v: VersionRecord, current: VersionRecord | None
) -> bool:
    """A past version is shown only if the viewer may read it AND may read the record as it is now.

    So an item reclassified from public side to private side disappears, with all its history, for
    public-side viewers; and a version that was once private stays hidden even after declassification.
    """
    return can_view_version(policy, principal, v) and can_view_version(policy, principal, current)


def sanitize_payload(policy: PolicyEngine, principal: Principal, v: VersionRecord) -> dict[str, Any] | None:
    """Apply the evidence redaction envelope to an old claim version, as for a live read."""
    if v.payload is None:
        return None
    if v.record_type != "claim":
        return dict(v.payload)
    claim = policy.sanitize_claim_for_principal(principal, Claim.model_validate(v.payload))
    return claim.model_dump(mode="json") if claim is not None else None


# ---------------------------------------------------------------------------
# Redline
# ---------------------------------------------------------------------------

# Words keep their trailing whitespace so a redline reads as whole words, not letters and gaps
_TOKEN_RE = re.compile(r"\w+\s*|[^\w\s]\s*|\s+", re.UNICODE)


def redline_text(old: str | None, new: str | None) -> list[dict[str, str]]:
    """Word-level redline: [{op: equal|delete|insert, text}], like tracked changes.

    Deletions come before insertions within each changed run. A single unchanged word sandwiched
    between two changes is folded into the change, so edits read as phrases rather than confetti.
    """
    a = _TOKEN_RE.findall(old or "")
    b = _TOKEN_RE.findall(new or "")
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()

    # Fold tiny equal runs between changes into the surrounding change
    blocks: list[list[Any]] = []
    for idx, (tag, i1, i2, j1, j2) in enumerate(ops):
        is_change = tag != "equal"
        sandwiched = (
            tag == "equal"
            and i2 - i1 <= 1
            and 0 < idx < len(ops) - 1
            and ops[idx - 1][0] != "equal"
            and ops[idx + 1][0] != "equal"
        )
        kind = "change" if is_change or sandwiched else "equal"
        if blocks and blocks[-1][0] == kind:
            blocks[-1][2], blocks[-1][4] = i2, j2
        else:
            blocks.append([kind, i1, i2, j1, j2])

    segments: list[dict[str, str]] = []
    for kind, i1, i2, j1, j2 in blocks:
        if kind == "equal":
            if i2 > i1:
                segments.append({"op": "equal", "text": "".join(a[i1:i2])})
            continue
        if i2 > i1:
            segments.append({"op": "delete", "text": "".join(a[i1:i2])})
        if j2 > j1:
            segments.append({"op": "insert", "text": "".join(b[j1:j2])})
    return segments


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not prefix and k in VOLATILE_FIELDS:
                continue
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict) and v and len(key.split(".")) < 3:
                out.update(_flatten(v, key))
            else:
                out[key] = v
        return out
    return {prefix: value}


# Bookkeeping that changes with every new claim row; shown in history, not in a redline
_DIFF_SKIP = frozenset({"claim_id", "recorded_at", "superseded_at", "superseded_by", "created_by"})


def field_changes(before: dict[str, Any] | None, after: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Field-by-field changes; long text fields also carry a word-level redline."""
    fb = _flatten(before or {})
    fa = _flatten(after or {})
    changes: list[dict[str, Any]] = []
    for key in sorted(set(fb) | set(fa)):
        if key in _DIFF_SKIP:
            continue
        old, new = fb.get(key), fa.get(key)
        if old == new:
            continue
        kind = "added" if key not in fb else "removed" if key not in fa else "changed"
        entry: dict[str, Any] = {"field": key, "kind": kind, "old": old, "new": new}
        if isinstance(old, str) and isinstance(new, str) and (len(old) > 30 or len(new) > 30 or " " in old + new):
            entry["redline"] = redline_text(old, new)
        changes.append(entry)
    return changes


TEXT_FIELD = {
    "knowledge_item": "body_markdown",
    "claim": "object_value",
    "entity": "description",
    "memory": "content",
    "topic": "pref_label",
}


def record_label(record_type: str, payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    for key in ("display_name", "pref_label", "key", "predicate"):
        if payload.get(key):
            if record_type == "claim":
                return f"{payload.get('subject_id')} · {payload.get('predicate')}"
            return str(payload[key])
    return str(payload.get("user_id") or "")


def wall_value(value: Any) -> str | None:
    if value is None:
        return None
    return value.value if isinstance(value, WallSide) else str(value)
