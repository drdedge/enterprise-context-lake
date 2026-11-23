"""Zero-hop ref-counted in-RAM ContextClient / ContextSDK with pinned historical replay (ADR-002)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

import httpx
from pydantic import BaseModel, Field

from context_core.bitemporal import resolve_stratified_claims
from context_core.compression import ensure_utc, format_utc, utc_now
from context_core.exceptions import NotFoundError
from context_core.models import (
    Claim,
    ClaimACL,
    ClaimFacets,
    ConflictSet,
    ContextAssemblyReceipt,
    ContextBundle,
    Entity,
    EntityIdentifier,
    EntityType,
    MemoryRecord,
    PolicyEpochState,
    Principal,
    Provenance,
    SnapshotManifest,
    WallSide,
)
from context_core.policy import PolicyEngine
from context_core.receipts import build_receipt
from context_sdk.compiler import _partition_slug
from context_sdk.handle import SnapshotHandle


class LookupResult(BaseModel):
    """Deterministic lookup result returned by ContextClient.lookup()."""

    entity_id: str | None = None
    canonical_key: str
    predicate: str
    context_purpose: str = "general"
    value: str | None = None
    claim: Claim | None = None
    competing_claims: list[Claim] = Field(default_factory=list)
    has_conflict: bool = False
    conflict_set_id: str | None = None
    snapshot_version: int
    data_epoch: int
    policy_epoch: int
    receipt: ContextAssemblyReceipt


def _row_to_entity(row: sqlite3.Row) -> Entity:
    return Entity(
        entity_id=row["entity_id"],
        entity_type=EntityType(row["entity_type"]),
        canonical_key=row["canonical_key"],
        display_name=row["display_name"],
        aliases=json.loads(row["aliases_json"]),
        description=row["description"],
        owner_group=row["owner_group"],
        wall_side=WallSide(row["wall_side"]),
        read_acl_groups=json.loads(row["read_acl_groups_json"]),
        write_acl_groups=json.loads(row["write_acl_groups_json"]),
        attributes=json.loads(row["attributes_json"]),
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_identifier(row: sqlite3.Row) -> EntityIdentifier:
    return EntityIdentifier(
        id=row["id"],
        entity_id=row["entity_id"],
        scheme=row["scheme"],
        identifier_value=row["identifier_value"],
        authority=row["authority"],
        is_primary=bool(row["is_primary"]),
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        recorded_at=row["recorded_at"],
        superseded_at=row["superseded_at"],
    )


def _row_to_claim(row: sqlite3.Row) -> Claim:
    return Claim(
        claim_id=row["claim_id"],
        subject_id=row["subject_id"],
        predicate=row["predicate"],
        object_value=row["object_value"],
        object_entity_id=row["object_entity_id"],
        context_purpose=row["context_purpose"],
        scope_type=row["scope_type"],
        scope_id=row["scope_id"],
        channel=row["channel"],
        epistemic_status=row["epistemic_status"],
        kind=row["kind"],
        is_mandatory=bool(row["is_mandatory"]),
        facets=ClaimFacets.model_validate(json.loads(row["facets_json"])),
        provenance=Provenance(
            source_system=row["source_system"],
            evidence_uri=row["evidence_uri"],
            content_sha256=row["content_sha256"],
            span_locator=row["span_locator"],
            excerpt=row["excerpt"],
        ),
        acl=ClaimACL(
            wall_side=WallSide(row["wall_side"]),
            read_acl_groups=json.loads(row["read_acl_groups_json"]),
            write_acl_groups=json.loads(row["write_acl_groups_json"]),
            evidence_acl_groups=json.loads(row["evidence_acl_groups_json"]),
        ),
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        recorded_at=row["recorded_at"],
        superseded_at=row["superseded_at"],
        superseded_by=row["superseded_by"],
        created_by=row["created_by"],
        evidence_redacted=bool(row["evidence_redacted"]),
        evidence_redaction_label=row["evidence_redaction_label"],
        evidence_request_group=row["evidence_request_group"],
    )


class _SnapshotQueryMixin:
    """Shared in-RAM SQLite bitemporal query engine operating over any acquired SnapshotHandle."""

    acl_tier: str
    wall_side: WallSide
    _default_principal: Principal
    _policy: PolicyEngine

    def _checkout_handle(self) -> tuple[SnapshotHandle, sqlite3.Connection]:
        raise NotImplementedError

    def _effective_principal(self, principal: Principal | None) -> Principal:
        return principal or self._default_principal

    def get_entity(self, entity_id_or_key: str) -> Entity | None:
        handle, conn = self._checkout_handle()
        try:
            row = conn.execute(
                "SELECT * FROM entities WHERE entity_id = ? OR canonical_key = ? LIMIT 1",
                (entity_id_or_key, entity_id_or_key.lower()),
            ).fetchone()
            return _row_to_entity(row) if row is not None else None
        finally:
            handle.release_connection(conn)

    def list_entities(self, *, entity_type: str | None = None) -> list[Entity]:
        handle, conn = self._checkout_handle()
        try:
            if entity_type:
                rows = conn.execute(
                    "SELECT * FROM entities WHERE entity_type = ? ORDER BY canonical_key ASC",
                    (entity_type,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM entities ORDER BY canonical_key ASC").fetchall()
            return [_row_to_entity(r) for r in rows]
        finally:
            handle.release_connection(conn)

    def lookup_identifiers(
        self,
        entity_id_or_key: str,
        *,
        scheme: str | None = None,
        as_of: datetime | str | None = None,
        recorded_as_of: datetime | str | None = None,
    ) -> list[EntityIdentifier]:
        eff_as_of = format_utc(ensure_utc(as_of) or utc_now())
        eff_rec = format_utc(ensure_utc(recorded_as_of) or utc_now())
        handle, conn = self._checkout_handle()
        try:
            ent_row = conn.execute(
                "SELECT entity_id FROM entities WHERE entity_id = ? OR canonical_key = ? LIMIT 1",
                (entity_id_or_key, entity_id_or_key.lower()),
            ).fetchone()
            if ent_row is None:
                return []
            eid = ent_row["entity_id"]
            sql = (
                "SELECT * FROM entity_identifiers "
                "WHERE entity_id = ? "
                "AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?) "
                "AND recorded_at <= ? AND (superseded_at IS NULL OR superseded_at > ?)"
            )
            params: list[Any] = [eid, eff_as_of, eff_as_of, eff_rec, eff_rec]
            if scheme is not None:
                sql += " AND scheme = ?"
                params.append(scheme)
            rows = conn.execute(sql, params).fetchall()
            return [_row_to_identifier(r) for r in rows]
        finally:
            handle.release_connection(conn)

    def lookup(
        self,
        canonical_key_or_id: str,
        predicate: str,
        *,
        context_purpose: str | None = None,
        as_of: datetime | str | None = None,
        recorded_as_of: datetime | str | None = None,
        principal: Principal | None = None,
    ) -> LookupResult:
        """Exact bitemporal lookup in RAM (fixing PDF Defect #2 via true valid_from/recorded_at/superseded_at query)."""
        eff_as_of_dt = ensure_utc(as_of) or utc_now()
        eff_rec_dt = ensure_utc(recorded_as_of) or utc_now()
        eff_as_of = format_utc(eff_as_of_dt)
        eff_rec = format_utc(eff_rec_dt)
        caller = self._effective_principal(principal)

        handle, conn = self._checkout_handle()
        try:
            ent_row = conn.execute(
                "SELECT entity_id, canonical_key FROM entities WHERE entity_id = ? OR canonical_key = ? LIMIT 1",
                (canonical_key_or_id, canonical_key_or_id.lower()),
            ).fetchone()

            if ent_row is None:
                epochs = PolicyEpochState(data_epoch=handle.data_epoch, policy_epoch=handle.policy_epoch)
                rcpt = build_receipt(
                    principal=caller,
                    epochs=epochs,
                    as_of=eff_as_of_dt,
                    recorded_as_of=eff_rec_dt,
                    snapshot_version=handle.version_id,
                    tool_or_endpoint="sdk.lookup",
                )
                return LookupResult(
                    entity_id=None,
                    canonical_key=canonical_key_or_id,
                    predicate=predicate,
                    context_purpose=context_purpose or "general",
                    value=None,
                    claim=None,
                    snapshot_version=handle.version_id,
                    data_epoch=handle.data_epoch,
                    policy_epoch=handle.policy_epoch,
                    receipt=rcpt,
                )

            eid = ent_row["entity_id"]
            ckey = ent_row["canonical_key"]

            sql = (
                "SELECT * FROM claims "
                "WHERE subject_id = ? AND predicate = ? "
                "AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?) "
                "AND recorded_at <= ? AND (superseded_at IS NULL OR superseded_at > ?) "
                "AND epistemic_status NOT IN ('unverified_observation', 'candidate_sop', 'revoked')"
            )
            params: list[Any] = [eid, predicate, eff_as_of, eff_as_of, eff_rec, eff_rec]
            if context_purpose is not None:
                sql += " AND context_purpose = ?"
                params.append(context_purpose)
            rows = conn.execute(sql, params).fetchall()
            claims = [_row_to_claim(r) for r in rows]

            # Load any open conflict sets touching these visible claims
            vis_ids = {c.claim_id for c in claims}
            conflict_set_id: str | None = None
            cs_rows = conn.execute(
                "SELECT conflict_set_id FROM conflict_sets WHERE subject_id = ? AND predicate = ? AND status = 'open'",
                (eid, predicate),
            ).fetchall()
            for csr in cs_rows:
                m_rows = conn.execute(
                    "SELECT claim_id FROM conflict_set_members WHERE conflict_set_id = ?",
                    (csr["conflict_set_id"],),
                ).fetchall()
                m_ids = [mr["claim_id"] for mr in m_rows if mr["claim_id"] in vis_ids]
                if len(m_ids) >= 2:
                    conflict_set_id = csr["conflict_set_id"]
                    break

            facts, _, _ = resolve_stratified_claims(claims, principal=caller, include_unverified=False)
            primary = next((f for f in facts if f.epistemic_status == "approved_fact"), None)
            if primary is None and facts:
                primary = facts[0]
            competing = [f for f in facts if primary is None or f.claim_id != primary.claim_id]
            has_conflict = conflict_set_id is not None or any(f.epistemic_status == "disputed" for f in facts)

            epochs = PolicyEpochState(data_epoch=handle.data_epoch, policy_epoch=handle.policy_epoch)
            rcpt = build_receipt(
                principal=caller,
                epochs=epochs,
                as_of=eff_as_of_dt,
                recorded_as_of=eff_rec_dt,
                claims=facts,
                conflict_set_ids=[conflict_set_id] if conflict_set_id else [],
                snapshot_version=handle.version_id,
                tool_or_endpoint="sdk.lookup",
            )
            return LookupResult(
                entity_id=eid,
                canonical_key=ckey,
                predicate=predicate,
                context_purpose=context_purpose or (primary.context_purpose if primary else "general"),
                value=primary.object_value if primary else None,
                claim=primary,
                competing_claims=competing,
                has_conflict=has_conflict,
                conflict_set_id=conflict_set_id,
                snapshot_version=handle.version_id,
                data_epoch=handle.data_epoch,
                policy_epoch=handle.policy_epoch,
                receipt=rcpt,
            )
        finally:
            handle.release_connection(conn)

    def resolve_context(
        self,
        entity_keys: Sequence[str],
        *,
        skill_id: str | None = None,
        project_id: str | None = None,
        context_purpose: str | None = None,
        predicates: Sequence[str] | None = None,
        as_of: datetime | str | None = None,
        recorded_as_of: datetime | str | None = None,
        principal: Principal | None = None,
        memory_fetcher: Callable[[Principal], list[MemoryRecord]] | None = None,
    ) -> ContextBundle:
        """Assemble a two-channel ContextBundle from the in-RAM snapshot (+ optional live personal memory fallback)."""
        eff_as_of_dt = ensure_utc(as_of) or utc_now()
        eff_rec_dt = ensure_utc(recorded_as_of) or utc_now()
        eff_as_of = format_utc(eff_as_of_dt)
        eff_rec = format_utc(eff_rec_dt)
        caller = self._effective_principal(principal)

        handle, conn = self._checkout_handle()
        try:
            entities: list[Entity] = []
            subject_ids: list[str] = []
            keys_to_load = list(entity_keys) + ([skill_id] if skill_id else [])
            for key in keys_to_load:
                row = conn.execute(
                    "SELECT * FROM entities WHERE entity_id = ? OR canonical_key = ? LIMIT 1",
                    (key, key.lower()),
                ).fetchone()
                if row is not None and row["entity_id"] not in subject_ids:
                    entities.append(_row_to_entity(row))
                    subject_ids.append(row["entity_id"])

            claims: list[Claim] = []
            conflicts: list[ConflictSet] = []
            if subject_ids:
                placeholders = ",".join("?" * len(subject_ids))
                sql = (
                    f"SELECT * FROM claims WHERE subject_id IN ({placeholders}) "
                    "AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?) "
                    "AND recorded_at <= ? AND (superseded_at IS NULL OR superseded_at > ?)"
                )
                params: list[Any] = [*subject_ids, eff_as_of, eff_as_of, eff_rec, eff_rec]
                if context_purpose is not None:
                    sql += " AND context_purpose = ?"
                    params.append(context_purpose)
                if predicates:
                    p_ph = ",".join("?" * len(predicates))
                    sql += f" AND predicate IN ({p_ph})"
                    params.extend(predicates)
                rows = conn.execute(sql, params).fetchall()
                claims = [_row_to_claim(r) for r in rows]
                claim_map = {c.claim_id: c for c in claims}

                cs_rows = conn.execute(
                    f"SELECT * FROM conflict_sets WHERE subject_id IN ({placeholders}) AND status = 'open'",
                    subject_ids,
                ).fetchall()
                for csr in cs_rows:
                    m_rows = conn.execute(
                        "SELECT claim_id FROM conflict_set_members WHERE conflict_set_id = ?",
                        (csr["conflict_set_id"],),
                    ).fetchall()
                    m_ids = [mr["claim_id"] for mr in m_rows if mr["claim_id"] in claim_map]
                    if len(m_ids) >= 2:
                        conflicts.append(
                            ConflictSet(
                                conflict_set_id=csr["conflict_set_id"],
                                subject_id=csr["subject_id"],
                                predicate=csr["predicate"],
                                context_purpose=csr["context_purpose"],
                                scope_type=csr["scope_type"],
                                scope_id=csr["scope_id"],
                                status=csr["status"],
                                claim_ids=m_ids,
                                claims=[claim_map[cid] for cid in m_ids],
                                winning_claim_id=csr["winning_claim_id"],
                                resolution_rationale=csr["resolution_rationale"],
                                resolved_by=csr["resolved_by"],
                                resolved_at=csr["resolved_at"],
                                created_at=csr["created_at"],
                                summary=csr["summary"],
                            )
                        )

            facts, directives, active_conflicts = resolve_stratified_claims(
                claims,
                principal=caller,
                skill_id=skill_id,
                project_id=project_id,
                include_unverified=False,
                open_conflicts=conflicts,
            )

            # Optional live personal memory fallback (strictly gated by OBO + direct channel)
            memories: list[MemoryRecord] = []
            if memory_fetcher is not None and self._policy.is_memory_allowed(caller):
                memories = list(memory_fetcher(caller))

            epochs = PolicyEpochState(data_epoch=handle.data_epoch, policy_epoch=handle.policy_epoch)
            rcpt = build_receipt(
                principal=caller,
                epochs=epochs,
                as_of=eff_as_of_dt,
                recorded_as_of=eff_rec_dt,
                claims=facts + directives,
                conflicts=active_conflicts,
                memories=memories,
                snapshot_version=handle.version_id,
                tool_or_endpoint="sdk.resolve_context",
            )
            return ContextBundle(
                source="snapshot",
                entities=entities,
                facts=facts,
                directives=directives,
                conflicts=active_conflicts,
                memories=memories,
                receipt=rcpt,
            )
        finally:
            handle.release_connection(conn)


class PinnedSnapshotView(_SnapshotQueryMixin):
    """Independent immutable historical snapshot view with ZERO background polling (fixes PDF Defect #6)."""

    def __init__(
        self,
        handle: SnapshotHandle,
        *,
        default_principal: Principal,
        policy: PolicyEngine | None = None,
    ) -> None:
        self._handle = handle
        self.acl_tier = handle.acl_tier
        self.wall_side = WallSide(handle.wall_side)
        self._default_principal = default_principal
        self._policy = policy or PolicyEngine()

    @property
    def version_id(self) -> int:
        return self._handle.version_id

    @property
    def etag(self) -> str:
        return self._handle.etag

    def _checkout_handle(self) -> tuple[SnapshotHandle, sqlite3.Connection]:
        conn = self._handle.acquire_connection()
        return self._handle, conn

    def close(self) -> None:
        self._handle.retire()

    def __enter__(self) -> PinnedSnapshotView:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


class ContextClient(_SnapshotQueryMixin):
    """Zero-hop in-RAM SQLite snapshot SDK with ref-counted atomic swaps & strict pinned_version isolation."""

    def __init__(
        self,
        *,
        snapshot_dir: str | Path | None = None,
        api_base_url: str | None = None,
        acl_tier: str = "firm-all",
        wall_side: WallSide | str = WallSide.public_side,
        pinned_version: int | None = None,
        poll_interval_seconds: float | None = None,
        principal: Principal | None = None,
        policy: PolicyEngine | None = None,
        auth_headers: dict[str, str] | None = None,
    ) -> None:
        if snapshot_dir is None and api_base_url is None:
            raise ValueError("Provide either snapshot_dir or api_base_url")
        self.snapshot_dir = Path(snapshot_dir).resolve() if snapshot_dir is not None else None
        self.api_base_url = api_base_url.rstrip("/") if api_base_url is not None else None
        self.acl_tier = acl_tier
        self.wall_side = WallSide(wall_side)
        self.pinned_version = pinned_version
        self._poll_interval = poll_interval_seconds
        self._policy = policy or PolicyEngine()
        self._auth_headers = dict(auth_headers or {})
        self._default_principal = principal or Principal(
            user_id=f"service:{acl_tier}",
            display_name=f"Snapshot Service ({acl_tier})",
            email=f"sdk-{acl_tier}@meridian.local",
            roles=frozenset({"service_runtime"}),
            groups=frozenset({acl_tier}),
            wall_side=self.wall_side,
            auth_mode="local_mock",
            actor_mode="service",
            channel_mode="direct",
        )

        self._ptr_lock = threading.Lock()
        self._active_handle: SnapshotHandle | None = None
        self._retired_handles: list[SnapshotHandle] = []
        self._stop_event = threading.Event()
        self._poller_thread: threading.Thread | None = None
        self._swap_count = 0

        # Load initial snapshot immediately
        if self.pinned_version is not None:
            handle = self._load_version_handle(self.pinned_version)
            self._active_handle = handle
        else:
            self.refresh_snapshot(force=True)
            # CRITICAL FIX (PDF Defect #6): start poller ONLY when NOT pinned!
            if self._poll_interval is not None and self._poll_interval > 0:
                self._poller_thread = threading.Thread(
                    target=self._poll_loop,
                    name=f"context-sdk-poller-{self.acl_tier}",
                    daemon=True,
                )
                self._poller_thread.start()

    @property
    def current_version(self) -> int:
        with self._ptr_lock:
            assert self._active_handle is not None
            return self._active_handle.version_id

    @property
    def current_etag(self) -> str:
        with self._ptr_lock:
            assert self._active_handle is not None
            return self._active_handle.etag

    @property
    def swap_count(self) -> int:
        with self._ptr_lock:
            return self._swap_count

    @property
    def is_polling(self) -> bool:
        return self._poller_thread is not None and self._poller_thread.is_alive()

    def _partition_dir(self) -> Path:
        assert self.snapshot_dir is not None
        return self.snapshot_dir / _partition_slug(self.acl_tier, self.wall_side)

    def _fetch_latest_manifest(self) -> SnapshotManifest:
        if self.snapshot_dir is not None:
            mpath = self._partition_dir() / "manifest.json"
            if not mpath.is_file():
                raise NotFoundError(
                    f"No snapshot manifest found for ({self.acl_tier}, {self.wall_side.value}) at {mpath}"
                )
            return SnapshotManifest.model_validate_json(mpath.read_text(encoding="utf-8"))
        assert self.api_base_url is not None
        with httpx.Client(timeout=10.0, headers=self._auth_headers) as client:
            resp = client.get(
                f"{self.api_base_url}/api/v1/snapshots/latest",
                params={"acl_tier": self.acl_tier, "wall_side": self.wall_side.value},
            )
            resp.raise_for_status()
            data = resp.json()
            return SnapshotManifest.model_validate(data.get("manifest", data))

    def _fetch_version_bytes(self, version_id: int) -> bytes:
        if self.snapshot_dir is not None:
            fpath = self._partition_dir() / f"v{version_id}.sqlite.zst"
            if not fpath.is_file():
                raise NotFoundError(f"Snapshot v{version_id} not found at {fpath}")
            return fpath.read_bytes()
        assert self.api_base_url is not None
        with httpx.Client(timeout=15.0, headers=self._auth_headers) as client:
            resp = client.get(
                f"{self.api_base_url}/api/v1/snapshots/{version_id}/download",
                params={"acl_tier": self.acl_tier, "wall_side": self.wall_side.value},
            )
            resp.raise_for_status()
            return resp.content

    def _load_version_handle(
        self, version_id: int, *, manifest: SnapshotManifest | None = None
    ) -> SnapshotHandle:
        raw_zst = self._fetch_version_bytes(version_id)
        sha256_hex = hashlib.sha256(raw_zst).hexdigest()
        if manifest is not None and manifest.version_id == version_id:
            if manifest.sha256 and manifest.sha256 != sha256_hex:
                raise RuntimeError(
                    f"Snapshot v{version_id} SHA-256 mismatch: expected {manifest.sha256}, got {sha256_hex}"
                )
            etag = manifest.etag
            data_epoch = manifest.data_epoch
            policy_epoch = manifest.policy_epoch
        else:
            # Read epochs from snapshot_meta inside deserialized image
            etag = f'"v{version_id}-{sha256_hex[:12]}"'
            data_epoch = 1
            policy_epoch = 1
            probe = SnapshotHandle(
                version_id=version_id,
                etag=etag,
                data_epoch=data_epoch,
                policy_epoch=policy_epoch,
                acl_tier=self.acl_tier,
                wall_side=self.wall_side.value,
                sha256=sha256_hex,
                compressed_bytes=raw_zst,
            )
            with probe.checkout() as conn:
                meta = conn.execute("SELECT data_epoch, policy_epoch FROM snapshot_meta LIMIT 1").fetchone()
                if meta is not None:
                    probe.data_epoch = int(meta["data_epoch"])
                    probe.policy_epoch = int(meta["policy_epoch"])
                    probe.etag = f'"{version_id}-{probe.data_epoch}-{probe.policy_epoch}-{sha256_hex[:12]}"'
            return probe

        return SnapshotHandle(
            version_id=version_id,
            etag=etag,
            data_epoch=data_epoch,
            policy_epoch=policy_epoch,
            acl_tier=self.acl_tier,
            wall_side=self.wall_side.value,
            sha256=sha256_hex,
            compressed_bytes=raw_zst,
        )

    def refresh_snapshot(self, *, force: bool = False) -> bool:
        """Check manifest ETag/version/policy_epoch and atomically swap SnapshotHandle.

        No-op when ``pinned_version`` is set (fixes PDF Defect #6).
        """
        if self.pinned_version is not None:
            return False

        manifest = self._fetch_latest_manifest()
        with self._ptr_lock:
            cur = self._active_handle
            if (
                not force
                and cur is not None
                and cur.etag == manifest.etag
                and cur.version_id == manifest.version_id
                and cur.policy_epoch == manifest.policy_epoch
            ):
                return False

        new_handle = self._load_version_handle(manifest.version_id, manifest=manifest)
        old_handle: SnapshotHandle | None = None
        with self._ptr_lock:
            old_handle = self._active_handle
            self._active_handle = new_handle
            if old_handle is not None:
                self._swap_count += 1
                self._retired_handles.append(old_handle)
                # Prune already-closed historical handles
                self._retired_handles = [h for h in self._retired_handles if not h.is_closed]

        if old_handle is not None:
            # Safe retirement: closes immediately ONLY if active_readers == 0; otherwise defers until last reader finishes!
            old_handle.retire()
        return True

    def _poll_loop(self) -> None:
        assert self._poll_interval is not None
        while not self._stop_event.wait(timeout=self._poll_interval):
            if self.pinned_version is not None:
                return
            try:
                self.refresh_snapshot(force=False)
            except Exception:
                # Keep serving last-known-good snapshot handle on transient read error
                pass

    def pinned(self, version_id: int) -> PinnedSnapshotView:
        """Open an independent immutable PinnedSnapshotView for historical replay without disturbing live polling."""
        handle = self._load_version_handle(version_id)
        return PinnedSnapshotView(
            handle,
            default_principal=self._default_principal,
            policy=self._policy,
        )

    def _checkout_handle(self) -> tuple[SnapshotHandle, sqlite3.Connection]:
        """Atomically acquire the current active handle + connection under 1-microsecond lock."""
        with self._ptr_lock:
            handle = self._active_handle
            if handle is None:
                raise RuntimeError("ContextClient has no loaded snapshot (or has been closed)")
            conn = handle.acquire_connection()
            return handle, conn

    def close(self) -> None:
        """Stop background poller and retire/close all in-RAM SQLite connections cleanly."""
        self._stop_event.set()
        if self._poller_thread is not None and self._poller_thread.is_alive():
            self._poller_thread.join(timeout=2.0)
        with self._ptr_lock:
            active = self._active_handle
            self._active_handle = None
            retired = list(self._retired_handles)
            self._retired_handles.clear()
        if active is not None:
            active.retire()
        for h in retired:
            h.retire()

    def __enter__(self) -> ContextClient:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


# Ergonomic alias matching ADR-002 naming
ContextSDK = ContextClient
