"""Async non-blocking, ACL/wall-partitioned SQLite+Zstd SnapshotCompiler (ADR-002 & ADR-003)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

from context_core.compression import (
    atomic_write_bytes,
    atomic_write_text,
    format_utc,
    to_posix_path,
    utc_now,
    zstd_compress,
)
from context_core.exceptions import NotFoundError
from context_core.models import (
    REDACTED_EVIDENCE_LABEL,
    Claim,
    ConflictSet,
    Entity,
    EntityIdentifier,
    PolicyEpochState,
    SkillBinding,
    SnapshotManifest,
    WallSide,
)
from context_core.policy import PolicyEngine, sanitize_uri_secrets
from context_core.ports import StorePort


def _partition_slug(acl_tier: str, wall_side: WallSide | str) -> str:
    ws = wall_side.value if isinstance(wall_side, WallSide) else str(wall_side)
    safe_tier = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in acl_tier)
    return f"{safe_tier}__{ws}"


def _build_snapshot_sqlite_zst_sync(
    *,
    version_id: int,
    acl_tier: str,
    wall_side: WallSide,
    epochs: PolicyEpochState,
    entities: list[Entity],
    identifiers: list[EntityIdentifier],
    claims: list[Claim],
    skill_bindings: list[SkillBinding],
    conflicts: list[ConflictSet],
    policy: PolicyEngine,
    zstd_level: int = 3,
) -> tuple[bytes, str, str, int, int]:
    """Build an in-memory SQLite snapshot image, serialize(), and Zstd-compress with ZERO temp files."""
    compiled_at = format_utc(utc_now()) or ""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(
            """
            CREATE TABLE snapshot_meta (
                version_id INTEGER NOT NULL,
                acl_tier TEXT NOT NULL,
                wall_side TEXT NOT NULL,
                data_epoch INTEGER NOT NULL,
                policy_epoch INTEGER NOT NULL,
                compiled_at TEXT NOT NULL
            );

            CREATE TABLE entities (
                entity_id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL,
                canonical_key TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                aliases_json TEXT NOT NULL,
                description TEXT NOT NULL,
                owner_group TEXT NOT NULL,
                wall_side TEXT NOT NULL,
                read_acl_groups_json TEXT NOT NULL,
                write_acl_groups_json TEXT NOT NULL,
                attributes_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX idx_snap_entities_key ON entities(canonical_key);
            CREATE INDEX idx_snap_entities_type ON entities(entity_type);

            CREATE TABLE entity_identifiers (
                id TEXT PRIMARY KEY,
                entity_id TEXT NOT NULL,
                scheme TEXT NOT NULL,
                identifier_value TEXT NOT NULL,
                authority TEXT NOT NULL,
                is_primary INTEGER NOT NULL,
                valid_from TEXT NOT NULL,
                valid_to TEXT,
                recorded_at TEXT NOT NULL,
                superseded_at TEXT
            );
            CREATE INDEX idx_snap_ident_ent ON entity_identifiers(entity_id);
            CREATE INDEX idx_snap_ident_val ON entity_identifiers(identifier_value);

            CREATE TABLE claims (
                claim_id TEXT PRIMARY KEY,
                subject_id TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object_value TEXT NOT NULL,
                object_entity_id TEXT,
                context_purpose TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                epistemic_status TEXT NOT NULL,
                kind TEXT,
                is_mandatory INTEGER NOT NULL,
                facets_json TEXT NOT NULL,
                source_system TEXT NOT NULL,
                evidence_uri TEXT,
                content_sha256 TEXT,
                span_locator TEXT,
                excerpt TEXT,
                wall_side TEXT NOT NULL,
                read_acl_groups_json TEXT NOT NULL,
                write_acl_groups_json TEXT NOT NULL,
                evidence_acl_groups_json TEXT NOT NULL,
                evidence_redacted INTEGER NOT NULL,
                evidence_redaction_label TEXT,
                evidence_request_group TEXT,
                valid_from TEXT NOT NULL,
                valid_to TEXT,
                recorded_at TEXT NOT NULL,
                superseded_at TEXT,
                superseded_by TEXT,
                created_by TEXT NOT NULL
            );
            CREATE INDEX idx_snap_claims_lookup ON claims(subject_id, predicate, context_purpose);
            CREATE INDEX idx_snap_claims_bitemp ON claims(valid_from, recorded_at);

            CREATE TABLE skill_bindings (
                binding_id TEXT PRIMARY KEY,
                skill_entity_id TEXT NOT NULL,
                target_entity_id TEXT NOT NULL,
                mapping_purpose TEXT NOT NULL,
                read_mode TEXT NOT NULL,
                required_predicates_json TEXT NOT NULL,
                active INTEGER NOT NULL
            );

            CREATE TABLE conflict_sets (
                conflict_set_id TEXT PRIMARY KEY,
                subject_id TEXT NOT NULL,
                predicate TEXT NOT NULL,
                context_purpose TEXT NOT NULL,
                scope_type TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                status TEXT NOT NULL,
                winning_claim_id TEXT,
                resolution_rationale TEXT,
                resolved_by TEXT,
                resolved_at TEXT,
                created_at TEXT NOT NULL,
                summary TEXT NOT NULL
            );

            CREATE TABLE conflict_set_members (
                conflict_set_id TEXT NOT NULL,
                claim_id TEXT NOT NULL,
                PRIMARY KEY (conflict_set_id, claim_id)
            );
            """
        )

        conn.execute(
            "INSERT INTO snapshot_meta VALUES (?, ?, ?, ?, ?, ?)",
            (version_id, acl_tier, wall_side.value, epochs.data_epoch, epochs.policy_epoch, compiled_at),
        )

        eligible_entities = [
            e
            for e in sorted(entities, key=lambda x: x.entity_id)
            if policy.entity_eligible_for_partition(e, acl_tier=acl_tier, wall_side=wall_side)
        ]
        eligible_eids = {e.entity_id for e in eligible_entities}

        conn.executemany(
            "INSERT INTO entities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    e.entity_id,
                    e.entity_type,
                    e.canonical_key,
                    e.display_name,
                    json.dumps(e.aliases),
                    e.description,
                    e.owner_group,
                    e.wall_side.value,
                    json.dumps(e.read_acl_groups),
                    json.dumps(e.write_acl_groups),
                    json.dumps(e.attributes),
                    e.status,
                    format_utc(e.created_at),
                    format_utc(e.updated_at),
                )
                for e in eligible_entities
            ],
        )

        eligible_idents = [i for i in identifiers if i.entity_id in eligible_eids]
        conn.executemany(
            "INSERT INTO entity_identifiers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    i.id,
                    i.entity_id,
                    i.scheme,
                    i.identifier_value,
                    i.authority,
                    1 if i.is_primary else 0,
                    format_utc(i.valid_from),
                    format_utc(i.valid_to),
                    format_utc(i.recorded_at),
                    format_utc(i.superseded_at),
                )
                for i in eligible_idents
            ],
        )

        # Strip unauthorized claims AND redact restricted evidence BEFORE writing any row to SQLite!
        eligible_claims: list[Claim] = []
        claim_rows: list[tuple[Any, ...]] = []
        for c in sorted(claims, key=lambda x: x.claim_id):
            if c.subject_id not in eligible_eids:
                continue
            if not policy.claim_eligible_for_partition(c, acl_tier=acl_tier, wall_side=wall_side):
                continue
            eligible_claims.append(c)
            ev_allowed = policy.evidence_allowed_in_partition(c, acl_tier=acl_tier)
            if ev_allowed:
                ev_uri = sanitize_uri_secrets(c.provenance.evidence_uri)
                ev_span = c.provenance.span_locator
                ev_excerpt = c.provenance.excerpt
                ev_redacted = 0
                ev_label = None
                ev_req_group = None
            else:
                ev_uri = None
                ev_span = None
                ev_excerpt = None
                ev_redacted = 1
                ev_label = REDACTED_EVIDENCE_LABEL
                ev_req_group = c.acl.evidence_acl_groups[0] if c.acl.evidence_acl_groups else "compliance-oversight"

            claim_rows.append(
                (
                    c.claim_id,
                    c.subject_id,
                    c.predicate,
                    c.object_value,
                    c.object_entity_id if c.object_entity_id in eligible_eids else None,
                    c.context_purpose,
                    c.scope_type,
                    c.scope_id,
                    c.channel,
                    c.epistemic_status,
                    c.kind,
                    1 if c.is_mandatory else 0,
                    json.dumps(c.facets.model_dump(mode="json")),
                    c.provenance.source_system,
                    ev_uri,
                    c.provenance.content_sha256,
                    ev_span,
                    ev_excerpt,
                    c.acl.wall_side.value,
                    json.dumps(c.acl.read_acl_groups),
                    json.dumps(c.acl.write_acl_groups),
                    json.dumps(c.acl.evidence_acl_groups),
                    ev_redacted,
                    ev_label,
                    ev_req_group,
                    format_utc(c.valid_from),
                    format_utc(c.valid_to),
                    format_utc(c.recorded_at),
                    format_utc(c.superseded_at),
                    c.superseded_by,
                    c.created_by,
                )
            )
        conn.executemany(
            "INSERT INTO claims VALUES (" + ",".join("?" * 31) + ")",
            claim_rows,
        )
        eligible_cids = {c.claim_id for c in eligible_claims}

        eligible_bindings = [
            sb
            for sb in skill_bindings
            if sb.skill_entity_id in eligible_eids and sb.target_entity_id in eligible_eids
        ]
        conn.executemany(
            "INSERT INTO skill_bindings VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    sb.binding_id,
                    sb.skill_entity_id,
                    sb.target_entity_id,
                    sb.mapping_purpose,
                    sb.read_mode,
                    json.dumps(sb.required_predicates),
                    1 if sb.active else 0,
                )
                for sb in eligible_bindings
            ],
        )

        for cs in conflicts:
            if cs.subject_id not in eligible_eids:
                continue
            member_ids = [cid for cid in cs.claim_ids if cid in eligible_cids]
            if not member_ids:
                continue
            conn.execute(
                "INSERT INTO conflict_sets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cs.conflict_set_id,
                    cs.subject_id,
                    cs.predicate,
                    cs.context_purpose,
                    cs.scope_type,
                    cs.scope_id,
                    cs.status,
                    cs.winning_claim_id,
                    cs.resolution_rationale,
                    cs.resolved_by,
                    format_utc(cs.resolved_at),
                    format_utc(cs.created_at),
                    cs.summary,
                ),
            )
            conn.executemany(
                "INSERT INTO conflict_set_members VALUES (?, ?)",
                [(cs.conflict_set_id, cid) for cid in member_ids],
            )

        conn.commit()
        raw_sqlite = conn.serialize()
    finally:
        conn.close()

    compressed = zstd_compress(raw_sqlite, level=zstd_level)
    sha256_hex = hashlib.sha256(compressed).hexdigest()
    etag = f'"{version_id}-{epochs.data_epoch}-{epochs.policy_epoch}-{sha256_hex[:12]}"'
    return compressed, sha256_hex, etag, len(eligible_claims), len(eligible_entities)


class SnapshotCompiler:
    """Async debounced SnapshotPort publishing immutable ACL/wall-partitioned `v{N}.sqlite.zst` files."""

    def __init__(
        self,
        store: StorePort,
        snapshot_dir: str | Path,
        *,
        policy: PolicyEngine | None = None,
        debounce_seconds: float = 0.05,
        zstd_level: int = 3,
        on_published: Callable[[SnapshotManifest], None] | None = None,
    ) -> None:
        self._store = store
        self.snapshot_dir = Path(snapshot_dir).resolve()
        self.policy = policy or PolicyEngine()
        self.debounce_seconds = debounce_seconds
        self.zstd_level = zstd_level
        self._on_published = on_published
        self._compile_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._scheduled_tasks: dict[tuple[str, str], asyncio.Task[SnapshotManifest]] = {}

    def set_store(self, store: StorePort) -> None:
        self._store = store

    def partition_dir(self, acl_tier: str, wall_side: WallSide | str) -> Path:
        return self.snapshot_dir / _partition_slug(acl_tier, wall_side)

    def _lock_for(self, acl_tier: str, wall_side: str) -> asyncio.Lock:
        key = (acl_tier, wall_side)
        lock = self._compile_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._compile_locks[key] = lock
        return lock

    def schedule_compile(self, acl_tier: str, wall_side: str) -> None:
        """Schedule a debounced non-blocking background snapshot compile (fixes PDF Defect #4)."""
        key = (acl_tier, wall_side)
        existing = self._scheduled_tasks.get(key)
        if existing is not None and not existing.done():
            existing.cancel()

        async def _debounced() -> SnapshotManifest:
            if self.debounce_seconds > 0:
                await asyncio.sleep(self.debounce_seconds)
            return await self.compile_and_publish(acl_tier, wall_side)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(_debounced(), name=f"snapshot-compile:{acl_tier}:{wall_side}")
        self._scheduled_tasks[key] = task

    async def flush_scheduled(self) -> list[SnapshotManifest]:
        """Await any pending debounced snapshot compilations."""
        tasks = [t for t in self._scheduled_tasks.values() if not t.done()]
        results: list[SnapshotManifest] = []
        for t in tasks:
            try:
                results.append(await t)
            except asyncio.CancelledError:
                pass
        return results

    async def close(self) -> None:
        """Cancel and drain any scheduled compile tasks cleanly on shutdown."""
        for task in list(self._scheduled_tasks.values()):
            if not task.done():
                task.cancel()
        for task in list(self._scheduled_tasks.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._scheduled_tasks.clear()

    def _read_latest_manifest_sync(self, acl_tier: str, wall_side: str) -> SnapshotManifest | None:
        mpath = self.partition_dir(acl_tier, wall_side) / "manifest.json"
        if not mpath.is_file():
            return None
        return SnapshotManifest.model_validate_json(mpath.read_text(encoding="utf-8"))

    async def get_latest_manifest(self, acl_tier: str, wall_side: str) -> SnapshotManifest | None:
        return await asyncio.to_thread(self._read_latest_manifest_sync, acl_tier, wall_side)

    def _read_snapshot_bytes_sync(self, acl_tier: str, wall_side: str, version_id: int) -> bytes:
        fpath = self.partition_dir(acl_tier, wall_side) / f"v{version_id}.sqlite.zst"
        if not fpath.is_file():
            raise NotFoundError(f"Snapshot v{version_id} not found for ({acl_tier}, {wall_side})")
        return fpath.read_bytes()

    async def get_snapshot_bytes(self, acl_tier: str, wall_side: str, version_id: int) -> bytes:
        return await asyncio.to_thread(self._read_snapshot_bytes_sync, acl_tier, wall_side, version_id)

    async def compile_and_publish(self, acl_tier: str, wall_side: str) -> SnapshotManifest:
        """Compile an immutable ACL/wall-partitioned `v{N}.sqlite.zst` and atomically publish `manifest.json`."""
        ws_enum = WallSide(wall_side)
        lock = self._lock_for(acl_tier, ws_enum.value)
        async with lock:
            if not hasattr(self._store, "export_state"):
                raise RuntimeError("StorePort implementation must support export_state() for SnapshotCompiler")
            state: dict[str, Any] = await self._store.export_state()  # type: ignore[attr-defined]
            epochs: PolicyEpochState = state["epochs"]

            prev = await self.get_latest_manifest(acl_tier, ws_enum.value)
            next_version = (prev.version_id + 1) if prev is not None else 1

            # Offload CPU-bound SQLite build, Zstd compression, and atomic file I/O off the event loop!
            def _compile_and_write() -> SnapshotManifest:
                compressed, sha256_hex, etag, claim_count, entity_count = _build_snapshot_sqlite_zst_sync(
                    version_id=next_version,
                    acl_tier=acl_tier,
                    wall_side=ws_enum,
                    epochs=epochs,
                    entities=state["entities"],
                    identifiers=state["identifiers"],
                    claims=state["claims"],
                    skill_bindings=state["skill_bindings"],
                    conflicts=state["conflict_sets"],
                    policy=self.policy,
                    zstd_level=self.zstd_level,
                )
                pdir = self.partition_dir(acl_tier, ws_enum.value)
                pdir.mkdir(parents=True, exist_ok=True)
                filename = f"v{next_version}.sqlite.zst"
                snap_path = pdir / filename
                atomic_write_bytes(snap_path, compressed)

                rel_path = to_posix_path(f"{_partition_slug(acl_tier, ws_enum)}/{filename}")
                manifest = SnapshotManifest(
                    version_id=next_version,
                    acl_tier=acl_tier,
                    wall_side=ws_enum,
                    policy_epoch=epochs.policy_epoch,
                    data_epoch=epochs.data_epoch,
                    etag=etag,
                    sha256=sha256_hex,
                    compiled_at=utc_now(),
                    filename=filename,
                    relative_path=rel_path,
                    claim_count=claim_count,
                    entity_count=entity_count,
                    byte_size=len(compressed),
                )
                atomic_write_text(
                    pdir / "manifest.json",
                    json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
                )
                return manifest

            manifest = await asyncio.to_thread(_compile_and_write)
            if self._on_published is not None:
                self._on_published(manifest)
            return manifest
