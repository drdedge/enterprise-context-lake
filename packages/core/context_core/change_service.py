"""Change sets: propose, approve, merge, revert; releases and channels; barrier-filtered history.

The information barrier is enforced here, in the application layer, on every version, diff and change
set (SQL Server row-level security is not assumed; see ADR-007).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Callable

from context_core.exceptions import (
    AuthorizationError,
    NotFoundError,
    OptimisticConcurrencyError,
    PendingApprovalError,
    ValidationError,
)
from context_core.models import (
    Claim,
    ClaimCreateInput,
    ClaimUpdateInput,
    Entity,
    KnowledgeItem,
    MemoryUpsertInput,
    Principal,
    Topic,
    TopicPatchInput,
    WatchlistUpdateInput,
)
from context_core.policy import PolicyEngine, wall_allows
from context_core.versioning import (
    CHANNEL_NAMES,
    TEXT_FIELD,
    ActiveChange,
    Approval,
    ChangeItem,
    ChangeItemInput,
    ChangeSet,
    ChannelMove,
    ItemFacts,
    Release,
    RuleSet,
    VersionRecord,
    active_change,
    approver_kinds_covered,
    approvals_satisfied,
    can_view_history,
    can_view_version,
    claim_slot_id,
    combine_decisions,
    current_attribution,
    field_changes,
    load_rules,
    match_rule,
    record_label,
    redline_text,
    sanitize_payload,
    ts,
    wall_value,
)

PRIVILEGED_STATUSES = {"approved_fact"}


def _principal_snapshot(p: Principal) -> dict[str, Any]:
    return p.model_dump(mode="json")


def _principal_from(snapshot: dict[str, Any] | None) -> Principal | None:
    return Principal.model_validate(snapshot) if snapshot else None


class ChangeSetService:
    """Application service behind the /api/v1 change-set, history and release endpoints."""

    def __init__(
        self,
        *,
        get_store: Callable[[], Any],
        ledger: Any,
        policy: PolicyEngine,
        rules: RuleSet | None = None,
    ) -> None:
        self._get_store = get_store
        self.ledger = ledger
        self.policy = policy
        self.rules = rules or load_rules()
        self._merge_lock = asyncio.Lock()

    @property
    def store(self) -> Any:
        return self._get_store()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _steward_groups(self, record_type: str, after: dict[str, Any] | None, before: dict[str, Any] | None) -> list[str]:
        rec = after or before or {}
        if record_type in ("entity", "knowledge_item"):
            groups = set(rec.get("write_acl_groups") or [])
            if before:
                groups |= set(before.get("write_acl_groups") or [])
            return sorted(groups)
        if record_type == "topic":
            return [rec.get("owner_group") or "fund-stewards"]
        if record_type == "claim":
            subject = rec.get("subject_id")
            ent = await self.store.get_entity_raw(subject) if subject and hasattr(self.store, "get_entity_raw") else None
            return list(ent.write_acl_groups) if ent is not None else ["fund-stewards"]
        return []

    def _visible(self, principal: Principal, v: VersionRecord | None, current: VersionRecord | None) -> bool:
        if v is None:
            return can_view_version(self.policy, principal, current)
        return can_view_history(self.policy, principal, v, current)

    def _item_visible(self, principal: Principal, item: ChangeItem) -> bool:
        if principal.revoked:
            return False
        if item.owner_user_id is not None:
            return item.owner_user_id == principal.user_id
        for payload, wall, acl in (
            (item.before, item.wall_side_before, None),
            (item.after, item.wall_side_after, item.read_acl_after),
        ):
            if payload is None:
                continue
            if item.record_type == "claim":
                if not self.policy.can_read_claim(principal, Claim.model_validate(payload)):
                    return False
                continue
            groups = acl if acl is not None and payload is item.after else payload.get("read_acl_groups")
            if wall is not None and not wall_allows(principal.wall_side, wall):
                return False
            if groups is not None and not self.policy.can_read_record(
                principal, wall_side=wall or "public_side", read_acl_groups=groups
            ):
                return False
        if item.record_id:
            current = self.ledger.latest_version(item.record_type, item.record_id)
            if current is not None and not can_view_version(self.policy, principal, current):
                return False
        return True

    def can_view_change_set(self, principal: Principal, cs: ChangeSet) -> bool:
        if principal.revoked:
            return False
        if cs.items:
            return all(self._item_visible(principal, i) for i in cs.items)
        versions = self.ledger.versions_for_change(cs.change_id)
        return all(
            can_view_history(self.policy, principal, v, self.ledger.latest_version(v.record_type, v.record_id))
            for v in versions
        )

    def _redact_change_set(self, principal: Principal, cs: ChangeSet) -> dict[str, Any]:
        """Serialize a change set for a viewer: drop the principal snapshot, sanitize claim evidence."""
        body = cs.model_dump(mode="json", exclude={"author"})
        for item in body["items"]:
            for key in ("before", "after"):
                payload = item.get(key)
                if payload and item["record_type"] == "claim":
                    clean = self.policy.sanitize_claim_for_principal(principal, Claim.model_validate(payload))
                    item[key] = clean.model_dump(mode="json") if clean is not None else None
            item["changes"] = field_changes(item.get("before"), item.get("after"))
            text_field = TEXT_FIELD.get(item["record_type"])
            if text_field:
                item["redline"] = redline_text(
                    (item.get("before") or {}).get(text_field), (item.get("after") or {}).get(text_field)
                )
        body["can_approve"] = False
        return body

    # ------------------------------------------------------------------
    # Preparing items (before image, preview of after image, facts for rules)
    # ------------------------------------------------------------------

    async def _prepare_item(self, seq: int, raw: ChangeItemInput, author: Principal) -> ChangeItem:
        rt, op = raw.record_type, raw.op
        payload = dict(raw.payload)
        record_id = raw.record_id
        before_v: VersionRecord | None = None
        after: dict[str, Any] | None
        owner: str | None = None

        if rt == "claim":
            if op == "claim_create":
                inp = ClaimCreateInput.model_validate(payload)
                subject = inp.subject_id or inp.subject_canonical_key or ""
                ent = await self.store.get_entity_raw(subject) if hasattr(self.store, "get_entity_raw") else None
                if ent is None:
                    raise NotFoundError(f"Subject entity not found: {subject}")
                preview = Claim(
                    claim_id="(new)",
                    subject_id=ent.entity_id,
                    predicate=inp.predicate,
                    object_value=inp.object_value,
                    object_entity_id=inp.object_entity_id,
                    context_purpose=inp.context_purpose,
                    scope_type=inp.scope_type,
                    scope_id=inp.scope_id,
                    channel=inp.channel,
                    epistemic_status=inp.epistemic_status,
                    is_mandatory=inp.is_mandatory,
                    facets=inp.facets,
                    provenance=inp.provenance or Claim.model_fields["provenance"].default_factory(),  # type: ignore[misc]
                    acl=inp.acl or Claim.model_fields["acl"].default_factory(),  # type: ignore[misc]
                )
                record_id = claim_slot_id(preview)
                before_v = self.ledger.latest_version("claim", record_id)
                after = preview.model_dump(mode="json")
            elif op == "claim_update":
                claim_id = payload.pop("claim_id", None) or (record_id if record_id and "|" not in record_id else None)
                if not claim_id:
                    raise ValidationError("claim_update needs payload.claim_id")
                existing = await self.store.get_claim_raw(claim_id) if hasattr(self.store, "get_claim_raw") else None
                if existing is None or not self.policy.can_read_claim(author, existing):
                    raise NotFoundError(f"Claim not found: {claim_id}")
                if existing.superseded_at is not None:
                    raise OptimisticConcurrencyError(f"Claim {claim_id} was already superseded")
                patch = ClaimUpdateInput.model_validate(payload)
                record_id = claim_slot_id(existing)
                before_v = self.ledger.latest_version("claim", record_id)
                upd = {k: v for k, v in patch.model_dump(exclude_none=True).items() if k in {"object_value", "epistemic_status", "valid_from", "valid_to", "is_mandatory", "facets"}}
                after = existing.model_copy(update=upd).model_dump(mode="json")
                payload = {"claim_id": claim_id, **patch.model_dump(mode="json", exclude_none=True)}
                before_payload = existing.model_dump(mode="json")
                before_v = before_v.model_copy(update={"payload": before_payload}) if before_v else None
            else:
                raise ValidationError(f"Unsupported op {op} for claims")
        elif rt == "topic" and op == "patch":
            # Rename, nest, merge or archive: applied through update_topic, which also keeps the
            # parent/child links of neighbouring topics consistent.
            record_id = record_id or payload.get("topic_id")
            payload.pop("topic_id", None)
            if not record_id:
                raise ValidationError("topic patch needs record_id")
            fields = TopicPatchInput.model_validate(payload).model_dump(mode="json", exclude_none=True)
            before_v = self.ledger.latest_version("topic", record_id)
            if before_v is None or before_v.payload is None:
                raise NotFoundError(f"Topic not found: {record_id}")
            after = Topic.model_validate({**before_v.payload, **fields}).model_dump(mode="json")
            payload = {"topic_id": record_id, **fields}
        elif rt in ("entity", "knowledge_item", "topic"):
            model = {"entity": Entity, "knowledge_item": KnowledgeItem, "topic": Topic}[rt]
            key = {"entity": "entity_id", "knowledge_item": "item_id", "topic": "topic_id"}[rt]
            if op != "upsert":
                raise ValidationError(f"{rt} supports op 'upsert' only (archive with status instead of deleting)")
            record_id = record_id or payload.get(key)
            if not record_id:
                raise ValidationError(f"{rt} upsert needs {key}")
            payload[key] = record_id
            before_v = self.ledger.latest_version(rt, record_id)
            merged = {**(before_v.payload if before_v and before_v.payload else {}), **payload}
            after = model.model_validate(merged).model_dump(mode="json")
            payload = after
        elif rt == "memory":
            owner = author.user_id
            if op == "delete":
                if not record_id:
                    raise ValidationError("memory delete needs record_id")
                before_v = self.ledger.latest_version("memory", record_id)
                after = None
            elif op == "upsert":
                inp = MemoryUpsertInput.model_validate({**payload, "memory_id": record_id or payload.get("memory_id")})
                record_id = inp.memory_id or f"mem-{uuid.uuid4().hex[:12]}"
                payload = {**inp.model_dump(mode="json"), "memory_id": record_id}
                before_v = self.ledger.latest_version("memory", record_id)
                after = {**(before_v.payload if before_v and before_v.payload else {}), **payload, "user_id": owner}
            else:
                raise ValidationError(f"Unsupported op {op} for memory")
            if before_v is not None and before_v.owner_user_id not in (None, owner):
                raise NotFoundError(f"Memory not found: {record_id}")
        elif rt == "watchlist":
            if op != "upsert":
                raise ValidationError("watchlist supports op 'upsert' only")
            owner = author.user_id
            record_id = owner
            inp_w = WatchlistUpdateInput.model_validate(payload)
            payload = inp_w.model_dump(mode="json")
            before_v = self.ledger.latest_version("watchlist", owner)
            after = {**payload, "user_id": owner}
        else:  # pragma: no cover - guarded by the RecordType literal
            raise ValidationError(f"Unknown record type {rt}")

        before = before_v.payload if before_v else None
        wall_before = wall_value((before or {}).get("wall_side") or ((before or {}).get("acl") or {}).get("wall_side")) if before else None
        wall_after = wall_value((after or {}).get("wall_side") or ((after or {}).get("acl") or {}).get("wall_side")) if after else None
        read_after: list[str] | None = None
        if after is not None:
            read_after = after.get("read_acl_groups") if rt in ("entity", "knowledge_item") else (
                (after.get("acl") or {}).get("read_acl_groups") if rt == "claim" else None
            )
        if raw.base_version is not None and before_v is not None and raw.base_version != before_v.version_no:
            raise OptimisticConcurrencyError(
                f"{rt} {record_id} is at version {before_v.version_no}, not {raw.base_version}; refresh and retry"
            )

        item = ChangeItem(
            seq=seq,
            record_type=rt,
            op=op,
            record_id=record_id,
            base_version=before_v.version_no if before_v else None,
            payload=payload,
            label=record_label(rt, after or before),
            before_version=before_v.version_no if before_v else None,
            before=before,
            after=after,
            wall_side_before=wall_before,
            wall_side_after=wall_after,
            read_acl_after=read_after,
            owner_user_id=owner,
        )
        item.steward_groups = await self._steward_groups(rt, after, before)
        # The author must be able to see what they change, before and after (no writing across the wall)
        if owner is None and not self._item_visible(author, item):
            raise AuthorizationError("Information barrier: you cannot propose a change to a record you cannot read")
        return item

    def _facts(self, item: ChangeItem, *, kind: str, source: str, author: Principal) -> ItemFacts:
        status = (item.after or {}).get("epistemic_status") if item.record_type == "claim" else None
        privileged = item.record_type == "claim" and (
            item.op == "claim_update"
            or status in PRIVILEGED_STATUSES
            or bool((item.after or {}).get("is_mandatory"))
        )
        return ItemFacts(
            kind=kind,
            record_type=item.record_type,
            op=item.op,
            source=source,
            wall_side=item.wall_side_after or item.wall_side_before,
            epistemic_status=status,
            privileged=privileged,
            crosses_wall=bool(
                item.wall_side_before and item.wall_side_after and item.wall_side_before != item.wall_side_after
            ),
            author_is_owner=item.owner_user_id is not None and item.owner_user_id == author.user_id,
            author_is_steward=bool(author.groups.intersection(item.steward_groups)),
        )

    # ------------------------------------------------------------------
    # Propose / approve / reject / merge
    # ------------------------------------------------------------------

    async def propose(
        self,
        *,
        title: str,
        items: list[ChangeItemInput],
        principal: Principal,
        reason: str = "",
        kind: str = "change",
        source: str | None = None,
        agent_id: str | None = None,
        run_id: str | None = None,
        reverts_change_id: str | None = None,
    ) -> ChangeSet:
        cs = await self._build(
            title=title, items=items, principal=principal, reason=reason, kind=kind, source=source,
            agent_id=agent_id, run_id=run_id, reverts_change_id=reverts_change_id,
        )
        self.ledger.save_change_set(cs)
        if cs.approval == "auto":
            cs = await self._merge(cs, merger=principal)
        return cs

    async def gate_direct_write(self, *, title: str, items: list[ChangeItemInput], principal: Principal) -> None:
        """Check a write from an existing endpoint (REST, MCP, agents) against the approval rules.

        Returns quietly when the rules auto-approve it, and the caller writes straight through as before
        (still recorded in history). Otherwise the edit is filed as a proposed change set and
        PendingApprovalError is raised, which the API returns as 202 with the change set.
        """
        cs = await self._build(title=title, items=items, principal=principal)
        if cs.approval == "auto":
            return
        self.ledger.save_change_set(cs)
        raise PendingApprovalError(
            f"This change needs approval ({', '.join(cs.rule_ids)}). It was filed as change set {cs.change_id}.",
            change_set=self.view(cs, principal),
        )

    async def _build(
        self,
        *,
        title: str,
        items: list[ChangeItemInput],
        principal: Principal,
        reason: str = "",
        kind: str = "change",
        source: str | None = None,
        agent_id: str | None = None,
        run_id: str | None = None,
        reverts_change_id: str | None = None,
    ) -> ChangeSet:
        if principal.revoked:
            raise AuthorizationError("Revoked principal cannot propose changes")
        if not items:
            raise ValidationError("A change set needs at least one item")
        attr = current_attribution.get()
        src = source or (attr.source if attr else "api")
        prepared = [await self._prepare_item(i, raw, principal) for i, raw in enumerate(items)]
        rules = []
        for item in prepared:
            rule = match_rule(self.rules, self._facts(item, kind=kind, source=src, author=principal))
            item.rule_id = rule.id
            rules.append(rule)
        decision = combine_decisions(rules)
        cs = ChangeSet(
            change_id=f"cs-{uuid.uuid4().hex[:12]}",
            title=title.strip() or "Untitled change",
            reason=reason,
            kind=kind,  # type: ignore[arg-type]
            source=src,
            author_id=principal.user_id,
            author_name=principal.display_name,
            author=_principal_snapshot(principal),
            agent_id=agent_id or (attr.agent_id if attr else None),
            run_id=run_id or (attr.run_id if attr else None),
            approval=decision.approval,
            rule_ids=decision.rule_ids,
            approvers=decision.approvers,
            min_approvals=decision.min_approvals,
            allow_self_approval=decision.allow_self_approval,
            items=prepared,
            reverts_change_id=reverts_change_id,
        )
        return cs

    def get(self, change_id: str, principal: Principal) -> ChangeSet:
        cs = self.ledger.get_change_set(change_id)
        if cs is None or not self.can_view_change_set(principal, cs):
            raise NotFoundError(f"Change set not found: {change_id}")
        return cs

    def view(self, cs: ChangeSet, principal: Principal) -> dict[str, Any]:
        body = self._redact_change_set(principal, cs)
        body["can_approve"] = self._can_approve(principal, cs)[0]
        body["approver_kinds_you_cover"] = approver_kinds_covered(principal, cs) if cs.status == "proposed" else []
        return body

    def list_visible(self, principal: Principal, *, statuses: list[str] | None = None, include_implicit: bool = False, limit: int = 100) -> list[ChangeSet]:
        out = []
        for cs in self.ledger.list_change_sets(statuses=statuses, include_implicit=include_implicit, limit=limit * 3):
            if self.can_view_change_set(principal, cs):
                out.append(cs)
            if len(out) >= limit:
                break
        return out

    def _can_approve(self, principal: Principal, cs: ChangeSet) -> tuple[bool, str, list[str]]:
        if cs.status != "proposed":
            return False, f"Change set is {cs.status}", []
        if principal.revoked:
            return False, "Revoked principal", []
        if principal.user_id == cs.author_id and not cs.allow_self_approval:
            return False, "Authors cannot approve their own change set", []
        if any(a.approver_id == principal.user_id and a.decision == "approve" for a in cs.approvals):
            return False, "You have already approved this change set", []
        if not self.can_view_change_set(principal, cs):
            return False, "Not visible", []
        covers = approver_kinds_covered(principal, cs)
        if not covers:
            return False, f"Needs approval from: {', '.join(cs.approvers)}", []
        return True, "", covers

    async def approve(self, change_id: str, principal: Principal, comment: str = "") -> ChangeSet:
        cs = self.get(change_id, principal)
        ok, why, covers = self._can_approve(principal, cs)
        if not ok:
            raise AuthorizationError(why)
        cs.approvals.append(
            Approval(approver_id=principal.user_id, approver_name=principal.display_name, decision="approve", comment=comment, covers=covers)
        )
        self.ledger.save_change_set(cs)
        if approvals_satisfied(cs):
            cs.decided_at = ts()
            cs = await self._merge(cs, merger=principal)
        return cs

    async def reject(self, change_id: str, principal: Principal, comment: str = "") -> ChangeSet:
        cs = self.get(change_id, principal)
        if cs.status != "proposed":
            raise ValidationError(f"Change set is {cs.status}")
        is_author = principal.user_id == cs.author_id
        if not is_author and not approver_kinds_covered(principal, cs):
            raise AuthorizationError(f"Needs one of: {', '.join(cs.approvers)} (or the author, to withdraw)")
        cs.approvals.append(
            Approval(approver_id=principal.user_id, approver_name=principal.display_name, decision="reject", comment=comment or ("Withdrawn" if is_author else ""))
        )
        cs.status = "rejected"
        cs.decided_at = ts()
        self.ledger.save_change_set(cs)
        return cs

    async def _apply_item(self, item: ChangeItem, *, author: Principal, merger: Principal) -> None:
        store = self.store
        rt, op, p = item.record_type, item.op, item.payload
        # Privileged claim edits are applied with the approver's authority; personal records as the owner.
        applier = merger if merger.groups.intersection(item.steward_groups) else author
        if rt == "entity":
            await store.upsert_entity(Entity.model_validate({**p, "updated_at": ts()}))
        elif rt == "knowledge_item":
            await store.upsert_knowledge_item(KnowledgeItem.model_validate({**p, "recorded_at": ts()}))
        elif rt == "topic" and op == "patch":
            body = {k: v for k, v in p.items() if k != "topic_id"}
            await store.update_topic(item.record_id, TopicPatchInput.model_validate(body), principal=applier)
        elif rt == "topic":
            await store.upsert_topic(Topic.model_validate(p))
        elif rt == "claim" and op == "claim_create":
            await store.write_claim(ClaimCreateInput.model_validate(p), principal=applier)
        elif rt == "claim" and op == "claim_update":
            body = dict(p)
            claim_id = body.pop("claim_id")
            body["force_supersede"] = True
            await store.update_claim(claim_id, ClaimUpdateInput.model_validate(body), principal=applier)
        elif rt == "memory" and op == "upsert":
            await store.upsert_memory(MemoryUpsertInput.model_validate(p), principal=author)
        elif rt == "memory" and op == "delete":
            await store.delete_memory(item.record_id, principal=author)
        elif rt == "watchlist":
            await store.put_watchlist(WatchlistUpdateInput.model_validate(p), principal=author)
        else:  # pragma: no cover
            raise ValidationError(f"Cannot apply {rt}/{op}")

    async def _merge(self, cs: ChangeSet, *, merger: Principal) -> ChangeSet:
        author = _principal_from(cs.author) or merger
        async with self._merge_lock:
            # Optimistic concurrency: every record must still be at the version the change was based on
            stale = []
            for item in cs.items:
                if not item.record_id:
                    continue
                latest = self.ledger.latest_version(item.record_type, item.record_id)
                now_no = latest.version_no if latest else None
                if now_no != item.base_version:
                    stale.append(f"{item.record_type} {item.label or item.record_id} (v{item.base_version} -> v{now_no})")
            if stale:
                cs.status = "conflict"
                cs.error = "Changed since proposed: " + "; ".join(stale)
                self.ledger.save_change_set(cs)
                raise OptimisticConcurrencyError(cs.error)
            token = active_change.set(ActiveChange(cs.change_id))
            try:
                for item in cs.items:
                    await self._apply_item(item, author=author, merger=merger)
                    if item.record_id:
                        latest = self.ledger.latest_version(item.record_type, item.record_id)
                        item.applied_version = latest.version_no if latest and latest.change_id == cs.change_id else None
            except Exception as exc:
                # Items applied before the failure stay applied and are visible in history under this
                # change set (prototype: not one transaction across stores; see ADR-007).
                cs.status = "failed"
                cs.error = f"{type(exc).__name__}: {exc}"
                self.ledger.save_change_set(cs)
                raise
            finally:
                active_change.reset(token)
            cs.status = "merged"
            cs.merged_at = ts()
            cs.merged_by = merger.user_id
            cs.decided_at = cs.decided_at or cs.merged_at
            self.ledger.save_change_set(cs)
            if cs.reverts_change_id:
                original = self.ledger.get_change_set(cs.reverts_change_id)
                if original is not None:
                    original.reverted_by_change_id = cs.change_id
                    self.ledger.save_change_set(original)
        return cs

    # ------------------------------------------------------------------
    # Revert
    # ------------------------------------------------------------------

    def _inverse_item(self, v: VersionRecord) -> ChangeItemInput:
        latest = self.ledger.latest_version(v.record_type, v.record_id)
        if latest is None or latest.version_no != v.version_no:
            raise OptimisticConcurrencyError(
                f"{v.record_type} {v.label or v.record_id} has changed since (now v{latest.version_no if latest else 0}); "
                "revert the later change first or edit it directly"
            )
        prev = self.ledger.get_version(v.record_type, v.record_id, v.version_no - 1) if v.version_no > 1 else None
        rt = v.record_type
        if rt in ("entity", "topic", "knowledge_item"):
            if prev is None or prev.payload is None:
                if rt == "knowledge_item":
                    raise ValidationError("Reverting the creation of a note is not supported yet; archive it instead")
                return ChangeItemInput(record_type=rt, op="upsert", record_id=v.record_id, payload={**(v.payload or {}), "status": "archived"})  # type: ignore[arg-type]
            return ChangeItemInput(record_type=rt, op="upsert", record_id=v.record_id, payload=dict(prev.payload))  # type: ignore[arg-type]
        if rt == "claim":
            current = Claim.model_validate(v.payload)
            if prev is None or prev.payload is None:
                return ChangeItemInput(record_type="claim", op="claim_update", payload={"claim_id": current.claim_id, "epistemic_status": "revoked", "rationale": "revert"})
            old = prev.payload
            return ChangeItemInput(
                record_type="claim",
                op="claim_update",
                payload={
                    "claim_id": current.claim_id,
                    "object_value": old.get("object_value"),
                    "epistemic_status": old.get("epistemic_status"),
                    "valid_from": old.get("valid_from"),
                    "valid_to": old.get("valid_to"),
                    "is_mandatory": old.get("is_mandatory"),
                    "facets": old.get("facets"),
                    "rationale": "revert",
                },
            )
        if rt == "memory":
            if v.payload is None:  # revert a deletion
                return ChangeItemInput(record_type="memory", op="upsert", record_id=v.record_id, payload=dict(prev.payload or {}) if prev else {})
            if prev is None or prev.payload is None:
                return ChangeItemInput(record_type="memory", op="delete", record_id=v.record_id)
            return ChangeItemInput(record_type="memory", op="upsert", record_id=v.record_id, payload=dict(prev.payload))
        if rt == "watchlist":
            payload = dict(prev.payload) if prev and prev.payload else {"entity_ids": [], "topic_ids": [], "source_ids": []}
            return ChangeItemInput(record_type="watchlist", op="upsert", record_id=v.record_id, payload=payload)
        raise ValidationError(f"Cannot revert {rt}")

    async def revert(self, change_id: str, principal: Principal, reason: str = "") -> ChangeSet:
        original = self.get(change_id, principal)
        if original.status != "merged":
            raise ValidationError("Only merged change sets can be reverted")
        if original.reverted_by_change_id:
            raise ValidationError(f"Already reverted by {original.reverted_by_change_id}")
        versions = self.ledger.versions_for_change(change_id)
        if not versions:
            raise ValidationError("This change set recorded no versions to revert")
        latest_per_record: dict[tuple[str, str], VersionRecord] = {}
        for v in versions:
            latest_per_record[(v.record_type, v.record_id)] = v
        items = [self._inverse_item(v) for v in latest_per_record.values()]
        return await self.propose(
            title=f"Revert: {original.title}",
            reason=reason or f"Revert {change_id}",
            items=items,
            principal=principal,
            kind="revert",
            reverts_change_id=change_id,
        )

    # ------------------------------------------------------------------
    # History and diffs
    # ------------------------------------------------------------------

    def _visible_versions(self, record_type: str, record_id: str, principal: Principal) -> list[VersionRecord]:
        versions = self.ledger.list_versions(record_type, record_id)
        if not versions:
            raise NotFoundError(f"No history for {record_type} {record_id}")
        current = versions[-1]
        visible = [v for v in versions if self._visible(principal, v, current)]
        if not visible:
            raise NotFoundError(f"No history for {record_type} {record_id}")
        return visible

    def history(self, record_type: str, record_id: str, principal: Principal) -> dict[str, Any]:
        visible = self._visible_versions(record_type, record_id, principal)
        releases = self.ledger.list_releases()
        channels = {c.name: c.release_id for c in self.ledger.get_channels()}
        in_release: dict[int, list[int]] = {}
        for rel in releases:
            v = self.ledger.version_at(record_type, record_id, rel.cut_at)
            if v is not None:
                in_release.setdefault(v.version_no, []).append(rel.release_id)
        change_cache: dict[str, ChangeSet | None] = {}

        def _cs(cid: str) -> ChangeSet | None:
            if cid not in change_cache:
                change_cache[cid] = self.ledger.get_change_set(cid)
            return change_cache[cid]

        latest_no = self.ledger.latest_version(record_type, record_id).version_no  # type: ignore[union-attr]
        out_versions = []
        for v in reversed(visible):
            cs = _cs(v.change_id)
            rels = in_release.get(v.version_no, [])
            out_versions.append(
                {
                    "version_no": v.version_no,
                    "op": v.op,
                    "recorded_at": v.recorded_at,
                    "label": v.label,
                    "change_id": v.change_id,
                    "title": cs.title if cs else "",
                    "reason": cs.reason if cs else "",
                    "author_id": cs.author_id if cs else "system",
                    "author_name": cs.author_name if cs else "",
                    "source": cs.source if cs else "system",
                    "agent_id": cs.agent_id if cs else None,
                    "run_id": cs.run_id if cs else None,
                    "kind": cs.kind if cs else "direct",
                    "approvals": [a.model_dump() for a in cs.approvals] if cs else [],
                    "reverted_by_change_id": cs.reverted_by_change_id if cs else None,
                    "reverts_change_id": cs.reverts_change_id if cs else None,
                    "releases": rels,
                    "channels": [n for n, rid in channels.items() if rid in rels],
                    "is_head": v.version_no == latest_no,
                }
            )
        proposals = []
        for cs in self.ledger.change_sets_for_record(record_type, record_id):
            if cs.status not in ("proposed", "rejected", "conflict") or cs.implicit:
                continue
            if not self.can_view_change_set(principal, cs):
                continue
            base = next((i.base_version for i in cs.items if i.record_id == record_id), None)
            proposals.append(
                {
                    "change_id": cs.change_id,
                    "title": cs.title,
                    "status": cs.status,
                    "base_version": base,
                    "author_name": cs.author_name,
                    "source": cs.source,
                    "created_at": cs.created_at,
                    "approvers": cs.approvers,
                    "approvals": [a.model_dump() for a in cs.approvals],
                }
            )
        return {
            "record_type": record_type,
            "record_id": record_id,
            "label": visible[-1].label,
            "head_version": visible[-1].version_no if visible[-1].version_no == latest_no else None,
            "versions": out_versions,
            "proposals": proposals,
            "channels": channels,
        }

    def get_version(self, record_type: str, record_id: str, version_no: int, principal: Principal) -> dict[str, Any]:
        current = self.ledger.latest_version(record_type, record_id)
        v = self.ledger.get_version(record_type, record_id, version_no)
        if v is None or not self._visible(principal, v, current):
            raise NotFoundError(f"Version not found: {record_type} {record_id} v{version_no}")
        body = v.model_dump(mode="json", exclude={"read_acl_groups", "owner_user_id", "content_hash"})
        body["payload"] = sanitize_payload(self.policy, principal, v)
        return body

    def diff(self, record_type: str, record_id: str, from_no: int | None, to_no: int | None, principal: Principal) -> dict[str, Any]:
        current = self.ledger.latest_version(record_type, record_id)
        if current is None:
            raise NotFoundError(f"No history for {record_type} {record_id}")
        to_no = to_no or current.version_no
        if from_no is None:
            from_no = to_no - 1
        b = self.ledger.get_version(record_type, record_id, from_no) if from_no >= 1 else None
        a = self.ledger.get_version(record_type, record_id, to_no)
        if a is None or not self._visible(principal, a, current):
            raise NotFoundError(f"Version not found: v{to_no}")
        restricted = b is not None and not self._visible(principal, b, current)
        before = None if (b is None or restricted) else sanitize_payload(self.policy, principal, b)
        after = sanitize_payload(self.policy, principal, a)
        text_field = TEXT_FIELD.get(record_type)
        return {
            "record_type": record_type,
            "record_id": record_id,
            "from_version": from_no if b is not None else None,
            "to_version": to_no,
            "restricted": restricted,
            "message": "Changed, details restricted" if restricted else None,
            "changes": [] if restricted else field_changes(before, after),
            "redline": [] if restricted or not text_field else redline_text((before or {}).get(text_field), (after or {}).get(text_field)),
            "text_field": text_field,
        }

    def subject_claim_slots(self, subject_id: str, principal: Principal) -> list[dict[str, Any]]:
        """Claim histories about one entity (each slot is one evolving piece of context)."""
        out = []
        for v in self.ledger.latest_versions_with_prefix("claim", f"{subject_id}|"):
            if can_view_version(self.policy, principal, v):
                out.append({"record_id": v.record_id, "label": v.label, "version_no": v.version_no, "recorded_at": v.recorded_at})
        return sorted(out, key=lambda s: s["label"])

    # ------------------------------------------------------------------
    # Releases and channels
    # ------------------------------------------------------------------

    def _require_release_manager(self, principal: Principal) -> None:
        if principal.revoked or not principal.groups.intersection(self.rules.release_manager_groups):
            raise AuthorizationError(
                "Only release managers can cut releases or move channels: " + ", ".join(self.rules.release_manager_groups)
            )

    async def cut_release(self, principal: Principal, notes: str = "") -> Release:
        self._require_release_manager(principal)
        async with self._merge_lock:
            epochs = await self.store.get_epochs()
            previous = self.ledger.list_releases()
            cut_at = ts()
            change_ids = self.ledger.merged_change_ids_between(previous[-1].cut_at if previous else None, cut_at)
            release = self.ledger.create_release(
                Release(release_id=0, cut_at=cut_at, data_epoch=epochs.data_epoch, change_ids=change_ids, notes=notes, created_by=principal.user_id)
            )
            self._move(principal, "dev", release.release_id, "New release")
        return release

    def _move(self, principal: Principal, name: str, release_id: int, reason: str) -> None:
        current = {c.name: c.release_id for c in self.ledger.get_channels()}
        self.ledger.move_channel(
            ChannelMove(name=name, from_release=current.get(name), to_release=release_id, moved_by=principal.user_id, reason=reason)
        )

    def move_channel(self, name: str, release_id: int, principal: Principal, reason: str = "") -> dict[str, Any]:
        self._require_release_manager(principal)
        if name not in CHANNEL_NAMES:
            raise ValidationError(f"Unknown channel {name}; use one of {', '.join(CHANNEL_NAMES)}")
        if self.ledger.get_release(release_id) is None:
            raise NotFoundError(f"Release not found: {release_id}")
        self._move(principal, name, release_id, reason)
        return self.channels()

    def channels(self) -> dict[str, Any]:
        rels = {r.release_id: r for r in self.ledger.list_releases()}
        return {
            "channels": [
                {**c.model_dump(), "cut_at": rels[c.release_id].cut_at if c.release_id in rels else None}
                for c in self.ledger.get_channels()
            ],
            "moves": [m.model_dump() for m in self.ledger.channel_moves()][-50:],
        }

    def releases(self, principal: Principal) -> list[dict[str, Any]]:
        channels = self.ledger.get_channels()
        out = []
        for r in reversed(self.ledger.list_releases()):
            body = r.model_dump()
            body["channels"] = [c.name for c in channels if c.release_id == r.release_id]
            # Only count changes the viewer may see, so release notes do not leak private-side activity
            visible = []
            for cid in r.change_ids:
                cs = self.ledger.get_change_set(cid)
                if cs is not None and self.can_view_change_set(principal, cs):
                    visible.append({"change_id": cid, "title": cs.title, "author_name": cs.author_name, "source": cs.source})
            body["change_ids"] = [c["change_id"] for c in visible]
            body["changes"] = visible
            out.append(body)
        return out

    def resolve_as_of(self, *, channel: str | None = None, release: int | None = None) -> str | None:
        """System-time cut to read at: a release's cut time. ``dev`` with no release means 'now'."""
        if release is not None:
            rel = self.ledger.get_release(release)
            if rel is None:
                raise NotFoundError(f"Release not found: {release}")
            return rel.cut_at
        if channel:
            if channel not in CHANNEL_NAMES:
                raise ValidationError(f"Unknown channel {channel}")
            ptr = next((c for c in self.ledger.get_channels() if c.name == channel), None)
            if ptr is None or ptr.release_id is None:
                if channel == "dev":
                    return None
                raise NotFoundError(f"Channel {channel} has no release yet")
            rel = self.ledger.get_release(ptr.release_id)
            return rel.cut_at if rel else None
        return None

    def record_at(self, record_type: str, record_id: str, principal: Principal, *, channel: str | None = None, release: int | None = None) -> dict[str, Any]:
        at = self.resolve_as_of(channel=channel, release=release)
        current = self.ledger.latest_version(record_type, record_id)
        v = current if at is None else self.ledger.version_at(record_type, record_id, at)
        if v is None or v.payload is None or not self._visible(principal, v, current):
            raise NotFoundError(f"{record_type} {record_id} not found at that version")
        body = v.model_dump(mode="json", exclude={"read_acl_groups", "owner_user_id", "content_hash"})
        body["payload"] = sanitize_payload(self.policy, principal, v)
        body["as_of"] = at
        return body
