"""OKF v0.2 Git-native Markdown Vault StorePort adapter with SHA-256 sidecars and user-scope isolation (ADR-002)."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import yaml

from context_core.bitemporal import (
    claims_conflict,
    is_claim_visible,
    is_identifier_visible,
    windows_overlap,
)
from context_core.compression import (
    atomic_write_text,
    ensure_utc,
    format_utc,
    lf_sha256_file,
    lf_sha256_text,
    normalize_lf_text,
    to_posix_path,
    utc_now,
)
from context_core.exceptions import (
    AuthenticationError,
    NotFoundError,
    OptimisticConcurrencyError,
    ValidationError,
)
from context_core.models import (
    clone_model,
    Claim,
    ClaimCreateInput,
    ClaimUpdateInput,
    ClaimWriteResult,
    ConflictResolutionInput,
    ConflictSet,
    ContextAssemblyReceipt,
    Coverage,
    CuratorDraftPatch,
    Entity,
    EntityIdentifier,
    EntityLineageEvent,
    EntityType,
    KnowledgeItem,
    MemoryRecord,
    MemoryUpsertInput,
    PolicyEpochState,
    Position,
    Principal,
    SkillBinding,
    StalenessItem,
    StalenessReport,
    Topic,
    TopicAttachment,
    TopicPatchInput,
    WallSide,
    WatchlistRecord,
    WatchlistUpdateInput,
)
from context_core.policy import PolicyEngine, sanitize_uri_secrets, wall_allows
from context_adapters._vocab_common import VocabularyStoreMixin
from context_core.vocabulary import VocabTerm, default_terms
from context_adapters._store_common import (
    apply_topic_patch,
    check_toctou_epoch,
    compute_sha_staleness,
    compute_watchlist_alerts,
    prepare_incoming_claim,
)

_SAFE_SEGMENT_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_INDEX_MAX_LINES = 200


def _safe_slug(raw: str) -> str:
    return _SAFE_SEGMENT_RE.sub("-", raw.strip().lower()).strip("-") or "item"


def _folder_for_entity(entity_type: EntityType) -> str:
    if entity_type in (
        EntityType.strategy,
        EntityType.fund_family,
        EntityType.reporting_group,
        EntityType.legal_vehicle,
        EntityType.share_class,
    ):
        return "funds"
    if entity_type in (
        EntityType.ultimate_parent,
        EntityType.pe_sponsor,
        EntityType.issuer,
        EntityType.instrument,
    ):
        return "issuers"
    if entity_type in (
        EntityType.legal_party,
        EntityType.client,
        EntityType.consultant,
        EntityType.commitment,
        EntityType.opportunity,
    ):
        return "clients"
    if entity_type in (EntityType.app, EntityType.skill):
        return "skills"
    if entity_type == EntityType.project:
        return "projects"
    return "entities"


def _render_okf_markdown(frontmatter: dict[str, Any], body: str) -> str:
    """Render deterministic LF-normalized OKF v0.2 Markdown with computed content_sha256."""
    body_lf = normalize_lf_text(body).strip()
    fm_copy = dict(frontmatter)
    fm_copy["okf_version"] = "0.2"
    fm_copy["content_sha256"] = lf_sha256_text(body_lf)
    yaml_text = yaml.safe_dump(fm_copy, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{yaml_text}\n---\n\n{body_lf}\n"


class OkfMarkdownVaultStore(VocabularyStoreMixin):
    """OKF v0.2 Markdown vault implementing the exact same StorePort contract as SqliteBitemporalStore."""

    def __init__(
        self,
        vault_dir: str | Path,
        *,
        policy: PolicyEngine | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.vault_dir = Path(vault_dir).resolve()
        self.policy = policy or PolicyEngine()
        self.repo_root = repo_root or Path(__file__).resolve().parents[3]
        self._lock = asyncio.Lock()
        self._initialized = False

        # Synchronized in-memory state backed by deterministic OKF files & JSON metadata on disk
        self._epochs = PolicyEpochState(data_epoch=1, policy_epoch=1)
        self._revoked: dict[str, dict[str, str]] = {}
        self._entities: dict[str, Entity] = {}
        self._key_to_id: dict[str, str] = {}
        self._identifiers: dict[str, EntityIdentifier] = {}
        self._lineage: dict[str, EntityLineageEvent] = {}
        self._positions: dict[str, Position] = {}
        self._coverages: dict[str, Coverage] = {}
        self._skill_bindings: dict[str, SkillBinding] = {}
        self._claims: dict[str, Claim] = {}
        self._conflicts: dict[str, ConflictSet] = {}
        self._topics: dict[str, Topic] = {}
        self._attachments: dict[str, TopicAttachment] = {}
        self._knowledge_items: dict[str, KnowledgeItem] = {}
        self._curator_patches: dict[str, CuratorDraftPatch] = {}
        self._memories: dict[str, MemoryRecord] = {}
        self._watchlists: dict[str, WatchlistRecord] = {}
        self._receipts: dict[str, ContextAssemblyReceipt] = {}
        self._vocab: dict[str, VocabTerm] = {}
        self._new_vocab_terms: list[VocabTerm] = []

    @property
    def meta_dir(self) -> Path:
        return self.vault_dir / "_meta"

    @property
    def claims_sidecar_dir(self) -> Path:
        return self.vault_dir / "claims"

    def _user_scope_dir(self, user_id: str) -> Path:
        """Fail-closed per-user personal memory directory under `_scopes/user/<user_id>/`."""
        safe_uid = _safe_slug(user_id)
        target = (self.vault_dir / "_scopes" / "user" / safe_uid).resolve()
        scopes_root = (self.vault_dir / "_scopes" / "user").resolve()
        # Prevent path traversal outside user scope root
        if scopes_root not in target.parents:
            raise ValidationError(f"Invalid user scope path for {user_id}")
        return target

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            self.vault_dir.mkdir(parents=True, exist_ok=True)
            self.meta_dir.mkdir(parents=True, exist_ok=True)
            self.claims_sidecar_dir.mkdir(parents=True, exist_ok=True)
            self._load_from_disk_sync()
            if not self._vocab:
                self._vocab = {t.term_id: t for t in default_terms()}
            self._initialized = True

    async def close(self) -> None:
        self._initialized = False

    async def _ensure_init(self) -> None:
        if not self._initialized:
            await self.initialize()

    async def _effective_principal(self, principal: Principal) -> Principal:
        if principal.revoked:
            return principal
        if principal.user_id in self._revoked:
            return principal.model_copy(update={"revoked": True})
        return principal

    # ------------------------------------------------------------------
    # Disk Persistence (OKF v0.2 Markdown, _index.md, SHA sidecars, _scopes/user/)
    # ------------------------------------------------------------------

    def _entity_relative_path(self, entity: Entity) -> str:
        folder = _folder_for_entity(entity.entity_type)
        slug = _safe_slug(entity.canonical_key)
        return to_posix_path(f"{folder}/{slug}.md")

    def _write_entity_markdown(self, entity: Entity) -> str:
        rel = self._entity_relative_path(entity)
        target = self.vault_dir / rel
        topics = sorted(
            {
                self._topics[att.topic_id].slug
                for att in self._attachments.values()
                if att.target_id == entity.entity_id and att.topic_id in self._topics
            }
        )
        linked_keys = sorted(
            {
                self._entities[c.object_entity_id].canonical_key
                for c in self._claims.values()
                if c.subject_id == entity.entity_id
                and c.object_entity_id
                and c.object_entity_id in self._entities
                and c.superseded_at is None
            }
        )
        wikilinks = "\n".join(f"- [[{k}]]" for k in linked_keys)
        body_parts = [f"# {entity.display_name}", "", entity.description or ""]
        if entity.aliases:
            body_parts.extend(["", f"**Aliases:** {', '.join(entity.aliases)}"])
        if wikilinks:
            body_parts.extend(["", "## Links", wikilinks])
        fm = {
            "okf_version": "0.2",
            "id": entity.canonical_key,
            "entity_id": entity.entity_id,
            "entity_type": entity.entity_type,
            "display_name": entity.display_name,
            "aliases": list(entity.aliases),
            "wall_side": entity.wall_side.value,
            "read_acl_groups": list(entity.read_acl_groups),
            "write_acl_groups": list(entity.write_acl_groups),
            "topics": topics,
            "valid_from": format_utc(entity.created_at),
            "recorded_at": format_utc(entity.updated_at),
        }
        atomic_write_text(target, _render_okf_markdown(fm, "\n".join(body_parts)))
        return rel

    def _write_knowledge_markdown(self, item: KnowledgeItem) -> str:
        rel = to_posix_path(f"knowledge/{item.knowledge_kind}/{_safe_slug(item.slug)}.md")
        target = self.vault_dir / rel
        fm = {
            "okf_version": item.okf_version,
            "id": item.item_id,
            "knowledge_kind": item.knowledge_kind,
            "display_name": item.display_name,
            "wall_side": item.wall_side.value,
            "read_acl_groups": list(item.read_acl_groups),
            "write_acl_groups": list(item.write_acl_groups),
            "topics": list(item.topics),
            "related_entities": list(item.related_entities),
            "source_path": to_posix_path(item.source_path),
            "valid_from": format_utc(item.valid_from),
            "recorded_at": format_utc(item.recorded_at),
        }
        atomic_write_text(target, _render_okf_markdown(fm, item.body_markdown))
        return rel

    def _write_claim_evidence_sidecar(self, claim: Claim) -> None:
        if not claim.provenance.evidence_uri or not claim.provenance.content_sha256:
            return
        sidecar_path = self.claims_sidecar_dir / f"{_safe_slug(claim.claim_id)}.evidence.sha256.json"
        payload = {
            "claim_id": claim.claim_id,
            "evidence_path": to_posix_path(claim.provenance.evidence_uri),
            "expected_sha256": claim.provenance.content_sha256,
            "wall_side": claim.acl.wall_side.value,
            "verified_at": format_utc(utc_now()),
        }
        atomic_write_text(sidecar_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def _write_memory_markdown(self, mem: MemoryRecord) -> None:
        u_dir = self._user_scope_dir(mem.user_id) / "memories"
        u_dir.mkdir(parents=True, exist_ok=True)
        note_path = u_dir / f"{_safe_slug(mem.memory_id)}.md"
        fm = {
            "okf_version": "0.2",
            "memory_id": mem.memory_id,
            "user_id": mem.user_id,
            "scope_type": mem.scope_type,
            "skill_id": mem.skill_id,
            "key": mem.key,
            "tags": list(mem.tags),
            "linked_entity_ids": list(mem.linked_entity_ids),
            "linked_topic_ids": list(mem.linked_topic_ids),
            "created_at": format_utc(mem.created_at),
            "updated_at": format_utc(mem.updated_at),
        }
        atomic_write_text(note_path, _render_okf_markdown(fm, mem.content))

    def _write_watchlist_file(self, wl: WatchlistRecord) -> None:
        u_dir = self._user_scope_dir(wl.user_id)
        u_dir.mkdir(parents=True, exist_ok=True)
        wl_path = u_dir / "watchlist.json"
        payload = {
            "user_id": wl.user_id,
            "entity_ids": list(wl.entity_ids),
            "topic_ids": list(wl.topic_ids),
            "source_ids": list(wl.source_ids),
            "updated_at": format_utc(wl.updated_at),
        }
        atomic_write_text(wl_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def materialize_indices(self, *, acl_tier: str = "firm-all", wall_side: str = "public_side") -> list[str]:
        """Generate deterministic `_index.md` per folder/topic (≤200 lines) strictly filtered by (acl_tier, wall_side)."""
        written: list[str] = []
        by_folder: dict[str, list[Entity]] = {}
        for ent in sorted(self._entities.values(), key=lambda e: e.canonical_key):
            if not self.policy.entity_eligible_for_partition(ent, acl_tier=acl_tier, wall_side=wall_side):
                continue
            folder = _folder_for_entity(ent.entity_type)
            by_folder.setdefault(folder, []).append(ent)

        for folder, ents in sorted(by_folder.items()):
            lines = [f"# {folder.title()} Index ({acl_tier} / {wall_side})", ""]
            for ent in ents[: _INDEX_MAX_LINES - 4]:
                rel = self._entity_relative_path(ent)
                lines.append(f"- [[{ent.canonical_key}]] — {ent.display_name} (`{rel}`)")
            if len(ents) > _INDEX_MAX_LINES - 4:
                lines.append(f"- ... ({len(ents) - (_INDEX_MAX_LINES - 4)} more split into sub-indices)")
            idx_path = self.vault_dir / folder / "_index.md"
            atomic_write_text(idx_path, "\n".join(lines) + "\n")
            written.append(to_posix_path(f"{folder}/_index.md"))

        # Root _index.md
        root_lines = [f"# Enterprise Context Lake Vault Index ({acl_tier} / {wall_side})", ""]
        for rel in written:
            root_lines.append(f"- [{rel}]({rel})")
        atomic_write_text(self.vault_dir / "_index.md", "\n".join(root_lines[:_INDEX_MAX_LINES]) + "\n")
        written.append("_index.md")
        return written

    def _persist_meta_sync(self) -> None:
        """Atomically persist structured vault state JSON under `_meta/` with LF endings."""
        snapshot = {
            "epochs": self._epochs.model_dump(mode="json"),
            "revoked": self._revoked,
            "entities": [e.model_dump(mode="json") for e in self._entities.values()],
            "identifiers": [i.model_dump(mode="json") for i in self._identifiers.values()],
            "lineage_events": [ev.model_dump(mode="json") for ev in self._lineage.values()],
            "positions": [p.model_dump(mode="json") for p in self._positions.values()],
            "coverages": [c.model_dump(mode="json") for c in self._coverages.values()],
            "skill_bindings": [sb.model_dump(mode="json") for sb in self._skill_bindings.values()],
            "claims": [c.model_dump(mode="json") for c in self._claims.values()],
            "conflict_sets": [
                cs.model_copy(update={"claims": []}).model_dump(mode="json")
                for cs in self._conflicts.values()
            ],
            "topics": [t.model_dump(mode="json") for t in self._topics.values()],
            "topic_attachments": [a.model_dump(mode="json") for a in self._attachments.values()],
            "knowledge_items": [k.model_dump(mode="json") for k in self._knowledge_items.values()],
            "curator_patches": [p.model_dump(mode="json") for p in self._curator_patches.values()],
            "memories": [m.model_dump(mode="json") for m in self._memories.values()],
            "watchlists": [
                w.model_copy(update={"alerts": []}).model_dump(mode="json")
                for w in self._watchlists.values()
            ],
            "receipts": [r.model_dump(mode="json") for r in self._receipts.values()],
            "vocab_terms": [t.model_dump(mode="json") for t in self._vocab.values()],
        }
        atomic_write_text(self.meta_dir / "state.json", json.dumps(snapshot, indent=2, sort_keys=True) + "\n")

    def _load_from_disk_sync(self) -> None:
        state_file = self.meta_dir / "state.json"
        if not state_file.is_file():
            return
        raw = json.loads(state_file.read_text(encoding="utf-8"))
        self._epochs = PolicyEpochState.model_validate(raw.get("epochs", {}))
        self._revoked = dict(raw.get("revoked", {}))
        self._entities = {e["entity_id"]: Entity.model_validate(e) for e in raw.get("entities", [])}
        self._key_to_id = {e.canonical_key: e.entity_id for e in self._entities.values()}
        self._identifiers = {
            i["id"]: EntityIdentifier.model_validate(i) for i in raw.get("identifiers", [])
        }
        self._lineage = {
            ev["event_id"]: EntityLineageEvent.model_validate(ev)
            for ev in raw.get("lineage_events", [])
        }
        self._positions = {
            p["position_id"]: Position.model_validate(p) for p in raw.get("positions", [])
        }
        self._coverages = {
            c["coverage_id"]: Coverage.model_validate(c) for c in raw.get("coverages", [])
        }
        self._skill_bindings = {
            sb["binding_id"]: SkillBinding.model_validate(sb)
            for sb in raw.get("skill_bindings", [])
        }
        self._claims = {c["claim_id"]: Claim.model_validate(c) for c in raw.get("claims", [])}
        self._conflicts = {
            cs["conflict_set_id"]: ConflictSet.model_validate(cs)
            for cs in raw.get("conflict_sets", [])
        }
        self._topics = {t["topic_id"]: Topic.model_validate(t) for t in raw.get("topics", [])}
        self._attachments = {
            a["attachment_id"]: TopicAttachment.model_validate(a)
            for a in raw.get("topic_attachments", [])
        }
        self._knowledge_items = {
            k["item_id"]: KnowledgeItem.model_validate(k) for k in raw.get("knowledge_items", [])
        }
        self._curator_patches = {
            p["patch_id"]: CuratorDraftPatch.model_validate(p)
            for p in raw.get("curator_patches", [])
        }
        self._memories = {
            m["memory_id"]: MemoryRecord.model_validate(m) for m in raw.get("memories", [])
        }
        self._watchlists = {
            w["user_id"]: WatchlistRecord.model_validate(w) for w in raw.get("watchlists", [])
        }
        self._receipts = {
            r["receipt_id"]: ContextAssemblyReceipt.model_validate(r)
            for r in raw.get("receipts", [])
        }
        self._vocab = {t.term_id: t for t in (VocabTerm.model_validate(v) for v in raw.get("vocab_terms", []))}

    def _bump_data_epoch(self, reason: str, actor: str) -> int:
        self._epochs = PolicyEpochState(
            data_epoch=self._epochs.data_epoch + 1,
            policy_epoch=self._epochs.policy_epoch,
            updated_at=utc_now(),
            last_reason=reason,
            last_actor=actor,
        )
        return self._epochs.data_epoch

    # ------------------------------------------------------------------
    # Epochs & Revocation
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Vocabulary storage primitives (ADR-008): terms live in _meta/state.json
    # ------------------------------------------------------------------

    async def _vocab_load(self) -> list[VocabTerm]:
        await self._ensure_init()
        return [clone_model(t) for t in self._vocab.values()]

    async def _vocab_count(self) -> int:
        await self._ensure_init()
        return len(self._vocab)

    async def _vocab_save(self, terms: Sequence[VocabTerm]) -> None:
        if not terms:
            return
        await self._ensure_init()
        async with self._lock:
            for t in terms:
                self._vocab[t.term_id] = clone_model(t)
            self._persist_meta_sync()

    async def _vocab_replace_all(self, terms: Sequence[VocabTerm]) -> None:
        await self._ensure_init()
        async with self._lock:
            self._vocab = {t.term_id: clone_model(t) for t in terms}
            self._persist_meta_sync()

    async def get_epochs(self) -> PolicyEpochState:
        await self._ensure_init()
        return clone_model(self._epochs)

    async def bump_policy_epoch(self, reason: str, actor: str) -> PolicyEpochState:
        await self._ensure_init()
        async with self._lock:
            self._epochs = PolicyEpochState(
                data_epoch=self._epochs.data_epoch,
                policy_epoch=self._epochs.policy_epoch + 1,
                updated_at=utc_now(),
                last_reason=reason,
                last_actor=actor,
            )
            self._persist_meta_sync()
            return clone_model(self._epochs)

    async def is_principal_revoked(self, user_id: str) -> bool:
        await self._ensure_init()
        return user_id in self._revoked

    async def revoke_principal(self, user_id: str, *, reason: str, actor: str) -> PolicyEpochState:
        await self._ensure_init()
        async with self._lock:
            self._revoked[user_id] = {
                "reason": reason,
                "actor": actor,
                "revoked_at": format_utc(utc_now()) or "",
            }
            self._epochs = PolicyEpochState(
                data_epoch=self._epochs.data_epoch,
                policy_epoch=self._epochs.policy_epoch + 1,
                updated_at=utc_now(),
                last_reason=f"revoke:{user_id}:{reason}",
                last_actor=actor,
            )
            self._persist_meta_sync()
            return clone_model(self._epochs)

    # ------------------------------------------------------------------
    # Plane 1: Entities, Crosswalks, Lineage & Reified Associations
    # ------------------------------------------------------------------

    def _resolve_entity_raw(self, entity_id_or_key: str) -> Entity | None:
        if entity_id_or_key in self._entities:
            return self._entities[entity_id_or_key]
        eid = self._key_to_id.get(entity_id_or_key.lower())
        return self._entities.get(eid) if eid else None

    async def upsert_entity(self, entity: Entity) -> Entity:
        await self._ensure_init()
        async with self._lock:
            self._entities[entity.entity_id] = clone_model(entity)
            self._key_to_id[entity.canonical_key] = entity.entity_id
            self._write_entity_markdown(entity)
            self._bump_data_epoch(f"upsert_entity:{entity.entity_id}", "system")
            self.materialize_indices()
            self._persist_meta_sync()
        await self._register_record_terms("entities", entity)
        return entity

    async def get_entity(self, entity_id_or_key: str, *, principal: Principal) -> Entity | None:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        ent = self._resolve_entity_raw(entity_id_or_key)
        if ent is None or not self.policy.can_read_entity(eff, ent):
            return None
        return clone_model(ent)

    async def list_entities(
        self,
        *,
        principal: Principal,
        entity_types: Sequence[str] | None = None,
        topic_slug: str | None = None,
        limit: int | None = 200,
        offset: int = 0,
    ) -> list[Entity]:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            return []
        # A broad type also returns its narrower types (party -> organisation -> issuer ...)
        type_set = await self.expand_entity_types(entity_types)
        authorized = [
            clone_model(e)
            for e in sorted(self._entities.values(), key=lambda x: x.canonical_key)
            if (type_set is None or e.entity_type in type_set)
            and self.policy.can_read_entity(eff, e)
        ]
        if topic_slug:
            slug_l = topic_slug.strip().lower()
            t_by_slug = {t.slug: t for t in self._topics.values()}
            matching_tids: set[str] = set()
            root_t = t_by_slug.get(slug_l)
            if root_t:
                matching_tids.add(root_t.topic_id)
                frontier = [root_t.topic_id]
                while frontier:
                    cur = frontier.pop()
                    for t in self._topics.values():
                        if (t.broader_topic_id == cur or t.merged_into_topic_id == cur) and t.topic_id not in matching_tids:
                            matching_tids.add(t.topic_id)
                            frontier.append(t.topic_id)
            claim_subj = {
                c.claim_id: c.subject_id
                for c in self._claims.values()
                if self.policy.can_read_claim(eff, c)
            }
            matched_eids: set[str] = set()
            for att in self._attachments.values():
                if att.topic_id not in matching_tids:
                    continue
                if not self.policy.can_read_record(
                    eff, wall_side=att.wall_side, read_acl_groups=att.read_acl_groups
                ):
                    continue
                if att.target_type == "claim":
                    if att.target_id in claim_subj:
                        matched_eids.add(claim_subj[att.target_id])
                else:
                    matched_eids.add(att.target_id)
            authorized = [e for e in authorized if e.entity_id in matched_eids]
        if limit is None:
            return authorized[offset:]
        return authorized[offset : offset + limit]

    async def upsert_identifier(self, ident: EntityIdentifier) -> EntityIdentifier:
        await self._ensure_init()
        async with self._lock:
            self._identifiers[ident.id] = clone_model(ident)
            self._persist_meta_sync()
        await self._register_record_terms("identifiers", ident)
        return ident

    async def list_identifiers(
        self,
        entity_id: str,
        *,
        as_of: datetime | None = None,
        recorded_as_of: datetime | None = None,
    ) -> list[EntityIdentifier]:
        await self._ensure_init()
        idents = [clone_model(i) for i in self._identifiers.values() if i.entity_id == entity_id]
        if as_of is not None or recorded_as_of is not None:
            idents = [
                i for i in idents if is_identifier_visible(i, as_of=as_of, recorded_as_of=recorded_as_of)
            ]
        return idents

    async def list_identifiers_for(
        self,
        entity_ids: Sequence[str] | None = None,
        *,
        as_of: datetime | None = None,
        recorded_as_of: datetime | None = None,
    ) -> dict[str, list[EntityIdentifier]]:
        """Batched ``list_identifiers`` (``None`` = all entities); same order and filtering per entity."""
        await self._ensure_init()
        wanted = None if entity_ids is None else set(entity_ids)
        out: dict[str, list[EntityIdentifier]] = {}
        for i in self._identifiers.values():
            if wanted is not None and i.entity_id not in wanted:
                continue
            if (as_of is not None or recorded_as_of is not None) and not is_identifier_visible(
                i, as_of=as_of, recorded_as_of=recorded_as_of
            ):
                continue
            out.setdefault(i.entity_id, []).append(clone_model(i))
        return out

    async def add_lineage_event(self, event: EntityLineageEvent) -> EntityLineageEvent:
        await self._ensure_init()
        async with self._lock:
            clean_ev = event.model_copy(
                deep=True, update={"evidence_uri": sanitize_uri_secrets(event.evidence_uri)}
            )
            self._lineage[clean_ev.event_id] = clean_ev
            self._persist_meta_sync()
        await self._register_record_terms("lineage_events", clean_ev)
        return clean_ev

    async def list_lineage_events(
        self, entity_id: str, *, principal: Principal
    ) -> list[EntityLineageEvent]:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            return []
        out: list[EntityLineageEvent] = []
        for ev in self._lineage.values():
            if ev.source_entity_id != entity_id and ev.target_entity_id != entity_id:
                continue
            san = self.policy.sanitize_lineage_for_principal(eff, ev)
            if san is not None:
                out.append(san)
        out.sort(key=lambda e: (e.effective_date, e.event_id))
        return out

    async def list_lineage_events_for(
        self, entity_ids: Sequence[str] | None = None, *, principal: Principal
    ) -> list[EntityLineageEvent]:
        """Batched ``list_lineage_events`` (``None`` = all events), sanitized and sorted."""
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            return []
        wanted = None if entity_ids is None else set(entity_ids)
        out: list[EntityLineageEvent] = []
        for ev in self._lineage.values():
            if wanted is not None and ev.source_entity_id not in wanted and ev.target_entity_id not in wanted:
                continue
            san = self.policy.sanitize_lineage_for_principal(eff, ev)
            if san is not None:
                out.append(san)
        out.sort(key=lambda e: (e.effective_date, e.event_id))
        return out

    async def upsert_position(self, pos: Position) -> Position:
        await self._ensure_init()
        async with self._lock:
            self._positions[pos.position_id] = clone_model(pos)
            self._persist_meta_sync()
        await self._register_record_terms("positions", pos)
        return pos

    async def list_positions(
        self,
        *,
        principal: Principal,
        vehicle_id: str | None = None,
        issuer_id: str | None = None,
    ) -> list[Position]:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            return []
        out: list[Position] = []
        for pos in self._positions.values():
            if vehicle_id and pos.vehicle_entity_id != vehicle_id:
                continue
            if issuer_id and pos.issuer_entity_id != issuer_id:
                continue
            if self.policy.can_read_record(
                eff, wall_side=pos.wall_side, read_acl_groups=pos.read_acl_groups
            ):
                out.append(clone_model(pos))
        return out

    async def upsert_coverage(self, cov: Coverage) -> Coverage:
        await self._ensure_init()
        async with self._lock:
            self._coverages[cov.coverage_id] = clone_model(cov)
            self._persist_meta_sync()
        await self._register_record_terms("coverages", cov)
        return cov

    async def list_coverages(
        self,
        *,
        principal: Principal,
        issuer_id: str | None = None,
        status: str | None = None,
    ) -> list[Coverage]:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            return []
        out: list[Coverage] = []
        for cov in self._coverages.values():
            if issuer_id and cov.issuer_entity_id != issuer_id:
                continue
            if status and cov.coverage_status != status:
                continue
            if self.policy.can_read_record(
                eff, wall_side=cov.wall_side, read_acl_groups=cov.read_acl_groups
            ):
                out.append(clone_model(cov))
        return out

    async def upsert_skill_binding(self, binding: SkillBinding) -> SkillBinding:
        await self._ensure_init()
        async with self._lock:
            self._skill_bindings[binding.binding_id] = clone_model(binding)
            self._persist_meta_sync()
        return binding

    async def list_skill_bindings(
        self,
        *,
        skill_entity_id: str | None = None,
        target_entity_id: str | None = None,
    ) -> list[SkillBinding]:
        await self._ensure_init()
        out: list[SkillBinding] = []
        for sb in self._skill_bindings.values():
            if skill_entity_id and sb.skill_entity_id != skill_entity_id:
                continue
            if target_entity_id and sb.target_entity_id != target_entity_id:
                continue
            out.append(clone_model(sb))
        return out

    # ------------------------------------------------------------------
    # Plane 2: Bitemporal Claims & N-Way Conflicts
    # ------------------------------------------------------------------

    async def seed_claim_raw(self, claim: Claim) -> Claim:
        await self._ensure_init()
        async with self._lock:
            self._claims[claim.claim_id] = clone_model(claim)
            self._write_claim_evidence_sidecar(claim)
            self._persist_meta_sync()
        await self._register_record_terms("claims", claim)
        return claim

    async def seed_conflict_raw(self, conflict: ConflictSet) -> ConflictSet:
        await self._ensure_init()
        async with self._lock:
            self._conflicts[conflict.conflict_set_id] = conflict.model_copy(
                deep=True, update={"claims": []}
            )
            self._persist_meta_sync()
        return conflict

    async def write_claim(
        self, payload: ClaimCreateInput, *, principal: Principal
    ) -> ClaimWriteResult:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            raise AuthenticationError("Revoked principal cannot write claims")

        async with self._lock:
            lookup_key = payload.subject_id or payload.subject_canonical_key or ""
            target = self._resolve_entity_raw(lookup_key)
            if target is None:
                raise NotFoundError(f"Subject entity not found: {lookup_key}")

            self.policy.authorize_write(
                eff,
                target_entity=target,
                scope_type=payload.scope_type,
                epistemic_status=payload.epistemic_status,
                force_supersede=payload.force_supersede,
                is_mandatory=payload.is_mandatory,
            )
            check_toctou_epoch(payload.expected_data_epoch, self._epochs.data_epoch)

            incoming = prepare_incoming_claim(
                payload, target_entity=target, principal=eff, repo_root=self.repo_root
            )
            overlapping = [
                c
                for c in self._claims.values()
                if c.subject_id == incoming.subject_id
                and c.predicate == incoming.predicate
                and c.context_purpose == incoming.context_purpose
                and c.scope_type == incoming.scope_type
                and c.scope_id == incoming.scope_id
                and c.superseded_at is None
                and c.epistemic_status != "revoked"
                and windows_overlap(c.valid_from, c.valid_to, incoming.valid_from, incoming.valid_to)
            ]

            superseded_claim_id: str | None = None
            conflict_detected = False
            conflict_set_id: str | None = None
            now = utc_now()

            if payload.force_supersede:
                for old in overlapping:
                    if old.channel == incoming.channel:
                        self._claims[old.claim_id] = old.model_copy(
                            update={"superseded_at": now, "superseded_by": incoming.claim_id}
                        )
                        if superseded_claim_id is None:
                            superseded_claim_id = old.claim_id
                self._claims[incoming.claim_id] = incoming
            else:
                colliding = [c for c in overlapping if claims_conflict(c, incoming)]
                if colliding:
                    conflict_detected = True
                    if incoming.epistemic_status not in ("unverified_observation", "candidate_sop"):
                        incoming = incoming.model_copy(update={"epistemic_status": "disputed"})
                    self._claims[incoming.claim_id] = incoming

                    existing_cs = next(
                        (
                            cs
                            for cs in self._conflicts.values()
                            if cs.status == "open"
                            and cs.subject_id == incoming.subject_id
                            and cs.predicate == incoming.predicate
                            and cs.context_purpose == incoming.context_purpose
                            and cs.scope_type == incoming.scope_type
                            and cs.scope_id == incoming.scope_id
                        ),
                        None,
                    )
                    if existing_cs is not None:
                        conflict_set_id = existing_cs.conflict_set_id
                        new_ids = list(
                            dict.fromkeys(
                                existing_cs.claim_ids
                                + [c.claim_id for c in colliding]
                                + [incoming.claim_id]
                            )
                        )
                        self._conflicts[conflict_set_id] = existing_cs.model_copy(
                            update={"claim_ids": new_ids}
                        )
                    else:
                        conflict_set_id = f"conflict-{uuid.uuid4().hex[:12]}"
                        member_ids = list(
                            dict.fromkeys([c.claim_id for c in colliding] + [incoming.claim_id])
                        )
                        self._conflicts[conflict_set_id] = ConflictSet(
                            conflict_set_id=conflict_set_id,
                            subject_id=incoming.subject_id,
                            predicate=incoming.predicate,
                            context_purpose=incoming.context_purpose,
                            scope_type=incoming.scope_type,
                            scope_id=incoming.scope_id,
                            status="open",
                            claim_ids=member_ids,
                            created_at=now,
                            summary=f"{len(member_ids)}-way conflict on {target.display_name} ({incoming.predicate})",
                        )
                else:
                    self._claims[incoming.claim_id] = incoming

            self._write_claim_evidence_sidecar(incoming)
            self._write_entity_markdown(target)
            new_epoch = self._bump_data_epoch(f"write_claim:{incoming.claim_id}", eff.user_id)
            self._persist_meta_sync()

        await self._register_record_terms("claims", incoming, created_by=eff.user_id)
        sanitized = self.policy.sanitize_claim_for_principal(eff, incoming) or incoming
        return ClaimWriteResult(
            claim=sanitized,
            conflict_detected=conflict_detected,
            conflict_set_id=conflict_set_id,
            superseded_claim_id=superseded_claim_id,
            data_epoch=new_epoch,
            snapshot_scheduled=payload.force_supersede and incoming.epistemic_status == "approved_fact",
        )

    async def update_claim(
        self, claim_id: str, patch: ClaimUpdateInput, *, principal: Principal
    ) -> ClaimWriteResult:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            raise AuthenticationError("Revoked principal cannot update claims")

        async with self._lock:
            existing = self._claims.get(claim_id)
            if existing is None or not self.policy.can_read_claim(eff, existing):
                raise NotFoundError(f"Claim not found: {claim_id}")
            target = self._entities.get(existing.subject_id)
            if target is None:
                raise NotFoundError(f"Subject entity not found: {existing.subject_id}")

            new_status = patch.epistemic_status or existing.epistemic_status
            new_mandatory = patch.is_mandatory if patch.is_mandatory is not None else existing.is_mandatory
            self.policy.authorize_write(
                eff,
                target_entity=target,
                scope_type=existing.scope_type,
                epistemic_status=new_status,
                force_supersede=patch.force_supersede,
                is_mandatory=new_mandatory,
            )
            check_toctou_epoch(patch.expected_data_epoch, self._epochs.data_epoch)
            if existing.superseded_at is not None:
                raise OptimisticConcurrencyError(f"Claim {claim_id} was already superseded")

            now = utc_now()
            new_claim_id = f"clm-{uuid.uuid4().hex[:12]}"
            updated = existing.model_copy(
                deep=True,
                update={
                    "claim_id": new_claim_id,
                    "object_value": patch.object_value if patch.object_value is not None else existing.object_value,
                    "epistemic_status": new_status,
                    "valid_from": ensure_utc(patch.valid_from) if patch.valid_from is not None else existing.valid_from,
                    "valid_to": ensure_utc(patch.valid_to) if patch.valid_to is not None else existing.valid_to,
                    "is_mandatory": new_mandatory,
                    "facets": patch.facets if patch.facets is not None else existing.facets,
                    "recorded_at": now,
                    "superseded_at": None,
                    "superseded_by": None,
                    "created_by": eff.user_id,
                },
            )
            self._claims[existing.claim_id] = existing.model_copy(
                update={"superseded_at": now, "superseded_by": new_claim_id}
            )
            self._claims[new_claim_id] = updated
            self._write_claim_evidence_sidecar(updated)
            new_epoch = self._bump_data_epoch(f"update_claim:{existing.claim_id}->{new_claim_id}", eff.user_id)
            self._persist_meta_sync()

        await self._register_record_terms("claims", updated, created_by=eff.user_id)
        sanitized = self.policy.sanitize_claim_for_principal(eff, updated) or updated
        return ClaimWriteResult(
            claim=sanitized,
            conflict_detected=False,
            superseded_claim_id=existing.claim_id,
            data_epoch=new_epoch,
            snapshot_scheduled=updated.epistemic_status == "approved_fact",
        )

    async def get_claim_raw(self, claim_id: str) -> Claim | None:
        """Unsanitized claim for internal bookkeeping (version history). Never return it to a caller."""
        await self._ensure_init()
        clm = self._claims.get(claim_id)
        return clone_model(clm) if clm is not None else None

    async def get_entity_raw(self, entity_id_or_key: str) -> Entity | None:
        """Unfiltered entity for internal policy decisions (steward groups). Never return it to a caller."""
        await self._ensure_init()
        ent = self._resolve_entity_raw(entity_id_or_key)
        return clone_model(ent) if ent is not None else None

    async def get_claim(self, claim_id: str, *, principal: Principal) -> Claim | None:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            return None
        clm = self._claims.get(claim_id)
        if clm is None:
            return None
        return self.policy.sanitize_claim_for_principal(eff, clm)

    async def query_claims(
        self,
        *,
        principal: Principal,
        subject_ids: Sequence[str] | None = None,
        object_entity_ids: Sequence[str] | None = None,
        predicates: Sequence[str] | None = None,
        context_purpose: str | None = None,
        scope_types: Sequence[str] | None = None,
        scope_id: str | None = None,
        epistemic_statuses: Sequence[str] | None = None,
        as_of: datetime | None = None,
        recorded_as_of: datetime | None = None,
        include_superseded: bool = False,
    ) -> list[Claim]:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            return []
        subj_set = set(subject_ids) if subject_ids is not None else None
        obj_set = set(object_entity_ids) if object_entity_ids is not None else None
        pred_set = set(predicates) if predicates is not None else None
        scope_set = set(scope_types) if scope_types is not None else None
        ep_set = set(epistemic_statuses) if epistemic_statuses is not None else None

        results: list[Claim] = []
        for raw in self._claims.values():
            if subj_set is not None and raw.subject_id not in subj_set:
                continue
            if obj_set is not None and raw.object_entity_id not in obj_set:
                continue
            if pred_set is not None and raw.predicate not in pred_set:
                continue
            if context_purpose is not None and raw.context_purpose != context_purpose:
                continue
            if scope_set is not None and raw.scope_type not in scope_set:
                continue
            if scope_id is not None and raw.scope_id != scope_id:
                continue
            if ep_set is not None and raw.epistemic_status not in ep_set:
                continue
            if not is_claim_visible(
                raw, as_of=as_of, recorded_as_of=recorded_as_of, include_superseded=include_superseded
            ):
                continue
            san = self.policy.sanitize_claim_for_principal(eff, raw)
            if san is not None:
                results.append(san)
        results.sort(key=lambda c: (-c.recorded_at.timestamp(), c.claim_id))
        return results

    async def list_conflicts(
        self,
        *,
        principal: Principal,
        status: str = "open",
        subject_id: str | None = None,
    ) -> list[ConflictSet]:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            return []
        out: list[ConflictSet] = []
        for cs in self._conflicts.values():
            if status != "all" and cs.status != status:
                continue
            if subject_id is not None and cs.subject_id != subject_id:
                continue
            target = self._entities.get(cs.subject_id)
            if target is None or not self.policy.can_read_entity(eff, target):
                continue
            vis_claims: list[Claim] = []
            for cid in cs.claim_ids:
                clm = self._claims.get(cid)
                if clm is None:
                    continue
                san = self.policy.sanitize_claim_for_principal(eff, clm)
                if san is not None:
                    vis_claims.append(san)
            if not vis_claims:
                continue
            out.append(
                cs.model_copy(
                    deep=True,
                    update={"claim_ids": [c.claim_id for c in vis_claims], "claims": vis_claims},
                )
            )
        out.sort(key=lambda cs: (-cs.created_at.timestamp(), cs.conflict_set_id))
        return out

    async def resolve_conflict(
        self, resolution: ConflictResolutionInput, *, principal: Principal
    ) -> ConflictSet:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            raise AuthenticationError("Revoked principal cannot resolve conflicts")

        async with self._lock:
            cs = self._conflicts.get(resolution.conflict_set_id)
            if cs is None:
                raise NotFoundError(f"ConflictSet not found: {resolution.conflict_set_id}")
            target = self._entities.get(cs.subject_id)
            if target is None or not self.policy.can_read_entity(eff, target):
                raise NotFoundError(f"ConflictSet not found: {resolution.conflict_set_id}")

            self.policy.authorize_write(
                eff,
                target_entity=target,
                scope_type=cs.scope_type,
                epistemic_status="approved_fact",
                force_supersede=True,
                resolving_conflict=True,
            )
            check_toctou_epoch(resolution.expected_data_epoch, self._epochs.data_epoch)
            if cs.status == "resolved":
                raise OptimisticConcurrencyError(
                    f"ConflictSet {resolution.conflict_set_id} is already resolved"
                )

            member_claims = {cid: self._claims[cid] for cid in cs.claim_ids if cid in self._claims}
            if not resolution.winning_claim_id and not resolution.replacement_object_value:
                raise ValidationError(
                    "Provide either winning_claim_id or replacement_object_value to resolve conflict"
                )

            now = utc_now()
            if resolution.winning_claim_id:
                if resolution.winning_claim_id not in member_claims:
                    raise ValidationError(
                        f"winning_claim_id {resolution.winning_claim_id} is not a member of {resolution.conflict_set_id}"
                    )
                winning_base = member_claims[resolution.winning_claim_id]
            else:
                winning_base = next(iter(member_claims.values()))

            needs_new_version = (
                resolution.replacement_object_value is not None
                or winning_base.epistemic_status != "approved_fact"
                or resolution.valid_from is not None
            )
            new_member_ids = list(cs.claim_ids)

            if needs_new_version:
                approved_id = f"clm-{uuid.uuid4().hex[:12]}"
                approved_claim = winning_base.model_copy(
                    deep=True,
                    update={
                        "claim_id": approved_id,
                        "object_value": (
                            resolution.replacement_object_value
                            if resolution.replacement_object_value is not None
                            else winning_base.object_value
                        ),
                        "epistemic_status": "approved_fact",
                        "valid_from": (
                            ensure_utc(resolution.valid_from)
                            if resolution.valid_from is not None
                            else winning_base.valid_from
                        ),
                        "recorded_at": now,
                        "superseded_at": None,
                        "superseded_by": None,
                        "created_by": eff.user_id,
                    },
                )
                for cid, clm in member_claims.items():
                    if clm.superseded_at is None:
                        upd: dict[str, Any] = {"superseded_at": now, "superseded_by": approved_id}
                        if resolution.close_prior_valid_to is not None and clm.epistemic_status == "approved_fact":
                            upd["valid_to"] = ensure_utc(resolution.close_prior_valid_to)
                        self._claims[cid] = clm.model_copy(update=upd)
                self._claims[approved_id] = approved_claim
                self._write_claim_evidence_sidecar(approved_claim)
                new_member_ids.append(approved_id)
                final_winner_id = approved_id
            else:
                final_winner_id = winning_base.claim_id
                for cid, clm in member_claims.items():
                    if cid != final_winner_id and clm.superseded_at is None:
                        self._claims[cid] = clm.model_copy(
                            update={"superseded_at": now, "superseded_by": final_winner_id}
                        )

            resolved = cs.model_copy(
                deep=True,
                update={
                    "status": "resolved",
                    "claim_ids": new_member_ids,
                    "winning_claim_id": final_winner_id,
                    "resolution_rationale": resolution.rationale,
                    "resolved_by": eff.user_id,
                    "resolved_at": now,
                    "claims": [],
                },
            )
            self._conflicts[resolved.conflict_set_id] = resolved
            self._bump_data_epoch(f"resolve_conflict:{resolved.conflict_set_id}", eff.user_id)
            self._persist_meta_sync()

            sanitized_claims = [
                san
                for cid in new_member_ids
                if cid in self._claims
                and (san := self.policy.sanitize_claim_for_principal(eff, self._claims[cid])) is not None
            ]
            return resolved.model_copy(
                update={"claim_ids": [c.claim_id for c in sanitized_claims], "claims": sanitized_claims}
            )

    # ------------------------------------------------------------------
    # Plane 4: Topics, Knowledge Factory, Curator Patches & Personal Memory
    # ------------------------------------------------------------------

    async def upsert_topic(self, topic: Topic) -> Topic:
        await self._ensure_init()
        async with self._lock:
            self._topics[topic.topic_id] = clone_model(topic)
            if topic.broader_topic_id and topic.broader_topic_id in self._topics:
                parent = self._topics[topic.broader_topic_id]
                if topic.topic_id not in parent.narrower_topic_ids:
                    parent.narrower_topic_ids.append(topic.topic_id)
            self._bump_data_epoch(f"upsert_topic:{topic.topic_id}", "system")
            self._persist_meta_sync()
        return topic

    async def update_topic(
        self, topic_id: str, patch: TopicPatchInput, *, principal: Principal
    ) -> Topic:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            raise AuthenticationError("Revoked principal cannot update topics")
        async with self._lock:
            updated, modified_list = apply_topic_patch(
                self._topics,
                topic_id,
                pref_label=patch.pref_label,
                alt_labels=patch.alt_labels,
                broader_topic_id=patch.broader_topic_id,
                related_topic_ids=patch.related_topic_ids,
                merged_into_topic_id=patch.merged_into_topic_id,
                status=patch.status,
                topic_kind=patch.topic_kind,
            )
            for t in modified_list:
                self._topics[t.topic_id] = t
            self._bump_data_epoch(f"update_topic:{topic_id}", eff.user_id)
            self._persist_meta_sync()
        return updated

    async def list_topics(
        self, *, principal: Principal, include_archived: bool = False
    ) -> list[Topic]:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            return []
        return [
            clone_model(t)
            for t in sorted(self._topics.values(), key=lambda x: x.slug)
            if include_archived or t.status != "archived"
        ]

    async def attach_topic(self, attachment: TopicAttachment) -> TopicAttachment:
        await self._ensure_init()
        async with self._lock:
            self._attachments[attachment.attachment_id] = clone_model(attachment)
            if attachment.target_id in self._entities:
                self._write_entity_markdown(self._entities[attachment.target_id])
            self._persist_meta_sync()
        return attachment

    async def list_topic_attachments(
        self,
        *,
        principal: Principal,
        topic_id: str | None = None,
        target_id: str | None = None,
    ) -> list[TopicAttachment]:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            return []
        out: list[TopicAttachment] = []
        for att in self._attachments.values():
            if topic_id and att.topic_id != topic_id:
                continue
            if target_id and att.target_id != target_id:
                continue
            if self.policy.can_read_record(
                eff, wall_side=att.wall_side, read_acl_groups=att.read_acl_groups
            ):
                out.append(clone_model(att))
        return out

    async def upsert_knowledge_item(self, item: KnowledgeItem) -> KnowledgeItem:
        await self._ensure_init()
        async with self._lock:
            self._knowledge_items[item.item_id] = clone_model(item)
            self._write_knowledge_markdown(item)
            self._persist_meta_sync()
        return item

    async def list_knowledge_items(
        self, *, principal: Principal, kind: str | None = None
    ) -> list[KnowledgeItem]:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            return []
        out: list[KnowledgeItem] = []
        for ki in sorted(self._knowledge_items.values(), key=lambda x: x.item_id):
            if kind and ki.knowledge_kind != kind:
                continue
            if self.policy.can_read_record(
                eff, wall_side=ki.wall_side, read_acl_groups=ki.read_acl_groups
            ):
                out.append(clone_model(ki))
        return out

    async def list_curator_patches(
        self, *, principal: Principal, status: str = "pending_steward_review"
    ) -> list[CuratorDraftPatch]:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            return []
        out: list[CuratorDraftPatch] = []
        for p in self._curator_patches.values():
            if status != "all" and p.status != status:
                continue
            if self.policy.can_read_record(
                eff, wall_side=p.wall_side, read_acl_groups=p.read_acl_groups
            ):
                out.append(clone_model(p))
        return out

    async def save_curator_patch(self, patch: CuratorDraftPatch) -> CuratorDraftPatch:
        await self._ensure_init()
        async with self._lock:
            self._curator_patches[patch.patch_id] = clone_model(patch)
            self._persist_meta_sync()
        return patch

    # ------------------------------------------------------------------
    # Personal Memory & Watchlist (Fail-Closed `_scopes/user/<user_id>/`)
    # ------------------------------------------------------------------

    async def seed_memory_raw(self, memory: MemoryRecord) -> MemoryRecord:
        await self._ensure_init()
        async with self._lock:
            self._memories[memory.memory_id] = clone_model(memory)
            self._write_memory_markdown(memory)
            self._persist_meta_sync()
        return memory

    async def seed_watchlist_raw(self, wl: WatchlistRecord) -> WatchlistRecord:
        await self._ensure_init()
        async with self._lock:
            self._watchlists[wl.user_id] = wl.model_copy(deep=True, update={"alerts": []})
            self._write_watchlist_file(wl)
            self._persist_meta_sync()
        return wl

    async def upsert_memory(
        self, record: MemoryUpsertInput, *, principal: Principal
    ) -> MemoryRecord:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        self.policy.authorize_memory_access(eff)

        now = utc_now()
        async with self._lock:
            existing: MemoryRecord | None = None
            if record.memory_id:
                cand = self._memories.get(record.memory_id)
                if cand is not None:
                    if cand.user_id != eff.user_id:
                        raise NotFoundError(f"Memory not found: {record.memory_id}")
                    existing = cand
            else:
                existing = next(
                    (
                        m
                        for m in self._memories.values()
                        if m.user_id == eff.user_id
                        and m.scope_type == record.scope_type
                        and m.key == record.key
                    ),
                    None,
                )
            mem_id = existing.memory_id if existing else (record.memory_id or f"mem-{uuid.uuid4().hex[:12]}")
            mem = MemoryRecord(
                memory_id=mem_id,
                user_id=eff.user_id,
                scope_type=record.scope_type,
                skill_id=record.skill_id,
                key=record.key,
                content=record.content,
                tags=list(record.tags),
                linked_entity_ids=list(record.linked_entity_ids),
                linked_topic_ids=list(record.linked_topic_ids),
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )
            self._memories[mem.memory_id] = mem
            self._write_memory_markdown(mem)
            self._bump_data_epoch(f"upsert_memory:{mem.memory_id}", eff.user_id)
            self._persist_meta_sync()
        return clone_model(mem)

    async def get_memory(self, memory_id: str, *, principal: Principal) -> MemoryRecord | None:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            raise AuthenticationError("Revoked principal cannot access personal memory")
        if not self.policy.is_memory_allowed(eff):
            return None
        mem = self._memories.get(memory_id)
        if mem is None or mem.user_id != eff.user_id:
            return None
        return clone_model(mem)

    async def list_memories(
        self, *, principal: Principal, skill_id: str | None = None
    ) -> list[MemoryRecord]:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            raise AuthenticationError("Revoked principal cannot access personal memory")
        if not self.policy.is_memory_allowed(eff):
            return []
        out = [
            clone_model(m)
            for m in self._memories.values()
            if m.user_id == eff.user_id
            and (skill_id is None or m.skill_id == skill_id or m.scope_type == "user")
        ]
        out.sort(key=lambda m: (-m.updated_at.timestamp(), m.memory_id))
        return out

    async def delete_memory(self, memory_id: str, *, principal: Principal) -> bool:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        self.policy.authorize_memory_access(eff)
        async with self._lock:
            mem = self._memories.get(memory_id)
            if mem is None or mem.user_id != eff.user_id:
                return False
            del self._memories[memory_id]
            note_path = self._user_scope_dir(eff.user_id) / "memories" / f"{_safe_slug(memory_id)}.md"
            if note_path.exists():
                note_path.unlink()
            self._bump_data_epoch(f"delete_memory:{memory_id}", eff.user_id)
            self._persist_meta_sync()
            return True

    async def get_watchlist(self, *, principal: Principal) -> WatchlistRecord:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            raise AuthenticationError("Revoked principal cannot access watchlist")
        if not self.policy.is_memory_allowed(eff):
            return WatchlistRecord(user_id=eff.user_id)
        stored = self._watchlists.get(eff.user_id)
        wl = clone_model(stored) if stored else WatchlistRecord(user_id=eff.user_id)
        auth_entities = {e.entity_id: e for e in await self.list_entities(principal=eff, limit=None)}
        auth_claims = await self.query_claims(principal=eff, include_superseded=False)
        auth_conflicts = await self.list_conflicts(principal=eff, status="open")
        auth_attachments = await self.list_topic_attachments(principal=eff)
        wl.alerts = compute_watchlist_alerts(
            wl,
            principal=eff,
            policy=self.policy,
            entities=auth_entities,
            claims=auth_claims,
            conflicts=auth_conflicts,
            attachments=auth_attachments,
        )
        return wl

    async def put_watchlist(
        self, watchlist: WatchlistUpdateInput, *, principal: Principal
    ) -> WatchlistRecord:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        self.policy.authorize_memory_access(eff)
        async with self._lock:
            wl = WatchlistRecord(
                user_id=eff.user_id,
                entity_ids=list(dict.fromkeys(watchlist.entity_ids)),
                topic_ids=list(dict.fromkeys(watchlist.topic_ids)),
                source_ids=list(dict.fromkeys(watchlist.source_ids)),
                updated_at=utc_now(),
            )
            self._watchlists[eff.user_id] = wl
            self._write_watchlist_file(wl)
            self._bump_data_epoch(f"put_watchlist:{eff.user_id}", eff.user_id)
            self._persist_meta_sync()
        return await self.get_watchlist(principal=eff)

    # ------------------------------------------------------------------
    # Receipts, Zero-LLM SHA Staleness Diff & Export/Import
    # ------------------------------------------------------------------

    async def record_receipt(self, receipt: ContextAssemblyReceipt) -> None:
        await self._ensure_init()
        async with self._lock:
            self._receipts[receipt.receipt_id] = clone_model(receipt)
            self._persist_meta_sync()

    async def get_receipt(
        self, receipt_id: str, *, principal: Principal
    ) -> ContextAssemblyReceipt | None:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            return None
        rcpt = self._receipts.get(receipt_id)
        if rcpt is None:
            return None
        if rcpt.principal_id != eff.user_id and eff.wall_side != WallSide.above_the_wall:
            return None
        return clone_model(rcpt)

    async def diff_staleness(self, *, wall_side: str | None = None) -> StalenessReport:
        """Zero-LLM SHA-256 staleness diff reading claim-evidence `.sha256.json` sidecars & knowledge items."""
        await self._ensure_init()
        # Check claim-evidence sidecars on disk first, plus active claims & knowledge items
        report = compute_sha_staleness(
            claims=self._claims.values(),
            knowledge_items=self._knowledge_items.values(),
            base_dir=self.repo_root,
            extra_search_dirs=(self.vault_dir,),
            wall_side=wall_side,
        )
        seen_keys = {(s.claim_id, s.evidence_path) for s in report.stale_items}
        if self.claims_sidecar_dir.is_dir():
            for sc in sorted(self.claims_sidecar_dir.glob("*.evidence.sha256.json")):
                meta = json.loads(sc.read_text(encoding="utf-8"))
                if wall_side is not None and meta.get("wall_side") != wall_side:
                    continue
                ev_path = meta.get("evidence_path", "")
                exp_sha = meta.get("expected_sha256", "")
                cid = meta.get("claim_id")
                if not ev_path or not exp_sha or "://" in ev_path:
                    continue
                if (cid, ev_path) in seen_keys:
                    continue
                target = self.repo_root / ev_path
                if not target.is_file():
                    target = self.vault_dir / ev_path
                if not target.is_file():
                    report.stale_items.append(
                        StalenessItem(
                            claim_id=cid,
                            evidence_path=to_posix_path(ev_path),
                            expected_sha256=exp_sha,
                            actual_sha256=None,
                            reason="missing_source",
                            wall_side=WallSide(meta.get("wall_side", "public_side")),
                        )
                    )
                else:
                    actual_sha = lf_sha256_file(target)
                    if actual_sha != exp_sha:
                        report.stale_items.append(
                            StalenessItem(
                                claim_id=cid,
                                evidence_path=to_posix_path(ev_path),
                                expected_sha256=exp_sha,
                                actual_sha256=actual_sha,
                                reason="hash_mismatch",
                                wall_side=WallSide(meta.get("wall_side", "public_side")),
                            )
                        )
        return report

    async def export_state(self) -> dict[str, Any]:
        await self._ensure_init()
        return {
            "epochs": clone_model(self._epochs),
            "entities": [clone_model(e) for e in self._entities.values()],
            "identifiers": [clone_model(i) for i in self._identifiers.values()],
            "lineage_events": [clone_model(ev) for ev in self._lineage.values()],
            "positions": [clone_model(p) for p in self._positions.values()],
            "coverages": [clone_model(c) for c in self._coverages.values()],
            "skill_bindings": [clone_model(sb) for sb in self._skill_bindings.values()],
            "claims": [clone_model(c) for c in self._claims.values()],
            "conflict_sets": [cs.model_copy(deep=True, update={"claims": []}) for cs in self._conflicts.values()],
            "topics": [clone_model(t) for t in self._topics.values()],
            "topic_attachments": [clone_model(a) for a in self._attachments.values()],
            "knowledge_items": [clone_model(k) for k in self._knowledge_items.values()],
            "curator_patches": [clone_model(p) for p in self._curator_patches.values()],
            "memories": [clone_model(m) for m in self._memories.values()],
            "watchlists": [w.model_copy(deep=True, update={"alerts": []}) for w in self._watchlists.values()],
            "revoked_principals": [(uid, meta.get("reason", "revoked")) for uid, meta in self._revoked.items()],
            "receipts": [clone_model(r) for r in self._receipts.values()],
            "vocab_terms": [clone_model(t) for t in self._vocab.values()],
        }

    async def import_state(self, state: dict[str, Any], *, reset_epochs: bool = False) -> None:
        await self._ensure_init()
        async with self._lock:
            self._entities = {e.entity_id: clone_model(e) for e in state.get("entities", [])}
            self._key_to_id = {e.canonical_key: e.entity_id for e in self._entities.values()}
            self._identifiers = {i.id: clone_model(i) for i in state.get("identifiers", [])}
            self._lineage = {ev.event_id: clone_model(ev) for ev in state.get("lineage_events", [])}
            self._positions = {p.position_id: clone_model(p) for p in state.get("positions", [])}
            self._coverages = {c.coverage_id: clone_model(c) for c in state.get("coverages", [])}
            self._skill_bindings = {
                sb.binding_id: clone_model(sb) for sb in state.get("skill_bindings", [])
            }
            self._claims = {c.claim_id: clone_model(c) for c in state.get("claims", [])}
            self._conflicts = {
                cs.conflict_set_id: cs.model_copy(deep=True, update={"claims": []})
                for cs in state.get("conflict_sets", [])
            }
            self._topics = {t.topic_id: clone_model(t) for t in state.get("topics", [])}
            self._attachments = {
                a.attachment_id: clone_model(a) for a in state.get("topic_attachments", [])
            }
            self._knowledge_items = {
                k.item_id: clone_model(k) for k in state.get("knowledge_items", [])
            }
            self._curator_patches = {
                p.patch_id: clone_model(p) for p in state.get("curator_patches", [])
            }
            self._memories = {m.memory_id: clone_model(m) for m in state.get("memories", [])}
            self._watchlists = {
                w.user_id: w.model_copy(deep=True, update={"alerts": []})
                for w in state.get("watchlists", [])
            }
            self._revoked = {
                uid: {"reason": reason, "actor": "system:import", "revoked_at": format_utc(utc_now()) or ""}
                for uid, reason in state.get("revoked_principals", [])
            }
            self._receipts = {r.receipt_id: clone_model(r) for r in state.get("receipts", [])}
            epochs: PolicyEpochState | None = state.get("epochs")
            if epochs is not None and not reset_epochs:
                self._epochs = clone_model(epochs)
            elif reset_epochs:
                self._epochs = PolicyEpochState(
                    data_epoch=1, policy_epoch=1, updated_at=utc_now(), last_reason="seed", last_actor="system:seed"
                )

            # Materialize OKF files, sidecars, user-scoped notes, and _index.md deterministically
            for ent in self._entities.values():
                self._write_entity_markdown(ent)
            for ki in self._knowledge_items.values():
                self._write_knowledge_markdown(ki)
            for clm in self._claims.values():
                self._write_claim_evidence_sidecar(clm)
            for mem in self._memories.values():
                self._write_memory_markdown(mem)
            for wl in self._watchlists.values():
                self._write_watchlist_file(wl)
            self.materialize_indices()
            self._persist_meta_sync()
        await self._import_vocab_state(state)
