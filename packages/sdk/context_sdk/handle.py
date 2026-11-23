"""Ref-counted in-RAM SQLite SnapshotHandle preventing unsafe old_conn.close() crashes (PDF Defect #5)."""

from __future__ import annotations

import sqlite3
import threading
from collections import deque
from contextlib import contextmanager
from typing import Iterator

from context_core.compression import zstd_decompress


class SnapshotHandle:
    """Thread-safe, reference-counted in-RAM deserialized SQLite snapshot handle.

    - Loads `.sqlite.zst` strictly in RAM via ``sqlite3.Connection.deserialize()`` (ZERO temp files on disk!).
    - Uses a lightweight checkout pool of read-only deserialized connections governed by `_refcount` + `_retired`.
    - When retired during an atomic pointer swap, connections stay alive until `active_readers == 0`,
      then close deterministically — completely eliminating PDF Defect #5 (`old_conn.close()` race).
    """

    def __init__(
        self,
        *,
        version_id: int,
        etag: str,
        data_epoch: int,
        policy_epoch: int,
        acl_tier: str,
        wall_side: str,
        sha256: str,
        compressed_bytes: bytes,
    ) -> None:
        self.version_id = version_id
        self.etag = etag
        self.data_epoch = data_epoch
        self.policy_epoch = policy_epoch
        self.acl_tier = acl_tier
        self.wall_side = wall_side
        self.sha256 = sha256
        # Decompress once into immutable in-RAM SQLite image
        self._raw_sqlite: bytes = zstd_decompress(compressed_bytes)

        self._lock = threading.Lock()
        self._refcount = 0
        self._retired = False
        self._closed = False
        self._idle_conns: deque[sqlite3.Connection] = deque()
        self._all_conns: list[sqlite3.Connection] = []

        # Eagerly deserialize one primary connection to validate integrity immediately
        first = self._create_connection()
        self._idle_conns.append(first)
        self._all_conns.append(first)

    def _create_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.deserialize(self._raw_sqlite)
        conn.execute("PRAGMA query_only = ON")
        return conn

    @property
    def active_readers(self) -> int:
        with self._lock:
            return self._refcount

    @property
    def is_retired(self) -> bool:
        with self._lock:
            return self._retired

    @property
    def is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def acquire_connection(self) -> sqlite3.Connection:
        """Increment refcount and check out an in-RAM read-only SQLite connection."""
        with self._lock:
            if self._closed:
                raise RuntimeError(f"SnapshotHandle v{self.version_id} is already closed")
            self._refcount += 1
            if self._idle_conns:
                return self._idle_conns.popleft()
            need_new = True
        if need_new:
            conn = self._create_connection()
            with self._lock:
                self._all_conns.append(conn)
            return conn
        raise AssertionError("Unreachable")

    def release_connection(self, conn: sqlite3.Connection) -> None:
        """Return connection to pool, decrement refcount, and finalize close if retired & drained."""
        conns_to_close: list[sqlite3.Connection] = []
        with self._lock:
            self._refcount -= 1
            if self._retired and self._refcount == 0 and not self._closed:
                self._closed = True
                conns_to_close = list(self._all_conns)
                self._all_conns.clear()
                self._idle_conns.clear()
            else:
                self._idle_conns.append(conn)
        for c in conns_to_close:
            try:
                c.close()
            except sqlite3.Error:
                pass

    def retire(self) -> None:
        """Mark handle retired; immediately close all connections iff zero active readers remain."""
        conns_to_close: list[sqlite3.Connection] = []
        with self._lock:
            self._retired = True
            if self._refcount == 0 and not self._closed:
                self._closed = True
                conns_to_close = list(self._all_conns)
                self._all_conns.clear()
                self._idle_conns.clear()
        for c in conns_to_close:
            try:
                c.close()
            except sqlite3.Error:
                pass

    @contextmanager
    def checkout(self) -> Iterator[sqlite3.Connection]:
        """Context manager holding a ref-counted in-RAM SQLite connection across a read."""
        conn = self.acquire_connection()
        try:
            yield conn
        finally:
            self.release_connection(conn)
