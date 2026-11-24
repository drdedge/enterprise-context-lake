"""SQL Server StorePort adapter for the shared production database (ADR-010).

``SqlServerBitemporalStore`` reuses every query of ``SqliteBitemporalStore`` (the schema and queries
are portable SQLAlchemy Core) and changes only what SQL Server needs:

* **Where the tables live.** In their own schema (``lake`` by default), or in ``dbo`` with a ``lake_``
  prefix when creating a schema is not allowed. Never bare names in ``dbo``.
* **Additive-only migrations.** Start-up creates missing tables, columns and indexes and records each
  statement in ``schema_log``. It never drops or alters an existing object, so it runs with
  ``db_ddladmin`` rights and cannot damage anyone else's tables.
* **Encryption.** Free-text columns are encrypted by the app with a KMS data key before they are sent
  (``sealed.py``), because the database is readable by many accounts.
* **One transaction per change.** ``unit_of_work()`` runs several store writes and their history rows
  in one SQL Server transaction. ``VersionedStore`` opens one around every write, and the change-set
  service opens one around a whole merge, so an approval is applied in full or not at all.
* **Information barrier.** Unchanged: every read goes through the same policy checks as on SQLite.

Search and the graph stay in memory on the app server; full-text search in SQL is not used.
"""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, event, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool
from sqlalchemy.schema import CreateIndex, CreateTable

from context_core.compression import format_utc, utc_now
from context_core.policy import PolicyEngine

from context_adapters import sqlserver_dialect  # noqa: F401 - registers the lake dialects
from context_adapters.sealed import FieldCipher, KmsEnvelopeCipher, LocalKeyCipher, attach_cipher
from context_core.exceptions import ValidationError
from context_adapters.sqlite_store import (
    SqliteBitemporalStore,
    metadata,
    t_claims,
    t_entities,
    t_entity_identifiers,
    t_knowledge_items,
    t_system_epochs,
)
from context_adapters.sqlserver_dialect import configure_dialect

# Write statements as SQL Server renders them: INSERT INTO [lake].[claims] / UPDATE lake_claims ...
_MSSQL_WRITE = re.compile(
    r"^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE(?:\s+INTO)?)\s+(?:\[?\w+\]?\.)?\[?(\w+)\]?",
    re.IGNORECASE,
)
_MSSQL_READ = re.compile(r"^\s*(?:SELECT|WITH|SAVE\s+TRANSACTION|ROLLBACK|COMMIT|BEGIN|SET)\b", re.IGNORECASE)
_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

# Lake-owned metadata kept next to the domain tables: one row per DDL statement ever applied.
schema_log_metadata = MetaData()
t_schema_log = Table(
    "schema_log",
    schema_log_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("applied_at", String(64), nullable=False),
    Column("statement", Text, nullable=False),
)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass
class SqlServerSettings:
    """Connection, placement and encryption settings. Credentials come from the environment only."""

    url: str
    schema: str = "lake"
    table_prefix: str = ""
    allow_replace: bool = False
    encryption: str = "kms"  # kms | local | off
    kms_key_id: str | None = None
    kms_region: str | None = None
    kms_endpoint_url: str | None = None
    local_master_key: bytes | None = None
    pool_size: int = 10
    max_overflow: int = 20
    extra_tables: list[MetaData] = field(default_factory=list)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "SqlServerSettings":
        """Read ``LAKE_SQLSERVER_*`` / ``LAKE_KMS_*`` variables (see docs/runbooks/06-sql-server-and-s3.md)."""
        e = dict(os.environ if env is None else env)
        url = e.get("LAKE_SQLSERVER_URL", "").strip()
        if not url:
            raise ValueError("LAKE_SQLSERVER_URL is not set")
        password = e.get("LAKE_SQLSERVER_PASSWORD")
        if password:
            url = make_url(url).set(password=password).render_as_string(hide_password=False)
        local_key = e.get("LAKE_LOCAL_MASTER_KEY")
        import base64

        return cls(
            url=url,
            schema=e.get("LAKE_SQL_SCHEMA", "lake").strip(),
            table_prefix=e.get("LAKE_SQL_TABLE_PREFIX", "").strip(),
            allow_replace=e.get("LAKE_SQL_ALLOW_REPLACE", "").strip().lower() in ("1", "true", "yes"),
            encryption=e.get("LAKE_ENCRYPTION", "kms").strip().lower(),
            kms_key_id=e.get("LAKE_KMS_KEY_ID") or None,
            kms_region=e.get("LAKE_KMS_REGION") or e.get("AWS_REGION") or None,
            kms_endpoint_url=e.get("LAKE_KMS_ENDPOINT_URL") or None,
            local_master_key=base64.b64decode(local_key) if local_key else None,
            pool_size=int(e.get("LAKE_SQL_POOL_SIZE", "10")),
            max_overflow=int(e.get("LAKE_SQL_MAX_OVERFLOW", "20")),
        )

    def validate(self) -> None:
        for name, value in (("schema", self.schema), ("table prefix", self.table_prefix)):
            if value and not _SAFE_IDENT.match(value):
                raise ValueError(f"Invalid SQL Server {name}: {value!r}")
        if (not self.schema or self.schema.lower() == "dbo") and not self.table_prefix:
            raise ValueError(
                "Lake tables need their own schema or a table prefix: set LAKE_SQL_SCHEMA=lake, or "
                "LAKE_SQL_SCHEMA=dbo with LAKE_SQL_TABLE_PREFIX=lake_"
            )
        if self.encryption not in ("kms", "local", "off"):
            raise ValueError(f"LAKE_ENCRYPTION must be kms, local or off, not {self.encryption!r}")
        if self.encryption == "kms" and not self.kms_key_id:
            raise ValueError("LAKE_ENCRYPTION=kms needs LAKE_KMS_KEY_ID")
        if self.encryption == "local" and not self.local_master_key:
            raise ValueError("LAKE_ENCRYPTION=local needs LAKE_LOCAL_MASTER_KEY (32 bytes, base64)")
        if self.encryption in ("off", "local") and os.environ.get("ENVIRONMENT", "").lower() == "production":
            raise ValueError("Production needs LAKE_ENCRYPTION=kms: the shared database must not hold plain text")

    @property
    def effective_schema(self) -> str:
        return self.schema or "dbo"

    def build_cipher(self) -> FieldCipher | None:
        if self.encryption == "kms":
            return KmsEnvelopeCipher(
                self.kms_key_id or "", region_name=self.kms_region, endpoint_url=self.kms_endpoint_url
            )
        if self.encryption == "local":
            return LocalKeyCipher(self.local_master_key or b"")
        return None


def _lake_url(url: str, *, asynchronous: bool) -> str:
    """Point any mssql URL at the lake dialect (async aioodbc for the store, pyodbc for the ledger)."""
    u = make_url(url)
    if u.get_backend_name() != "mssql":
        raise ValueError("LAKE_SQLSERVER_URL must be a mssql URL, e.g. mssql+aioodbc://user@host/BSPAI?...")
    return u.set(drivername="mssql+lake_aioodbc" if asynchronous else "mssql+lake_pyodbc").render_as_string(
        hide_password=False
    )


# ---------------------------------------------------------------------------
# Unit of work: one SQL Server transaction around several store writes
# ---------------------------------------------------------------------------


class UnitOfWork:
    """An open transaction owned by one asyncio task, shared by every store call made inside it."""

    def __init__(self, store: "SqlServerBitemporalStore", conn: AsyncConnection) -> None:
        self.store = store
        self.conn = conn
        self.task = asyncio.current_task()
        self.active = True
        self.before_commit: list[Callable[[AsyncConnection], Any]] = []
        self.after_commit: list[Callable[[], None]] = []
        self.data: dict[str, Any] = {}  # per-transaction scratch space (the ledger's pending rows)

    def owns_current_task(self) -> bool:
        return self.active and asyncio.current_task() is self.task


_current_uow: ContextVar[UnitOfWork | None] = ContextVar("lake_sqlserver_uow", default=None)


def current_unit_of_work(store: Any) -> UnitOfWork | None:
    uow = _current_uow.get()
    if uow is not None and uow.store is store and uow.owns_current_task():
        return uow
    return None


class _SharedConnection:
    """``engine.begin()`` / ``engine.connect()`` stand-in that hands out the unit of work's connection.

    ``begin()`` opens a savepoint, so a store call that fails inside a unit of work undoes only its own
    statements, as it would have with its own transaction; the caller still decides about the rest.
    """

    def __init__(self, uow: UnitOfWork, *, savepoint: bool) -> None:
        self._uow = uow
        self._savepoint = savepoint
        self._sp: Any = None

    async def __aenter__(self) -> AsyncConnection:
        if self._savepoint:
            self._sp = await self._uow.conn.begin_nested()
        return self._uow.conn

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._sp is not None:
            if exc_type is None:
                await self._sp.commit()
            else:
                await self._sp.rollback()
        return False


class _UowEngine:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def begin(self) -> _SharedConnection:
        return _SharedConnection(self._uow, savepoint=True)

    def connect(self) -> _SharedConnection:
        return _SharedConnection(self._uow, savepoint=False)


class _UowAwareLock:
    """The store's write lock, skipped by calls made inside the task's own unit of work (which holds it)."""

    def __init__(self, store: "SqlServerBitemporalStore", inner: asyncio.Lock) -> None:
        self._store = store
        self._inner = inner

    def locked(self) -> bool:
        return self._inner.locked()

    async def __aenter__(self) -> None:
        # Whether the calling task is inside its unit of work cannot change during the block, so
        # entry and exit make the same decision without keeping per-task state on the shared lock
        if current_unit_of_work(self._store) is None:
            await self._inner.acquire()

    async def __aexit__(self, *exc: Any) -> bool:
        if current_unit_of_work(self._store) is None:
            self._inner.release()
        return False


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class SqlServerBitemporalStore(SqliteBitemporalStore):
    """StorePort on SQL Server 2019+ (compatibility level 130 or later)."""

    def __init__(
        self,
        settings: SqlServerSettings | None = None,
        *,
        policy: PolicyEngine | None = None,
        repo_root: Any = None,
        cipher: FieldCipher | None = None,
    ) -> None:
        self.settings = settings or SqlServerSettings.from_env()
        self.settings.validate()
        super().__init__(
            db_path=None,
            database_url=_lake_url(self.settings.url, asynchronous=True),
            policy=policy,
            repo_root=repo_root,
        )
        self._real_write_lock = self._write_lock
        self._write_lock = _UowAwareLock(self, self._real_write_lock)  # type: ignore[assignment]
        self.cipher = cipher if cipher is not None else self.settings.build_cipher()
        self._extra_metadata: list[MetaData] = [schema_log_metadata, *self.settings.extra_tables]
        # Run inside initialize()'s transaction, after the tables exist (the ledger seeds its rows)
        self._init_hooks: list[Callable[[Any], None]] = []
        self._table_map = self._physical_names()
        self._check_name_collisions()

    # -- names ---------------------------------------------------------------

    def _all_tables(self) -> list[Table]:
        tables = list(metadata.sorted_tables)
        for md in self._extra_metadata:
            tables.extend(md.sorted_tables)
        return tables

    def register_metadata(self, md: MetaData) -> None:
        """Have another component's tables (the version ledger) created and named like the lake's."""
        if md not in self._extra_metadata:
            self._extra_metadata.append(md)
            self._table_map = self._physical_names()
            self._check_name_collisions()
            for engine in self._live_engines():
                configure_dialect(engine.dialect, table_names=self._table_map)

    def register_init_hook(self, hook: Callable[[Any], None]) -> None:
        """``hook(sync_connection)`` runs at start-up in the same transaction as the migrations."""
        self._init_hooks.append(hook)

    def _physical_names(self) -> dict[str, str]:
        prefix = self.settings.table_prefix
        return {t.name: prefix + t.name for t in self._all_tables()} if prefix else {}

    def physical_name(self, logical: str) -> str:
        return self._table_map.get(logical, logical)

    def _check_name_collisions(self) -> None:
        """The prefix map applies to every identifier, so no column may share a table's name."""
        if not self._table_map:
            return
        names = set(self._table_map)
        for t in self._all_tables():
            clash = names.intersection(c.name for c in t.columns)
            if clash:
                raise RuntimeError(f"Column name(s) {sorted(clash)} in {t.name} collide with table names")

    @property
    def schema_name(self) -> str:
        return self.settings.effective_schema

    # -- engines -------------------------------------------------------------

    def _execution_options(self) -> dict[str, Any]:
        return {"schema_translate_map": {None: self.schema_name}}

    def configure_engine(self, engine: Any) -> None:
        """Shared set-up for this store's engines (also used by the ledger's sync engine)."""
        dialect = engine.dialect
        configure_dialect(dialect, table_names=self._table_map)
        attach_cipher(dialect, self.cipher)

    def _make_engine(self, *, pooled: bool) -> AsyncEngine:
        kwargs: dict[str, Any] = {
            "future": True,
            "pool_pre_ping": True,
            "execution_options": self._execution_options(),
            "fast_executemany": True,
            # Let pyodbc size each string parameter itself (always NVARCHAR, MAX when long), rather
            # than from the column type: a JSON list for OPENJSON is longer than any column
            "use_setinputsizes": False,
        }
        if pooled:
            kwargs.update(
                poolclass=AsyncAdaptedQueuePool,
                pool_size=self.settings.pool_size,
                max_overflow=self.settings.max_overflow,
                pool_recycle=1800,
            )
        else:
            kwargs["poolclass"] = NullPool
        engine = create_async_engine(self.database_url, **kwargs)
        self.configure_engine(engine)
        reverse = {v.lower(): k for k, v in self._table_map.items()}

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def _track_writes(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
            m = _MSSQL_WRITE.match(statement)
            if m:
                name = m.group(1).lower()
                name = reverse.get(name, name)
                self._table_writes[name] = self._table_writes.get(name, 0) + 1
            elif not _MSSQL_READ.match(statement):
                self._any_writes += 1

        return engine

    def _live_engines(self) -> list[Any]:
        out = [e for e in (self._pooled_engine, self._fallback_engine) if e is not None]
        out.extend(self._other_engines.values())
        return out

    @property
    def _engine(self) -> Any:  # type: ignore[override]
        uow = current_unit_of_work(self)
        if uow is not None:
            return _UowEngine(uow)
        return super()._engine

    def _insertion_order(self, table: Table) -> Any:
        # SQL Server has no rowid; identifiers are listed in the order they were recorded
        return [table.c.recorded_at, table.c.id]

    # -- unit of work --------------------------------------------------------

    @asynccontextmanager
    async def unit_of_work(self) -> AsyncIterator[UnitOfWork]:
        """Run the store calls in this block, and the history rows they add, in one transaction.

        Nested calls join the outer unit of work. Holds the store's write lock throughout, so writes
        from other requests wait rather than interleave with a half-applied change.
        """
        existing = current_unit_of_work(self)
        if existing is not None:
            yield existing
            return
        await self._ensure_init()
        async with self._real_write_lock:
            engine = super()._engine
            async with engine.connect() as conn:
                trans = await conn.begin()
                uow = UnitOfWork(self, conn)
                token = _current_uow.set(uow)
                try:
                    yield uow
                    for hook in uow.before_commit:
                        res = hook(conn)
                        if asyncio.iscoroutine(res):
                            await res
                    await trans.commit()
                except BaseException:
                    if trans.is_active:
                        await trans.rollback()
                    # Anything read or cached during the transaction may include rolled-back rows
                    self._any_writes += 1
                    self._scan_cache.clear()
                    raise
                finally:
                    uow.active = False
                    _current_uow.reset(token)
        for cb in uow.after_commit:
            cb()

    async def _cached_scan(self, name: str, load: Any) -> list[Any]:
        # Inside a transaction the rows may not be committed yet: never cache them
        if current_unit_of_work(self) is not None:
            return await load()
        return await super()._cached_scan(name, load)

    # -- schema --------------------------------------------------------------

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._write_lock:
            if self._initialized:
                return
            if isinstance(self.cipher, KmsEnvelopeCipher):
                await asyncio.to_thread(self.cipher.check)
            engine = super()._engine
            async with engine.begin() as conn:
                await conn.run_sync(self._ensure_schema_sync)
                for hook in self._init_hooks:
                    await conn.run_sync(hook)
            async with engine.begin() as conn:
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

    def _ensure_schema_sync(self, conn: Any) -> list[str]:
        """Create what is missing. Never drops, renames or narrows anything (additive-only)."""
        applied: list[str] = []
        schema = self.schema_name
        quote = conn.dialect.identifier_preparer.quote_identifier

        def run(sql: str) -> None:
            conn.exec_driver_sql(sql)
            applied.append(sql)

        def ddl(element: Any) -> str:
            return str(element.compile(dialect=conn.dialect))

        placed = MetaData()  # copies of the tables with the schema written in, for DDL only

        if conn.exec_driver_sql("SELECT SCHEMA_ID(?)", (schema,)).scalar() is None:
            # No DENY to public here: tested on SQL Server 2022, a DENY on the schema also applies to
            # the account that owns it, so it would lock the lake out of its own tables. Keeping other
            # readers out is the job of the column encryption (and of a DBA, see ADR-010).
            run(f"CREATE SCHEMA {quote(schema)}")

        existing_cols: dict[str, set[str]] = {}
        for tname, cname in conn.exec_driver_sql(
            "SELECT t.name, c.name FROM sys.tables t JOIN sys.columns c ON c.object_id = t.object_id "
            "WHERE t.schema_id = SCHEMA_ID(?)",
            (schema,),
        ):
            existing_cols.setdefault(tname.lower(), set()).add(cname.lower())
        existing_idx: set[tuple[str, str]] = {
            (t.lower(), (i or "").lower())
            for t, i in conn.exec_driver_sql(
                "SELECT t.name, i.name FROM sys.tables t JOIN sys.indexes i ON i.object_id = t.object_id "
                "WHERE t.schema_id = SCHEMA_ID(?)",
                (schema,),
            )
        }

        for logical in self._all_tables():
            table = logical.to_metadata(placed, schema=schema)
            # Keep the logical index names (a schema-qualified copy would put the schema in them)
            names = {tuple(c.name for c in i.columns): i.name for i in logical.indexes}
            for idx in table.indexes:
                original = names.get(tuple(c.name for c in idx.columns))
                if original:
                    idx.name = original
            phys = self.physical_name(table.name).lower()
            if phys not in existing_cols:
                run(ddl(CreateTable(table)))
                for idx in table.indexes:
                    run(ddl(CreateIndex(idx)))
                continue
            for col in table.columns:
                if col.name.lower() in existing_cols[phys]:
                    continue
                col_type = col.type.compile(dialect=conn.dialect)
                # Added columns are always NULL-able, so existing rows stay valid
                run(
                    f"ALTER TABLE {quote(schema)}.{quote(self.physical_name(table.name))} "
                    f"ADD {quote(col.name)} {col_type} NULL"
                )
            for idx in table.indexes:
                if (phys, (idx.name or "").lower()) not in existing_idx:
                    run(ddl(CreateIndex(idx)))

        if applied:
            now = format_utc(utc_now())
            conn.execute(t_schema_log.insert(), [{"applied_at": now, "statement": s} for s in applied])
        return applied

    async def is_empty(self) -> bool:
        async with self._engine.connect() as conn:
            for t in (t_entities, t_claims, t_knowledge_items):
                if (await conn.execute(select(t.c[0]).limit(1))).first() is not None:
                    return False
        return True

    async def import_state(self, state: dict[str, Any], *, reset_epochs: bool = False) -> None:
        """Replace the store's contents (seeding, adapter swap). Refuses to wipe a lake holding data.

        On the shared production database a stray seed or swap would replace real context with demo
        fixtures, so a non-empty store needs ``LAKE_SQL_ALLOW_REPLACE=1`` for this.
        """
        await self._ensure_init()
        if not self.settings.allow_replace and not await self.is_empty():
            raise ValidationError(
                "The SQL Server lake already holds data; refusing to replace it. "
                "Set LAKE_SQL_ALLOW_REPLACE=1 to allow a full replace."
            )
        await super().import_state(state, reset_epochs=reset_epochs)

    async def schema_log(self) -> list[dict[str, Any]]:
        await self._ensure_init()
        async with self._engine.connect() as conn:
            rows = (await conn.execute(select(t_schema_log).order_by(t_schema_log.c.id))).fetchall()
        return [{"id": r.id, "applied_at": r.applied_at, "statement": r.statement} for r in rows]

    async def drop_all_for_tests(self) -> None:
        """Remove this store's schema objects. Only for throwaway test schemas: refuses ``lake``/``dbo``."""
        schema = self.schema_name
        if schema.lower() in ("lake", "dbo") or not schema.lower().startswith("lake_test_"):
            raise RuntimeError("drop_all_for_tests only runs on lake_test_* schemas")
        engine = super()._engine
        async with engine.begin() as conn:
            for t in reversed(self._all_tables()):
                await conn.exec_driver_sql(
                    f"IF OBJECT_ID(N'[{schema}].[{self.physical_name(t.name)}]', N'U') IS NOT NULL "
                    f"DROP TABLE [{schema}].[{self.physical_name(t.name)}]"
                )
            await conn.exec_driver_sql(f"IF SCHEMA_ID(N'{schema}') IS NOT NULL DROP SCHEMA [{schema}]")


__all__ = [
    "SqlServerBitemporalStore",
    "SqlServerSettings",
    "UnitOfWork",
    "current_unit_of_work",
    "t_entity_identifiers",
]
