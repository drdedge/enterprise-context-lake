"""SQLAlchemy 2.x async bitemporal relational StorePort adapter with FTS5 lexical index (ADR-002)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import threading
import time
import uuid
import weakref
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    delete,
    event,
    or_,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DisconnectionError
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool, StaticPool

from context_core.bitemporal import (
    claim_collision_key,
    claims_conflict,
    is_claim_visible,
    is_identifier_visible,
    windows_overlap,
)
from context_core.compression import ensure_utc, format_utc, utc_now
from context_core.exceptions import (
    AuthenticationError,
    NotFoundError,
    OptimisticConcurrencyError,
    ValidationError,
)
from context_core.models import (
    clone_model,
    Claim,
    ClaimACL,
    ClaimCreateInput,
    ClaimFacets,
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
    Provenance,
    SkillBinding,
    StalenessReport,
    Topic,
    TopicAttachment,
    TopicPatchInput,
    WallSide,
    WatchlistRecord,
    WatchlistUpdateInput,
)
from context_core.policy import PolicyEngine, sanitize_uri_secrets, wall_allows
from context_core.vocabulary import VocabTerm, default_terms
from context_adapters._vocab_common import VocabularyStoreMixin
from context_adapters.sealed import SealedText
from context_adapters._store_common import (
    apply_topic_patch,
    check_toctou_epoch,
    compute_sha_staleness,
    compute_watchlist_alerts,
    prepare_incoming_claim,
)

# ---------------------------------------------------------------------------
# Portable SQLAlchemy Core Schema (SQLite / SQL Server / Postgres compatible)
# ---------------------------------------------------------------------------
# SealedText columns hold free text that the SQL Server store encrypts with a KMS data key before
# saving (ADR-010). On SQLite no cipher is attached and they are ordinary TEXT columns.

metadata = MetaData()

t_system_epochs = Table(
    "system_epochs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("data_epoch", Integer, nullable=False, default=1),
    Column("policy_epoch", Integer, nullable=False, default=1),
    Column("updated_at", String(64), nullable=False),
    Column("last_reason", Text, nullable=True),
    Column("last_actor", String(128), nullable=True),
)

t_revoked_principals = Table(
    "revoked_principals",
    metadata,
    Column("user_id", String(128), primary_key=True),
    Column("reason", Text, nullable=False),
    Column("actor", String(128), nullable=False),
    Column("revoked_at", String(64), nullable=False),
)

t_entities = Table(
    "entities",
    metadata,
    Column("entity_id", String(128), primary_key=True),
    Column("entity_type", String(64), nullable=False, index=True),
    Column("canonical_key", String(256), nullable=False, unique=True, index=True),
    Column("display_name", SealedText, nullable=False),
    Column("aliases_json", SealedText, nullable=False),
    Column("description", SealedText, nullable=False),
    Column("owner_group", String(128), nullable=False),
    Column("wall_side", String(32), nullable=False, index=True),
    Column("read_acl_groups_json", Text, nullable=False),
    Column("write_acl_groups_json", Text, nullable=False),
    Column("attributes_json", SealedText, nullable=False),
    Column("status", String(32), nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("updated_at", String(64), nullable=False),
    # Deduplicated (wall_side, read groups) set, so reads filter by access inside the query (ADR-008)
    Column("acl_set_id", Integer, nullable=True, index=True),
)

t_entity_identifiers = Table(
    "entity_identifiers",
    metadata,
    Column("id", String(128), primary_key=True),
    Column("entity_id", String(128), nullable=False, index=True),
    Column("scheme", String(64), nullable=False, index=True),
    Column("identifier_value", String(256), nullable=False, index=True),
    Column("authority", String(128), nullable=False),
    Column("is_primary", Boolean, nullable=False),
    Column("valid_from", String(64), nullable=False),
    Column("valid_to", String(64), nullable=True),
    Column("recorded_at", String(64), nullable=False),
    Column("superseded_at", String(64), nullable=True),
)

t_entity_lineage_events = Table(
    "entity_lineage_events",
    metadata,
    Column("event_id", String(128), primary_key=True),
    Column("source_entity_id", String(128), nullable=False, index=True),
    Column("target_entity_id", String(128), nullable=False, index=True),
    Column("event_type", String(64), nullable=False),
    Column("effective_date", String(64), nullable=False),
    Column("recorded_at", String(64), nullable=False),
    Column("summary", SealedText, nullable=False),
    Column("evidence_uri", SealedText, nullable=True),
    Column("evidence_acl_groups_json", Text, nullable=False),
    Column("wall_side", String(32), nullable=False),
    Column("read_acl_groups_json", Text, nullable=False),
)

t_positions = Table(
    "positions",
    metadata,
    Column("position_id", String(128), primary_key=True),
    Column("vehicle_entity_id", String(128), nullable=False, index=True),
    Column("instrument_entity_id", String(128), nullable=False, index=True),
    Column("issuer_entity_id", String(128), nullable=False, index=True),
    Column("position_type", String(64), nullable=False),
    Column("quantity", Float, nullable=True),
    Column("market_value", Float, nullable=True),
    Column("currency", String(16), nullable=False),
    Column("weight_bps", Float, nullable=True),
    Column("as_of_date", String(32), nullable=False),
    Column("recorded_at", String(64), nullable=False),
    Column("source_system", String(128), nullable=False),
    Column("wall_side", String(32), nullable=False),
    Column("read_acl_groups_json", Text, nullable=False),
)

t_coverages = Table(
    "coverages",
    metadata,
    Column("coverage_id", String(128), primary_key=True),
    Column("analyst_entity_id", String(128), nullable=False, index=True),
    Column("issuer_entity_id", String(128), nullable=False, index=True),
    Column("coverage_role", String(64), nullable=False),
    Column("coverage_status", String(32), nullable=False, index=True),
    Column("valid_from", String(64), nullable=False),
    Column("valid_to", String(64), nullable=True),
    Column("wall_side", String(32), nullable=False),
    Column("read_acl_groups_json", Text, nullable=False),
)

t_skill_bindings = Table(
    "skill_bindings",
    metadata,
    Column("binding_id", String(128), primary_key=True),
    Column("skill_entity_id", String(128), nullable=False, index=True),
    Column("target_entity_id", String(128), nullable=False, index=True),
    Column("mapping_purpose", String(128), nullable=False),
    Column("read_mode", String(64), nullable=False),
    Column("required_predicates_json", Text, nullable=False),
    Column("active", Boolean, nullable=False),
)

t_claims = Table(
    "claims",
    metadata,
    Column("claim_id", String(128), primary_key=True),
    Column("subject_id", String(128), nullable=False, index=True),
    Column("predicate", String(128), nullable=False, index=True),
    Column("object_value", SealedText, nullable=False),
    Column("object_entity_id", String(128), nullable=True, index=True),
    Column("context_purpose", String(128), nullable=False, index=True),
    Column("scope_type", String(32), nullable=False, index=True),
    Column("scope_id", String(256), nullable=False),
    Column("channel", String(32), nullable=False),
    Column("epistemic_status", String(64), nullable=False, index=True),
    Column("kind", String(64), nullable=True),
    Column("is_mandatory", Boolean, nullable=False),
    Column("facets_json", SealedText, nullable=False),
    Column("provenance_json", SealedText, nullable=False),
    Column("wall_side", String(32), nullable=False, index=True),
    Column("acl_json", Text, nullable=False),
    Column("valid_from", String(64), nullable=False, index=True),
    Column("valid_to", String(64), nullable=True),
    Column("recorded_at", String(64), nullable=False, index=True),
    Column("superseded_at", String(64), nullable=True),
    Column("superseded_by", String(128), nullable=True),
    Column("created_by", String(128), nullable=False),
    Column("acl_set_id", Integer, nullable=True, index=True),
)

t_conflict_sets = Table(
    "conflict_sets",
    metadata,
    Column("conflict_set_id", String(128), primary_key=True),
    Column("subject_id", String(128), nullable=False, index=True),
    Column("predicate", String(128), nullable=False),
    Column("context_purpose", String(128), nullable=False),
    Column("scope_type", String(32), nullable=False),
    Column("scope_id", String(256), nullable=False),
    Column("status", String(32), nullable=False, index=True),
    Column("winning_claim_id", String(128), nullable=True),
    Column("resolution_rationale", SealedText, nullable=True),
    Column("resolved_by", String(128), nullable=True),
    Column("resolved_at", String(64), nullable=True),
    Column("created_at", String(64), nullable=False),
    Column("summary", SealedText, nullable=False),
)

t_conflict_set_members = Table(
    "conflict_set_members",
    metadata,
    Column("conflict_set_id", String(128), primary_key=True),
    Column("claim_id", String(128), primary_key=True),
)

t_topics = Table(
    "topics",
    metadata,
    Column("topic_id", String(128), primary_key=True),
    Column("slug", String(128), nullable=False, unique=True, index=True),
    Column("pref_label", String(256), nullable=False),
    Column("alt_labels_json", Text, nullable=False),
    Column("broader_topic_id", String(128), nullable=True),
    Column("narrower_topic_ids_json", Text, nullable=False),
    Column("related_topic_ids_json", Text, nullable=False),
    Column("topic_kind", String(64), nullable=False),
    Column("merged_into_topic_id", String(128), nullable=True),
    Column("owner_group", String(128), nullable=False),
    Column("status", String(32), nullable=False),
)

t_topic_attachments = Table(
    "topic_attachments",
    metadata,
    Column("attachment_id", String(128), primary_key=True),
    Column("topic_id", String(128), nullable=False, index=True),
    Column("target_type", String(64), nullable=False),
    Column("target_id", String(128), nullable=False, index=True),
    Column("attachment_source", String(32), nullable=False),
    Column("confidence", Float, nullable=False),
    Column("wall_side", String(32), nullable=False),
    Column("read_acl_groups_json", Text, nullable=False),
)

t_knowledge_items = Table(
    "knowledge_items",
    metadata,
    Column("item_id", String(128), primary_key=True),
    Column("knowledge_kind", String(64), nullable=False, index=True),
    Column("display_name", SealedText, nullable=False),
    Column("slug", String(256), nullable=False),
    Column("source_path", Text, nullable=False),
    Column("body_markdown", SealedText, nullable=False),
    Column("wall_side", String(32), nullable=False),
    Column("read_acl_groups_json", Text, nullable=False),
    Column("write_acl_groups_json", Text, nullable=False),
    Column("topics_json", Text, nullable=False),
    Column("related_entities_json", Text, nullable=False),
    Column("metadata_json", SealedText, nullable=False),
    Column("content_sha256", String(128), nullable=True),
    Column("valid_from", String(64), nullable=False),
    Column("recorded_at", String(64), nullable=False),
    Column("okf_version", String(16), nullable=False),
)

t_curator_patches = Table(
    "curator_patches",
    metadata,
    Column("patch_id", String(128), primary_key=True),
    Column("wall_side", String(32), nullable=False),
    Column("read_acl_groups_json", Text, nullable=False),
    Column("target_type", String(64), nullable=False),
    Column("target_id", String(128), nullable=False),
    Column("title", SealedText, nullable=False),
    Column("rationale", SealedText, nullable=False),
    Column("unified_diff", SealedText, nullable=False),
    Column("proposed_changes_json", SealedText, nullable=False),
    Column("source_claim_ids_json", Text, nullable=False),
    Column("stale_evidence_paths_json", Text, nullable=False),
    Column("status", String(64), nullable=False, index=True),
    Column("created_at", String(64), nullable=False),
    Column("created_by", String(128), nullable=False),
    Column("reviewed_by", String(128), nullable=True),
    Column("reviewed_at", String(64), nullable=True),
)

t_personal_memories = Table(
    "personal_memories",
    metadata,
    Column("memory_id", String(128), primary_key=True),
    Column("user_id", String(128), nullable=False, index=True),
    Column("scope_type", String(32), nullable=False),
    Column("skill_id", String(128), nullable=True),
    Column("key", String(256), nullable=False),
    Column("content", SealedText, nullable=False),
    Column("tags_json", Text, nullable=False),
    Column("linked_entity_ids_json", Text, nullable=False),
    Column("linked_topic_ids_json", Text, nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("updated_at", String(64), nullable=False),
)

t_personal_watchlists = Table(
    "personal_watchlists",
    metadata,
    Column("user_id", String(128), primary_key=True),
    Column("entity_ids_json", Text, nullable=False),
    Column("topic_ids_json", Text, nullable=False),
    Column("source_ids_json", Text, nullable=False),
    Column("updated_at", String(64), nullable=False),
)

t_receipts = Table(
    "receipts",
    metadata,
    Column("receipt_id", String(128), primary_key=True),
    Column("principal_id", String(128), nullable=False, index=True),
    Column("payload_json", SealedText, nullable=False),
    Column("created_at", String(64), nullable=False),
)

# Steward-editable vocabularies: entity types, identifier schemes, predicates, tag values (ADR-008)
t_vocab_terms = Table(
    "vocab_terms",
    metadata,
    Column("term_id", String(320), primary_key=True),
    Column("vocab", String(64), nullable=False, index=True),
    Column("key", String(256), nullable=False),
    Column("label", String(512), nullable=False),
    Column("parent_key", String(256), nullable=True),
    Column("description", Text, nullable=False),
    Column("aliases_json", Text, nullable=False),
    Column("status", String(32), nullable=False, index=True),
    Column("merged_into_key", String(256), nullable=True),
    Column("attributes_json", Text, nullable=False),
    Column("owner_group", String(128), nullable=False),
    Column("created_by", String(128), nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("updated_at", String(64), nullable=False),
)

# One row per distinct (wall_side, read groups) combination. Records point at it with acl_set_id,
# and a caller's readable sets are found with one indexed join on their groups (ADR-008).
t_acl_sets = Table(
    "acl_sets",
    metadata,
    Column("acl_set_id", Integer, primary_key=True, autoincrement=True),
    Column("signature", String(1024), nullable=False, unique=True),
    Column("wall_side", String(32), nullable=False, index=True),
)

t_acl_set_groups = Table(
    "acl_set_groups",
    metadata,
    Column("acl_set_id", Integer, primary_key=True),
    Column("group_name", String(256), primary_key=True, index=True),
)


def acl_signature(wall_side: Any, groups: Sequence[str]) -> tuple[str, str, list[str]]:
    """(signature, wall_side, sorted unique groups) for an ACL set."""
    wall = wall_side.value if hasattr(wall_side, "value") else str(wall_side)
    clean = sorted({str(g) for g in groups if g})
    return wall + "|" + "\x1f".join(clean), wall, clean


def _sqlite_url(db_path: str | Path | None) -> str:
    if db_path is None or str(db_path) == ":memory:":
        return "sqlite+aiosqlite:///:memory:"
    p = Path(db_path).resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{p.as_posix()}"


_WRITE_SQL = re.compile(
    r"^\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)\s+[\"\[`]?(\w+)",
    re.IGNORECASE,
)
_READ_SQL = re.compile(r"^\s*(?:SELECT|PRAGMA|WITH|EXPLAIN|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE)\b", re.IGNORECASE)

#: Upper bound on how long a cached full-table scan is reused, whatever the change detection says.
SCAN_CACHE_TTL_SECONDS = 2.0


class _ReleaseHookLock(asyncio.Lock):
    """asyncio.Lock that runs a callback just before each release (i.e. after a write committed)."""

    def __init__(self, on_release: Any) -> None:
        super().__init__()
        self._on_release = on_release

    def release(self) -> None:
        try:
            self._on_release()
        finally:
            super().release()


class SqliteBitemporalStore(VocabularyStoreMixin):
    """SQLAlchemy 2.x async bitemporal store implementing StorePort + FTS5 + Windows lock hygiene."""

    def __init__(
        self,
        db_path: str | Path | None = ":memory:",
        *,
        database_url: str | None = None,
        policy: PolicyEngine | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.db_path = db_path
        self.database_url = database_url or _sqlite_url(db_path)
        self.policy = policy or PolicyEngine()
        self.repo_root = repo_root or Path(__file__).resolve().parents[3]
        self._is_sqlite = self.database_url.startswith("sqlite")
        self._is_memory = ":memory:" in self.database_url

        self._file_path: str | None = None
        if self._is_sqlite and not self._is_memory:
            self._file_path = make_url(self.database_url).database
        # The pooled engine is bound to the event loop that first uses it (aiosqlite connections and
        # the pool queue are loop-bound). Calls from any other running loop (e.g. the sync bridge in
        # context_agents that does asyncio.run in a worker thread) get their own NullPool engine,
        # which is how every call behaved before pooling.
        self._pooled_engine: AsyncEngine | None = None
        self._pooled_loop: asyncio.AbstractEventLoop | None = None
        self._other_engines: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, AsyncEngine] = (
            weakref.WeakKeyDictionary()
        )
        self._fallback_engine: AsyncEngine | None = None

        self._initialized = False
        self._fts_enabled = False
        self._write_lock = _ReleaseHookLock(self._after_own_write)
        # Read cache for full-table scans (entities, claims).
        # - Writes by this store: every INSERT/UPDATE/DELETE this engine executes bumps a per-table
        #   counter (any other write statement bumps all tables), so they invalidate exactly.
        # - Writes by anything else (another store instance, another process, a raw sqlite3 handle):
        #   detected with SQLite's PRAGMA data_version on a dedicated connection. Our own commits also
        #   move it, so it is re-read after each of our writes; a foreign commit landing in that same
        #   instant could be missed, which SCAN_CACHE_TTL_SECONDS bounds.
        # Nothing is cached while a write holds the lock, and a load is only cached if its key did
        # not move while it ran.
        self._scan_cache: dict[str, tuple[tuple[int, int, int], float, list[Any]]] = {}
        self._table_writes: dict[str, int] = {}
        self._any_writes = 0
        self._foreign_writes = 0
        self._own_data_version: int | None = None
        self._version_conn: sqlite3.Connection | None = None
        self._version_identity: tuple[int, int] | None = None
        self._version_lock = threading.Lock()
        self._new_vocab_terms: list[VocabTerm] = []

    def _make_engine(self, *, pooled: bool) -> AsyncEngine:
        engine_kwargs: dict[str, Any] = {"future": True}
        if self._is_sqlite:
            if self._is_memory:
                engine_kwargs["poolclass"] = StaticPool
                engine_kwargs["connect_args"] = {"check_same_thread": False}
            elif pooled:
                # A small pool: NullPool reopened the database file and started a new aiosqlite
                # thread for every query, which dominated request latency. close() disposes it, so
                # no handle outlives the store (no WinError 32 file locks on Windows).
                engine_kwargs["poolclass"] = AsyncAdaptedQueuePool
                engine_kwargs["pool_size"] = 5
                engine_kwargs["max_overflow"] = 10
                engine_kwargs["connect_args"] = {"timeout": 30}
            else:
                engine_kwargs["poolclass"] = NullPool
                engine_kwargs["connect_args"] = {"timeout": 30}
        engine = create_async_engine(self.database_url, **engine_kwargs)
        if self._is_sqlite:
            sync_engine = engine.sync_engine

            @event.listens_for(sync_engine, "connect")
            def _configure_sqlite(dbapi_conn: Any, record: Any) -> None:
                cur = dbapi_conn.cursor()
                try:
                    cur.execute("PRAGMA busy_timeout = 30000")
                    cur.execute("PRAGMA foreign_keys = ON")
                    if not self._is_memory:
                        cur.execute("PRAGMA synchronous = NORMAL")
                finally:
                    cur.close()
                record.info["file_identity"] = self._file_identity()

            @event.listens_for(sync_engine, "checkout")
            def _check_same_file(_dbapi_conn: Any, record: Any, _proxy: Any) -> None:
                # A pooled handle keeps reading the file it opened. If the database file has been
                # replaced or deleted since (restore from backup, reseed), drop the handle and
                # reconnect, as NullPool effectively did.
                opened = record.info.get("file_identity")
                if opened is not None and opened != self._file_identity():
                    raise DisconnectionError("database file replaced since this connection was opened")

            @event.listens_for(sync_engine, "before_cursor_execute")
            def _track_writes(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
                m = _WRITE_SQL.match(statement)
                if m:
                    name = m.group(1).lower()
                    self._table_writes[name] = self._table_writes.get(name, 0) + 1
                elif not _READ_SQL.match(statement):
                    self._any_writes += 1  # DDL or anything unrecognised: invalidate everything

        return engine

    def _file_identity(self) -> tuple[int, int] | None:
        if self._file_path is None:
            return None
        try:
            st = os.stat(self._file_path)
        except OSError:
            return None
        return (st.st_dev, st.st_ino)

    @property
    def _engine(self) -> AsyncEngine:
        if self._is_memory:
            # One shared in-memory database: a single StaticPool engine for every caller, as before
            if self._fallback_engine is None:
                self._fallback_engine = self._make_engine(pooled=False)
            return self._fallback_engine
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            if self._fallback_engine is None:
                self._fallback_engine = self._make_engine(pooled=False)
            return self._fallback_engine
        if self._pooled_engine is None or self._pooled_loop is None or self._pooled_loop.is_closed():
            # First loop to use the store (or the previous owner loop has gone): it owns the pool
            self._pooled_engine = self._make_engine(pooled=True)
            self._pooled_loop = loop
        if loop is self._pooled_loop:
            return self._pooled_engine
        engine = self._other_engines.get(loop)
        if engine is None:
            engine = self._make_engine(pooled=False)
            self._other_engines[loop] = engine
        return engine

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._write_lock:
            if self._initialized:
                return
            async with self._engine.begin() as conn:
                if self._is_sqlite and not self._is_memory:
                    # DELETE journal avoids lingering -wal/-shm files on Windows teardown; set once at init
                    await conn.execute(text("PRAGMA journal_mode = DELETE"))
                await conn.run_sync(metadata.create_all)
                if self._is_sqlite:
                    try:
                        await conn.execute(
                            text(
                                "CREATE VIRTUAL TABLE IF NOT EXISTS fts_documents "
                                "USING fts5(doc_type UNINDEXED, doc_id UNINDEXED, title, body, tokenize='unicode61')"
                            )
                        )
                        self._fts_enabled = True
                    except Exception:
                        self._fts_enabled = False
                await self._migrate_acl_columns(conn)
                if (await conn.execute(select(t_vocab_terms.c.term_id).limit(1))).first() is None:
                    await self._vocab_save_conn(conn, default_terms())
                row = (await conn.execute(select(t_system_epochs).where(t_system_epochs.c.id == 1))).first()
                if row is None:
                    await conn.execute(
                        t_system_epochs.insert().values(
                            id=1,
                            data_epoch=1,
                            policy_epoch=1,
                            updated_at=format_utc(utc_now()),
                            last_reason="initialize",
                            last_actor="system",
                        )
                    )
            self._initialized = True

    async def close(self) -> None:
        self._scan_cache.clear()
        with self._version_lock:
            if self._version_conn is not None:
                try:
                    self._version_conn.close()
                finally:
                    self._version_conn = None
        try:
            current: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if self._pooled_engine is not None:
            if current is not None and current is self._pooled_loop:
                await self._pooled_engine.dispose()
            else:
                # Owned by another (possibly closed) loop: release the handles without awaiting
                await self._pooled_engine.dispose(close=False)
            self._pooled_engine = None
            self._pooled_loop = None
        for engine in list(self._other_engines.values()):
            await engine.dispose(close=False)
        self._other_engines.clear()
        if self._fallback_engine is not None:
            await self._fallback_engine.dispose()
            self._fallback_engine = None
        self._initialized = False

    def _data_version(self) -> int:
        """SQLite's per-connection change counter for commits made by other connections.

        Runs on the event loop, so it never waits for a lock (timeout=0): if the database is busy it
        raises sqlite3.OperationalError and the caller treats that as a cache miss. If the database
        file was replaced, the watcher reopens it and the change counts as a foreign write.
        """
        if self._is_memory or not self._is_sqlite or self._file_path is None:
            return 0
        # The watcher connection is shared by every thread/event loop using this store
        with self._version_lock:
            return self._data_version_locked()

    def _data_version_locked(self) -> int:
        identity = self._file_identity()
        if self._version_conn is not None and identity != self._version_identity:
            self._version_conn.close()
            self._version_conn = None
            self._foreign_writes += 1
        if self._version_conn is None:
            if identity is None:
                raise sqlite3.OperationalError("database file missing")
            self._version_conn = sqlite3.connect(self._file_path, check_same_thread=False, timeout=0)
            self._version_identity = identity
        return int(self._version_conn.execute("PRAGMA data_version").fetchone()[0])

    def _after_own_write(self) -> None:
        """Re-baseline data_version after this store's own commit so it is not seen as foreign."""
        try:
            dv = self._data_version()
        except sqlite3.Error:
            self._foreign_writes += 1
            self._own_data_version = None
            return
        self._own_data_version = dv

    def _cache_key(self, table: str) -> tuple[int, int, int] | None:
        """Current cache key for ``table``, or None while a write holds the lock (no caching then)."""
        if self._write_lock.locked():
            return None
        try:
            dv = self._data_version()
        except sqlite3.Error:
            return None
        if dv != self._own_data_version:
            # Someone else committed since our last look
            self._foreign_writes += 1
            self._own_data_version = dv
        return (self._table_writes.get(table, 0), self._any_writes, self._foreign_writes)

    async def _cached_scan(self, name: str, load: Any) -> list[Any]:
        """Return the converted rows of a full-table read, reusing them until any write lands.

        ``load`` is an async callable returning the freshly converted list. The cached list is shared,
        so callers must copy (``clone_model``) or sanitize before handing objects out.
        """
        key = self._cache_key(name)
        hit = self._scan_cache.get(name)
        now = time.monotonic()
        if key is not None and hit is not None and hit[0] == key and now - hit[1] < SCAN_CACHE_TTL_SECONDS:
            return hit[2]
        items = await load()
        if key is not None and self._cache_key(name) == key:
            self._scan_cache[name] = (key, now, items)
        return items

    async def _ensure_init(self) -> None:
        if not self._initialized:
            await self.initialize()

    # ------------------------------------------------------------------
    # ACL sets: access checks inside the query (ADR-008)
    # ------------------------------------------------------------------

    async def _migrate_acl_columns(self, conn: Any) -> None:
        """Add acl_set_id to databases created before ADR-008 and fill it for every row."""
        memo: dict[str, int] = {}
        if self._is_sqlite:
            for table in ("entities", "claims"):
                cols = {r[1] for r in (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()}
                if "acl_set_id" not in cols:
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN acl_set_id INTEGER"))
                    await conn.execute(
                        text(f"CREATE INDEX IF NOT EXISTS ix_{table}_acl_set_id ON {table} (acl_set_id)")
                    )
        for r in (
            await conn.execute(
                select(t_entities.c.entity_id, t_entities.c.wall_side, t_entities.c.read_acl_groups_json).where(
                    t_entities.c.acl_set_id.is_(None)
                )
            )
        ).fetchall():
            acl_id = await self._acl_set_id_conn(conn, r.wall_side, json.loads(r.read_acl_groups_json), memo)
            await conn.execute(t_entities.update().where(t_entities.c.entity_id == r.entity_id).values(acl_set_id=acl_id))
        for r in (
            await conn.execute(select(t_claims.c.claim_id, t_claims.c.acl_json).where(t_claims.c.acl_set_id.is_(None)))
        ).fetchall():
            acl = json.loads(r.acl_json)
            acl_id = await self._acl_set_id_conn(
                conn, acl.get("wall_side", "public_side"), acl.get("read_acl_groups") or [], memo
            )
            await conn.execute(t_claims.update().where(t_claims.c.claim_id == r.claim_id).values(acl_set_id=acl_id))

    async def _acl_set_id_conn(
        self, conn: Any, wall_side: Any, groups: Sequence[str], memo: dict[str, int] | None = None
    ) -> int:
        """Get or create the ACL set for (wall_side, groups). Call inside a write transaction.

        ``memo`` caches lookups for the length of one transaction (bulk import). Nothing is cached
        across transactions, so a rolled-back insert or a replaced database file can't leave a
        stale id behind.
        """
        sig, wall, clean = acl_signature(wall_side, groups)
        if memo is not None and sig in memo:
            return memo[sig]
        row = (await conn.execute(select(t_acl_sets.c.acl_set_id).where(t_acl_sets.c.signature == sig))).first()
        if row is None:
            res = await conn.execute(t_acl_sets.insert().values(signature=sig, wall_side=wall))
            acl_id = int(res.inserted_primary_key[0])
            if clean:
                await conn.execute(
                    t_acl_set_groups.insert(), [{"acl_set_id": acl_id, "group_name": g} for g in clean]
                )
        else:
            acl_id = int(row.acl_set_id)
        if memo is not None:
            memo[sig] = acl_id
        return acl_id

    @staticmethod
    def _readable_acl_sets_query(principal: Principal) -> Any:
        """SELECT of the acl_set_ids this caller may read: the wall matrix plus any-of group match.

        A set with no groups never matches, which keeps the fail-closed rule of PolicyEngine.
        """
        walls = [w.value for w in WallSide if wall_allows(principal.wall_side, w)]
        groups = sorted(principal.groups)
        return (
            select(t_acl_sets.c.acl_set_id)
            .join(t_acl_set_groups, t_acl_set_groups.c.acl_set_id == t_acl_sets.c.acl_set_id)
            .where(t_acl_sets.c.wall_side.in_(walls))
            .where(t_acl_set_groups.c.group_name.in_(groups or ["\x00no-groups"]))
            .distinct()
        )

    async def _readable_acl_set_ids(self, principal: Principal) -> set[int]:
        async with self._engine.connect() as conn:
            return {int(r[0]) for r in (await conn.execute(self._readable_acl_sets_query(principal))).fetchall()}

    # ------------------------------------------------------------------
    # Vocabulary storage primitives (ADR-008)
    # ------------------------------------------------------------------

    @staticmethod
    def _vocab_row(t: VocabTerm) -> dict[str, Any]:
        return dict(
            term_id=t.term_id,
            vocab=t.vocab,
            key=t.key,
            label=t.label,
            parent_key=t.parent_key,
            description=t.description,
            aliases_json=json.dumps(t.aliases),
            status=t.status,
            merged_into_key=t.merged_into_key,
            attributes_json=json.dumps(t.attributes),
            owner_group=t.owner_group,
            created_by=t.created_by,
            created_at=format_utc(t.created_at),
            updated_at=format_utc(t.updated_at),
        )

    @staticmethod
    def _row_to_term(r: Any) -> VocabTerm:
        return VocabTerm(
            vocab=r.vocab,
            key=r.key,
            label=r.label,
            parent_key=r.parent_key,
            description=r.description,
            aliases=json.loads(r.aliases_json),
            status=r.status,
            merged_into_key=r.merged_into_key,
            attributes=json.loads(r.attributes_json),
            owner_group=r.owner_group,
            created_by=r.created_by,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )

    async def _vocab_save_conn(self, conn: Any, terms: Sequence[VocabTerm]) -> None:
        ids = [t.term_id for t in terms]
        for n in range(0, len(ids), 500):
            await conn.execute(delete(t_vocab_terms).where(t_vocab_terms.c.term_id.in_(ids[n : n + 500])))
        rows = [self._vocab_row(t) for t in terms]
        for n in range(0, len(rows), 1000):
            await conn.execute(t_vocab_terms.insert(), rows[n : n + 1000])

    async def _vocab_load(self) -> list[VocabTerm]:
        await self._ensure_init()
        async with self._engine.connect() as conn:
            return [self._row_to_term(r) for r in (await conn.execute(select(t_vocab_terms))).fetchall()]

    async def _vocab_count(self) -> int:
        await self._ensure_init()
        async with self._engine.connect() as conn:
            return len((await conn.execute(select(t_vocab_terms.c.term_id))).fetchall())

    async def _vocab_save(self, terms: Sequence[VocabTerm]) -> None:
        if not terms:
            return
        await self._ensure_init()
        async with self._write_lock:
            async with self._engine.begin() as conn:
                await self._vocab_save_conn(conn, terms)

    async def _vocab_replace_all(self, terms: Sequence[VocabTerm]) -> None:
        await self._ensure_init()
        async with self._write_lock:
            async with self._engine.begin() as conn:
                await conn.execute(delete(t_vocab_terms))
                await self._vocab_save_conn(conn, terms)

    async def _effective_principal(self, principal: Principal) -> Principal:
        if principal.revoked:
            return principal
        if await self.is_principal_revoked(principal.user_id):
            return principal.model_copy(update={"revoked": True})
        return principal

    async def _bump_data_epoch_conn(
        self, conn: Any, *, reason: str, actor: str
    ) -> int:
        row = (await conn.execute(select(t_system_epochs).where(t_system_epochs.c.id == 1))).first()
        new_epoch = (row.data_epoch + 1) if row else 2
        await conn.execute(
            t_system_epochs.update()
            .where(t_system_epochs.c.id == 1)
            .values(
                data_epoch=new_epoch,
                updated_at=format_utc(utc_now()),
                last_reason=reason,
                last_actor=actor,
            )
        )
        return new_epoch

    async def _fts_upsert(self, conn: Any, doc_type: str, doc_id: str, title: str, body: str) -> None:
        if not self._fts_enabled:
            return
        await conn.execute(
            text("DELETE FROM fts_documents WHERE doc_type = :dt AND doc_id = :did"),
            {"dt": doc_type, "did": doc_id},
        )
        await conn.execute(
            text("INSERT INTO fts_documents(doc_type, doc_id, title, body) VALUES (:dt, :did, :title, :body)"),
            {"dt": doc_type, "did": doc_id, "title": title, "body": body},
        )

    async def fts_search(self, query: str, *, limit: int = 100) -> list[tuple[str, str, float]]:
        """Execute SQLite FTS5 lexical match returning (doc_type, doc_id, score). Caller MUST apply ACL filtering!"""
        await self._ensure_init()
        if not self._fts_enabled or not query.strip():
            return []
        tokens = [
            "".join(ch for ch in tok if ch.isalnum() or ch in ("_", "-"))
            for tok in query.strip().split()
        ]
        clean_tokens = [t for t in tokens if t]
        if not clean_tokens:
            return []
        fts_expr = " AND ".join(f'"{t}"*' for t in clean_tokens)
        async with self._engine.connect() as conn:
            try:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT doc_type, doc_id, bm25(fts_documents) AS rank_score "
                            "FROM fts_documents WHERE fts_documents MATCH :q "
                            "ORDER BY rank_score ASC LIMIT :lim"
                        ),
                        {"q": fts_expr, "lim": limit},
                    )
                ).fetchall()
            except Exception:
                return []
        results: list[tuple[str, str, float]] = []
        for dt, did, rank_score in rows:
            # bm25 returns negative values (more negative = better match) in SQLite FTS5
            score = 1.0 / (1.0 + max(0.0, float(rank_score) + 10.0)) if rank_score is not None else 1.0
            results.append((str(dt), str(did), score))
        return results

    # ------------------------------------------------------------------
    # Epochs & Revocation
    # ------------------------------------------------------------------

    async def get_epochs(self) -> PolicyEpochState:
        await self._ensure_init()
        async with self._engine.connect() as conn:
            row = (await conn.execute(select(t_system_epochs).where(t_system_epochs.c.id == 1))).first()
            assert row is not None
            return PolicyEpochState(
                data_epoch=row.data_epoch,
                policy_epoch=row.policy_epoch,
                updated_at=row.updated_at,
                last_reason=row.last_reason,
                last_actor=row.last_actor,
            )

    async def bump_policy_epoch(self, reason: str, actor: str) -> PolicyEpochState:
        await self._ensure_init()
        async with self._write_lock:
            async with self._engine.begin() as conn:
                row = (await conn.execute(select(t_system_epochs).where(t_system_epochs.c.id == 1))).first()
                assert row is not None
                new_pe = row.policy_epoch + 1
                now = utc_now()
                await conn.execute(
                    t_system_epochs.update()
                    .where(t_system_epochs.c.id == 1)
                    .values(
                        policy_epoch=new_pe,
                        updated_at=format_utc(now),
                        last_reason=reason,
                        last_actor=actor,
                    )
                )
                return PolicyEpochState(
                    data_epoch=row.data_epoch,
                    policy_epoch=new_pe,
                    updated_at=now,
                    last_reason=reason,
                    last_actor=actor,
                )

    async def is_principal_revoked(self, user_id: str) -> bool:
        await self._ensure_init()
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(t_revoked_principals.c.user_id).where(t_revoked_principals.c.user_id == user_id)
                )
            ).first()
            return row is not None

    async def revoke_principal(self, user_id: str, *, reason: str, actor: str) -> PolicyEpochState:
        await self._ensure_init()
        async with self._write_lock:
            async with self._engine.begin() as conn:
                await conn.execute(
                    delete(t_revoked_principals).where(t_revoked_principals.c.user_id == user_id)
                )
                await conn.execute(
                    t_revoked_principals.insert().values(
                        user_id=user_id,
                        reason=reason,
                        actor=actor,
                        revoked_at=format_utc(utc_now()),
                    )
                )
                row = (await conn.execute(select(t_system_epochs).where(t_system_epochs.c.id == 1))).first()
                assert row is not None
                new_pe = row.policy_epoch + 1
                now = utc_now()
                await conn.execute(
                    t_system_epochs.update()
                    .where(t_system_epochs.c.id == 1)
                    .values(
                        policy_epoch=new_pe,
                        updated_at=format_utc(now),
                        last_reason=f"revoke:{user_id}:{reason}",
                        last_actor=actor,
                    )
                )
                return PolicyEpochState(
                    data_epoch=row.data_epoch,
                    policy_epoch=new_pe,
                    updated_at=now,
                    last_reason=f"revoke:{user_id}:{reason}",
                    last_actor=actor,
                )

    # ------------------------------------------------------------------
    # Row Deserializers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_entity(row: Any) -> Entity:
        return Entity(
            entity_id=row.entity_id,
            entity_type=row.entity_type,
            canonical_key=row.canonical_key,
            display_name=row.display_name,
            aliases=json.loads(row.aliases_json),
            description=row.description,
            owner_group=row.owner_group,
            wall_side=WallSide(row.wall_side),
            read_acl_groups=json.loads(row.read_acl_groups_json),
            write_acl_groups=json.loads(row.write_acl_groups_json),
            attributes=json.loads(row.attributes_json),
            status=row.status,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _row_to_identifier(row: Any) -> EntityIdentifier:
        return EntityIdentifier(
            id=row.id,
            entity_id=row.entity_id,
            scheme=row.scheme,
            identifier_value=row.identifier_value,
            authority=row.authority,
            is_primary=bool(row.is_primary),
            valid_from=row.valid_from,
            valid_to=row.valid_to,
            recorded_at=row.recorded_at,
            superseded_at=row.superseded_at,
        )

    @staticmethod
    def _row_to_lineage(row: Any) -> EntityLineageEvent:
        return EntityLineageEvent(
            event_id=row.event_id,
            source_entity_id=row.source_entity_id,
            target_entity_id=row.target_entity_id,
            event_type=row.event_type,
            effective_date=row.effective_date,
            recorded_at=row.recorded_at,
            summary=row.summary,
            evidence_uri=row.evidence_uri,
            evidence_acl_groups=json.loads(row.evidence_acl_groups_json),
            wall_side=WallSide(row.wall_side),
            read_acl_groups=json.loads(row.read_acl_groups_json),
        )

    @staticmethod
    def _row_to_position(row: Any) -> Position:
        return Position(
            position_id=row.position_id,
            vehicle_entity_id=row.vehicle_entity_id,
            instrument_entity_id=row.instrument_entity_id,
            issuer_entity_id=row.issuer_entity_id,
            position_type=row.position_type,
            quantity=row.quantity,
            market_value=row.market_value,
            currency=row.currency,
            weight_bps=row.weight_bps,
            as_of_date=date.fromisoformat(row.as_of_date),
            recorded_at=row.recorded_at,
            source_system=row.source_system,
            wall_side=WallSide(row.wall_side),
            read_acl_groups=json.loads(row.read_acl_groups_json),
        )

    @staticmethod
    def _row_to_coverage(row: Any) -> Coverage:
        return Coverage(
            coverage_id=row.coverage_id,
            analyst_entity_id=row.analyst_entity_id,
            issuer_entity_id=row.issuer_entity_id,
            coverage_role=row.coverage_role,
            coverage_status=row.coverage_status,
            valid_from=row.valid_from,
            valid_to=row.valid_to,
            wall_side=WallSide(row.wall_side),
            read_acl_groups=json.loads(row.read_acl_groups_json),
        )

    @staticmethod
    def _row_to_skill_binding(row: Any) -> SkillBinding:
        return SkillBinding(
            binding_id=row.binding_id,
            skill_entity_id=row.skill_entity_id,
            target_entity_id=row.target_entity_id,
            mapping_purpose=row.mapping_purpose,
            read_mode=row.read_mode,
            required_predicates=json.loads(row.required_predicates_json),
            active=bool(row.active),
        )

    @staticmethod
    def _row_to_claim(row: Any) -> Claim:
        return Claim(
            claim_id=row.claim_id,
            subject_id=row.subject_id,
            predicate=row.predicate,
            object_value=row.object_value,
            object_entity_id=row.object_entity_id,
            context_purpose=row.context_purpose,
            scope_type=row.scope_type,
            scope_id=row.scope_id,
            channel=row.channel,
            epistemic_status=row.epistemic_status,
            kind=row.kind,
            is_mandatory=bool(row.is_mandatory),
            facets=ClaimFacets.model_validate(json.loads(row.facets_json)),
            provenance=Provenance.model_validate(json.loads(row.provenance_json)),
            acl=ClaimACL.model_validate(json.loads(row.acl_json)),
            valid_from=row.valid_from,
            valid_to=row.valid_to,
            recorded_at=row.recorded_at,
            superseded_at=row.superseded_at,
            superseded_by=row.superseded_by,
            created_by=row.created_by,
        )

    @staticmethod
    def _row_to_topic(row: Any) -> Topic:
        return Topic(
            topic_id=row.topic_id,
            slug=row.slug,
            pref_label=row.pref_label,
            alt_labels=json.loads(row.alt_labels_json),
            broader_topic_id=row.broader_topic_id,
            narrower_topic_ids=json.loads(row.narrower_topic_ids_json),
            related_topic_ids=json.loads(row.related_topic_ids_json),
            topic_kind=row.topic_kind,
            merged_into_topic_id=row.merged_into_topic_id,
            owner_group=row.owner_group,
            status=row.status,
        )

    @staticmethod
    def _row_to_attachment(row: Any) -> TopicAttachment:
        return TopicAttachment(
            attachment_id=row.attachment_id,
            topic_id=row.topic_id,
            target_type=row.target_type,
            target_id=row.target_id,
            attachment_source=row.attachment_source,
            confidence=row.confidence,
            wall_side=WallSide(row.wall_side),
            read_acl_groups=json.loads(row.read_acl_groups_json),
        )

    @staticmethod
    def _row_to_knowledge_item(row: Any) -> KnowledgeItem:
        return KnowledgeItem(
            item_id=row.item_id,
            knowledge_kind=row.knowledge_kind,
            display_name=row.display_name,
            slug=row.slug,
            source_path=row.source_path,
            body_markdown=row.body_markdown,
            wall_side=WallSide(row.wall_side),
            read_acl_groups=json.loads(row.read_acl_groups_json),
            write_acl_groups=json.loads(row.write_acl_groups_json),
            topics=json.loads(row.topics_json),
            related_entities=json.loads(row.related_entities_json),
            metadata=json.loads(row.metadata_json),
            content_sha256=row.content_sha256,
            valid_from=row.valid_from,
            recorded_at=row.recorded_at,
            okf_version=row.okf_version,
        )

    @staticmethod
    def _row_to_patch(row: Any) -> CuratorDraftPatch:
        return CuratorDraftPatch(
            patch_id=row.patch_id,
            wall_side=WallSide(row.wall_side),
            read_acl_groups=json.loads(row.read_acl_groups_json),
            target_type=row.target_type,
            target_id=row.target_id,
            title=row.title,
            rationale=row.rationale,
            unified_diff=row.unified_diff,
            proposed_changes=json.loads(row.proposed_changes_json),
            source_claim_ids=json.loads(row.source_claim_ids_json),
            stale_evidence_paths=json.loads(row.stale_evidence_paths_json),
            status=row.status,
            created_at=row.created_at,
            created_by=row.created_by,
            reviewed_by=row.reviewed_by,
            reviewed_at=row.reviewed_at,
        )

    @staticmethod
    def _row_to_memory(row: Any) -> MemoryRecord:
        return MemoryRecord(
            memory_id=row.memory_id,
            user_id=row.user_id,
            scope_type=row.scope_type,
            skill_id=row.skill_id,
            key=row.key,
            content=row.content,
            tags=json.loads(row.tags_json),
            linked_entity_ids=json.loads(row.linked_entity_ids_json),
            linked_topic_ids=json.loads(row.linked_topic_ids_json),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    # ------------------------------------------------------------------
    # Plane 1: Entities, Crosswalks, Lineage & Reified Associations
    # ------------------------------------------------------------------

    @staticmethod
    def _entity_row(entity: Entity, acl_set_id: int | None = None) -> dict[str, Any]:
        return dict(
            acl_set_id=acl_set_id,
            entity_id=entity.entity_id,
            entity_type=entity.entity_type,
            canonical_key=entity.canonical_key,
            display_name=entity.display_name,
            aliases_json=json.dumps(entity.aliases),
            description=entity.description,
            owner_group=entity.owner_group,
            wall_side=entity.wall_side.value,
            read_acl_groups_json=json.dumps(entity.read_acl_groups),
            write_acl_groups_json=json.dumps(entity.write_acl_groups),
            attributes_json=json.dumps(entity.attributes),
            status=entity.status,
            created_at=format_utc(entity.created_at),
            updated_at=format_utc(entity.updated_at),
        )

    @staticmethod
    def _entity_fts(entity: Entity) -> tuple[str, str, str, str]:
        return (
            "entity",
            entity.entity_id,
            f"{entity.display_name} {entity.canonical_key}",
            f"{' '.join(entity.aliases)} {entity.description} {entity.entity_type}",
        )

    async def _upsert_entity_conn(self, conn: Any, entity: Entity) -> None:
        await conn.execute(delete(t_entities).where(t_entities.c.entity_id == entity.entity_id))
        acl_id = await self._acl_set_id_conn(conn, entity.wall_side, entity.read_acl_groups)
        await conn.execute(t_entities.insert().values(**self._entity_row(entity, acl_id)))
        await self._fts_upsert(conn, *self._entity_fts(entity))

    async def upsert_entity(self, entity: Entity) -> Entity:
        await self._ensure_init()
        async with self._write_lock:
            async with self._engine.begin() as conn:
                await self._upsert_entity_conn(conn, entity)
                await self._bump_data_epoch_conn(conn, reason=f"upsert_entity:{entity.entity_id}", actor="system")
        await self._register_record_terms("entities", entity)
        return entity

    async def _get_entity_raw(self, conn: Any, entity_id_or_key: str) -> Entity | None:
        row = (
            await conn.execute(
                select(t_entities).where(
                    (t_entities.c.entity_id == entity_id_or_key)
                    | (t_entities.c.canonical_key == entity_id_or_key.lower())
                )
            )
        ).first()
        return self._row_to_entity(row) if row is not None else None

    async def get_entity(self, entity_id_or_key: str, *, principal: Principal) -> Entity | None:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        async with self._engine.connect() as conn:
            ent = await self._get_entity_raw(conn, entity_id_or_key)
        if ent is None or not self.policy.can_read_entity(eff, ent):
            return None
        return ent

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
        readable = self._readable_acl_sets_query(eff)

        if topic_slug is None and limit is not None:
            # Paged read: access, type, order and page all run inside the query, so a page is
            # always full and the cost follows what the caller can see (ADR-008).
            stmt = select(t_entities).where(t_entities.c.acl_set_id.in_(readable))
            if type_set is not None:
                stmt = stmt.where(t_entities.c.entity_type.in_(sorted(type_set)))
            stmt = stmt.order_by(t_entities.c.canonical_key.asc()).limit(limit).offset(offset)
            async with self._engine.connect() as c:
                rows = (await c.execute(stmt)).fetchall()
            # Defence in depth: the policy engine still checks every row it hands out
            return [ent for r in rows if self.policy.can_read_entity(eff, (ent := self._row_to_entity(r)))]

        # Whole-list reads (graph, retrieval) reuse the shared scan cache; the caller's readable ACL
        # sets still come from the database, so the access decision is the same one the query makes.
        async def _load_entities() -> list[tuple[int | None, Entity]]:
            async with self._engine.connect() as c:
                stmt = select(t_entities).order_by(t_entities.c.canonical_key.asc())
                return [(r.acl_set_id, self._row_to_entity(r)) for r in (await c.execute(stmt)).fetchall()]

        all_entities = await self._cached_scan("entities", _load_entities)
        async with self._engine.connect() as c:
            readable_ids = {int(r[0]) for r in (await c.execute(readable)).fetchall()}
        authorized = [
            ent
            for acl_id, ent in all_entities
            if acl_id in readable_ids
            and (type_set is None or ent.entity_type in type_set)
            and self.policy.can_read_entity(eff, ent)
        ]
        if topic_slug:
            async with self._engine.connect() as conn:
                slug_l = topic_slug.strip().lower()
                topic_rows = (await conn.execute(select(t_topics))).fetchall()
                all_topics = [self._row_to_topic(r) for r in topic_rows]
                t_by_slug = {t.slug: t for t in all_topics}
                matching_tids: set[str] = set()
                root_t = t_by_slug.get(slug_l)
                if root_t:
                    matching_tids.add(root_t.topic_id)
                    frontier = [root_t.topic_id]
                    while frontier:
                        cur = frontier.pop()
                        for t in all_topics:
                            if (t.broader_topic_id == cur or t.merged_into_topic_id == cur) and t.topic_id not in matching_tids:
                                matching_tids.add(t.topic_id)
                                frontier.append(t.topic_id)
                att_rows = (await conn.execute(select(t_topic_attachments))).fetchall()
                claim_rows = (await conn.execute(select(t_claims))).fetchall()
                claim_subj = {
                    c.claim_id: c.subject_id
                    for r in claim_rows
                    if self.policy.can_read_claim(eff, (c := self._row_to_claim(r)))
                }
                matched_eids: set[str] = set()
                for r in att_rows:
                    att = self._row_to_attachment(r)
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
        page = authorized[offset:] if limit is None else authorized[offset : offset + limit]
        # Entities come from the shared scan cache, so hand out copies
        return [clone_model(ent) for ent in page]

    async def upsert_identifier(self, ident: EntityIdentifier) -> EntityIdentifier:
        await self._ensure_init()
        async with self._write_lock:
            async with self._engine.begin() as conn:
                await conn.execute(
                    delete(t_entity_identifiers).where(t_entity_identifiers.c.id == ident.id)
                )
                await conn.execute(
                    t_entity_identifiers.insert().values(
                        id=ident.id,
                        entity_id=ident.entity_id,
                        scheme=ident.scheme,
                        identifier_value=ident.identifier_value,
                        authority=ident.authority,
                        is_primary=ident.is_primary,
                        valid_from=format_utc(ident.valid_from),
                        valid_to=format_utc(ident.valid_to),
                        recorded_at=format_utc(ident.recorded_at),
                        superseded_at=format_utc(ident.superseded_at),
                    )
                )
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
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(t_entity_identifiers).where(t_entity_identifiers.c.entity_id == entity_id)
                )
            ).fetchall()
        idents = [self._row_to_identifier(r) for r in rows]
        if as_of is not None or recorded_as_of is not None:
            idents = [
                i for i in idents if is_identifier_visible(i, as_of=as_of, recorded_as_of=recorded_as_of)
            ]
        return idents

    def _insertion_order(self, table: Table) -> list[Any]:
        """ORDER BY terms that list a table's rows in the order they were written."""
        return [text("rowid")]

    async def list_identifiers_for(
        self,
        entity_ids: Sequence[str] | None = None,
        *,
        as_of: datetime | None = None,
        recorded_as_of: datetime | None = None,
    ) -> dict[str, list[EntityIdentifier]]:
        """Batched ``list_identifiers``: one query for many entities (``None`` = all entities).

        Returns ``{entity_id: [identifiers]}`` with the same per-entity order and bitemporal
        filtering as calling ``list_identifiers`` once per entity.
        """
        await self._ensure_init()
        wanted = None if entity_ids is None else set(entity_ids)
        if wanted is not None and not wanted:
            return {}
        async with self._engine.connect() as conn:
            stmt = select(t_entity_identifiers)
            if wanted is not None and len(wanted) <= 500:
                stmt = stmt.where(t_entity_identifiers.c.entity_id.in_(sorted(wanted)))
            rows = (await conn.execute(stmt.order_by(*self._insertion_order(t_entity_identifiers)))).fetchall()
        out: dict[str, list[EntityIdentifier]] = {}
        for r in rows:
            if wanted is not None and r.entity_id not in wanted:
                continue
            ident = self._row_to_identifier(r)
            if (as_of is not None or recorded_as_of is not None) and not is_identifier_visible(
                ident, as_of=as_of, recorded_as_of=recorded_as_of
            ):
                continue
            out.setdefault(ident.entity_id, []).append(ident)
        return out

    async def add_lineage_event(self, event: EntityLineageEvent) -> EntityLineageEvent:
        await self._ensure_init()
        async with self._write_lock:
            async with self._engine.begin() as conn:
                await conn.execute(
                    delete(t_entity_lineage_events).where(
                        t_entity_lineage_events.c.event_id == event.event_id
                    )
                )
                await conn.execute(
                    t_entity_lineage_events.insert().values(
                        event_id=event.event_id,
                        source_entity_id=event.source_entity_id,
                        target_entity_id=event.target_entity_id,
                        event_type=event.event_type,
                        effective_date=format_utc(event.effective_date),
                        recorded_at=format_utc(event.recorded_at),
                        summary=event.summary,
                        evidence_uri=sanitize_uri_secrets(event.evidence_uri),
                        evidence_acl_groups_json=json.dumps(event.evidence_acl_groups),
                        wall_side=event.wall_side.value,
                        read_acl_groups_json=json.dumps(event.read_acl_groups),
                    )
                )
        await self._register_record_terms("lineage_events", event)
        return event

    async def list_lineage_events(
        self, entity_id: str, *, principal: Principal
    ) -> list[EntityLineageEvent]:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            return []
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(t_entity_lineage_events).where(
                        (t_entity_lineage_events.c.source_entity_id == entity_id)
                        | (t_entity_lineage_events.c.target_entity_id == entity_id)
                    )
                )
            ).fetchall()
        out: list[EntityLineageEvent] = []
        for r in rows:
            san = self.policy.sanitize_lineage_for_principal(eff, self._row_to_lineage(r))
            if san is not None:
                out.append(san)
        out.sort(key=lambda e: (e.effective_date, e.event_id))
        return out

    async def list_lineage_events_for(
        self, entity_ids: Sequence[str] | None = None, *, principal: Principal
    ) -> list[EntityLineageEvent]:
        """Batched ``list_lineage_events``: every caller-visible event touching any of ``entity_ids``
        (``None`` = all events) in one query, sanitized and sorted by (effective_date, event_id)."""
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            return []
        wanted = None if entity_ids is None else set(entity_ids)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(select(t_entity_lineage_events))).fetchall()
        out: list[EntityLineageEvent] = []
        for r in rows:
            if wanted is not None and r.source_entity_id not in wanted and r.target_entity_id not in wanted:
                continue
            san = self.policy.sanitize_lineage_for_principal(eff, self._row_to_lineage(r))
            if san is not None:
                out.append(san)
        out.sort(key=lambda e: (e.effective_date, e.event_id))
        return out

    async def upsert_position(self, pos: Position) -> Position:
        await self._ensure_init()
        async with self._write_lock:
            async with self._engine.begin() as conn:
                await conn.execute(delete(t_positions).where(t_positions.c.position_id == pos.position_id))
                await conn.execute(
                    t_positions.insert().values(
                        position_id=pos.position_id,
                        vehicle_entity_id=pos.vehicle_entity_id,
                        instrument_entity_id=pos.instrument_entity_id,
                        issuer_entity_id=pos.issuer_entity_id,
                        position_type=pos.position_type,
                        quantity=pos.quantity,
                        market_value=pos.market_value,
                        currency=pos.currency,
                        weight_bps=pos.weight_bps,
                        as_of_date=pos.as_of_date.isoformat(),
                        recorded_at=format_utc(pos.recorded_at),
                        source_system=pos.source_system,
                        wall_side=pos.wall_side.value,
                        read_acl_groups_json=json.dumps(pos.read_acl_groups),
                    )
                )
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
        async with self._engine.connect() as conn:
            stmt = select(t_positions)
            if vehicle_id:
                stmt = stmt.where(t_positions.c.vehicle_entity_id == vehicle_id)
            if issuer_id:
                stmt = stmt.where(t_positions.c.issuer_entity_id == issuer_id)
            rows = (await conn.execute(stmt)).fetchall()
        return [
            pos
            for r in rows
            if self.policy.can_read_record(
                eff, wall_side=(pos := self._row_to_position(r)).wall_side, read_acl_groups=pos.read_acl_groups
            )
        ]

    async def upsert_coverage(self, cov: Coverage) -> Coverage:
        await self._ensure_init()
        async with self._write_lock:
            async with self._engine.begin() as conn:
                await conn.execute(delete(t_coverages).where(t_coverages.c.coverage_id == cov.coverage_id))
                await conn.execute(
                    t_coverages.insert().values(
                        coverage_id=cov.coverage_id,
                        analyst_entity_id=cov.analyst_entity_id,
                        issuer_entity_id=cov.issuer_entity_id,
                        coverage_role=cov.coverage_role,
                        coverage_status=cov.coverage_status,
                        valid_from=format_utc(cov.valid_from),
                        valid_to=format_utc(cov.valid_to),
                        wall_side=cov.wall_side.value,
                        read_acl_groups_json=json.dumps(cov.read_acl_groups),
                    )
                )
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
        async with self._engine.connect() as conn:
            stmt = select(t_coverages)
            if issuer_id:
                stmt = stmt.where(t_coverages.c.issuer_entity_id == issuer_id)
            if status:
                stmt = stmt.where(t_coverages.c.coverage_status == status)
            rows = (await conn.execute(stmt)).fetchall()
        return [
            cov
            for r in rows
            if self.policy.can_read_record(
                eff, wall_side=(cov := self._row_to_coverage(r)).wall_side, read_acl_groups=cov.read_acl_groups
            )
        ]

    async def upsert_skill_binding(self, binding: SkillBinding) -> SkillBinding:
        await self._ensure_init()
        async with self._write_lock:
            async with self._engine.begin() as conn:
                await conn.execute(
                    delete(t_skill_bindings).where(t_skill_bindings.c.binding_id == binding.binding_id)
                )
                await conn.execute(
                    t_skill_bindings.insert().values(
                        binding_id=binding.binding_id,
                        skill_entity_id=binding.skill_entity_id,
                        target_entity_id=binding.target_entity_id,
                        mapping_purpose=binding.mapping_purpose,
                        read_mode=binding.read_mode,
                        required_predicates_json=json.dumps(binding.required_predicates),
                        active=binding.active,
                    )
                )
        return binding

    async def list_skill_bindings(
        self,
        *,
        skill_entity_id: str | None = None,
        target_entity_id: str | None = None,
    ) -> list[SkillBinding]:
        await self._ensure_init()
        async with self._engine.connect() as conn:
            stmt = select(t_skill_bindings)
            if skill_entity_id:
                stmt = stmt.where(t_skill_bindings.c.skill_entity_id == skill_entity_id)
            if target_entity_id:
                stmt = stmt.where(t_skill_bindings.c.target_entity_id == target_entity_id)
            rows = (await conn.execute(stmt)).fetchall()
        return [self._row_to_skill_binding(r) for r in rows]

    # ------------------------------------------------------------------
    # Plane 2: Bitemporal Claims & N-Way Conflicts
    # ------------------------------------------------------------------

    @staticmethod
    def _claim_row(claim: Claim, acl_set_id: int | None = None) -> dict[str, Any]:
        return dict(
            acl_set_id=acl_set_id,
            claim_id=claim.claim_id,
            subject_id=claim.subject_id,
            predicate=claim.predicate,
            object_value=claim.object_value,
            object_entity_id=claim.object_entity_id,
            context_purpose=claim.context_purpose,
            scope_type=claim.scope_type,
            scope_id=claim.scope_id,
            channel=claim.channel,
            epistemic_status=claim.epistemic_status,
            kind=claim.kind,
            is_mandatory=claim.is_mandatory,
            facets_json=json.dumps(claim.facets.model_dump(mode="json")),
            provenance_json=json.dumps(claim.provenance.model_dump(mode="json")),
            wall_side=claim.acl.wall_side.value,
            acl_json=json.dumps(claim.acl.model_dump(mode="json")),
            valid_from=format_utc(claim.valid_from),
            valid_to=format_utc(claim.valid_to),
            recorded_at=format_utc(claim.recorded_at),
            superseded_at=format_utc(claim.superseded_at),
            superseded_by=claim.superseded_by,
            created_by=claim.created_by,
        )

    @staticmethod
    def _claim_fts(claim: Claim) -> tuple[str, str, str, str]:
        # Index claim value & predicate ONLY (never restricted excerpt!) into FTS5
        return (
            "claim",
            claim.claim_id,
            f"{claim.predicate} {claim.object_value}",
            f"{claim.context_purpose} {claim.provenance.source_system} {claim.epistemic_status}",
        )

    async def _insert_claim_conn(self, conn: Any, claim: Claim) -> None:
        await conn.execute(delete(t_claims).where(t_claims.c.claim_id == claim.claim_id))
        acl_id = await self._acl_set_id_conn(conn, claim.acl.wall_side, claim.acl.read_acl_groups)
        await conn.execute(t_claims.insert().values(**self._claim_row(claim, acl_id)))
        await self._fts_upsert(conn, *self._claim_fts(claim))

    async def _upsert_conflict_conn(self, conn: Any, conflict: ConflictSet) -> None:
        await conn.execute(
            delete(t_conflict_set_members).where(
                t_conflict_set_members.c.conflict_set_id == conflict.conflict_set_id
            )
        )
        await conn.execute(
            delete(t_conflict_sets).where(t_conflict_sets.c.conflict_set_id == conflict.conflict_set_id)
        )
        await conn.execute(t_conflict_sets.insert().values(**self._conflict_row(conflict)))
        for cid in dict.fromkeys(conflict.claim_ids):
            await conn.execute(
                t_conflict_set_members.insert().values(
                    conflict_set_id=conflict.conflict_set_id,
                    claim_id=cid,
                )
            )

    @staticmethod
    def _conflict_row(conflict: ConflictSet) -> dict[str, Any]:
        return dict(
            conflict_set_id=conflict.conflict_set_id,
            subject_id=conflict.subject_id,
            predicate=conflict.predicate,
            context_purpose=conflict.context_purpose,
            scope_type=conflict.scope_type,
            scope_id=conflict.scope_id,
            status=conflict.status,
            winning_claim_id=conflict.winning_claim_id,
            resolution_rationale=conflict.resolution_rationale,
            resolved_by=conflict.resolved_by,
            resolved_at=format_utc(conflict.resolved_at),
            created_at=format_utc(conflict.created_at),
            summary=conflict.summary,
        )

    async def seed_claim_raw(self, claim: Claim) -> Claim:
        await self._ensure_init()
        async with self._write_lock:
            async with self._engine.begin() as conn:
                await self._insert_claim_conn(conn, claim)
        await self._register_record_terms("claims", claim)
        return claim

    async def seed_conflict_raw(self, conflict: ConflictSet) -> ConflictSet:
        await self._ensure_init()
        async with self._write_lock:
            async with self._engine.begin() as conn:
                await self._upsert_conflict_conn(conn, conflict)
        return conflict

    async def write_claim(
        self, payload: ClaimCreateInput, *, principal: Principal
    ) -> ClaimWriteResult:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            raise AuthenticationError("Revoked principal cannot write claims")

        async with self._write_lock:
            async with self._engine.begin() as conn:
                lookup_key = payload.subject_id or payload.subject_canonical_key or ""
                target = await self._get_entity_raw(conn, lookup_key)
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

                epoch_row = (
                    await conn.execute(select(t_system_epochs).where(t_system_epochs.c.id == 1))
                ).first()
                assert epoch_row is not None
                check_toctou_epoch(payload.expected_data_epoch, epoch_row.data_epoch)

                incoming = prepare_incoming_claim(
                    payload, target_entity=target, principal=eff, repo_root=self.repo_root
                )

                # Load active claims sharing the collision tuple
                rows = (
                    await conn.execute(
                        select(t_claims).where(
                            (t_claims.c.subject_id == incoming.subject_id)
                            & (t_claims.c.predicate == incoming.predicate)
                            & (t_claims.c.context_purpose == incoming.context_purpose)
                            & (t_claims.c.scope_type == incoming.scope_type)
                            & (t_claims.c.scope_id == incoming.scope_id)
                            & (t_claims.c.superseded_at.is_(None))
                        )
                    )
                ).fetchall()
                existing_active = [
                    self._row_to_claim(r)
                    for r in rows
                    if r.epistemic_status != "revoked"
                ]
                overlapping = [
                    c
                    for c in existing_active
                    if windows_overlap(c.valid_from, c.valid_to, incoming.valid_from, incoming.valid_to)
                ]

                superseded_claim_id: str | None = None
                conflict_detected = False
                conflict_set_id: str | None = None
                now = utc_now()

                if payload.force_supersede:
                    for old in overlapping:
                        if old.channel == incoming.channel:
                            await conn.execute(
                                t_claims.update()
                                .where(t_claims.c.claim_id == old.claim_id)
                                .values(superseded_at=format_utc(now), superseded_by=incoming.claim_id)
                            )
                            if superseded_claim_id is None:
                                superseded_claim_id = old.claim_id
                    await self._insert_claim_conn(conn, incoming)
                else:
                    colliding = [c for c in overlapping if claims_conflict(c, incoming)]
                    if colliding:
                        conflict_detected = True
                        # Preserve active approved_fact untouched; never auto-overwrite!
                        if incoming.epistemic_status not in ("unverified_observation", "candidate_sop"):
                            incoming = incoming.model_copy(update={"epistemic_status": "disputed"})
                        await self._insert_claim_conn(conn, incoming)

                        # Find existing open ConflictSet for this collision tuple or create one
                        cs_row = (
                            await conn.execute(
                                select(t_conflict_sets).where(
                                    (t_conflict_sets.c.subject_id == incoming.subject_id)
                                    & (t_conflict_sets.c.predicate == incoming.predicate)
                                    & (t_conflict_sets.c.context_purpose == incoming.context_purpose)
                                    & (t_conflict_sets.c.scope_type == incoming.scope_type)
                                    & (t_conflict_sets.c.scope_id == incoming.scope_id)
                                    & (t_conflict_sets.c.status == "open")
                                )
                            )
                        ).first()
                        if cs_row is not None:
                            conflict_set_id = cs_row.conflict_set_id
                            existing_members = [
                                r.claim_id
                                for r in (
                                    await conn.execute(
                                        select(t_conflict_set_members.c.claim_id).where(
                                            t_conflict_set_members.c.conflict_set_id == conflict_set_id
                                        )
                                    )
                                ).fetchall()
                            ]
                            member_ids = list(
                                dict.fromkeys(
                                    existing_members + [c.claim_id for c in colliding] + [incoming.claim_id]
                                )
                            )
                            for cid in member_ids:
                                if cid not in existing_members:
                                    await conn.execute(
                                        t_conflict_set_members.insert().values(
                                            conflict_set_id=conflict_set_id, claim_id=cid
                                        )
                                    )
                        else:
                            conflict_set_id = f"conflict-{uuid.uuid4().hex[:12]}"
                            member_ids = list(
                                dict.fromkeys([c.claim_id for c in colliding] + [incoming.claim_id])
                            )
                            cs = ConflictSet(
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
                            await self._upsert_conflict_conn(conn, cs)
                    else:
                        await self._insert_claim_conn(conn, incoming)

                new_epoch = await self._bump_data_epoch_conn(
                    conn, reason=f"write_claim:{incoming.claim_id}", actor=eff.user_id
                )

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

        async with self._write_lock:
            async with self._engine.begin() as conn:
                row = (await conn.execute(select(t_claims).where(t_claims.c.claim_id == claim_id))).first()
                if row is None:
                    raise NotFoundError(f"Claim not found: {claim_id}")
                existing = self._row_to_claim(row)
                if not self.policy.can_read_claim(eff, existing):
                    raise NotFoundError(f"Claim not found: {claim_id}")
                target = await self._get_entity_raw(conn, existing.subject_id)
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

                epoch_row = (
                    await conn.execute(select(t_system_epochs).where(t_system_epochs.c.id == 1))
                ).first()
                assert epoch_row is not None
                check_toctou_epoch(patch.expected_data_epoch, epoch_row.data_epoch)

                if existing.superseded_at is not None:
                    raise OptimisticConcurrencyError(f"Claim {claim_id} was already superseded")

                now = utc_now()
                new_claim_id = f"clm-{uuid.uuid4().hex[:12]}"
                updated_claim = existing.model_copy(
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
                await conn.execute(
                    t_claims.update()
                    .where(t_claims.c.claim_id == existing.claim_id)
                    .values(superseded_at=format_utc(now), superseded_by=new_claim_id)
                )
                await self._insert_claim_conn(conn, updated_claim)
                new_epoch = await self._bump_data_epoch_conn(
                    conn, reason=f"update_claim:{existing.claim_id}->{new_claim_id}", actor=eff.user_id
                )

        await self._register_record_terms("claims", updated_claim, created_by=eff.user_id)
        sanitized = self.policy.sanitize_claim_for_principal(eff, updated_claim) or updated_claim
        return ClaimWriteResult(
            claim=sanitized,
            conflict_detected=False,
            superseded_claim_id=existing.claim_id,
            data_epoch=new_epoch,
            snapshot_scheduled=updated_claim.epistemic_status == "approved_fact",
        )

    async def get_claim_raw(self, claim_id: str) -> Claim | None:
        """Unsanitized claim for internal bookkeeping (version history). Never return it to a caller."""
        await self._ensure_init()
        async with self._engine.connect() as conn:
            row = (await conn.execute(select(t_claims).where(t_claims.c.claim_id == claim_id))).first()
        return self._row_to_claim(row) if row is not None else None

    async def get_entity_raw(self, entity_id_or_key: str) -> Entity | None:
        """Unfiltered entity for internal policy decisions (steward groups). Never return it to a caller."""
        await self._ensure_init()
        async with self._engine.connect() as conn:
            return await self._get_entity_raw(conn, entity_id_or_key)

    async def get_claim(self, claim_id: str, *, principal: Principal) -> Claim | None:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            return None
        async with self._engine.connect() as conn:
            row = (await conn.execute(select(t_claims).where(t_claims.c.claim_id == claim_id))).first()
        if row is None:
            return None
        return self.policy.sanitize_claim_for_principal(eff, self._row_to_claim(row))

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
        unfiltered = all(
            f is None
            for f in (subject_ids, object_entity_ids, predicates, context_purpose, scope_types, scope_id, epistemic_statuses)
        )
        if unfiltered:

            async def _load_claims() -> list[tuple[int | None, Claim]]:
                async with self._engine.connect() as c:
                    return [(r.acl_set_id, self._row_to_claim(r)) for r in (await c.execute(select(t_claims))).fetchall()]

            async with self._engine.connect() as c:
                readable_ids = {int(r[0]) for r in (await c.execute(self._readable_acl_sets_query(eff))).fetchall()}
            raw_claims: list[Claim] = [
                clm for acl_id, clm in await self._cached_scan("claims", _load_claims) if acl_id in readable_ids
            ]
        else:
            raw_claims = await self._query_claims_filtered(
                principal=eff,
                subject_ids=subject_ids,
                object_entity_ids=object_entity_ids,
                predicates=predicates,
                context_purpose=context_purpose,
                scope_types=scope_types,
                scope_id=scope_id,
                epistemic_statuses=epistemic_statuses,
            )

        results: list[Claim] = []
        for raw in raw_claims:
            # sanitize_claim_for_principal returns a copy, so cached claims are never handed out
            if not is_claim_visible(
                raw, as_of=as_of, recorded_as_of=recorded_as_of, include_superseded=include_superseded
            ):
                continue
            sanitized = self.policy.sanitize_claim_for_principal(eff, raw)
            if sanitized is not None:
                results.append(sanitized)
        results.sort(key=lambda c: (-c.recorded_at.timestamp(), c.claim_id))
        return results

    async def _query_claims_filtered(
        self,
        *,
        principal: Principal,
        subject_ids: Sequence[str] | None,
        object_entity_ids: Sequence[str] | None,
        predicates: Sequence[str] | None,
        context_purpose: str | None,
        scope_types: Sequence[str] | None,
        scope_id: str | None,
        epistemic_statuses: Sequence[str] | None,
    ) -> list[Claim]:
        async with self._engine.connect() as conn:
            # Access runs inside the query: wall side and read groups through acl_sets, plus the
            # personal-scope rule (user and user_x_skill claims belong to one person). The policy
            # engine re-checks and redacts each row afterwards.
            stmt = select(t_claims).where(t_claims.c.acl_set_id.in_(self._readable_acl_sets_query(principal)))
            scope_ok = [t_claims.c.scope_type.not_in(["user", "user_x_skill"])]
            if self.policy.is_memory_allowed(principal):
                scope_ok.append((t_claims.c.scope_type == "user") & (t_claims.c.scope_id == principal.user_id))
                scope_ok.append(
                    (t_claims.c.scope_type == "user_x_skill")
                    & t_claims.c.scope_id.startswith(f"{principal.user_id}:", autoescape=True)
                )
            stmt = stmt.where(or_(*scope_ok))
            if subject_ids is not None:
                if not subject_ids:
                    return []
                stmt = stmt.where(t_claims.c.subject_id.in_(list(subject_ids)))
            if object_entity_ids is not None:
                if not object_entity_ids:
                    return []
                stmt = stmt.where(t_claims.c.object_entity_id.in_(list(object_entity_ids)))
            if predicates is not None:
                stmt = stmt.where(t_claims.c.predicate.in_(list(predicates)))
            if context_purpose is not None:
                stmt = stmt.where(t_claims.c.context_purpose == context_purpose)
            if scope_types is not None:
                stmt = stmt.where(t_claims.c.scope_type.in_(list(scope_types)))
            if scope_id is not None:
                stmt = stmt.where(t_claims.c.scope_id == scope_id)
            if epistemic_statuses is not None:
                stmt = stmt.where(t_claims.c.epistemic_status.in_(list(epistemic_statuses)))
            rows = (await conn.execute(stmt)).fetchall()
        return [self._row_to_claim(r) for r in rows]

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
        async with self._engine.connect() as conn:
            stmt = select(t_conflict_sets)
            if status != "all":
                stmt = stmt.where(t_conflict_sets.c.status == status)
            if subject_id is not None:
                stmt = stmt.where(t_conflict_sets.c.subject_id == subject_id)
            cs_rows = (await conn.execute(stmt)).fetchall()
            if not cs_rows:
                return []
            member_rows = (await conn.execute(select(t_conflict_set_members))).fetchall()
            members_map: dict[str, list[str]] = {}
            for mr in member_rows:
                members_map.setdefault(mr.conflict_set_id, []).append(mr.claim_id)

            all_cids = {cid for cids in members_map.values() for cid in cids}
            claim_rows = (
                await conn.execute(select(t_claims).where(t_claims.c.claim_id.in_(list(all_cids))))
            ).fetchall() if all_cids else []
            claim_map = {r.claim_id: self._row_to_claim(r) for r in claim_rows}

            out: list[ConflictSet] = []
            for r in cs_rows:
                target = await self._get_entity_raw(conn, r.subject_id)
                if target is None or not self.policy.can_read_entity(eff, target):
                    continue
                visible_claims: list[Claim] = []
                for cid in members_map.get(r.conflict_set_id, []):
                    clm = claim_map.get(cid)
                    if clm is None:
                        continue
                    san = self.policy.sanitize_claim_for_principal(eff, clm)
                    if san is not None:
                        visible_claims.append(san)
                if not visible_claims:
                    continue
                out.append(
                    ConflictSet(
                        conflict_set_id=r.conflict_set_id,
                        subject_id=r.subject_id,
                        predicate=r.predicate,
                        context_purpose=r.context_purpose,
                        scope_type=r.scope_type,
                        scope_id=r.scope_id,
                        status=r.status,
                        claim_ids=[c.claim_id for c in visible_claims],
                        claims=visible_claims,
                        winning_claim_id=r.winning_claim_id,
                        resolution_rationale=r.resolution_rationale,
                        resolved_by=r.resolved_by,
                        resolved_at=r.resolved_at,
                        created_at=r.created_at,
                        summary=r.summary,
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

        async with self._write_lock:
            async with self._engine.begin() as conn:
                cs_row = (
                    await conn.execute(
                        select(t_conflict_sets).where(
                            t_conflict_sets.c.conflict_set_id == resolution.conflict_set_id
                        )
                    )
                ).first()
                if cs_row is None:
                    raise NotFoundError(f"ConflictSet not found: {resolution.conflict_set_id}")
                target = await self._get_entity_raw(conn, cs_row.subject_id)
                if target is None or not self.policy.can_read_entity(eff, target):
                    raise NotFoundError(f"ConflictSet not found: {resolution.conflict_set_id}")

                self.policy.authorize_write(
                    eff,
                    target_entity=target,
                    scope_type=cs_row.scope_type,
                    epistemic_status="approved_fact",
                    force_supersede=True,
                    resolving_conflict=True,
                )

                epoch_row = (
                    await conn.execute(select(t_system_epochs).where(t_system_epochs.c.id == 1))
                ).first()
                assert epoch_row is not None
                check_toctou_epoch(resolution.expected_data_epoch, epoch_row.data_epoch)

                if cs_row.status == "resolved":
                    raise OptimisticConcurrencyError(
                        f"ConflictSet {resolution.conflict_set_id} is already resolved"
                    )

                member_ids = [
                    r.claim_id
                    for r in (
                        await conn.execute(
                            select(t_conflict_set_members.c.claim_id).where(
                                t_conflict_set_members.c.conflict_set_id == resolution.conflict_set_id
                            )
                        )
                    ).fetchall()
                ]
                claim_rows = (
                    await conn.execute(select(t_claims).where(t_claims.c.claim_id.in_(member_ids)))
                ).fetchall()
                member_claims = {r.claim_id: self._row_to_claim(r) for r in claim_rows}

                if not resolution.winning_claim_id and not resolution.replacement_object_value:
                    raise ValidationError(
                        "Provide either winning_claim_id or replacement_object_value to resolve conflict"
                    )

                now = utc_now()
                winning_base: Claim | None = None
                if resolution.winning_claim_id:
                    if resolution.winning_claim_id not in member_claims:
                        raise ValidationError(
                            f"winning_claim_id {resolution.winning_claim_id} is not a member of {resolution.conflict_set_id}"
                        )
                    winning_base = member_claims[resolution.winning_claim_id]
                else:
                    winning_base = next(iter(member_claims.values()))

                # Determine whether to keep existing approved_fact in-place or mint new bitemporal approved_fact
                needs_new_version = (
                    resolution.replacement_object_value is not None
                    or winning_base.epistemic_status != "approved_fact"
                    or resolution.valid_from is not None
                )

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
                    # Supersede all competing active claims (preserving full bitemporal system-time history!)
                    for cid, clm in member_claims.items():
                        if clm.superseded_at is None:
                            update_vals: dict[str, Any] = {
                                "superseded_at": format_utc(now),
                                "superseded_by": approved_id,
                            }
                            if resolution.close_prior_valid_to is not None and clm.epistemic_status == "approved_fact":
                                update_vals["valid_to"] = format_utc(resolution.close_prior_valid_to)
                            await conn.execute(
                                t_claims.update().where(t_claims.c.claim_id == cid).values(**update_vals)
                            )
                    await self._insert_claim_conn(conn, approved_claim)
                    member_ids.append(approved_id)
                    await conn.execute(
                        t_conflict_set_members.insert().values(
                            conflict_set_id=resolution.conflict_set_id, claim_id=approved_id
                        )
                    )
                    member_claims[approved_id] = approved_claim
                    final_winner_id = approved_id
                else:
                    final_winner_id = winning_base.claim_id
                    for cid, clm in member_claims.items():
                        if cid != final_winner_id and clm.superseded_at is None:
                            await conn.execute(
                                t_claims.update()
                                .where(t_claims.c.claim_id == cid)
                                .values(superseded_at=format_utc(now), superseded_by=final_winner_id)
                            )

                await conn.execute(
                    t_conflict_sets.update()
                    .where(t_conflict_sets.c.conflict_set_id == resolution.conflict_set_id)
                    .values(
                        status="resolved",
                        winning_claim_id=final_winner_id,
                        resolution_rationale=resolution.rationale,
                        resolved_by=eff.user_id,
                        resolved_at=format_utc(now),
                    )
                )
                await self._bump_data_epoch_conn(
                    conn, reason=f"resolve_conflict:{resolution.conflict_set_id}", actor=eff.user_id
                )

                # Reload updated member claims
                refreshed_rows = (
                    await conn.execute(select(t_claims).where(t_claims.c.claim_id.in_(member_ids)))
                ).fetchall()
                sanitized_claims = [
                    san
                    for r in refreshed_rows
                    if (san := self.policy.sanitize_claim_for_principal(eff, self._row_to_claim(r))) is not None
                ]

                return ConflictSet(
                    conflict_set_id=cs_row.conflict_set_id,
                    subject_id=cs_row.subject_id,
                    predicate=cs_row.predicate,
                    context_purpose=cs_row.context_purpose,
                    scope_type=cs_row.scope_type,
                    scope_id=cs_row.scope_id,
                    status="resolved",
                    claim_ids=[c.claim_id for c in sanitized_claims],
                    claims=sanitized_claims,
                    winning_claim_id=final_winner_id,
                    resolution_rationale=resolution.rationale,
                    resolved_by=eff.user_id,
                    resolved_at=now,
                    created_at=cs_row.created_at,
                    summary=cs_row.summary,
                )

    # ------------------------------------------------------------------
    # Plane 4: Dynamic Topics, Knowledge Factory, Curator Patches, Personal Memory
    # ------------------------------------------------------------------

    @staticmethod
    def _topic_row(topic: Topic) -> dict[str, Any]:
        return dict(
            topic_id=topic.topic_id,
            slug=topic.slug,
            pref_label=topic.pref_label,
            alt_labels_json=json.dumps(topic.alt_labels),
            broader_topic_id=topic.broader_topic_id,
            narrower_topic_ids_json=json.dumps(topic.narrower_topic_ids),
            related_topic_ids_json=json.dumps(topic.related_topic_ids),
            topic_kind=topic.topic_kind,
            merged_into_topic_id=topic.merged_into_topic_id,
            owner_group=topic.owner_group,
            status=topic.status,
        )

    @staticmethod
    def _topic_fts(topic: Topic) -> tuple[str, str, str, str]:
        return ("topic", topic.topic_id, f"{topic.pref_label} {topic.slug}", " ".join(topic.alt_labels))

    async def _upsert_topic_conn(self, conn: Any, topic: Topic) -> None:
        await conn.execute(delete(t_topics).where(t_topics.c.topic_id == topic.topic_id))
        await conn.execute(t_topics.insert().values(**self._topic_row(topic)))
        await self._fts_upsert(conn, *self._topic_fts(topic))

    async def upsert_topic(self, topic: Topic) -> Topic:
        await self._ensure_init()
        async with self._write_lock:
            async with self._engine.begin() as conn:
                await self._upsert_topic_conn(conn, topic)
                # Keep broader topic's narrower_topic_ids in sync
                if topic.broader_topic_id:
                    parent_row = (
                        await conn.execute(
                            select(t_topics).where(t_topics.c.topic_id == topic.broader_topic_id)
                        )
                    ).first()
                    if parent_row is not None:
                        parent = self._row_to_topic(parent_row)
                        if topic.topic_id not in parent.narrower_topic_ids:
                            parent.narrower_topic_ids.append(topic.topic_id)
                            await self._upsert_topic_conn(conn, parent)
                await self._bump_data_epoch_conn(conn, reason=f"upsert_topic:{topic.topic_id}", actor="system")
        return topic

    async def update_topic(
        self, topic_id: str, patch: TopicPatchInput, *, principal: Principal
    ) -> Topic:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            raise AuthenticationError("Revoked principal cannot update topics")
        async with self._write_lock:
            async with self._engine.begin() as conn:
                rows = (await conn.execute(select(t_topics))).fetchall()
                topics_map = {r.topic_id: self._row_to_topic(r) for r in rows}
                updated, modified_list = apply_topic_patch(
                    topics_map,
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
                    await self._upsert_topic_conn(conn, t)
                await self._bump_data_epoch_conn(conn, reason=f"update_topic:{topic_id}", actor=eff.user_id)
        return updated

    async def list_topics(
        self, *, principal: Principal, include_archived: bool = False
    ) -> list[Topic]:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            return []
        async with self._engine.connect() as conn:
            stmt = select(t_topics).order_by(t_topics.c.slug.asc())
            if not include_archived:
                stmt = stmt.where(t_topics.c.status != "archived")
            rows = (await conn.execute(stmt)).fetchall()
        return [self._row_to_topic(r) for r in rows]

    async def attach_topic(self, attachment: TopicAttachment) -> TopicAttachment:
        await self._ensure_init()
        async with self._write_lock:
            async with self._engine.begin() as conn:
                await conn.execute(
                    delete(t_topic_attachments).where(
                        t_topic_attachments.c.attachment_id == attachment.attachment_id
                    )
                )
                await conn.execute(
                    t_topic_attachments.insert().values(
                        attachment_id=attachment.attachment_id,
                        topic_id=attachment.topic_id,
                        target_type=attachment.target_type,
                        target_id=attachment.target_id,
                        attachment_source=attachment.attachment_source,
                        confidence=attachment.confidence,
                        wall_side=attachment.wall_side.value,
                        read_acl_groups_json=json.dumps(attachment.read_acl_groups),
                    )
                )
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
        async with self._engine.connect() as conn:
            stmt = select(t_topic_attachments)
            if topic_id:
                stmt = stmt.where(t_topic_attachments.c.topic_id == topic_id)
            if target_id:
                stmt = stmt.where(t_topic_attachments.c.target_id == target_id)
            rows = (await conn.execute(stmt)).fetchall()
        return [
            att
            for r in rows
            if self.policy.can_read_record(
                eff, wall_side=(att := self._row_to_attachment(r)).wall_side, read_acl_groups=att.read_acl_groups
            )
        ]

    async def upsert_knowledge_item(self, item: KnowledgeItem) -> KnowledgeItem:
        await self._ensure_init()
        async with self._write_lock:
            async with self._engine.begin() as conn:
                await conn.execute(
                    delete(t_knowledge_items).where(t_knowledge_items.c.item_id == item.item_id)
                )
                await conn.execute(
                    t_knowledge_items.insert().values(
                        item_id=item.item_id,
                        knowledge_kind=item.knowledge_kind,
                        display_name=item.display_name,
                        slug=item.slug,
                        source_path=item.source_path,
                        body_markdown=item.body_markdown,
                        wall_side=item.wall_side.value,
                        read_acl_groups_json=json.dumps(item.read_acl_groups),
                        write_acl_groups_json=json.dumps(item.write_acl_groups),
                        topics_json=json.dumps(item.topics),
                        related_entities_json=json.dumps(item.related_entities),
                        metadata_json=json.dumps(item.metadata),
                        content_sha256=item.content_sha256,
                        valid_from=format_utc(item.valid_from),
                        recorded_at=format_utc(item.recorded_at),
                        okf_version=item.okf_version,
                    )
                )
                await self._fts_upsert(
                    conn,
                    "knowledge_item",
                    item.item_id,
                    f"{item.display_name} {item.slug}",
                    f"{item.body_markdown} {' '.join(item.topics)} {' '.join(item.related_entities)}",
                )
        return item

    async def list_knowledge_items(
        self, *, principal: Principal, kind: str | None = None
    ) -> list[KnowledgeItem]:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            return []
        async with self._engine.connect() as conn:
            stmt = select(t_knowledge_items).order_by(t_knowledge_items.c.item_id.asc())
            if kind:
                stmt = stmt.where(t_knowledge_items.c.knowledge_kind == kind)
            rows = (await conn.execute(stmt)).fetchall()
        return [
            ki
            for r in rows
            if self.policy.can_read_record(
                eff, wall_side=(ki := self._row_to_knowledge_item(r)).wall_side, read_acl_groups=ki.read_acl_groups
            )
        ]

    async def list_curator_patches(
        self, *, principal: Principal, status: str = "pending_steward_review"
    ) -> list[CuratorDraftPatch]:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            return []
        async with self._engine.connect() as conn:
            stmt = select(t_curator_patches)
            if status != "all":
                stmt = stmt.where(t_curator_patches.c.status == status)
            rows = (await conn.execute(stmt)).fetchall()
        return [
            p
            for r in rows
            if self.policy.can_read_record(
                eff, wall_side=(p := self._row_to_patch(r)).wall_side, read_acl_groups=p.read_acl_groups
            )
        ]

    async def save_curator_patch(self, patch: CuratorDraftPatch) -> CuratorDraftPatch:
        await self._ensure_init()
        async with self._write_lock:
            async with self._engine.begin() as conn:
                await conn.execute(
                    delete(t_curator_patches).where(t_curator_patches.c.patch_id == patch.patch_id)
                )
                await conn.execute(
                    t_curator_patches.insert().values(
                        patch_id=patch.patch_id,
                        wall_side=patch.wall_side.value,
                        read_acl_groups_json=json.dumps(patch.read_acl_groups),
                        target_type=patch.target_type,
                        target_id=patch.target_id,
                        title=patch.title,
                        rationale=patch.rationale,
                        unified_diff=patch.unified_diff,
                        proposed_changes_json=json.dumps(patch.proposed_changes),
                        source_claim_ids_json=json.dumps(patch.source_claim_ids),
                        stale_evidence_paths_json=json.dumps(patch.stale_evidence_paths),
                        status=patch.status,
                        created_at=format_utc(patch.created_at),
                        created_by=patch.created_by,
                        reviewed_by=patch.reviewed_by,
                        reviewed_at=format_utc(patch.reviewed_at),
                    )
                )
        return patch

    # ------------------------------------------------------------------
    # Personal Memory & Watchlist
    # ------------------------------------------------------------------

    async def seed_memory_raw(self, memory: MemoryRecord) -> MemoryRecord:
        await self._ensure_init()
        async with self._write_lock:
            async with self._engine.begin() as conn:
                await conn.execute(
                    delete(t_personal_memories).where(t_personal_memories.c.memory_id == memory.memory_id)
                )
                await conn.execute(
                    t_personal_memories.insert().values(
                        memory_id=memory.memory_id,
                        user_id=memory.user_id,
                        scope_type=memory.scope_type,
                        skill_id=memory.skill_id,
                        key=memory.key,
                        content=memory.content,
                        tags_json=json.dumps(memory.tags),
                        linked_entity_ids_json=json.dumps(memory.linked_entity_ids),
                        linked_topic_ids_json=json.dumps(memory.linked_topic_ids),
                        created_at=format_utc(memory.created_at),
                        updated_at=format_utc(memory.updated_at),
                    )
                )
        return memory

    async def seed_watchlist_raw(self, wl: WatchlistRecord) -> WatchlistRecord:
        await self._ensure_init()
        async with self._write_lock:
            async with self._engine.begin() as conn:
                await conn.execute(
                    delete(t_personal_watchlists).where(t_personal_watchlists.c.user_id == wl.user_id)
                )
                await conn.execute(
                    t_personal_watchlists.insert().values(
                        user_id=wl.user_id,
                        entity_ids_json=json.dumps(wl.entity_ids),
                        topic_ids_json=json.dumps(wl.topic_ids),
                        source_ids_json=json.dumps(wl.source_ids),
                        updated_at=format_utc(wl.updated_at),
                    )
                )
        return wl

    async def upsert_memory(
        self, record: MemoryUpsertInput, *, principal: Principal
    ) -> MemoryRecord:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        self.policy.authorize_memory_access(eff)

        now = utc_now()
        async with self._write_lock:
            async with self._engine.begin() as conn:
                existing_row = None
                if record.memory_id:
                    existing_row = (
                        await conn.execute(
                            select(t_personal_memories).where(
                                t_personal_memories.c.memory_id == record.memory_id
                            )
                        )
                    ).first()
                    if existing_row is not None and existing_row.user_id != eff.user_id:
                        raise NotFoundError(f"Memory not found: {record.memory_id}")
                else:
                    existing_row = (
                        await conn.execute(
                            select(t_personal_memories).where(
                                (t_personal_memories.c.user_id == eff.user_id)
                                & (t_personal_memories.c.scope_type == record.scope_type)
                                & (t_personal_memories.c.key == record.key)
                            )
                        )
                    ).first()

                mem_id = (
                    existing_row.memory_id
                    if existing_row is not None
                    else (record.memory_id or f"mem-{uuid.uuid4().hex[:12]}")
                )
                created_at = ensure_utc(existing_row.created_at) if existing_row is not None else now
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
                    created_at=created_at or now,
                    updated_at=now,
                )
                await conn.execute(
                    delete(t_personal_memories).where(t_personal_memories.c.memory_id == mem.memory_id)
                )
                await conn.execute(
                    t_personal_memories.insert().values(
                        memory_id=mem.memory_id,
                        user_id=mem.user_id,
                        scope_type=mem.scope_type,
                        skill_id=mem.skill_id,
                        key=mem.key,
                        content=mem.content,
                        tags_json=json.dumps(mem.tags),
                        linked_entity_ids_json=json.dumps(mem.linked_entity_ids),
                        linked_topic_ids_json=json.dumps(mem.linked_topic_ids),
                        created_at=format_utc(mem.created_at),
                        updated_at=format_utc(mem.updated_at),
                    )
                )
                await self._bump_data_epoch_conn(conn, reason=f"upsert_memory:{mem.memory_id}", actor=eff.user_id)
        return mem

    async def get_memory(self, memory_id: str, *, principal: Principal) -> MemoryRecord | None:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            raise AuthenticationError("Revoked principal cannot access personal memory")
        if not self.policy.is_memory_allowed(eff):
            return None
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(t_personal_memories).where(
                        (t_personal_memories.c.memory_id == memory_id)
                        & (t_personal_memories.c.user_id == eff.user_id)
                    )
                )
            ).first()
        return self._row_to_memory(row) if row is not None else None

    async def list_memories(
        self, *, principal: Principal, skill_id: str | None = None
    ) -> list[MemoryRecord]:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            raise AuthenticationError("Revoked principal cannot access personal memory")
        if not self.policy.is_memory_allowed(eff):
            return []
        async with self._engine.connect() as conn:
            stmt = select(t_personal_memories).where(t_personal_memories.c.user_id == eff.user_id)
            if skill_id is not None:
                stmt = stmt.where(
                    (t_personal_memories.c.skill_id == skill_id)
                    | (t_personal_memories.c.scope_type == "user")
                )
            rows = (await conn.execute(stmt)).fetchall()
        out = [self._row_to_memory(r) for r in rows]
        out.sort(key=lambda m: (-m.updated_at.timestamp(), m.memory_id))
        return out

    async def delete_memory(self, memory_id: str, *, principal: Principal) -> bool:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        self.policy.authorize_memory_access(eff)
        async with self._write_lock:
            async with self._engine.begin() as conn:
                res = await conn.execute(
                    delete(t_personal_memories).where(
                        (t_personal_memories.c.memory_id == memory_id)
                        & (t_personal_memories.c.user_id == eff.user_id)
                    )
                )
                deleted = (res.rowcount or 0) > 0
                if deleted:
                    await self._bump_data_epoch_conn(
                        conn, reason=f"delete_memory:{memory_id}", actor=eff.user_id
                    )
                return deleted

    async def get_watchlist(self, *, principal: Principal) -> WatchlistRecord:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            raise AuthenticationError("Revoked principal cannot access watchlist")
        if not self.policy.is_memory_allowed(eff):
            return WatchlistRecord(user_id=eff.user_id)
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(t_personal_watchlists).where(t_personal_watchlists.c.user_id == eff.user_id)
                )
            ).first()
        if row is None:
            wl = WatchlistRecord(user_id=eff.user_id)
        else:
            wl = WatchlistRecord(
                user_id=row.user_id,
                entity_ids=json.loads(row.entity_ids_json),
                topic_ids=json.loads(row.topic_ids_json),
                source_ids=json.loads(row.source_ids_json),
                updated_at=row.updated_at,
            )
        # Recompute strictly post-ACL alerts
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
        now = utc_now()
        async with self._write_lock:
            async with self._engine.begin() as conn:
                await conn.execute(
                    delete(t_personal_watchlists).where(t_personal_watchlists.c.user_id == eff.user_id)
                )
                await conn.execute(
                    t_personal_watchlists.insert().values(
                        user_id=eff.user_id,
                        entity_ids_json=json.dumps(list(dict.fromkeys(watchlist.entity_ids))),
                        topic_ids_json=json.dumps(list(dict.fromkeys(watchlist.topic_ids))),
                        source_ids_json=json.dumps(list(dict.fromkeys(watchlist.source_ids))),
                        updated_at=format_utc(now),
                    )
                )
                await self._bump_data_epoch_conn(conn, reason=f"put_watchlist:{eff.user_id}", actor=eff.user_id)
        return await self.get_watchlist(principal=eff)

    # ------------------------------------------------------------------
    # Receipts, Staleness & State Export/Import (for runtime adapter swap)
    # ------------------------------------------------------------------

    async def record_receipt(self, receipt: ContextAssemblyReceipt) -> None:
        await self._ensure_init()
        async with self._write_lock:
            async with self._engine.begin() as conn:
                await conn.execute(delete(t_receipts).where(t_receipts.c.receipt_id == receipt.receipt_id))
                await conn.execute(
                    t_receipts.insert().values(
                        receipt_id=receipt.receipt_id,
                        principal_id=receipt.principal_id,
                        payload_json=receipt.model_dump_json(),
                        created_at=format_utc(receipt.timestamp),
                    )
                )

    async def get_receipt(
        self, receipt_id: str, *, principal: Principal
    ) -> ContextAssemblyReceipt | None:
        await self._ensure_init()
        eff = await self._effective_principal(principal)
        if eff.revoked:
            return None
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(select(t_receipts).where(t_receipts.c.receipt_id == receipt_id))
            ).first()
        if row is None:
            return None
        # Only owner or compliance/steward above_the_wall may inspect another user's receipt
        if row.principal_id != eff.user_id and eff.wall_side != WallSide.above_the_wall:
            return None
        return ContextAssemblyReceipt.model_validate_json(row.payload_json)

    async def diff_staleness(self, *, wall_side: str | None = None) -> StalenessReport:
        await self._ensure_init()
        async with self._engine.connect() as conn:
            claim_rows = (await conn.execute(select(t_claims))).fetchall()
            ki_rows = (await conn.execute(select(t_knowledge_items))).fetchall()
        claims = [self._row_to_claim(r) for r in claim_rows]
        items = [self._row_to_knowledge_item(r) for r in ki_rows]
        return compute_sha_staleness(
            claims=claims, knowledge_items=items, base_dir=self.repo_root, wall_side=wall_side
        )

    async def export_state(self) -> dict[str, Any]:
        """Export complete unfiltered store state for snapshot compilation and runtime adapter swap."""
        await self._ensure_init()
        async with self._write_lock:
            async with self._engine.connect() as conn:
                vocab_terms = [self._row_to_term(r) for r in (await conn.execute(select(t_vocab_terms))).fetchall()]
                epoch_row = (
                    await conn.execute(select(t_system_epochs).where(t_system_epochs.c.id == 1))
                ).first()
                assert epoch_row is not None
                epochs = PolicyEpochState(
                    data_epoch=epoch_row.data_epoch,
                    policy_epoch=epoch_row.policy_epoch,
                    updated_at=epoch_row.updated_at,
                    last_reason=epoch_row.last_reason,
                    last_actor=epoch_row.last_actor,
                )
                entities = [self._row_to_entity(r) for r in (await conn.execute(select(t_entities))).fetchall()]
                identifiers = [
                    self._row_to_identifier(r) for r in (await conn.execute(select(t_entity_identifiers))).fetchall()
                ]
                lineage_events = [
                    self._row_to_lineage(r) for r in (await conn.execute(select(t_entity_lineage_events))).fetchall()
                ]
                positions = [self._row_to_position(r) for r in (await conn.execute(select(t_positions))).fetchall()]
                coverages = [self._row_to_coverage(r) for r in (await conn.execute(select(t_coverages))).fetchall()]
                skill_bindings = [
                    self._row_to_skill_binding(r) for r in (await conn.execute(select(t_skill_bindings))).fetchall()
                ]
                claims = [self._row_to_claim(r) for r in (await conn.execute(select(t_claims))).fetchall()]
                cs_rows = (await conn.execute(select(t_conflict_sets))).fetchall()
                m_rows = (await conn.execute(select(t_conflict_set_members))).fetchall()
                m_map: dict[str, list[str]] = {}
                for mr in m_rows:
                    m_map.setdefault(mr.conflict_set_id, []).append(mr.claim_id)
                conflict_sets = [
                    ConflictSet(
                        conflict_set_id=r.conflict_set_id,
                        subject_id=r.subject_id,
                        predicate=r.predicate,
                        context_purpose=r.context_purpose,
                        scope_type=r.scope_type,
                        scope_id=r.scope_id,
                        status=r.status,
                        claim_ids=m_map.get(r.conflict_set_id, []),
                        winning_claim_id=r.winning_claim_id,
                        resolution_rationale=r.resolution_rationale,
                        resolved_by=r.resolved_by,
                        resolved_at=r.resolved_at,
                        created_at=r.created_at,
                        summary=r.summary,
                    )
                    for r in cs_rows
                ]
                topics = [self._row_to_topic(r) for r in (await conn.execute(select(t_topics))).fetchall()]
                attachments = [
                    self._row_to_attachment(r) for r in (await conn.execute(select(t_topic_attachments))).fetchall()
                ]
                knowledge_items = [
                    self._row_to_knowledge_item(r) for r in (await conn.execute(select(t_knowledge_items))).fetchall()
                ]
                curator_patches = [
                    self._row_to_patch(r) for r in (await conn.execute(select(t_curator_patches))).fetchall()
                ]
                memories = [
                    self._row_to_memory(r) for r in (await conn.execute(select(t_personal_memories))).fetchall()
                ]
                watchlists = [
                    WatchlistRecord(
                        user_id=r.user_id,
                        entity_ids=json.loads(r.entity_ids_json),
                        topic_ids=json.loads(r.topic_ids_json),
                        source_ids=json.loads(r.source_ids_json),
                        updated_at=r.updated_at,
                    )
                    for r in (await conn.execute(select(t_personal_watchlists))).fetchall()
                ]
                revoked_principals = [
                    (r.user_id, r.reason)
                    for r in (await conn.execute(select(t_revoked_principals))).fetchall()
                ]
                receipts = [
                    ContextAssemblyReceipt.model_validate_json(r.payload_json)
                    for r in (await conn.execute(select(t_receipts))).fetchall()
                ]
        return {
            "epochs": epochs,
            "entities": entities,
            "identifiers": identifiers,
            "lineage_events": lineage_events,
            "positions": positions,
            "coverages": coverages,
            "skill_bindings": skill_bindings,
            "claims": claims,
            "conflict_sets": conflict_sets,
            "topics": topics,
            "topic_attachments": attachments,
            "knowledge_items": knowledge_items,
            "curator_patches": curator_patches,
            "memories": memories,
            "watchlists": watchlists,
            "revoked_principals": revoked_principals,
            "receipts": receipts,
            "vocab_terms": vocab_terms,
        }

    async def import_state(self, state: dict[str, Any], *, reset_epochs: bool = False) -> None:
        """Import a complete domain state dictionary atomically (used by seeder & runtime adapter swap)."""
        await self._ensure_init()
        async with self._write_lock:
            async with self._engine.begin() as conn:
                for tbl in (
                    t_conflict_set_members,
                    t_conflict_sets,
                    t_claims,
                    t_topic_attachments,
                    t_topics,
                    t_knowledge_items,
                    t_curator_patches,
                    t_personal_memories,
                    t_personal_watchlists,
                    t_skill_bindings,
                    t_coverages,
                    t_positions,
                    t_entity_lineage_events,
                    t_entity_identifiers,
                    t_entities,
                    t_revoked_principals,
                    t_receipts,
                ):
                    await conn.execute(delete(tbl))
                if self._fts_enabled:
                    await conn.execute(text("DELETE FROM fts_documents"))

                # Batched import: rows are collected per table and written with executemany in the
                # original table order, one round trip per 1,000 rows instead of one per row.
                # Entities, claims, conflict sets and topics were written with delete+insert helpers,
                # so a repeated id keeps the last record. Every other table was a plain INSERT, so a
                # repeated id still fails the whole import with IntegrityError (rows are kept apart).
                last_wins = {t_entities, t_claims, t_conflict_sets, t_conflict_set_members, t_topics}
                pending: dict[Any, dict[tuple[Any, ...], dict[str, Any]]] = {}
                fts_rows: dict[tuple[str, str], dict[str, Any]] = {}

                def _add(table: Any, **row: Any) -> None:
                    rows = pending.setdefault(table, {})
                    pk = tuple(row.get(col.name) for col in table.primary_key.columns)
                    if table in last_wins:
                        rows.pop(pk, None)
                        rows[pk] = row
                    else:
                        rows[(len(rows), *pk)] = row

                def _fts(doc_type: str, doc_id: str, title: str, body: str) -> None:
                    fts_rows.pop((doc_type, doc_id), None)
                    fts_rows[(doc_type, doc_id)] = {"dt": doc_type, "did": doc_id, "title": title, "body": body}

                acl_memo: dict[str, int] = {}
                for e in state.get("entities", []):
                    acl_id = await self._acl_set_id_conn(conn, e.wall_side, e.read_acl_groups, acl_memo)
                    _add(t_entities, **self._entity_row(e, acl_id))
                    _fts(*self._entity_fts(e))
                for ident in state.get("identifiers", []):
                    _add(
                        t_entity_identifiers,
                        id=ident.id,
                        entity_id=ident.entity_id,
                        scheme=ident.scheme,
                        identifier_value=ident.identifier_value,
                        authority=ident.authority,
                        is_primary=ident.is_primary,
                        valid_from=format_utc(ident.valid_from),
                        valid_to=format_utc(ident.valid_to),
                        recorded_at=format_utc(ident.recorded_at),
                        superseded_at=format_utc(ident.superseded_at),
                    )
                for ev in state.get("lineage_events", []):
                    _add(
                        t_entity_lineage_events,
                        event_id=ev.event_id,
                        source_entity_id=ev.source_entity_id,
                        target_entity_id=ev.target_entity_id,
                        event_type=ev.event_type,
                        effective_date=format_utc(ev.effective_date),
                        recorded_at=format_utc(ev.recorded_at),
                        summary=ev.summary,
                        evidence_uri=sanitize_uri_secrets(ev.evidence_uri),
                        evidence_acl_groups_json=json.dumps(ev.evidence_acl_groups),
                        wall_side=ev.wall_side.value,
                        read_acl_groups_json=json.dumps(ev.read_acl_groups),
                    )
                for pos in state.get("positions", []):
                    _add(
                        t_positions,
                        position_id=pos.position_id,
                        vehicle_entity_id=pos.vehicle_entity_id,
                        instrument_entity_id=pos.instrument_entity_id,
                        issuer_entity_id=pos.issuer_entity_id,
                        position_type=pos.position_type,
                        quantity=pos.quantity,
                        market_value=pos.market_value,
                        currency=pos.currency,
                        weight_bps=pos.weight_bps,
                        as_of_date=pos.as_of_date.isoformat(),
                        recorded_at=format_utc(pos.recorded_at),
                        source_system=pos.source_system,
                        wall_side=pos.wall_side.value,
                        read_acl_groups_json=json.dumps(pos.read_acl_groups),
                    )
                for cov in state.get("coverages", []):
                    _add(
                        t_coverages,
                        coverage_id=cov.coverage_id,
                        analyst_entity_id=cov.analyst_entity_id,
                        issuer_entity_id=cov.issuer_entity_id,
                        coverage_role=cov.coverage_role,
                        coverage_status=cov.coverage_status,
                        valid_from=format_utc(cov.valid_from),
                        valid_to=format_utc(cov.valid_to),
                        wall_side=cov.wall_side.value,
                        read_acl_groups_json=json.dumps(cov.read_acl_groups),
                    )
                for sb in state.get("skill_bindings", []):
                    _add(
                        t_skill_bindings,
                        binding_id=sb.binding_id,
                        skill_entity_id=sb.skill_entity_id,
                        target_entity_id=sb.target_entity_id,
                        mapping_purpose=sb.mapping_purpose,
                        read_mode=sb.read_mode,
                        required_predicates_json=json.dumps(sb.required_predicates),
                        active=sb.active,
                    )
                for c in state.get("claims", []):
                    acl_id = await self._acl_set_id_conn(conn, c.acl.wall_side, c.acl.read_acl_groups, acl_memo)
                    _add(t_claims, **self._claim_row(c, acl_id))
                    _fts(*self._claim_fts(c))
                for cs in state.get("conflict_sets", []):
                    _add(t_conflict_sets, **self._conflict_row(cs))
                    # A repeated conflict set replaces the earlier one's members (the old helper deleted them)
                    members = pending.setdefault(t_conflict_set_members, {})
                    for pk in [pk for pk in members if pk[0] == cs.conflict_set_id]:
                        del members[pk]
                    for cid in dict.fromkeys(cs.claim_ids):
                        _add(t_conflict_set_members, conflict_set_id=cs.conflict_set_id, claim_id=cid)
                for t in state.get("topics", []):
                    _add(t_topics, **self._topic_row(t))
                    _fts(*self._topic_fts(t))
                for att in state.get("topic_attachments", []):
                    _add(
                        t_topic_attachments,
                        attachment_id=att.attachment_id,
                        topic_id=att.topic_id,
                        target_type=att.target_type,
                        target_id=att.target_id,
                        attachment_source=att.attachment_source,
                        confidence=att.confidence,
                        wall_side=att.wall_side.value,
                        read_acl_groups_json=json.dumps(att.read_acl_groups),
                    )
                for ki in state.get("knowledge_items", []):
                    _add(
                        t_knowledge_items,
                        item_id=ki.item_id,
                        knowledge_kind=ki.knowledge_kind,
                        display_name=ki.display_name,
                        slug=ki.slug,
                        source_path=ki.source_path,
                        body_markdown=ki.body_markdown,
                        wall_side=ki.wall_side.value,
                        read_acl_groups_json=json.dumps(ki.read_acl_groups),
                        write_acl_groups_json=json.dumps(ki.write_acl_groups),
                        topics_json=json.dumps(ki.topics),
                        related_entities_json=json.dumps(ki.related_entities),
                        metadata_json=json.dumps(ki.metadata),
                        content_sha256=ki.content_sha256,
                        valid_from=format_utc(ki.valid_from),
                        recorded_at=format_utc(ki.recorded_at),
                        okf_version=ki.okf_version,
                    )
                    _fts(
                        "knowledge_item",
                        ki.item_id,
                        f"{ki.display_name} {ki.slug}",
                        f"{ki.body_markdown} {' '.join(ki.topics)} {' '.join(ki.related_entities)}",
                    )
                for p in state.get("curator_patches", []):
                    _add(
                        t_curator_patches,
                        patch_id=p.patch_id,
                        wall_side=p.wall_side.value,
                        read_acl_groups_json=json.dumps(p.read_acl_groups),
                        target_type=p.target_type,
                        target_id=p.target_id,
                        title=p.title,
                        rationale=p.rationale,
                        unified_diff=p.unified_diff,
                        proposed_changes_json=json.dumps(p.proposed_changes),
                        source_claim_ids_json=json.dumps(p.source_claim_ids),
                        stale_evidence_paths_json=json.dumps(p.stale_evidence_paths),
                        status=p.status,
                        created_at=format_utc(p.created_at),
                        created_by=p.created_by,
                        reviewed_by=p.reviewed_by,
                        reviewed_at=format_utc(p.reviewed_at),
                    )
                for m in state.get("memories", []):
                    _add(
                        t_personal_memories,
                        memory_id=m.memory_id,
                        user_id=m.user_id,
                        scope_type=m.scope_type,
                        skill_id=m.skill_id,
                        key=m.key,
                        content=m.content,
                        tags_json=json.dumps(m.tags),
                        linked_entity_ids_json=json.dumps(m.linked_entity_ids),
                        linked_topic_ids_json=json.dumps(m.linked_topic_ids),
                        created_at=format_utc(m.created_at),
                        updated_at=format_utc(m.updated_at),
                    )
                for wl in state.get("watchlists", []):
                    _add(
                        t_personal_watchlists,
                        user_id=wl.user_id,
                        entity_ids_json=json.dumps(wl.entity_ids),
                        topic_ids_json=json.dumps(wl.topic_ids),
                        source_ids_json=json.dumps(wl.source_ids),
                        updated_at=format_utc(wl.updated_at),
                    )
                for uid, reason in state.get("revoked_principals", []):
                    _add(
                        t_revoked_principals,
                        user_id=uid,
                        reason=reason,
                        actor="system:import",
                        revoked_at=format_utc(utc_now()),
                    )
                for rcpt in state.get("receipts", []):
                    _add(
                        t_receipts,
                        receipt_id=rcpt.receipt_id,
                        principal_id=rcpt.principal_id,
                        payload_json=rcpt.model_dump_json(),
                        created_at=format_utc(rcpt.timestamp),
                    )

                for table, rows in pending.items():
                    batch = list(rows.values())
                    for n in range(0, len(batch), 1000):
                        await conn.execute(table.insert(), batch[n : n + 1000])
                if self._fts_enabled and fts_rows:
                    fts_batch = list(fts_rows.values())
                    for n in range(0, len(fts_batch), 1000):
                        await conn.execute(
                            text(
                                "INSERT INTO fts_documents(doc_type, doc_id, title, body) "
                                "VALUES (:dt, :did, :title, :body)"
                            ),
                            fts_batch[n : n + 1000],
                        )
                epochs: PolicyEpochState | None = state.get("epochs")
                if epochs is not None and not reset_epochs:
                    await conn.execute(
                        t_system_epochs.update()
                        .where(t_system_epochs.c.id == 1)
                        .values(
                            data_epoch=epochs.data_epoch,
                            policy_epoch=epochs.policy_epoch,
                            updated_at=format_utc(epochs.updated_at),
                            last_reason=epochs.last_reason,
                            last_actor=epochs.last_actor,
                        )
                    )
                elif reset_epochs:
                    await conn.execute(
                        t_system_epochs.update()
                        .where(t_system_epochs.c.id == 1)
                        .values(
                            data_epoch=1,
                            policy_epoch=1,
                            updated_at=format_utc(utc_now()),
                            last_reason="seed",
                            last_actor="system:seed",
                        )
                    )
        await self._import_vocab_state(state)
