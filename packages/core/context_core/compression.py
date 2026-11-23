"""Cross-platform Zstd compression, deterministic LF SHA-256 hashing, and Windows-safe atomic I/O."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from compression import zstd as _stdlib_zstd  # Python 3.14+ / 3.15 stdlib
except ImportError:  # pragma: no cover - fallback only on Python < 3.14
    _stdlib_zstd = None  # type: ignore[assignment]

TEXT_SUFFIXES = frozenset({".json", ".md", ".csv", ".yml", ".yaml", ".txt", ".py", ".toml", ".sql", ".ps1"})


def zstd_compress(data: bytes, *, level: int = 3) -> bytes:
    """Compress bytes using Python 3.15 stdlib ``compression.zstd`` (zero C-extension requirement)."""
    if _stdlib_zstd is not None:
        return _stdlib_zstd.compress(data, level=level)
    import zstandard  # pragma: no cover

    return zstandard.ZstdCompressor(level=level).compress(data)


def zstd_decompress(data: bytes) -> bytes:
    """Decompress Zstd payload using Python 3.15 stdlib ``compression.zstd``."""
    if _stdlib_zstd is not None:
        return _stdlib_zstd.decompress(data)
    import zstandard  # pragma: no cover

    return zstandard.ZstdDecompressor().decompress(data)


def normalize_lf_bytes(raw: bytes) -> bytes:
    """Normalize CRLF line endings to LF so SHA-256 hashes match identically across Windows and Linux."""
    return raw.replace(b"\r\n", b"\n")


def normalize_lf_text(text: str) -> str:
    """Normalize text line endings to LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def lf_sha256_bytes(raw: bytes, *, is_text: bool = True) -> str:
    """Return hex SHA-256 of bytes, LF-normalizing text payloads first."""
    payload = normalize_lf_bytes(raw) if is_text else raw
    return hashlib.sha256(payload).hexdigest()


def lf_sha256_text(text: str) -> str:
    """Return hex SHA-256 of UTF-8 LF-normalized text."""
    return hashlib.sha256(normalize_lf_text(text).encode("utf-8")).hexdigest()


def lf_sha256_file(path: Path) -> str:
    """Return deterministic SHA-256 for a file on disk (LF-normalized for text formats, raw for binary)."""
    raw = path.read_bytes()
    is_text = path.suffix.lower() in TEXT_SUFFIXES
    return lf_sha256_bytes(raw, is_text=is_text)


def to_posix_path(path: str | Path) -> str:
    """Normalize a relative or POSIX-like path string to forward slashes."""
    if isinstance(path, Path):
        return path.as_posix()
    return path.replace("\\", "/")


def utc_now() -> datetime:
    """Return current timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime | str | None) -> datetime | None:
    """Parse ISO-8601 string or normalize datetime to timezone-aware UTC."""
    if dt is None:
        return None
    if isinstance(dt, str):
        val = dt.strip()
        if not val:
            return None
        if val.endswith("Z"):
            val = val[:-1] + "+00:00"
        parsed = datetime.fromisoformat(val)
    else:
        parsed = dt
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_utc(dt: datetime | None) -> str | None:
    """Format a UTC datetime as canonical ISO-8601 Z string."""
    if dt is None:
        return None
    normalized = ensure_utc(dt)
    assert normalized is not None
    return normalized.isoformat().replace("+00:00", "Z")


def canonical_json(obj: Any) -> str:
    """Serialize any JSON-compatible object deterministically with sorted keys and compact separators."""

    def _default(value: Any) -> Any:
        if isinstance(value, datetime):
            return format_utc(value)
        if isinstance(value, (set, frozenset)):
            return sorted(value)
        if isinstance(value, Path):
            return value.as_posix()
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default)


def atomic_replace_with_retry(src: Path, dst: Path, *, max_attempts: int = 6) -> None:
    """Atomically replace ``dst`` with ``src`` with short backoff for transient Windows NTFS locks."""
    delay_s = 0.015
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last_exc = exc
            if attempt == max_attempts - 1:
                break
            time.sleep(delay_s)
            delay_s = min(delay_s * 1.8, 0.12)
    if src.exists():
        try:
            src.unlink()
        except OSError:
            pass
    assert last_exc is not None
    raise last_exc


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write binary payload atomically using a PID/TID temp file and Windows-safe ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
        atomic_replace_with_retry(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def atomic_write_text(path: Path, text: str) -> None:
    """Write UTF-8 text strictly with LF (``\\n``) endings atomically."""
    normalized = normalize_lf_text(text).encode("utf-8")
    atomic_write_bytes(path, normalized)
