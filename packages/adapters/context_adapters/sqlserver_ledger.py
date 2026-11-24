"""Version ledger on SQL Server, written in the same transaction as the change it records (ADR-010).

Same interface as ``SqliteVersionLedger``, so ``VersionedStore`` and ``ChangeSetService`` are
unchanged. The tables live next to the lake's own (same schema or prefix) and are plain append-only
tables, not system-versioned temporal tables, because the lake's account cannot switch versioning
off or drop a temporal table if it ever needs to.

How the single transaction works
--------------------------------
Ledger calls are synchronous, store calls are async. Inside a store unit of work (``VersionedStore``
opens one around every write, ``ChangeSetService`` one around a whole merge), ledger writes are not
sent straight away: they are kept with the unit of work, numbered on the spot, and inserted on the
unit of work's own connection just before it commits. So the record and its history row commit
together or not at all. Reads made inside the unit of work see those pending rows.

Outside a unit of work (proposing, approving, cutting a release), each call is its own short
transaction on a small synchronous connection pool.

Reads use READ UNCOMMITTED with a lock timeout: a ledger read runs on the event loop thread, so it
must never wait for a transaction that needs that same loop to finish. History rows only appear in
the moment between a unit of work's flush and its commit, and a failed commit there is the only way
to see a row that then disappears.

Free-text columns (record payloads, change-set bodies) are SealedText, so they are encrypted with the
store's KMS data key like the records themselves.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Iterable

from sqlalchemy import (
    Boolean,
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    create_engine,
    event,
    exists,
    func,
    literal,
    select,
)
from sqlalchemy.engine import Connection

from context_core.versioning import (
    CHANNEL_NAMES,
    ChangeSet,
    ChannelMove,
    ChannelPointer,
    Release,
    VersionRecord,
)

from context_adapters.sealed import SealedText
from context_adapters.sqlserver_store import SqlServerBitemporalStore, _lake_url, current_unit_of_work

ledger_metadata = MetaData()

t_versions = Table(
    "vc_record_versions",
    ledger_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("record_type", String(32), nullable=False),
    Column("record_id", String(400), nullable=False),
    Column("version_no", Integer, nullable=False),
    Column("change_id", String(64), nullable=False, index=True),
    Column("op", String(16), nullable=False),
    Column("recorded_at", String(64), nullable=False),
    Column("wall_side", String(32), nullable=True),
    Column("read_acl_json", String(4000), nullable=True),
    Column("owner_user_id", String(128), nullable=True),
    Column("label", String(512), nullable=False, default=""),
    Column("payload_json", SealedText, nullable=True),
    Column("content_hash", String(128), nullable=True),
    Index("ux_vc_versions_record", "record_type", "record_id", "version_no", unique=True),
    Index("ix_vc_versions_time", "record_type", "record_id", "recorded_at"),
)

t_change_sets = Table(
    "vc_change_sets",
    ledger_metadata,
    Column("change_id", String(64), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("author_id", String(128), nullable=False),
    Column("implicit", Boolean, nullable=False, default=False),
    Column("created_at", String(64), nullable=False),
    Column("merged_at", String(64), nullable=True),
    Column("body_json", SealedText, nullable=False),
    Index("ix_vc_cs_status", "status", "created_at"),
    Index("ix_vc_cs_merged", "merged_at"),
)

t_change_set_records = Table(
    "vc_change_set_records",
    ledger_metadata,
    Column("change_id", String(64), primary_key=True),
    Column("record_type", String(32), primary_key=True),
    Column("record_id", String(400), primary_key=True),
    Index("ix_vc_csr_record", "record_type", "record_id"),
)

t_releases = Table(
    "vc_releases",
    ledger_metadata,
    Column("release_id", Integer, primary_key=True, autoincrement=True),
    Column("cut_at", String(64), nullable=False),
    Column("body_json", SealedText, nullable=False),
)

t_channels = Table(
    "vc_channels",
    ledger_metadata,
    Column("name", String(64), primary_key=True),
    Column("release_id", Integer, nullable=True),
    Column("moved_at", String(64), nullable=True),
    Column("moved_by", String(128), nullable=True),
)

t_channel_moves = Table(
    "vc_channel_moves",
    ledger_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(64), nullable=False),
    Column("body_json", SealedText, nullable=False),
)

_PENDING = "ledger"
#: Milliseconds a ledger statement waits for a lock before failing (never hang the event loop)
LOCK_TIMEOUT_MS = 5000


def _version_row(v: VersionRecord) -> dict[str, Any]:
    return {
        "record_type": v.record_type,
        "record_id": v.record_id,
        "version_no": v.version_no,
        "change_id": v.change_id,
        "op": v.op,
        "recorded_at": v.recorded_at,
        "wall_side": v.wall_side,
        "read_acl_json": json.dumps(v.read_acl_groups) if v.read_acl_groups is not None else None,
        "owner_user_id": v.owner_user_id,
        "label": v.label,
        "payload_json": json.dumps(v.payload, default=str) if v.payload is not None else None,
        "content_hash": v.content_hash,
    }


def _row_to_version(r: Any) -> VersionRecord:
    return VersionRecord(
        record_type=r.record_type,
        record_id=r.record_id,
        version_no=r.version_no,
        change_id=r.change_id,
        op=r.op,
        recorded_at=r.recorded_at,
        wall_side=r.wall_side,
        read_acl_groups=json.loads(r.read_acl_json) if r.read_acl_json is not None else None,
        owner_user_id=r.owner_user_id,
        label=r.label,
        payload=json.loads(r.payload_json) if r.payload_json is not None else None,
        content_hash=r.content_hash,
    )


class _Pending:
    """Ledger rows waiting for their unit of work to commit."""

    def __init__(self) -> None:
        self.versions: list[VersionRecord] = []
        self.change_sets: dict[str, ChangeSet] = {}
        self.links: set[tuple[str, str, str]] = set()

    def latest(self, record_type: str, record_id: str) -> VersionRecord | None:
        found = [v for v in self.versions if v.record_type == record_type and v.record_id == record_id]
        return max(found, key=lambda v: v.version_no) if found else None


class SqlServerVersionLedger:
    def __init__(self, store: SqlServerBitemporalStore) -> None:
        self._store = store
        store.register_metadata(ledger_metadata)
        self._lock = threading.RLock()
        self._engine = create_engine(
            _lake_url(store.settings.url, asynchronous=False),
            pool_size=3,
            max_overflow=5,
            pool_pre_ping=True,
            pool_recycle=1800,
            isolation_level="READ UNCOMMITTED",
            use_setinputsizes=False,
            execution_options={"schema_translate_map": {None: store.schema_name}},
        )
        store.configure_engine(self._engine)

        @event.listens_for(self._engine, "connect")
        def _lock_timeout(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            try:
                cur.execute(f"SET LOCK_TIMEOUT {LOCK_TIMEOUT_MS}")
            finally:
                cur.close()

        self._ready = False
        # Seed the channel rows during the store's start-up, on its connection, so the first ledger
        # call never has to write
        store.register_init_hook(self._seed_channels)

    # -- plumbing --------------------------------------------------------

    def _seed_channels(self, conn: Connection) -> None:
        for name in CHANNEL_NAMES:
            conn.execute(
                t_channels.insert().from_select(
                    ["name"],
                    select(literal(name)).where(~exists().where(t_channels.c.name == name)),
                )
            )
        self._ready = True

    def _ensure_ready(self) -> None:
        """Normally done by the store's initialize(); this covers a ledger used before it."""
        if self._ready:
            return
        with self._lock, self._engine.begin() as conn:
            self._seed_channels(conn)

    def _pending(self) -> _Pending | None:
        uow = current_unit_of_work(self._store)
        if uow is None:
            return None
        pending = uow.data.get(_PENDING)
        if pending is None:
            pending = _Pending()
            uow.data[_PENDING] = pending

            async def _flush(conn: Any) -> None:
                await conn.run_sync(lambda sync_conn: self._write_pending(sync_conn, pending))

            uow.before_commit.append(_flush)
        return pending

    def _read(self) -> Connection:
        self._ensure_ready()
        return self._engine.connect()

    def close(self) -> None:
        with self._lock:
            self._engine.dispose()

    # -- writes ----------------------------------------------------------

    def _db_max_version(self, conn: Connection, record_type: str, record_id: str) -> int:
        return int(
            conn.execute(
                select(func.coalesce(func.max(t_versions.c.version_no), 0)).where(
                    and_(t_versions.c.record_type == record_type, t_versions.c.record_id == record_id)
                )
            ).scalar()
            or 0
        )

    @staticmethod
    def _upsert_change_set(conn: Connection, cs: ChangeSet) -> None:
        values = {
            "status": cs.status,
            "merged_at": cs.merged_at,
            "body_json": cs.model_dump_json(),
        }
        updated = conn.execute(
            t_change_sets.update().where(t_change_sets.c.change_id == cs.change_id).values(**values)
        ).rowcount
        if not updated:
            conn.execute(
                t_change_sets.insert().values(
                    change_id=cs.change_id,
                    author_id=cs.author_id,
                    implicit=bool(cs.implicit),
                    created_at=cs.created_at,
                    **values,
                )
            )

    @staticmethod
    def _link(conn: Connection, links: Iterable[tuple[str, str, str]]) -> None:
        for change_id, record_type, record_id in sorted(set(links)):
            conn.execute(
                t_change_set_records.insert().from_select(
                    ["change_id", "record_type", "record_id"],
                    select(literal(change_id), literal(record_type), literal(record_id)).where(
                        ~exists().where(
                            and_(
                                t_change_set_records.c.change_id == change_id,
                                t_change_set_records.c.record_type == record_type,
                                t_change_set_records.c.record_id == record_id,
                            )
                        )
                    ),
                )
            )

    def _write_pending(self, conn: Connection, pending: _Pending) -> None:
        """Runs on the unit of work's connection just before it commits."""
        for cs in pending.change_sets.values():
            self._upsert_change_set(conn, cs)
        if pending.versions:
            conn.execute(t_versions.insert(), [_version_row(v) for v in pending.versions])
        self._link(conn, pending.links)

    def append_version(self, v: VersionRecord) -> VersionRecord:
        """Append a version numbered after the latest one (version_no on input is ignored)."""
        pending = self._pending()
        if pending is not None:
            before = pending.latest(v.record_type, v.record_id)
            if before is not None:
                n = before.version_no
            else:
                with self._lock, self._read() as conn:
                    n = self._db_max_version(conn, v.record_type, v.record_id)
            v = v.model_copy(update={"version_no": n + 1})
            pending.versions.append(v)
            pending.links.add((v.change_id, v.record_type, v.record_id))
            return v
        self._ensure_ready()
        with self._lock, self._engine.begin() as conn:
            conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            n = int(
                conn.execute(
                    select(func.coalesce(func.max(t_versions.c.version_no), 0))
                    .with_hint(t_versions, "WITH (UPDLOCK, HOLDLOCK)")
                    .where(and_(t_versions.c.record_type == v.record_type, t_versions.c.record_id == v.record_id))
                ).scalar()
                or 0
            )
            v = v.model_copy(update={"version_no": n + 1})
            conn.execute(t_versions.insert().values(**_version_row(v)))
            self._link(conn, [(v.change_id, v.record_type, v.record_id)])
        return v

    def append_versions(self, versions: list[VersionRecord]) -> int:
        """Append many versions (bulk import), each numbered after its record's latest."""
        if not versions:
            return 0
        pending = self._pending()
        next_no: dict[tuple[str, str], int] = {}
        with self._lock, self._read() as conn:
            for rt in {v.record_type for v in versions}:
                for rid, n in conn.execute(
                    select(t_versions.c.record_id, func.max(t_versions.c.version_no))
                    .where(t_versions.c.record_type == rt)
                    .group_by(t_versions.c.record_id)
                ):
                    next_no[(rt, rid)] = int(n)
        if pending is not None:
            for pv in pending.versions:
                key = (pv.record_type, pv.record_id)
                next_no[key] = max(next_no.get(key, 0), pv.version_no)
        numbered: list[VersionRecord] = []
        for v in versions:
            key = (v.record_type, v.record_id)
            next_no[key] = next_no.get(key, 0) + 1
            numbered.append(v.model_copy(update={"version_no": next_no[key]}))
        links = {(v.change_id, v.record_type, v.record_id) for v in numbered}
        if pending is not None:
            pending.versions.extend(numbered)
            pending.links.update(links)
            return len(numbered)
        with self._lock, self._engine.begin() as conn:
            conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            conn.execute(t_versions.insert(), [_version_row(v) for v in numbered])
            self._link(conn, links)
        return len(numbered)

    def save_change_set(self, cs: ChangeSet) -> ChangeSet:
        links = [(cs.change_id, i.record_type, i.record_id) for i in cs.items if i.record_id]
        pending = self._pending()
        if pending is not None:
            pending.change_sets[cs.change_id] = cs.model_copy(deep=True)
            pending.links.update(links)
            return cs
        self._ensure_ready()
        with self._lock, self._engine.begin() as conn:
            conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            self._upsert_change_set(conn, cs)
            self._link(conn, links)
        return cs

    # -- version reads -----------------------------------------------------

    def latest_version(self, record_type: str, record_id: str) -> VersionRecord | None:
        pending = self._pending()
        if pending is not None:
            hit = pending.latest(record_type, record_id)
            if hit is not None:
                return hit
        with self._lock, self._read() as conn:
            row = conn.execute(
                select(t_versions)
                .where(and_(t_versions.c.record_type == record_type, t_versions.c.record_id == record_id))
                .order_by(t_versions.c.version_no.desc())
                .limit(1)
            ).first()
        return _row_to_version(row) if row else None

    def latest_hashes(self, record_type: str) -> dict[str, str | None]:
        latest = (
            select(t_versions.c.record_id, func.max(t_versions.c.version_no).label("n"))
            .where(t_versions.c.record_type == record_type)
            .group_by(t_versions.c.record_id)
            .subquery()
        )
        with self._lock, self._read() as conn:
            rows = conn.execute(
                select(t_versions.c.record_id, t_versions.c.content_hash).join(
                    latest,
                    and_(t_versions.c.record_id == latest.c.record_id, t_versions.c.version_no == latest.c.n),
                ).where(t_versions.c.record_type == record_type)
            ).fetchall()
        out = {r[0]: r[1] for r in rows}
        pending = self._pending()
        if pending is not None:
            for v in sorted(pending.versions, key=lambda x: x.version_no):
                if v.record_type == record_type:
                    out[v.record_id] = v.content_hash
        return out

    def all_hashes(self, record_type: str) -> set[tuple[str, str | None]]:
        with self._lock, self._read() as conn:
            rows = conn.execute(
                select(t_versions.c.record_id, t_versions.c.content_hash).where(t_versions.c.record_type == record_type)
            ).fetchall()
        out = {(r[0], r[1]) for r in rows}
        pending = self._pending()
        if pending is not None:
            out.update((v.record_id, v.content_hash) for v in pending.versions if v.record_type == record_type)
        return out

    def list_versions(self, record_type: str, record_id: str) -> list[VersionRecord]:
        with self._lock, self._read() as conn:
            rows = conn.execute(
                select(t_versions)
                .where(and_(t_versions.c.record_type == record_type, t_versions.c.record_id == record_id))
                .order_by(t_versions.c.version_no)
            ).fetchall()
        out = [_row_to_version(r) for r in rows]
        pending = self._pending()
        if pending is not None:
            out.extend(v for v in pending.versions if v.record_type == record_type and v.record_id == record_id)
        return out

    def get_version(self, record_type: str, record_id: str, version_no: int) -> VersionRecord | None:
        pending = self._pending()
        if pending is not None:
            for v in pending.versions:
                if (v.record_type, v.record_id, v.version_no) == (record_type, record_id, version_no):
                    return v
        with self._lock, self._read() as conn:
            row = conn.execute(
                select(t_versions).where(
                    and_(
                        t_versions.c.record_type == record_type,
                        t_versions.c.record_id == record_id,
                        t_versions.c.version_no == version_no,
                    )
                )
            ).first()
        return _row_to_version(row) if row else None

    def version_at(self, record_type: str, record_id: str, at: str) -> VersionRecord | None:
        with self._lock, self._read() as conn:
            row = conn.execute(
                select(t_versions)
                .where(
                    and_(
                        t_versions.c.record_type == record_type,
                        t_versions.c.record_id == record_id,
                        t_versions.c.recorded_at <= at,
                    )
                )
                .order_by(t_versions.c.version_no.desc())
                .limit(1)
            ).first()
        return _row_to_version(row) if row else None

    def versions_for_change(self, change_id: str) -> list[VersionRecord]:
        with self._lock, self._read() as conn:
            rows = conn.execute(
                select(t_versions).where(t_versions.c.change_id == change_id).order_by(t_versions.c.id)
            ).fetchall()
        out = [_row_to_version(r) for r in rows]
        pending = self._pending()
        if pending is not None:
            out.extend(v for v in pending.versions if v.change_id == change_id)
        return out

    def known_record_ids(self, record_type: str) -> set[str]:
        with self._lock, self._read() as conn:
            rows = conn.execute(
                select(t_versions.c.record_id).where(t_versions.c.record_type == record_type).distinct()
            ).fetchall()
        out = {r[0] for r in rows}
        pending = self._pending()
        if pending is not None:
            out.update(v.record_id for v in pending.versions if v.record_type == record_type)
        return out

    def latest_versions_with_prefix(self, record_type: str, prefix: str) -> list[VersionRecord]:
        """Latest version of every record whose id starts with ``prefix`` (claim slots of one subject)."""
        pattern = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("[", "\\[") + "%"
        latest = (
            select(t_versions.c.record_id, func.max(t_versions.c.version_no).label("n"))
            .where(and_(t_versions.c.record_type == record_type, t_versions.c.record_id.like(pattern, escape="\\")))
            .group_by(t_versions.c.record_id)
            .subquery()
        )
        with self._lock, self._read() as conn:
            rows = conn.execute(
                select(t_versions)
                .join(
                    latest,
                    and_(t_versions.c.record_id == latest.c.record_id, t_versions.c.version_no == latest.c.n),
                )
                .where(t_versions.c.record_type == record_type)
            ).fetchall()
        by_id = {r.record_id: _row_to_version(r) for r in rows}
        pending = self._pending()
        if pending is not None:
            for v in pending.versions:
                if v.record_type == record_type and v.record_id.startswith(prefix):
                    cur = by_id.get(v.record_id)
                    if cur is None or v.version_no > cur.version_no:
                        by_id[v.record_id] = v
        return list(by_id.values())

    # -- change sets -----------------------------------------------------

    def get_change_set(self, change_id: str) -> ChangeSet | None:
        pending = self._pending()
        if pending is not None and change_id in pending.change_sets:
            return pending.change_sets[change_id].model_copy(deep=True)
        with self._lock, self._read() as conn:
            row = conn.execute(select(t_change_sets.c.body_json).where(t_change_sets.c.change_id == change_id)).first()
        return ChangeSet.model_validate_json(row[0]) if row else None

    def list_change_sets(
        self, *, statuses: Iterable[str] | None = None, include_implicit: bool = True, limit: int = 200
    ) -> list[ChangeSet]:
        stmt = select(t_change_sets.c.body_json)
        status_list = list(statuses or [])
        if status_list:
            stmt = stmt.where(t_change_sets.c.status.in_(status_list))
        if not include_implicit:
            stmt = stmt.where(t_change_sets.c.implicit == False)  # noqa: E712
        stmt = stmt.order_by(t_change_sets.c.created_at.desc()).limit(limit)
        with self._lock, self._read() as conn:
            rows = conn.execute(stmt).fetchall()
        return [ChangeSet.model_validate_json(r[0]) for r in rows]

    def change_sets_for_record(self, record_type: str, record_id: str) -> list[ChangeSet]:
        with self._lock, self._read() as conn:
            rows = conn.execute(
                select(t_change_sets.c.body_json)
                .join(t_change_set_records, t_change_sets.c.change_id == t_change_set_records.c.change_id)
                .where(
                    and_(
                        t_change_set_records.c.record_type == record_type,
                        t_change_set_records.c.record_id == record_id,
                    )
                )
                .order_by(t_change_sets.c.created_at)
            ).fetchall()
        return [ChangeSet.model_validate_json(r[0]) for r in rows]

    def merged_change_ids_between(self, after: str | None, until: str) -> list[str]:
        stmt = select(t_change_sets.c.change_id).where(
            and_(t_change_sets.c.merged_at.is_not(None), t_change_sets.c.merged_at <= until)
        )
        if after is not None:
            stmt = stmt.where(t_change_sets.c.merged_at > after)
        with self._lock, self._read() as conn:
            rows = conn.execute(stmt.order_by(t_change_sets.c.merged_at)).fetchall()
        return [r[0] for r in rows]

    # -- releases and channels ---------------------------------------------

    def create_release(self, release: Release) -> Release:
        self._ensure_ready()
        with self._lock, self._engine.begin() as conn:
            conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            rid = conn.execute(
                t_releases.insert().values(cut_at=release.cut_at, body_json="{}").returning(t_releases.c.release_id)
            ).scalar_one()
            release = release.model_copy(update={"release_id": int(rid)})
            conn.execute(
                t_releases.update()
                .where(t_releases.c.release_id == release.release_id)
                .values(body_json=release.model_dump_json())
            )
        return release

    def update_release(self, release: Release) -> Release:
        self._ensure_ready()
        with self._lock, self._engine.begin() as conn:
            conn.execute(
                t_releases.update()
                .where(t_releases.c.release_id == release.release_id)
                .values(body_json=release.model_dump_json())
            )
        return release

    def get_release(self, release_id: int) -> Release | None:
        with self._lock, self._read() as conn:
            row = conn.execute(select(t_releases.c.body_json).where(t_releases.c.release_id == release_id)).first()
        return Release.model_validate_json(row[0]) if row else None

    def list_releases(self) -> list[Release]:
        with self._lock, self._read() as conn:
            rows = conn.execute(select(t_releases.c.body_json).order_by(t_releases.c.release_id)).fetchall()
        return [Release.model_validate_json(r[0]) for r in rows]

    def get_channels(self) -> list[ChannelPointer]:
        with self._lock, self._read() as conn:
            rows = conn.execute(select(t_channels)).fetchall()
        order = {n: i for i, n in enumerate(CHANNEL_NAMES)}
        out = [
            ChannelPointer(name=r.name, release_id=r.release_id, moved_at=r.moved_at, moved_by=r.moved_by)
            for r in rows
        ]
        return sorted(out, key=lambda c: order.get(c.name, 99))

    def move_channel(self, move: ChannelMove) -> ChannelPointer:
        self._ensure_ready()
        with self._lock, self._engine.begin() as conn:
            conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            conn.execute(
                t_channels.update()
                .where(t_channels.c.name == move.name)
                .values(release_id=move.to_release, moved_at=move.moved_at, moved_by=move.moved_by)
            )
            conn.execute(t_channel_moves.insert().values(name=move.name, body_json=move.model_dump_json()))
        return ChannelPointer(name=move.name, release_id=move.to_release, moved_at=move.moved_at, moved_by=move.moved_by)

    def channel_moves(self, name: str | None = None) -> list[ChannelMove]:
        stmt = select(t_channel_moves.c.body_json).order_by(t_channel_moves.c.id)
        if name:
            stmt = stmt.where(t_channel_moves.c.name == name)
        with self._lock, self._read() as conn:
            rows = conn.execute(stmt).fetchall()
        return [ChannelMove.model_validate_json(r[0]) for r in rows]


__all__ = ["SqlServerVersionLedger", "ledger_metadata"]
