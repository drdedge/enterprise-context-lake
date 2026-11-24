"""SQLite version ledger: record versions, change sets, releases and channel pointers.

Kept in its own database file next to the lake database so it works under both store adapters and
survives a runtime adapter swap. Uses the stdlib sqlite3 driver behind a thread lock, so it is safe
to call from any event loop or thread (the agents' sync bridge runs its own loop). Each call is a
short single-row statement.

Production note: on SQL Server the version rows would be system-versioned temporal tables plus a
``change_sets`` table written in the same transaction as the record (ADR-007). This ledger keeps the
same shape so the service layer does not change.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from context_core.versioning import (
    CHANNEL_NAMES,
    ChangeSet,
    ChannelMove,
    ChannelPointer,
    Release,
    VersionRecord,
    ts,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vc_record_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    change_id TEXT NOT NULL,
    op TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    wall_side TEXT,
    read_acl_json TEXT,
    owner_user_id TEXT,
    label TEXT NOT NULL DEFAULT '',
    payload_json TEXT,
    content_hash TEXT,
    UNIQUE (record_type, record_id, version_no)
);
CREATE INDEX IF NOT EXISTS ix_vc_versions_change ON vc_record_versions(change_id);
CREATE INDEX IF NOT EXISTS ix_vc_versions_time ON vc_record_versions(record_type, record_id, recorded_at);

CREATE TABLE IF NOT EXISTS vc_change_sets (
    change_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    author_id TEXT NOT NULL,
    implicit INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    merged_at TEXT,
    body_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_vc_cs_status ON vc_change_sets(status, created_at);
CREATE INDEX IF NOT EXISTS ix_vc_cs_merged ON vc_change_sets(merged_at);

CREATE TABLE IF NOT EXISTS vc_change_set_records (
    change_id TEXT NOT NULL,
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    PRIMARY KEY (change_id, record_type, record_id)
);
CREATE INDEX IF NOT EXISTS ix_vc_csr_record ON vc_change_set_records(record_type, record_id);

CREATE TABLE IF NOT EXISTS vc_releases (
    release_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cut_at TEXT NOT NULL,
    body_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vc_channels (
    name TEXT PRIMARY KEY,
    release_id INTEGER,
    moved_at TEXT,
    moved_by TEXT
);

CREATE TABLE IF NOT EXISTS vc_channel_moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    body_json TEXT NOT NULL
);
"""


def ledger_path_for(db_path: str | Path | None) -> str:
    if db_path is None or str(db_path) == ":memory:":
        return ":memory:"
    p = Path(db_path)
    return str(p.with_name(p.name + ".versions.sqlite3"))


class SqliteVersionLedger:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = DELETE")
        self._conn.executescript(_SCHEMA)
        for name in CHANNEL_NAMES:
            self._conn.execute("INSERT OR IGNORE INTO vc_channels(name) VALUES (?)", (name,))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- versions --------------------------------------------------------

    @staticmethod
    def _row_to_version(row: sqlite3.Row) -> VersionRecord:
        return VersionRecord(
            record_type=row["record_type"],
            record_id=row["record_id"],
            version_no=row["version_no"],
            change_id=row["change_id"],
            op=row["op"],
            recorded_at=row["recorded_at"],
            wall_side=row["wall_side"],
            read_acl_groups=json.loads(row["read_acl_json"]) if row["read_acl_json"] is not None else None,
            owner_user_id=row["owner_user_id"],
            label=row["label"],
            payload=json.loads(row["payload_json"]) if row["payload_json"] is not None else None,
            content_hash=row["content_hash"],
        )

    def latest_version(self, record_type: str, record_id: str) -> VersionRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM vc_record_versions WHERE record_type=? AND record_id=? "
                "ORDER BY version_no DESC LIMIT 1",
                (record_type, record_id),
            ).fetchone()
        return self._row_to_version(row) if row else None

    def append_version(self, v: VersionRecord) -> VersionRecord:
        """Append a version, numbering it after the latest one (version_no on input is ignored)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(version_no), 0) FROM vc_record_versions WHERE record_type=? AND record_id=?",
                    (v.record_type, v.record_id),
                ).fetchone()
                v = v.model_copy(update={"version_no": int(row[0]) + 1})
                self._conn.execute(
                    "INSERT INTO vc_record_versions(record_type, record_id, version_no, change_id, op, recorded_at, "
                    "wall_side, read_acl_json, owner_user_id, label, payload_json, content_hash) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        v.record_type,
                        v.record_id,
                        v.version_no,
                        v.change_id,
                        v.op,
                        v.recorded_at,
                        v.wall_side,
                        json.dumps(v.read_acl_groups) if v.read_acl_groups is not None else None,
                        v.owner_user_id,
                        v.label,
                        json.dumps(v.payload, default=str) if v.payload is not None else None,
                        v.content_hash,
                    ),
                )
                self._conn.execute(
                    "INSERT OR IGNORE INTO vc_change_set_records(change_id, record_type, record_id) VALUES (?,?,?)",
                    (v.change_id, v.record_type, v.record_id),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return v

    def append_versions(self, versions: list[VersionRecord]) -> int:
        """Append many versions in one transaction (bulk import). Numbers each after its record's latest."""
        if not versions:
            return 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                next_no: dict[tuple[str, str], int] = {}
                for rt in {v.record_type for v in versions}:
                    for rid, n in self._conn.execute(
                        "SELECT record_id, MAX(version_no) FROM vc_record_versions WHERE record_type=? GROUP BY record_id",
                        (rt,),
                    ):
                        next_no[(rt, rid)] = int(n)
                rows = []
                links = set()
                for v in versions:
                    key = (v.record_type, v.record_id)
                    next_no[key] = next_no.get(key, 0) + 1
                    rows.append(
                        (
                            v.record_type, v.record_id, next_no[key], v.change_id, v.op, v.recorded_at, v.wall_side,
                            json.dumps(v.read_acl_groups) if v.read_acl_groups is not None else None,
                            v.owner_user_id, v.label,
                            json.dumps(v.payload, default=str) if v.payload is not None else None,
                            v.content_hash,
                        )
                    )
                    links.add((v.change_id, v.record_type, v.record_id))
                self._conn.executemany(
                    "INSERT INTO vc_record_versions(record_type, record_id, version_no, change_id, op, recorded_at, "
                    "wall_side, read_acl_json, owner_user_id, label, payload_json, content_hash) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows,
                )
                self._conn.executemany(
                    "INSERT OR IGNORE INTO vc_change_set_records(change_id, record_type, record_id) VALUES (?,?,?)",
                    sorted(links),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return len(rows)

    def latest_hashes(self, record_type: str) -> dict[str, str | None]:
        """record_id -> content hash of its latest version."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT v.record_id, v.content_hash FROM vc_record_versions v JOIN ("
                " SELECT record_id, MAX(version_no) AS n FROM vc_record_versions WHERE record_type=? GROUP BY record_id"
                ") m ON v.record_id=m.record_id AND v.version_no=m.n WHERE v.record_type=?",
                (record_type, record_type),
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def all_hashes(self, record_type: str) -> set[tuple[str, str | None]]:
        """(record_id, hash) of every version of a type (claim slots hold several live claims)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT record_id, content_hash FROM vc_record_versions WHERE record_type=?", (record_type,)
            ).fetchall()
        return {(r[0], r[1]) for r in rows}

    def list_versions(self, record_type: str, record_id: str) -> list[VersionRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM vc_record_versions WHERE record_type=? AND record_id=? ORDER BY version_no",
                (record_type, record_id),
            ).fetchall()
        return [self._row_to_version(r) for r in rows]

    def get_version(self, record_type: str, record_id: str, version_no: int) -> VersionRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM vc_record_versions WHERE record_type=? AND record_id=? AND version_no=?",
                (record_type, record_id, version_no),
            ).fetchone()
        return self._row_to_version(row) if row else None

    def version_at(self, record_type: str, record_id: str, at: str) -> VersionRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM vc_record_versions WHERE record_type=? AND record_id=? AND recorded_at<=? "
                "ORDER BY version_no DESC LIMIT 1",
                (record_type, record_id, at),
            ).fetchone()
        return self._row_to_version(row) if row else None

    def versions_for_change(self, change_id: str) -> list[VersionRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM vc_record_versions WHERE change_id=? ORDER BY id", (change_id,)
            ).fetchall()
        return [self._row_to_version(r) for r in rows]

    def known_record_ids(self, record_type: str) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT record_id FROM vc_record_versions WHERE record_type=?", (record_type,)
            ).fetchall()
        return {r[0] for r in rows}

    def latest_versions_with_prefix(self, record_type: str, prefix: str) -> list[VersionRecord]:
        """Latest version of every record whose id starts with ``prefix`` (claim slots of one subject)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT v.* FROM vc_record_versions v JOIN ("
                " SELECT record_id, MAX(version_no) AS n FROM vc_record_versions"
                " WHERE record_type=? AND record_id LIKE ? ESCAPE '\\' GROUP BY record_id"
                ") m ON v.record_id=m.record_id AND v.version_no=m.n WHERE v.record_type=?",
                (record_type, prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", record_type),
            ).fetchall()
        return [self._row_to_version(r) for r in rows]

    # -- change sets -----------------------------------------------------

    def save_change_set(self, cs: ChangeSet) -> ChangeSet:
        with self._lock:
            self._conn.execute(
                "INSERT INTO vc_change_sets(change_id, status, author_id, implicit, created_at, merged_at, body_json) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(change_id) DO UPDATE SET status=excluded.status, "
                "merged_at=excluded.merged_at, body_json=excluded.body_json",
                (
                    cs.change_id,
                    cs.status,
                    cs.author_id,
                    1 if cs.implicit else 0,
                    cs.created_at,
                    cs.merged_at,
                    cs.model_dump_json(),
                ),
            )
            for item in cs.items:
                if item.record_id:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO vc_change_set_records(change_id, record_type, record_id) VALUES (?,?,?)",
                        (cs.change_id, item.record_type, item.record_id),
                    )
        return cs

    def get_change_set(self, change_id: str) -> ChangeSet | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body_json FROM vc_change_sets WHERE change_id=?", (change_id,)
            ).fetchone()
        return ChangeSet.model_validate_json(row[0]) if row else None

    def list_change_sets(
        self, *, statuses: Iterable[str] | None = None, include_implicit: bool = True, limit: int = 200
    ) -> list[ChangeSet]:
        sql = "SELECT body_json FROM vc_change_sets WHERE 1=1"
        args: list[Any] = []
        status_list = list(statuses or [])
        if status_list:
            sql += f" AND status IN ({','.join('?' * len(status_list))})"
            args.extend(status_list)
        if not include_implicit:
            sql += " AND implicit=0"
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [ChangeSet.model_validate_json(r[0]) for r in rows]

    def change_sets_for_record(self, record_type: str, record_id: str) -> list[ChangeSet]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.body_json FROM vc_change_sets c JOIN vc_change_set_records r ON c.change_id=r.change_id "
                "WHERE r.record_type=? AND r.record_id=? ORDER BY c.created_at",
                (record_type, record_id),
            ).fetchall()
        return [ChangeSet.model_validate_json(r[0]) for r in rows]

    def merged_change_ids_between(self, after: str | None, until: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT change_id FROM vc_change_sets WHERE merged_at IS NOT NULL AND merged_at<=? "
                "AND (? IS NULL OR merged_at>?) ORDER BY merged_at",
                (until, after, after),
            ).fetchall()
        return [r[0] for r in rows]

    # -- releases and channels ---------------------------------------------

    def create_release(self, release: Release) -> Release:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO vc_releases(cut_at, body_json) VALUES (?, ?)", (release.cut_at, "{}")
            )
            release = release.model_copy(update={"release_id": int(cur.lastrowid or 0)})
            self._conn.execute(
                "UPDATE vc_releases SET body_json=? WHERE release_id=?",
                (release.model_dump_json(), release.release_id),
            )
        return release

    def update_release(self, release: Release) -> Release:
        with self._lock:
            self._conn.execute(
                "UPDATE vc_releases SET body_json=? WHERE release_id=?",
                (release.model_dump_json(), release.release_id),
            )
        return release

    def get_release(self, release_id: int) -> Release | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT body_json FROM vc_releases WHERE release_id=?", (release_id,)
            ).fetchone()
        return Release.model_validate_json(row[0]) if row else None

    def list_releases(self) -> list[Release]:
        with self._lock:
            rows = self._conn.execute("SELECT body_json FROM vc_releases ORDER BY release_id").fetchall()
        return [Release.model_validate_json(r[0]) for r in rows]

    def get_channels(self) -> list[ChannelPointer]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM vc_channels").fetchall()
        order = {n: i for i, n in enumerate(CHANNEL_NAMES)}
        out = [
            ChannelPointer(name=r["name"], release_id=r["release_id"], moved_at=r["moved_at"], moved_by=r["moved_by"])
            for r in rows
        ]
        return sorted(out, key=lambda c: order.get(c.name, 99))

    def move_channel(self, move: ChannelMove) -> ChannelPointer:
        with self._lock:
            self._conn.execute(
                "UPDATE vc_channels SET release_id=?, moved_at=?, moved_by=? WHERE name=?",
                (move.to_release, move.moved_at, move.moved_by, move.name),
            )
            self._conn.execute(
                "INSERT INTO vc_channel_moves(name, body_json) VALUES (?, ?)", (move.name, move.model_dump_json())
            )
        return ChannelPointer(name=move.name, release_id=move.to_release, moved_at=move.moved_at, moved_by=move.moved_by)

    def channel_moves(self, name: str | None = None) -> list[ChannelMove]:
        with self._lock:
            if name:
                rows = self._conn.execute(
                    "SELECT body_json FROM vc_channel_moves WHERE name=? ORDER BY id", (name,)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT body_json FROM vc_channel_moves ORDER BY id").fetchall()
        return [ChannelMove.model_validate_json(r[0]) for r in rows]


__all__ = ["SqliteVersionLedger", "ledger_path_for", "ts"]
