"""StorePort decorator that records a version for every write (ADR-007).

Wraps any StorePort (SQLite, Markdown vault, later SQL Server) and appends the record's new state to
the version ledger after each successful write. Writes made while a change set is merging are filed
under that change set; any other write (the existing direct REST/MCP/agent paths) gets its own
implicit, already-merged change set so it still shows in history with its author and source.

Every other attribute passes straight through to the wrapped store.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any

from context_core.models import (
    Claim,
    ClaimCreateInput,
    ClaimUpdateInput,
    ClaimWriteResult,
    ConflictResolutionInput,
    ConflictSet,
    Entity,
    KnowledgeItem,
    MemoryRecord,
    MemoryUpsertInput,
    Principal,
    Topic,
    TopicPatchInput,
    WatchlistRecord,
    WatchlistUpdateInput,
)
from context_core.versioning import (
    ChangeItem,
    ChangeSet,
    VersionRecord,
    active_change,
    claim_slot_id,
    content_hash,
    current_attribution,
    record_label,
    ts,
    wall_value,
)

from context_adapters.version_ledger import SqliteVersionLedger

# The authenticated caller of the current request, set by the API's principal dependency so that
# writes made without a principal argument (topics, entities) are still attributed.
current_actor: ContextVar[Principal | None] = ContextVar("vc_current_actor", default=None)


def new_change_id() -> str:
    return f"cs-{uuid.uuid4().hex[:12]}"


class VersionedStore:
    """Transparent StorePort wrapper that keeps a version history in a ledger."""

    def __init__(self, inner: Any, ledger: SqliteVersionLedger) -> None:
        self._inner = inner
        self._ledger = ledger

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def inner(self) -> Any:
        return self._inner

    @property
    def ledger(self) -> SqliteVersionLedger:
        return self._ledger

    # ------------------------------------------------------------------
    # Recording helpers
    # ------------------------------------------------------------------

    def _change_id_for(
        self, *, title: str, author: Principal | None, record_type: str, record_id: str, op: str, label: str
    ) -> str:
        active = active_change.get()
        if active is not None:
            return active.change_id
        actor = author or current_actor.get()
        attr = current_attribution.get()
        now = ts()
        cs = ChangeSet(
            change_id=new_change_id(),
            title=title,
            kind="direct",
            source=attr.source if attr else "system",
            agent_id=attr.agent_id if attr else None,
            run_id=attr.run_id if attr else None,
            author_id=actor.user_id if actor else "system",
            author_name=actor.display_name if actor else "System",
            status="merged",
            approval="auto",
            rule_ids=["direct-write"],
            implicit=True,
            created_at=now,
            decided_at=now,
            merged_at=now,
            merged_by=actor.user_id if actor else "system",
            items=[ChangeItem(record_type=record_type, op="upsert" if op != "delete" else "delete", record_id=record_id, label=label)],  # type: ignore[arg-type]
        )
        self._ledger.save_change_set(cs)
        return cs.change_id

    def _record(
        self,
        record_type: str,
        record_id: str,
        payload: dict[str, Any] | None,
        *,
        wall_side: str | None,
        read_acl: list[str] | None,
        owner: str | None = None,
        op: str | None = None,
        author: Principal | None = None,
        title: str,
    ) -> VersionRecord | None:
        latest = self._ledger.latest_version(record_type, record_id)
        digest = content_hash(payload)
        if payload is not None and latest is not None and latest.content_hash == digest:
            return latest  # no-op write
        if payload is None and (latest is None or latest.payload is None):
            return latest
        vop = op or ("create" if latest is None or latest.payload is None else "update")
        label = record_label(record_type, payload) or (latest.label if latest else record_id)
        change_id = self._change_id_for(
            title=title, author=author, record_type=record_type, record_id=record_id, op=vop, label=label
        )
        return self._ledger.append_version(
            VersionRecord(
                record_type=record_type,
                record_id=record_id,
                version_no=0,
                change_id=change_id,
                op=vop,  # type: ignore[arg-type]
                recorded_at=ts(),
                wall_side=wall_side,
                read_acl_groups=read_acl,
                owner_user_id=owner,
                label=label,
                payload=payload,
                content_hash=digest,
            )
        )

    def _record_entity(self, e: Entity, *, author: Principal | None = None, op: str | None = None) -> None:
        self._record(
            "entity", e.entity_id, e.model_dump(mode="json"), wall_side=wall_value(e.wall_side),
            read_acl=list(e.read_acl_groups), author=author, op=op, title=f"Update entity {e.display_name}",
        )

    def _record_item(self, k: KnowledgeItem, *, author: Principal | None = None, op: str | None = None) -> None:
        self._record(
            "knowledge_item", k.item_id, k.model_dump(mode="json"), wall_side=wall_value(k.wall_side),
            read_acl=list(k.read_acl_groups), author=author, op=op, title=f"Update note {k.display_name}",
        )

    def _record_topic(self, t: Topic, *, author: Principal | None = None, op: str | None = None) -> None:
        self._record(
            "topic", t.topic_id, t.model_dump(mode="json"), wall_side=None, read_acl=None,
            author=author, op=op, title=f"Update topic {t.pref_label}",
        )

    def _record_claim(self, c: Claim, *, author: Principal | None = None, op: str | None = None) -> None:
        self._record(
            "claim", claim_slot_id(c), c.model_dump(mode="json"), wall_side=wall_value(c.acl.wall_side),
            read_acl=list(c.acl.read_acl_groups), author=author, op=op,
            title=f"{c.predicate} on {c.subject_id}",
        )

    def _record_memory(self, m: MemoryRecord, *, author: Principal | None = None, op: str | None = None) -> None:
        self._record(
            "memory", m.memory_id, m.model_dump(mode="json"), wall_side=None, read_acl=None,
            owner=m.user_id, author=author, op=op, title=f"Update memory {m.key}",
        )

    def _record_watchlist(self, w: WatchlistRecord, *, author: Principal | None = None, op: str | None = None) -> None:
        payload = w.model_dump(mode="json", exclude={"alerts"})
        self._record(
            "watchlist", w.user_id, payload, wall_side=None, read_acl=None, owner=w.user_id,
            author=author, op=op, title="Update watchlist",
        )

    async def _raw_claim(self, claim: Claim) -> Claim:
        getter = getattr(self._inner, "get_claim_raw", None)
        if getter is None:
            return claim
        raw = await getter(claim.claim_id)
        return raw or claim

    # ------------------------------------------------------------------
    # Wrapped writes
    # ------------------------------------------------------------------

    async def upsert_entity(self, entity: Entity) -> Entity:
        result = await self._inner.upsert_entity(entity)
        self._record_entity(result)
        return result

    async def upsert_knowledge_item(self, item: KnowledgeItem) -> KnowledgeItem:
        result = await self._inner.upsert_knowledge_item(item)
        self._record_item(result)
        return result

    async def upsert_topic(self, topic: Topic) -> Topic:
        result = await self._inner.upsert_topic(topic)
        self._record_topic(result)
        return result

    async def update_topic(self, topic_id: str, patch: TopicPatchInput, *, principal: Principal) -> Topic:
        result = await self._inner.update_topic(topic_id, patch, principal=principal)
        self._record_topic(result, author=principal)
        return result

    async def seed_claim_raw(self, claim: Claim) -> Claim:
        result = await self._inner.seed_claim_raw(claim)
        self._record_claim(result, op="baseline")
        return result

    async def write_claim(self, payload: ClaimCreateInput, *, principal: Principal) -> ClaimWriteResult:
        result = await self._inner.write_claim(payload, principal=principal)
        self._record_claim(await self._raw_claim(result.claim), author=principal)
        return result

    async def update_claim(
        self, claim_id: str, patch: ClaimUpdateInput, *, principal: Principal
    ) -> ClaimWriteResult:
        result = await self._inner.update_claim(claim_id, patch, principal=principal)
        self._record_claim(await self._raw_claim(result.claim), author=principal, op="update")
        return result

    async def resolve_conflict(
        self, resolution: ConflictResolutionInput, *, principal: Principal
    ) -> ConflictSet:
        result = await self._inner.resolve_conflict(resolution, principal=principal)
        getter = getattr(self._inner, "get_claim_raw", None)
        if getter is not None and result.winning_claim_id:
            winner = await getter(result.winning_claim_id)
            if winner is not None:
                self._record_claim(winner, author=principal, op="update")
        return result

    async def seed_memory_raw(self, memory: MemoryRecord) -> MemoryRecord:
        result = await self._inner.seed_memory_raw(memory)
        self._record_memory(result, op="baseline")
        return result

    async def upsert_memory(self, record: MemoryUpsertInput, *, principal: Principal) -> MemoryRecord:
        result = await self._inner.upsert_memory(record, principal=principal)
        self._record_memory(result, author=principal)
        return result

    async def delete_memory(self, memory_id: str, *, principal: Principal) -> bool:
        deleted = await self._inner.delete_memory(memory_id, principal=principal)
        if deleted:
            self._record(
                "memory", memory_id, None, wall_side=None, read_acl=None, owner=principal.user_id,
                op="delete", author=principal, title="Delete memory",
            )
        return deleted

    async def seed_watchlist_raw(self, wl: WatchlistRecord) -> WatchlistRecord:
        result = await self._inner.seed_watchlist_raw(wl)
        self._record_watchlist(result, op="baseline")
        return result

    async def put_watchlist(self, watchlist: WatchlistUpdateInput, *, principal: Principal) -> WatchlistRecord:
        result = await self._inner.put_watchlist(watchlist, principal=principal)
        self._record_watchlist(result, author=principal)
        return result

    @staticmethod
    def _describe(record_type: str, rec: Any) -> tuple[str, dict[str, Any], str | None, list[str] | None, str | None]:
        """(record_id, payload, wall_side, read_acl, owner) for a domain record."""
        if record_type == "entity":
            return rec.entity_id, rec.model_dump(mode="json"), wall_value(rec.wall_side), list(rec.read_acl_groups), None
        if record_type == "knowledge_item":
            return rec.item_id, rec.model_dump(mode="json"), wall_value(rec.wall_side), list(rec.read_acl_groups), None
        if record_type == "topic":
            return rec.topic_id, rec.model_dump(mode="json"), None, None, None
        if record_type == "claim":
            return claim_slot_id(rec), rec.model_dump(mode="json"), wall_value(rec.acl.wall_side), list(rec.acl.read_acl_groups), None
        if record_type == "memory":
            return rec.memory_id, rec.model_dump(mode="json"), None, None, rec.user_id
        if record_type == "watchlist":
            return rec.user_id, rec.model_dump(mode="json", exclude={"alerts"}), None, None, rec.user_id
        raise ValueError(record_type)

    async def import_state(self, state: dict[str, Any], *, reset_epochs: bool = False) -> None:
        """Bulk import (seeding, adapter swap). Records a version only where the ledger differs.

        Written to the ledger in one transaction, so seeding thousands of records stays fast.
        """
        await self._inner.import_state(state, reset_epochs=reset_epochs)
        sources = {
            "entity": state.get("entities", []),
            "knowledge_item": state.get("knowledge_items", []),
            "topic": state.get("topics", []),
            "memory": state.get("memories", []),
            "watchlist": state.get("watchlists", []),
            "claim": state.get("claims", []),
        }
        if not any(sources.values()):
            return
        first_time = not any(self._ledger.latest_hashes(rt) for rt in sources)
        op = "baseline" if first_time else "import"
        cs_id = new_change_id()
        now = ts()
        versions: list[VersionRecord] = []
        for rt, records in sources.items():
            if first_time:
                seen_latest: dict[str, str | None] = {}
                seen_any: set[tuple[str, str | None]] = set()
            elif rt == "claim":
                seen_latest, seen_any = {}, self._ledger.all_hashes("claim")
            else:
                seen_latest, seen_any = self._ledger.latest_hashes(rt), set()
            for rec in records:
                rid, payload, wall, acl, owner = self._describe(rt, rec)
                digest = content_hash(payload)
                if (rid, digest) in seen_any or (rid in seen_latest and seen_latest[rid] == digest):
                    continue
                if rt == "claim":
                    seen_any.add((rid, digest))
                else:
                    seen_latest[rid] = digest
                versions.append(
                    VersionRecord(
                        record_type=rt, record_id=rid, version_no=0, change_id=cs_id, op=op, recorded_at=now,  # type: ignore[arg-type]
                        wall_side=wall, read_acl_groups=acl, owner_user_id=owner,
                        label=record_label(rt, payload), payload=payload, content_hash=digest,
                    )
                )
        if not versions:
            return
        actor = current_actor.get()
        self._ledger.save_change_set(
            ChangeSet(
                change_id=cs_id,
                title="Initial import (baseline)" if first_time else "Import",
                kind="import",
                source="import",
                author_id=actor.user_id if actor else "system",
                author_name=actor.display_name if actor else "System",
                status="merged",
                approval="auto",
                rule_ids=["import"],
                implicit=True,
                created_at=now,
                decided_at=now,
                merged_at=now,
                merged_by=actor.user_id if actor else "system",
            )
        )
        self._ledger.append_versions(versions)


__all__ = ["VersionedStore", "current_actor", "new_change_id"]
